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
_FileSnapshot = tuple[int, int, int, str]  # device, inode, permission mode, content hash


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
                    os.fsync(pending_fd)
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
                staged = self._read_regular_at(pending_fd, name)
                if staged is None:
                    return False
                chunk = redact(staged).strip()
                if not chunk or len(chunk) > _MAX_CONTENT_CHARS:
                    raise MemoryReviewError("đề xuất staging rỗng/quá dài hoặc không hợp lệ")
                audit = self._audit_state(memory_fd)
                lifecycle = self._verify_pending_audit(audit[0], pid, staged)
                previous_item = self._read_regular_snapshot_at(root_fd, MEMORY_FILENAME)
                previous = previous_item[0] if previous_item is not None else None
                previous_snapshot = previous_item[1] if previous_item is not None else None
                current = previous or ""
                entry = f"{marker}\n{chunk}"
                if lifecycle == "approved":
                    if entry not in current:
                        raise MemoryReviewError(
                            "audit báo đã duyệt nhưng MEMORY.md không có nội dung tương ứng"
                        )
                    os.unlink(name, dir_fd=pending_fd)
                    os.fsync(pending_fd)
                    return True
                if lifecycle != "proposed":
                    raise MemoryReviewError("đề xuất đã kết thúc; không thể duyệt")
                if marker in current and entry not in current:
                    raise MemoryReviewError(
                        "marker đề xuất trong MEMORY.md không khớp nội dung audit"
                    )
                memory_changed = False
                merged_snapshot = previous_snapshot
                if entry not in current:
                    merged = current + ("\n\n" if current else "") + entry
                    merged_snapshot = self._atomic_write_at(
                        root_fd, MEMORY_FILENAME, merged, expected=previous_snapshot
                    )
                    memory_changed = True
                try:
                    self._append_audit(
                        memory_fd, audit, actor=actor, action="approved", pid=pid, content=chunk
                    )
                except Exception:
                    # Proposal is still staged; restore curated memory if audit cannot commit.
                    if memory_changed:
                        assert merged_snapshot is not None
                        if previous is None:
                            self._unlink_if_snapshot(
                                root_fd, MEMORY_FILENAME, expected=merged_snapshot
                            )
                        else:
                            self._atomic_write_at(
                                root_fd,
                                MEMORY_FILENAME,
                                previous,
                                expected=merged_snapshot,
                            )
                    raise
                # Audit is the durable commit record.  Consume staging last so a crash before
                # this unlink is recoverable by the terminal-lifecycle branch above.
                os.unlink(name, dir_fd=pending_fd)
                os.fsync(pending_fd)
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
                lifecycle = self._verify_pending_audit(audit[0], pid, chunk)
                if lifecycle == "approved":
                    raise MemoryReviewError("đề xuất đã được duyệt; không thể từ chối")
                if lifecycle == "proposed":
                    self._append_audit(
                        memory_fd, audit, actor=actor, action="rejected", pid=pid, content=chunk
                    )
                os.unlink(name, dir_fd=pending_fd)
                os.fsync(pending_fd)
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
        os.fsync(pending_fd)

    @classmethod
    def _read_regular_at(cls, dir_fd: int, name: str) -> str | None:
        item = cls._read_regular_snapshot_at(dir_fd, name)
        return item[0] if item is not None else None

    @staticmethod
    def _read_regular_snapshot_at(
        dir_fd: int, name: str
    ) -> tuple[str, _FileSnapshot] | None:
        """Read one stable regular-file version and return its identity/content snapshot."""
        try:
            fd = os.open(name, _flags(), dir_fd=dir_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise MemoryReviewError(f"từ chối đọc file memory không an toàn '{name}': {exc}") from exc
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
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
            after = os.fstat(fd)
            before_identity = (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
                stat.S_IMODE(before.st_mode),
            )
            after_identity = (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
                stat.S_IMODE(after.st_mode),
            )
            if before_identity != after_identity:
                raise MemoryReviewError(
                    f"file '{name}' đổi trong lúc đọc; từ chối TOCTOU"
                )
            snapshot: _FileSnapshot = (
                after.st_dev,
                after.st_ino,
                stat.S_IMODE(after.st_mode),
                hashlib.sha256(data).hexdigest(),
            )
            return data.decode("utf-8"), snapshot
        finally:
            os.close(fd)

    @classmethod
    def _atomic_write_at(
        cls,
        dir_fd: int,
        name: str,
        text: str,
        *,
        expected: _FileSnapshot | None,
    ) -> _FileSnapshot:
        """Replace only the exact version read by the caller.

        Comparing the content hash as well as inode closes the lost-update gap where an
        uncooperative writer edits a file in place without replacing its inode.
        """
        current_item = cls._read_regular_snapshot_at(dir_fd, name)
        current = current_item[1] if current_item is not None else None
        if current != expected:
            raise MemoryReviewError(f"file '{name}' đổi trong lúc duyệt; từ chối TOCTOU")
        tmp = f".{name}.{uuid.uuid4().hex}.tmp"
        fd = os.open(
            tmp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            current[2] if current else 0o600,
            dir_fd=dir_fd,
        )
        try:
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(text.encode("utf-8"))
                stream.flush()
                os.fsync(fd)
            latest_item = cls._read_regular_snapshot_at(dir_fd, name)
            latest = latest_item[1] if latest_item is not None else None
            if latest != current:
                raise MemoryReviewError(f"file '{name}' đổi trong lúc duyệt; từ chối TOCTOU")
            os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            os.fsync(dir_fd)
            replaced = cls._read_regular_snapshot_at(dir_fd, name)
            if replaced is None or replaced[0] != text:
                raise MemoryReviewError(
                    f"file '{name}' đổi ngay sau khi ghi; từ chối TOCTOU"
                )
            return replaced[1]
        finally:
            os.close(fd)
            try:
                os.unlink(tmp, dir_fd=dir_fd)
            except FileNotFoundError:
                pass

    @classmethod
    def _unlink_if_snapshot(
        cls, dir_fd: int, name: str, *, expected: _FileSnapshot
    ) -> None:
        item = cls._read_regular_snapshot_at(dir_fd, name)
        current = item[1] if item is not None else None
        if current != expected:
            raise MemoryReviewError(f"file '{name}' đổi trong lúc rollback; từ chối TOCTOU")
        os.unlink(name, dir_fd=dir_fd)
        os.fsync(dir_fd)

    def _audit_state(self, memory_fd: int) -> tuple[str, str, _FileSnapshot | None]:
        item = self._read_regular_snapshot_at(memory_fd, MEMORY_AUDIT_FILENAME)
        text = item[0] if item is not None else ""
        snapshot = item[1] if item is not None else None
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
        return text, prev, snapshot

    @staticmethod
    def _verify_pending_audit(audit_text: str, pid: str, content: str) -> str:
        """Require a pending file to match its latest audited proposal exactly."""
        latest: dict | None = None
        for line in audit_text.splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if isinstance(record, dict) and record.get("proposal_id") == pid:
                latest = record
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if (
            latest is None
            or latest.get("content_sha256") != digest
        ):
            raise MemoryReviewError(
                "đề xuất staging không khớp audit hoặc đã kết thúc; từ chối mutation"
            )
        action = latest.get("action")
        if not isinstance(action, str) or action not in {"proposed", "approved", "rejected"}:
            raise MemoryReviewError("memory review audit có lifecycle không hợp lệ")
        return action

    def _append_audit(
        self,
        memory_fd: int,
        state: tuple[str, str, _FileSnapshot | None],
        *,
        actor: str,
        action: str,
        pid: str,
        content: str,
    ) -> None:
        text, prev, snapshot = state
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
        self._atomic_write_at(
            memory_fd, MEMORY_AUDIT_FILENAME, updated, expected=snapshot
        )
