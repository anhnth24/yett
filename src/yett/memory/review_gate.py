"""Memory proposal/review transaction boundary.

Only this module may mutate curated memory.

On POSIX, files are accessed relative to already-opened directory descriptors with
``O_NOFOLLOW``, so a staged proposal or ``MEMORY.md`` symlink cannot redirect an
approval outside the workspace.

On Windows, Python cannot use POSIX directory fds / ``dir_fd``.  The Windows backend
uses path-based IO under a resolved workspace root, rejects symlink/junction/reparse
traversal as far as Python 3.11 allows, and serializes with ``msvcrt`` locking.  Audit
chain, lifecycle, and rollback semantics match the POSIX backend.
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
_DirRef = int | Path
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


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


def _is_safe_entry_name(name: str) -> bool:
    """True for a single path component that cannot escape its parent directory."""
    if not isinstance(name, str) or not name or name in {".", ".."}:
        return False
    if "\x00" in name or "/" in name or "\\" in name:
        return False
    if os.sep in name or (os.altsep is not None and os.altsep in name):
        return False
    return True


def _is_reparse_lstat(st: os.stat_result) -> bool:
    """Detect symlink/junction/reparse points from an ``lstat`` result."""
    if stat.S_ISLNK(st.st_mode):
        return True
    attrs = int(getattr(st, "st_file_attributes", 0) or 0)
    return bool(attrs & _FILE_ATTRIBUTE_REPARSE_POINT)


def _is_reparse_path(path: Path) -> bool:
    try:
        return _is_reparse_lstat(os.lstat(path))
    except OSError:
        return False


def _child_under_root(root: Path, *parts: str) -> Path:
    """Join ``parts`` under ``root`` after rejecting unsafe names and lexical escape."""
    for part in parts:
        if not _is_safe_entry_name(part):
            raise MemoryReviewError(f"tên mục memory không an toàn: {part!r}")
    path = root.joinpath(*parts)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise MemoryReviewError("đường dẫn memory thoát khỏi workspace") from exc
    return path


def _stat_identity(st: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        st.st_dev,
        st.st_ino,
        st.st_size,
        st.st_mtime_ns,
        st.st_ctime_ns,
        stat.S_IMODE(st.st_mode),
    )


def _snapshot_from_open_fd(fd: int, name: str) -> tuple[str, _FileSnapshot]:
    """Read one stable regular-file version from an already-opened descriptor."""
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
    if _stat_identity(before) != _stat_identity(after):
        raise MemoryReviewError(f"file '{name}' đổi trong lúc đọc; từ chối TOCTOU")
    snapshot: _FileSnapshot = (
        after.st_dev,
        after.st_ino,
        stat.S_IMODE(after.st_mode),
        hashlib.sha256(data).hexdigest(),
    )
    return data.decode("utf-8"), snapshot


class MemoryReviewGate:
    def __init__(self, workspace_root: Path, *, clock=time.time) -> None:
        self._root = workspace_root.resolve()
        self._pending = self._root.joinpath(*PENDING_PARTS)
        self._clock = clock
        self._thread_lock = _thread_lock(self._root)
        self._windows = os.name == "nt"

    @property
    def workspace_root(self) -> Path:
        return self._root

    @property
    def pending_dir(self) -> Path:
        return self._pending

    def ensure_storage(self) -> None:
        """Create/check control directories without following directory symlinks."""
        with self._transaction() as root_dir:
            memory_dir, pending_dir = self._storage_dirs(root_dir)
            try:
                self._read_regular_at(root_dir, MEMORY_FILENAME)
                self._audit_state(memory_dir)
            finally:
                self._close_dir(pending_dir)
                self._close_dir(memory_dir)

    def propose(self, content: str, *, actor: str = "agent") -> str:
        """Redact, stage exclusively, and audit a proposal without touching MEMORY.md."""
        text = redact(str(content or "")).strip()
        if not text:
            raise MemoryReviewError("nội dung đề xuất memory trống sau khi lọc secret")
        if len(text) > _MAX_CONTENT_CHARS:
            raise MemoryReviewError(
                f"nội dung đề xuất vượt {_MAX_CONTENT_CHARS} ký tự — rút gọn rồi thử lại"
            )
        with self._transaction() as root_dir:
            memory_dir, pending_dir = self._storage_dirs(root_dir)
            try:
                audit = self._audit_state(memory_dir)
                pid = self._create_proposal(pending_dir, text)
                try:
                    self._append_audit(
                        memory_dir, audit, actor=actor, action="proposed", pid=pid, content=text
                    )
                except Exception:
                    self._unlink_entry(pending_dir, f"{pid}.md")
                    raise
                return pid
            finally:
                self._close_dir(pending_dir)
                self._close_dir(memory_dir)

    def list_pending(self) -> list[tuple[str, str]]:
        with self._transaction() as root_dir:
            memory_dir, pending_dir = self._storage_dirs(root_dir)
            try:
                # Corrupt audit storage is a lifecycle error, not an empty pending queue.
                self._audit_state(memory_dir)
                out: list[tuple[str, str]] = []
                for name in sorted(self._listdir(pending_dir)):
                    if not name.endswith(".md") or not is_valid_proposal_id(name[:-3]):
                        continue
                    text = self._read_regular_at(pending_dir, name)
                    if text is None:
                        continue
                    out.append((name[:-3], text))
                return out
            finally:
                self._close_dir(pending_dir)
                self._close_dir(memory_dir)

    def approve(self, pid: str, *, actor: str = "operator") -> bool:
        """Atomically merge once, audit, then consume the proposal under a process lock."""
        if not is_valid_proposal_id(pid):
            return False
        name = f"{pid}.md"
        marker = f"{_MARKER_PREFIX}{pid} -->"
        with self._transaction() as root_dir:
            memory_dir, pending_dir = self._storage_dirs(root_dir)
            try:
                staged = self._read_regular_at(pending_dir, name)
                if staged is None:
                    return False
                chunk = redact(staged).strip()
                if not chunk or len(chunk) > _MAX_CONTENT_CHARS:
                    raise MemoryReviewError("đề xuất staging rỗng/quá dài hoặc không hợp lệ")
                audit = self._audit_state(memory_dir)
                lifecycle = self._verify_pending_audit(audit[0], pid, staged)
                previous_item = self._read_regular_snapshot_at(root_dir, MEMORY_FILENAME)
                previous = previous_item[0] if previous_item is not None else None
                previous_snapshot = previous_item[1] if previous_item is not None else None
                current = previous or ""
                entry = f"{marker}\n{chunk}"
                if lifecycle == "approved":
                    if entry not in current:
                        raise MemoryReviewError(
                            "audit báo đã duyệt nhưng MEMORY.md không có nội dung tương ứng"
                        )
                    self._unlink_entry(pending_dir, name)
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
                        root_dir, MEMORY_FILENAME, merged, expected=previous_snapshot
                    )
                    memory_changed = True
                try:
                    self._append_audit(
                        memory_dir, audit, actor=actor, action="approved", pid=pid, content=chunk
                    )
                except Exception:
                    # Proposal is still staged; restore curated memory if audit cannot commit.
                    if memory_changed:
                        assert merged_snapshot is not None
                        if previous is None:
                            self._unlink_if_snapshot(
                                root_dir, MEMORY_FILENAME, expected=merged_snapshot
                            )
                        else:
                            self._atomic_write_at(
                                root_dir,
                                MEMORY_FILENAME,
                                previous,
                                expected=merged_snapshot,
                            )
                    raise
                # Audit is the durable commit record.  Consume staging last so a crash before
                # this unlink is recoverable by the terminal-lifecycle branch above.
                self._unlink_entry(pending_dir, name)
                return True
            finally:
                self._close_dir(pending_dir)
                self._close_dir(memory_dir)

    def reject(self, pid: str, *, actor: str = "operator") -> bool:
        if not is_valid_proposal_id(pid):
            return False
        name = f"{pid}.md"
        with self._transaction() as root_dir:
            memory_dir, pending_dir = self._storage_dirs(root_dir)
            try:
                chunk = self._read_regular_at(pending_dir, name)
                if chunk is None:
                    return False
                audit = self._audit_state(memory_dir)
                lifecycle = self._verify_pending_audit(audit[0], pid, chunk)
                if lifecycle == "approved":
                    raise MemoryReviewError("đề xuất đã được duyệt; không thể từ chối")
                if lifecycle == "proposed":
                    self._append_audit(
                        memory_dir, audit, actor=actor, action="rejected", pid=pid, content=chunk
                    )
                self._unlink_entry(pending_dir, name)
                return True
            finally:
                self._close_dir(pending_dir)
                self._close_dir(memory_dir)

    @contextmanager
    def _transaction(self) -> Iterator[_DirRef]:
        """Serialize threads and processes; hold the workspace root for the critical section."""
        if self._windows:  # pragma: no cover - exercised on Windows CI
            with self._windows_transaction() as root:
                yield root
            return
        with self._thread_lock:
            try:
                root_fd = os.open(self._root, _flags(directory=True))
            except OSError as exc:
                raise MemoryReviewError(
                    f"workspace memory không an toàn/không tồn tại: {exc}"
                ) from exc
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

    @contextmanager
    def _windows_transaction(self) -> Iterator[Path]:
        """Path-based transaction: thread lock + msvcrt file lock, no directory fds."""
        with self._thread_lock:
            if not self._root.is_dir() or _is_reparse_path(self._root):
                raise MemoryReviewError("workspace memory không an toàn/không tồn tại")
            lock_path = _child_under_root(self._root, MEMORY_LOCK_FILENAME)
            lock_fd = -1
            try:
                if lock_path.exists() or lock_path.is_symlink():
                    st = os.lstat(lock_path)
                    if _is_reparse_lstat(st) or not stat.S_ISREG(st.st_mode):
                        raise MemoryReviewError("memory review lock không phải regular file")
                lock_fd = os.open(
                    lock_path,
                    os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                    raise MemoryReviewError("memory review lock không phải regular file")
                self._lock_file(lock_fd)
                if _is_reparse_path(self._root) or not self._root.is_dir():
                    raise MemoryReviewError("workspace memory không an toàn/không tồn tại")
                yield self._root
            except MemoryReviewError:
                raise
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise MemoryReviewError(f"memory review thất bại an toàn: {exc}") from exc
            finally:
                if lock_fd >= 0:
                    self._unlock_file(lock_fd)
                    os.close(lock_fd)

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

            # Windows typeshed exposes an empty ``fcntl`` compatibility module even
            # though this branch is POSIX-only; dynamic lookup keeps cross-OS mypy
            # honest without weakening the runtime lock.
            getattr(fcntl, "flock")(fd, getattr(fcntl, "LOCK_EX"))

    @staticmethod
    def _unlock_file(fd: int) -> None:
        try:
            if os.name == "nt":  # pragma: no cover - exercised on Windows CI
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                getattr(msvcrt, "locking")(fd, getattr(msvcrt, "LK_UNLCK"), 1)
            else:
                import fcntl

                getattr(fcntl, "flock")(fd, getattr(fcntl, "LOCK_UN"))
        except OSError:
            pass

    def _storage_dirs(self, root_dir: _DirRef) -> tuple[_DirRef, _DirRef]:
        if isinstance(root_dir, Path):
            memory_dir = self._windows_open_child_dir(root_dir, PENDING_PARTS[0])
            try:
                pending_dir = self._windows_open_child_dir(memory_dir, PENDING_PARTS[1])
            except Exception:
                self._close_dir(memory_dir)
                raise
            return memory_dir, pending_dir
        memory_fd = self._open_child_dir(root_dir, PENDING_PARTS[0])
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

    @classmethod
    def _windows_open_child_dir(cls, parent: Path, name: str) -> Path:
        path = _child_under_root(parent, name)
        try:
            if path.exists() or path.is_symlink():
                st = os.lstat(path)
                if _is_reparse_lstat(st):
                    raise MemoryReviewError(f"memory/{name} không phải thư mục an toàn")
                if not stat.S_ISDIR(st.st_mode):
                    raise MemoryReviewError(f"memory/{name} không phải thư mục")
            else:
                os.mkdir(path, 0o700)
                st = os.lstat(path)
                if _is_reparse_lstat(st) or not stat.S_ISDIR(st.st_mode):
                    raise MemoryReviewError(f"memory/{name} không phải thư mục an toàn")
        except MemoryReviewError:
            raise
        except OSError as exc:
            raise MemoryReviewError(f"memory/{name} không phải thư mục an toàn: {exc}") from exc
        if _is_reparse_path(parent) or not parent.is_dir():
            raise MemoryReviewError("workspace memory không an toàn/không tồn tại")
        return path

    @staticmethod
    def _close_dir(directory: _DirRef) -> None:
        if isinstance(directory, int):
            os.close(directory)

    @staticmethod
    def _listdir(directory: _DirRef) -> list[str]:
        if isinstance(directory, Path):
            if _is_reparse_path(directory) or not directory.is_dir():
                raise MemoryReviewError("memory pending không phải thư mục an toàn")
            return os.listdir(directory)
        return os.listdir(directory)

    def _unlink_entry(self, directory: _DirRef, name: str) -> None:
        if isinstance(directory, Path):
            path = self._windows_regular_path(directory, name, missing_ok=False)
            if path is None:
                raise FileNotFoundError(name)
            os.unlink(path)
            self._windows_fsync_dir(directory)
            return
        os.unlink(name, dir_fd=directory)
        os.fsync(directory)

    def _create_proposal(self, pending_dir: _DirRef, text: str) -> str:
        for _ in range(16):
            pid = uuid.uuid4().hex
            try:
                self._create_named_proposal(pending_dir, f"{pid}.md", text)
            except FileExistsError:
                continue
            return pid
        raise MemoryReviewError("không cấp được id đề xuất duy nhất")

    @classmethod
    def _create_named_proposal(cls, pending_dir: _DirRef, name: str, text: str) -> None:
        if isinstance(pending_dir, Path):
            cls._windows_create_named_proposal(pending_dir, name, text)
            return
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=pending_dir,
        )
        try:
            data = text.encode("utf-8")
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(pending_dir)

    @classmethod
    def _windows_create_named_proposal(cls, pending_dir: Path, name: str, text: str) -> None:
        path = _child_under_root(pending_dir, name)
        if path.exists() or path.is_symlink():
            if _is_reparse_path(path):
                raise MemoryReviewError(f"từ chối ghi file memory không an toàn '{name}'")
            raise FileExistsError(name)
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise MemoryReviewError(f"file memory '{name}' không phải regular file")
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(text.encode("utf-8"))
                stream.flush()
                os.fsync(fd)
        finally:
            os.close(fd)
        cls._windows_fsync_dir(pending_dir)

    @classmethod
    def _read_regular_at(cls, directory: _DirRef, name: str) -> str | None:
        item = cls._read_regular_snapshot_at(directory, name)
        return item[0] if item is not None else None

    @classmethod
    def _read_regular_snapshot_at(
        cls, directory: _DirRef, name: str
    ) -> tuple[str, _FileSnapshot] | None:
        """Read one stable regular-file version and return its identity/content snapshot."""
        if isinstance(directory, Path):
            return cls._windows_read_regular_snapshot_at(directory, name)
        try:
            fd = os.open(name, _flags(), dir_fd=directory)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise MemoryReviewError(
                f"từ chối đọc file memory không an toàn '{name}': {exc}"
            ) from exc
        try:
            return _snapshot_from_open_fd(fd, name)
        finally:
            os.close(fd)

    @classmethod
    def _windows_read_regular_snapshot_at(
        cls, directory: Path, name: str
    ) -> tuple[str, _FileSnapshot] | None:
        try:
            path = cls._windows_regular_path(directory, name, missing_ok=True)
        except MemoryReviewError:
            raise
        if path is None:
            return None
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise MemoryReviewError(
                f"từ chối đọc file memory không an toàn '{name}': {exc}"
            ) from exc
        try:
            return _snapshot_from_open_fd(fd, name)
        finally:
            os.close(fd)

    @staticmethod
    def _windows_regular_path(
        directory: Path, name: str, *, missing_ok: bool
    ) -> Path | None:
        if _is_reparse_path(directory) or not directory.is_dir():
            raise MemoryReviewError("memory directory không phải thư mục an toàn")
        path = _child_under_root(directory, name)
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            if missing_ok:
                return None
            raise
        if _is_reparse_lstat(st):
            raise MemoryReviewError(f"từ chối đọc file memory không an toàn '{name}'")
        if not stat.S_ISREG(st.st_mode):
            raise MemoryReviewError(f"file memory '{name}' không phải regular file")
        return path

    @staticmethod
    def _windows_fsync_dir(directory: Path) -> None:
        """Best-effort directory durability on Windows (directory fds are unavailable)."""
        try:
            fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    @classmethod
    def _atomic_write_at(
        cls,
        directory: _DirRef,
        name: str,
        text: str,
        *,
        expected: _FileSnapshot | None,
    ) -> _FileSnapshot:
        """Replace only the exact version read by the caller.

        Comparing the content hash as well as inode closes the lost-update gap where an
        uncooperative writer edits a file in place without replacing its inode.
        """
        if isinstance(directory, Path):
            return cls._windows_atomic_write_at(directory, name, text, expected=expected)
        current_item = cls._read_regular_snapshot_at(directory, name)
        current = current_item[1] if current_item is not None else None
        if current != expected:
            raise MemoryReviewError(f"file '{name}' đổi trong lúc duyệt; từ chối TOCTOU")
        tmp = f".{name}.{uuid.uuid4().hex}.tmp"
        fd = os.open(
            tmp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            current[2] if current else 0o600,
            dir_fd=directory,
        )
        try:
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(text.encode("utf-8"))
                stream.flush()
                os.fsync(fd)
            latest_item = cls._read_regular_snapshot_at(directory, name)
            latest = latest_item[1] if latest_item is not None else None
            if latest != current:
                raise MemoryReviewError(f"file '{name}' đổi trong lúc duyệt; từ chối TOCTOU")
            os.replace(tmp, name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
            replaced = cls._read_regular_snapshot_at(directory, name)
            if replaced is None or replaced[0] != text:
                raise MemoryReviewError(
                    f"file '{name}' đổi ngay sau khi ghi; từ chối TOCTOU"
                )
            return replaced[1]
        finally:
            os.close(fd)
            try:
                os.unlink(tmp, dir_fd=directory)
            except FileNotFoundError:
                pass

    @classmethod
    def _windows_atomic_write_at(
        cls,
        directory: Path,
        name: str,
        text: str,
        *,
        expected: _FileSnapshot | None,
    ) -> _FileSnapshot:
        current_item = cls._read_regular_snapshot_at(directory, name)
        current = current_item[1] if current_item is not None else None
        if current != expected:
            raise MemoryReviewError(f"file '{name}' đổi trong lúc duyệt; từ chối TOCTOU")
        if not _is_safe_entry_name(name):
            raise MemoryReviewError(f"tên mục memory không an toàn: {name!r}")
        tmp_name = f".{name}.{uuid.uuid4().hex}.tmp"
        if not _is_safe_entry_name(tmp_name):
            raise MemoryReviewError("không tạo được tên tạm an toàn")
        tmp_path = _child_under_root(directory, tmp_name)
        final_path = _child_under_root(directory, name)
        fd = os.open(
            tmp_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            current[2] if current else 0o600,
        )
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise MemoryReviewError(f"file memory '{tmp_name}' không phải regular file")
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(text.encode("utf-8"))
                stream.flush()
                os.fsync(fd)
            latest_item = cls._read_regular_snapshot_at(directory, name)
            latest = latest_item[1] if latest_item is not None else None
            if latest != current:
                raise MemoryReviewError(f"file '{name}' đổi trong lúc duyệt; từ chối TOCTOU")
            if final_path.exists() or final_path.is_symlink():
                st = os.lstat(final_path)
                if _is_reparse_lstat(st):
                    raise MemoryReviewError(
                        f"từ chối ghi file memory không an toàn '{name}'"
                    )
            os.replace(tmp_path, final_path)
            cls._windows_fsync_dir(directory)
            replaced = cls._read_regular_snapshot_at(directory, name)
            if replaced is None or replaced[0] != text:
                raise MemoryReviewError(
                    f"file '{name}' đổi ngay sau khi ghi; từ chối TOCTOU"
                )
            return replaced[1]
        finally:
            os.close(fd)
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass

    @classmethod
    def _unlink_if_snapshot(
        cls, directory: _DirRef, name: str, *, expected: _FileSnapshot
    ) -> None:
        item = cls._read_regular_snapshot_at(directory, name)
        current = item[1] if item is not None else None
        if current != expected:
            raise MemoryReviewError(f"file '{name}' đổi trong lúc rollback; từ chối TOCTOU")
        if isinstance(directory, Path):
            path = cls._windows_regular_path(directory, name, missing_ok=False)
            assert path is not None
            os.unlink(path)
            cls._windows_fsync_dir(directory)
            return
        os.unlink(name, dir_fd=directory)
        os.fsync(directory)

    def _audit_state(self, memory_dir: _DirRef) -> tuple[str, str, _FileSnapshot | None]:
        item = self._read_regular_snapshot_at(memory_dir, MEMORY_AUDIT_FILENAME)
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
                raise MemoryReviewError(
                    "memory review audit hash-chain bị hỏng; từ chối mutation"
                )
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
        memory_dir: _DirRef,
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
            memory_dir, MEMORY_AUDIT_FILENAME, updated, expected=snapshot
        )
