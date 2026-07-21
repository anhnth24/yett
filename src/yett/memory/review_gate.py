"""Memory proposal/review transaction boundary.

Only this module may mutate curated memory.  Files are accessed relative to already-opened
directory descriptors with ``O_NOFOLLOW`` on POSIX, so a staged proposal or ``MEMORY.md``
symlink cannot redirect an approval outside the workspace.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from yett.errors import UserFacingError
from yett.memory.paths import (
    MEMORY_AUDIT_FILENAME,
    MEMORY_FILENAME,
    MEMORY_LOCK_FILENAME,
    PENDING_PARTS,
)
from yett.security.filters import redact

_PID_RE = re.compile(r"^[a-f0-9]{32}$")
_MAX_CONTENT_CHARS = 16_384
_MAX_CONTROL_FILE_BYTES = 16 * 1024 * 1024
_GENESIS = "0" * 64
_MARKER_PREFIX = "<!-- yett-memory-proposal:"
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class MemoryReviewError(UserFacingError, ValueError):
    """Unsafe/corrupt review storage; callers must fail closed."""


def is_valid_proposal_id(pid: object) -> bool:
    return isinstance(pid, str) and _PID_RE.fullmatch(pid) is not None


def _thread_lock(root: Path) -> threading.RLock:
    key = str(root)
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


def _flags(*, directory: bool = False) -> int:
    value = os.O_RDONLY
    if directory:
        value |= getattr(os, "O_DIRECTORY", 0)
    value |= getattr(os, "O_NOFOLLOW", 0)
    return value


def _entry_hash(prev_hash: str, entry: dict) -> str:
    payload = prev_hash + json.dumps(entry, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class MemoryReviewGate:
    def __init__(self, workspace_root: Path, *, clock=time.time) -> None:
        self._root = workspace_root.resolve()
        self._pending = self._root.joinpath(*PENDING_PARTS)
        self._clock = clock
        self._thread_lock = _thread_lock(self._root)

    @property
    def workspace_root(self) -> Path:
        return self._root

    @property
    def pending_dir(self) -> Path:
        return self._pending

    def ensure_storage(self) -> None:
        """Create/check control directories without following directory symlinks."""
        with self._transaction() as root_fd:
            memory_fd, pending_fd = self._storage_fds(root_fd)
            try:
                self._read_regular_at(root_fd, MEMORY_FILENAME)
                self._audit_state(memory_fd)
            finally:
                os.close(pending_fd)
                os.close(memory_fd)

    def propose(self, content: str, *, actor: str = "agent") -> str:
        """Redact, stage exclusively, and audit a proposal without touching MEMORY.md."""
        text = redact(str(content or "")).strip()
        if not text:
            raise MemoryReviewError("nội dung đề xuất memory trống sau khi lọc secret")
        if len(text) > _MAX_CONTENT_CHARS:
            raise MemoryReviewError(
                f"nội dung đề xuất vượt {_MAX_CONTENT_CHARS} ký tự — rút gọn rồi thử lại"
            )
        with self._transaction() as root_fd:
            memory_fd, pending_fd = self._storage_fds(root_fd)
            try:
                audit = self._audit_state(memory_fd)
                pid = self._create_proposal(pending_fd, text)
                try:
                    self._append_audit(
                        memory_fd, audit, actor=actor, action="proposed", pid=pid, content=text
                    )
                except Exception:
                    os.unlink(f"{pid}.md", dir_fd=pending_fd)
                    raise
                return pid
            finally:
                os.close(pending_fd)
                os.close(memory_fd)

    def list_pending(self) -> list[tuple[str, str]]:
        with self._transaction() as root_fd:
            memory_fd, pending_fd = self._storage_fds(root_fd)
            try:
                # Corrupt audit storage is a lifecycle error, not an empty pending queue.
                self._audit_state(memory_fd)
                out: list[tuple[str, str]] = []
                for name in sorted(os.listdir(pending_fd)):
                    if not name.endswith(".md") or not is_valid_proposal_id(name[:-3]):
                        continue
                    text = self._read_regular_at(pending_fd, name)
                    if text is None:
                        continue
                    out.append((name[:-3], text))
                return out
            finally:
                os.close(pending_fd)
                os.close(memory_fd)

    def approve(self, pid: str, *, actor: str = "operator") -> bool:
        """Atomically merge once, audit, then consume the proposal under a process lock."""
        if not is_valid_proposal_id(pid):
            return False
        name = f"{pid}.md"
        marker = f"{_MARKER_PREFIX}{pid} -->"
        with self._transaction() as root_fd:
            memory_fd, pending_fd = self._storage_fds(root_fd)
            try:
                chunk = self._read_regular_at(pending_fd, name)
                if chunk is None:
                    return False
                chunk = redact(chunk).strip()
                if not chunk or len(chunk) > _MAX_CONTENT_CHARS:
                    raise MemoryReviewError("đề xuất staging rỗng/quá dài hoặc không hợp lệ")
                audit = self._audit_state(memory_fd)
                previous = self._read_regular_at(root_fd, MEMORY_FILENAME)
                current = previous or ""
                merged = current
                if marker not in current:
                    entry = f"{marker}\n{chunk}"
                    merged = current + ("\n\n" if current else "") + entry
                    self._atomic_write_at(root_fd, MEMORY_FILENAME, merged)
                os.unlink(name, dir_fd=pending_fd)
                try:
                    self._append_audit(
                        memory_fd, audit, actor=actor, action="approved", pid=pid, content=chunk
                    )
                except Exception:
                    # Keep approval retryable and restore curated memory if audit cannot commit.
                    self._create_named_proposal(pending_fd, name, chunk)
                    if previous is None:
                        os.unlink(MEMORY_FILENAME, dir_fd=root_fd)
                    else:
                        self._atomic_write_at(root_fd, MEMORY_FILENAME, previous)
                    raise
                return True
            finally:
                os.close(pending_fd)
                os.close(memory_fd)

    def reject(self, pid: str, *, actor: str = "operator") -> bool:
        if not is_valid_proposal_id(pid):
            return False
        name = f"{pid}.md"
        with self._transaction() as root_fd:
            memory_fd, pending_fd = self._storage_fds(root_fd)
            try:
                chunk = self._read_regular_at(pending_fd, name)
                if chunk is None:
                    return False
                audit = self._audit_state(memory_fd)
                os.unlink(name, dir_fd=pending_fd)
                try:
                    self._append_audit(
                        memory_fd, audit, actor=actor, action="rejected", pid=pid, content=chunk
                    )
                except Exception:
                    self._create_named_proposal(pending_fd, name, chunk)
                    raise
                return True
            finally:
                os.close(pending_fd)
                os.close(memory_fd)

    @contextmanager
    def _transaction(self) -> Iterator[int]:
        """Serialize threads and processes; hold an opened workspace directory throughout."""
        with self._thread_lock:
            try:
                root_fd = os.open(self._root, _flags(directory=True))
            except OSError as exc:
                raise MemoryReviewError(f"workspace memory không an toàn/không tồn tại: {exc}") from exc
            lock_fd = -1
            try:
                lock_fd = os.open(
                    MEMORY_LOCK_FILENAME,
                    os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=root_fd,
                )
                if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                    raise MemoryReviewError("memory review lock không phải regular file")
                self._lock_file(lock_fd)
                yield root_fd
            except MemoryReviewError:
                raise
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise MemoryReviewError(f"memory review thất bại an toàn: {exc}") from exc
            finally:
                if lock_fd >= 0:
                    self._unlock_file(lock_fd)
                    os.close(lock_fd)
                os.close(root_fd)

    @staticmethod
    def _lock_file(fd: int) -> None:
        if os.name == "nt":  # pragma: no cover - exercised on Windows CI
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            getattr(msvcrt, "locking")(fd, getattr(msvcrt, "LK_LOCK"), 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)

    @staticmethod
    def _unlock_file(fd: int) -> None:
        try:
            if os.name == "nt":  # pragma: no cover - exercised on Windows CI
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                getattr(msvcrt, "locking")(fd, getattr(msvcrt, "LK_UNLCK"), 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass

    def _storage_fds(self, root_fd: int) -> tuple[int, int]:
        memory_fd = self._open_child_dir(root_fd, PENDING_PARTS[0])
        try:
            pending_fd = self._open_child_dir(memory_fd, PENDING_PARTS[1])
        except Exception:
            os.close(memory_fd)
            raise
        return memory_fd, pending_fd

    @staticmethod
    def _open_child_dir(parent_fd: int, name: str) -> int:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        try:
            fd = os.open(name, _flags(directory=True), dir_fd=parent_fd)
        except OSError as exc:
            raise MemoryReviewError(f"memory/{name} không phải thư mục an toàn: {exc}") from exc
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            os.close(fd)
            raise MemoryReviewError(f"memory/{name} không phải thư mục")
        return fd

    def _create_proposal(self, pending_fd: int, text: str) -> str:
        for _ in range(16):
            pid = uuid.uuid4().hex
            try:
                self._create_named_proposal(pending_fd, f"{pid}.md", text)
            except FileExistsError:
                continue
            return pid
        raise MemoryReviewError("không cấp được id đề xuất duy nhất")

    @staticmethod
    def _create_named_proposal(pending_fd: int, name: str, text: str) -> None:
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=pending_fd,
        )
        try:
            data = text.encode("utf-8")
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _read_regular_at(dir_fd: int, name: str) -> str | None:
        try:
            fd = os.open(name, _flags(), dir_fd=dir_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise MemoryReviewError(f"từ chối đọc file memory không an toàn '{name}': {exc}") from exc
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise MemoryReviewError(f"file memory '{name}' không phải regular file")
            chunks: list[bytes] = []
            remaining = _MAX_CONTROL_FILE_BYTES + 1
            while remaining:
                chunk = os.read(fd, min(remaining, 1024 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) > _MAX_CONTROL_FILE_BYTES:
                raise MemoryReviewError(f"file memory '{name}' quá lớn; từ chối xử lý")
            return data.decode("utf-8")
        finally:
            os.close(fd)

    @staticmethod
    def _atomic_write_at(dir_fd: int, name: str, text: str) -> None:
        existing: tuple[int, int, int] | None = None
        try:
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode):
                raise MemoryReviewError(f"từ chối thay file memory không phải regular file: {name}")
            existing = (st.st_dev, st.st_ino, stat.S_IMODE(st.st_mode))
        except FileNotFoundError:
            pass
        tmp = f".{name}.{uuid.uuid4().hex}.tmp"
        fd = os.open(
            tmp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            existing[2] if existing else 0o600,
            dir_fd=dir_fd,
        )
        try:
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(text.encode("utf-8"))
                stream.flush()
                os.fsync(fd)
            try:
                now = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                current = (now.st_dev, now.st_ino)
            except FileNotFoundError:
                current = None
            expected = existing[:2] if existing else None
            if current != expected:
                raise MemoryReviewError(f"file '{name}' đổi trong lúc duyệt; từ chối TOCTOU")
            os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            os.fsync(dir_fd)
        finally:
            os.close(fd)
            try:
                os.unlink(tmp, dir_fd=dir_fd)
            except FileNotFoundError:
                pass

    def _audit_state(self, memory_fd: int) -> tuple[str, str]:
        text = self._read_regular_at(memory_fd, MEMORY_AUDIT_FILENAME) or ""
        prev = _GENESIS
        for line in text.splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                raise MemoryReviewError("memory review audit không phải JSON object")
            body = {k: v for k, v in record.items() if k != "hash"}
            record_hash = record.get("hash")
            if (
                not isinstance(record_hash, str)
                or body.get("prev_hash") != prev
                or record_hash != _entry_hash(prev, body)
            ):
                raise MemoryReviewError("memory review audit hash-chain bị hỏng; từ chối mutation")
            prev = record_hash
        return text, prev

    def _append_audit(
        self,
        memory_fd: int,
        state: tuple[str, str],
        *,
        actor: str,
        action: str,
        pid: str,
        content: str,
    ) -> None:
        text, prev = state
        body = {
            "ts": self._clock(),
            "actor": actor,
            "action": action,
            "proposal_id": pid,
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "prev_hash": prev,
        }
        record = {**body, "hash": _entry_hash(prev, body)}
        updated = text + ("" if not text or text.endswith("\n") else "\n")
        updated += json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        self._atomic_write_at(memory_fd, MEMORY_AUDIT_FILENAME, updated)
