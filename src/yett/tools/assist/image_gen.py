"""image_gen (spec P3 §5.3, WP3.6). API key từ secret store; ảnh lưu trong workspace.

Backend injectable để test offline / local không-egress. Cost span ghi vào ledger
(per_call) qua `span_attrs` trên ToolResult — loop gắn vào TOOL_CALL span (không chứa secret).
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import stat
import uuid
from pathlib import Path

from yett.errors import SecretNotFound, UserFacingError
from yett.tools.assist.image_backend import (
    ChargedImageError,
    GeneratedImage,
    ImageGenFn,
    sniff_image_mime,
)
from yett.tools.base import ToolCtx, ToolResult
from yett.tools.projects import ProjectScope

_MAX_PROMPT_CHARS = 4_000
_ALLOWED_SIZES = frozenset(
    {"256x256", "512x512", "1024x1024", "1792x1024", "1024x1792"}
)
_MIME_TO_EXTS = {
    "image/png": (".png",),
    "image/jpeg": (".jpg", ".jpeg"),
    "image/webp": (".webp",),
}
# Relative path: segments of [A-Za-z0-9._-] only; no absolute / drive / ..
_SAFE_REL_PATH = re.compile(
    r"^(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+\.(?:png|jpg|jpeg|webp)$"
)
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in (*range(1, 10), "¹", "²", "³")),
        *(f"LPT{i}" for i in (*range(1, 10), "¹", "²", "³")),
    }
)
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_WINDOWS_BINARY = getattr(os, "O_BINARY", 0)
_DEFAULT_TIMEOUT_SEC = 60.0


def _is_reparse(st: os.stat_result) -> bool:
    attrs = int(getattr(st, "st_file_attributes", 0) or 0)
    return stat.S_ISLNK(st.st_mode) or bool(attrs & _FILE_ATTRIBUTE_REPARSE_POINT)


def _stat_identity(st: os.stat_result) -> tuple[int, ...]:
    return (
        st.st_dev,
        st.st_ino,
        st.st_size,
        st.st_mtime_ns,
        st.st_ctime_ns,
        stat.S_IMODE(st.st_mode),
    )


def _supports_secure_dir_fd() -> bool:
    required = {os.open, os.mkdir, os.stat, os.unlink, os.rename}
    return bool(getattr(os, "O_NOFOLLOW", 0)) and required.issubset(os.supports_dir_fd)


class ImageGenTool:
    """Sinh ảnh qua provider API (hoặc backend inject); ghi bytes vào workspace."""

    name = "image_gen"
    schema = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "minLength": 1,
                "maxLength": _MAX_PROMPT_CHARS,
            },
            "path": {
                "type": "string",
                "description": "Đường dẫn tương đối trong workspace (vd images/cover.png)",
            },
            "size": {
                "type": "string",
                "enum": sorted(_ALLOWED_SIZES),
            },
        },
        "required": ["prompt", "path"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        image_fn: ImageGenFn,
        secrets,
        api_key_secret: str,
        *,
        scope: ProjectScope,
        cost_usd: float = 0.0,
        cost_provider: str = "openai_compat",
        cost_model: str = "dall-e-3",
        timeout_sec: float = _DEFAULT_TIMEOUT_SEC,
    ) -> None:
        if not math.isfinite(timeout_sec) or timeout_sec <= 0:
            raise ValueError("image_gen timeout_sec must be finite and positive")
        self._gen = image_fn
        self._secrets = secrets
        self._key_name = api_key_secret
        self._scope = scope
        self._cost_usd = cost_usd
        self._cost_provider = cost_provider
        self._cost_model = cost_model
        self._timeout_sec = timeout_sec

    def validate(self, args: dict) -> None:
        prompt = args.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise UserFacingError("thiếu 'prompt'")
        if len(prompt) > _MAX_PROMPT_CHARS:
            raise UserFacingError(f"'prompt' vượt {_MAX_PROMPT_CHARS} ký tự")
        path = args.get("path")
        if not isinstance(path, str) or not path.strip():
            raise UserFacingError("thiếu 'path'")
        self._check_safe_rel_path(path.strip())
        size = args.get("size", "1024x1024")
        if size not in _ALLOWED_SIZES:
            raise UserFacingError(
                f"'size' phải là một trong {sorted(_ALLOWED_SIZES)}"
            )

    @staticmethod
    def _check_safe_rel_path(path: str) -> None:
        if path.startswith("/") or path.startswith("\\") or ":" in path:
            raise UserFacingError("'path' phải là đường dẫn tương đối trong workspace")
        if "\x00" in path:
            raise UserFacingError("'path' không hợp lệ")
        norm = path.replace("\\", "/").strip("/")
        parts = norm.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise UserFacingError("'path' không được chứa '.', '..' hoặc segment rỗng")
        if any(
            part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
            for part in parts
        ):
            raise UserFacingError("'path' chứa tên thiết bị dành riêng trên Windows")
        if any(
            part.endswith(".")
            or len(part.encode("utf-16-le")) // 2 > 255
            for part in parts
        ):
            raise UserFacingError("'path' chứa segment không hợp lệ trên Windows")
        if not _SAFE_REL_PATH.match(norm):
            raise UserFacingError(
                "'path' chỉ gồm [A-Za-z0-9._-/] và đuôi .png/.jpg/.jpeg/.webp"
            )

    def _resolve_key(self) -> str:
        try:
            key = self._secrets.get(self._key_name)
        except SecretNotFound as e:
            raise UserFacingError(
                f"secret '{self._key_name}' chưa đặt — đặt qua secret store/env "
                f"trước khi dùng image_gen"
            ) from e
        if not isinstance(key, str) or not key.strip():
            raise UserFacingError(
                f"secret '{self._key_name}' rỗng — cấu hình lại image API key"
            )
        return key

    def _resolve_dest(self, rel_path: str) -> Path:
        """Resolve path vào workspace root + chống symlink escape / ghi đè qua link.

        Chỉ resolve thư mục cha (để bắt directory-symlink nhảy ra ngoài). Thành phần
        cuối nếu là symlink → từ chối (không follow tới target).
        """
        roots = self._scope.roots()
        if not roots:
            raise UserFacingError("[DENIED] workspace root chưa cấu hình")
        ws = roots[0]
        rel = rel_path.replace("\\", "/").strip("/")
        parts = rel.split("/")
        lexical = ws.joinpath(*parts)
        current = ws
        for part in parts[:-1]:
            current = current / part
            try:
                current_st = os.lstat(current)
            except FileNotFoundError:
                break
            except OSError as e:
                raise UserFacingError(
                    f"đường dẫn '{rel_path}' không an toàn — bị từ chối"
                ) from e
            if _is_reparse(current_st) or not stat.S_ISDIR(current_st.st_mode):
                raise UserFacingError(
                    "[DENIED] image_gen từ chối thư mục đích không an toàn"
                )
        try:
            parent = lexical.parent.resolve()
        except OSError as e:
            raise UserFacingError(
                f"đường dẫn '{rel_path}' nằm ngoài workspace — bị từ chối"
            ) from e
        if parent != ws and ws not in parent.parents:
            raise UserFacingError(
                f"đường dẫn '{rel_path}' nằm ngoài workspace — bị từ chối"
            )
        # Parent phải nằm trong scope (workspace hoặc project đã đăng ký).
        self._scope.resolve_in_scope(parent)
        dest = parent / lexical.name
        try:
            dest_st = os.lstat(dest)
        except FileNotFoundError:
            dest_st = None
        if dest_st is not None and _is_reparse(dest_st):
            raise UserFacingError("[DENIED] image_gen từ chối ghi đè reparse point")
        if dest_st is not None and not stat.S_ISREG(dest_st.st_mode):
            raise UserFacingError("[DENIED] image_gen chỉ ghi đè regular file")
        if dest.exists():
            # Regular/existing file: resolve và xác nhận vẫn trong workspace.
            resolved = dest.resolve()
            if resolved != ws and ws not in resolved.parents:
                raise UserFacingError(
                    f"đường dẫn '{rel_path}' nằm ngoài workspace — bị từ chối"
                )
            return self._scope.resolve_in_scope(resolved)
        return dest

    @staticmethod
    def _ext_matches_mime(path: Path, mime: str) -> bool:
        allowed = _MIME_TO_EXTS.get(mime, ())
        return path.suffix.lower() in allowed

    def _open_secure_parent(self, rel_path: str) -> tuple[int, str]:
        """Open/create each parent via dir_fd without following any path component."""
        ws = self._scope.roots()[0]
        parts = rel_path.split("/")
        flags_dir = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags_dir |= os.O_DIRECTORY
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if not _supports_secure_dir_fd():
            raise UserFacingError(
                "[DENIED] nền tảng không hỗ trợ ghi file image_gen an toàn bằng dir_fd"
            )
        try:
            dir_fd = os.open(ws, flags_dir | nofollow)
        except OSError as e:
            raise UserFacingError(
                "[DENIED] không mở được workspace cho image_gen"
            ) from e
        try:
            for part in parts[:-1]:
                try:
                    next_fd = os.open(part, flags_dir | nofollow, dir_fd=dir_fd)
                except FileNotFoundError:
                    try:
                        os.mkdir(part, 0o700, dir_fd=dir_fd)
                    except FileExistsError:
                        pass
                    next_fd = os.open(part, flags_dir | nofollow, dir_fd=dir_fd)
                st = os.fstat(next_fd)
                if not stat.S_ISDIR(st.st_mode):
                    os.close(next_fd)
                    raise UserFacingError(
                        "[DENIED] image_gen từ chối thư mục đích không an toàn"
                    )
                os.close(dir_fd)
                dir_fd = next_fd
            return dir_fd, parts[-1]
        except Exception:
            os.close(dir_fd)
            raise

    @staticmethod
    def _dest_snapshot(dir_fd: int, name: str) -> tuple[int, ...] | None:
        try:
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if _is_reparse(st):
            raise UserFacingError("[DENIED] image_gen từ chối ghi đè symlink")
        if not stat.S_ISREG(st.st_mode):
            raise UserFacingError("[DENIED] image_gen chỉ ghi regular file")
        return _stat_identity(st)

    def _atomic_write_posix(self, rel_path: str, data: bytes) -> None:
        """Secure dir_fd traversal + atomic replace; reject destination TOCTOU changes."""
        dir_fd, dest_name = self._open_secure_parent(rel_path)
        before = self._dest_snapshot(dir_fd, dest_name)
        tmp_name = f".{dest_name}.{uuid.uuid4().hex}.tmp"
        fd: int | None = None
        try:
            fd = os.open(
                tmp_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=dir_fd,
            )
            written = 0
            view = memoryview(data)
            while written < len(data):
                count = os.write(fd, view[written:])
                if count <= 0:
                    raise OSError("short write while saving image")
                written += count
            os.fsync(fd)
            os.close(fd)
            fd = None
            if self._dest_snapshot(dir_fd, dest_name) != before:
                raise UserFacingError(
                    "[DENIED] file đích đổi trong lúc tạo ảnh; từ chối TOCTOU"
                )
            os.replace(tmp_name, dest_name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            os.fsync(dir_fd)
        except UserFacingError:
            raise
        except OSError as e:
            raise UserFacingError("[DENIED] không ghi được file ảnh vào workspace") from e
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.unlink(tmp_name, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
            except OSError:
                pass
            os.close(dir_fd)

    def _safe_path_parent(self, rel_path: str) -> tuple[Path, str]:
        """Windows/path backend: create/check each parent and reject every reparse point."""
        ws = self._scope.roots()[0]
        try:
            root_st = os.lstat(ws)
        except OSError as e:
            raise UserFacingError("[DENIED] workspace image_gen không tồn tại") from e
        if _is_reparse(root_st) or not stat.S_ISDIR(root_st.st_mode):
            raise UserFacingError("[DENIED] workspace image_gen không an toàn")
        parent = ws
        parts = rel_path.split("/")
        for part in parts[:-1]:
            child = parent / part
            try:
                child.mkdir(mode=0o700)
            except FileExistsError:
                pass
            try:
                child_st = os.lstat(child)
            except OSError as e:
                raise UserFacingError(
                    "[DENIED] image_gen không mở được thư mục đích"
                ) from e
            if _is_reparse(child_st) or not stat.S_ISDIR(child_st.st_mode):
                raise UserFacingError(
                    "[DENIED] image_gen từ chối thư mục đích không an toàn"
                )
            parent = child
        return parent, parts[-1]

    def _lock_windows_parent_chain(self, rel_path: str) -> tuple[Path, str, list[int]]:
        """Hold non-write/non-delete-shared handles so parent directories cannot be swapped."""
        if os.name != "nt":
            parent, name = self._safe_path_parent(rel_path)
            return parent, name, []

        import ctypes

        kernel32 = getattr(getattr(ctypes, "windll"), "kernel32")
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        create_file.restype = ctypes.c_void_p
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int
        invalid_handle = ctypes.c_void_p(-1).value
        file_read_attributes = 0x0080
        file_share_read = 0x00000001
        open_existing = 3
        backup_semantics = 0x02000000
        open_reparse_point = 0x00200000
        handles: list[int] = []

        def lock_directory(path: Path) -> None:
            handle = create_file(
                str(path),
                file_read_attributes,
                file_share_read,
                None,
                open_existing,
                backup_semantics | open_reparse_point,
                None,
            )
            value = int(handle or 0)
            if value in (0, invalid_handle):
                get_last_error = getattr(ctypes, "get_last_error", lambda: 0)
                raise OSError(get_last_error(), f"cannot lock directory {path.name}")
            handles.append(value)
            st = os.lstat(path)
            if _is_reparse(st) or not stat.S_ISDIR(st.st_mode):
                raise UserFacingError(
                    "[DENIED] image_gen từ chối thư mục đích không an toàn"
                )

        ws = self._scope.roots()[0]
        parent = ws
        parts = rel_path.split("/")
        try:
            lock_directory(parent)
            for part in parts[:-1]:
                child = parent / part
                try:
                    child.mkdir(mode=0o700)
                except FileExistsError:
                    pass
                lock_directory(child)
                parent = child
            return parent, parts[-1], handles
        except Exception:
            for handle in reversed(handles):
                close_handle(handle)
            raise

    @staticmethod
    def _close_windows_handles(handles: list[int]) -> None:
        if not handles:
            return
        import ctypes

        close_handle = getattr(getattr(ctypes, "windll"), "kernel32").CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int
        for handle in reversed(handles):
            close_handle(handle)

    @staticmethod
    def _path_dest_snapshot(path: Path) -> tuple[int, ...] | None:
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return None
        if _is_reparse(st):
            raise UserFacingError("[DENIED] image_gen từ chối ghi đè reparse point")
        if not stat.S_ISREG(st.st_mode):
            raise UserFacingError("[DENIED] image_gen chỉ ghi regular file")
        return _stat_identity(st)

    @staticmethod
    def _verify_named_fd(fd: int, path: Path) -> None:
        try:
            opened = os.fstat(fd)
            named = os.lstat(path)
        except OSError as e:
            raise UserFacingError(
                "[DENIED] file image_gen đổi trong lúc mở; từ chối TOCTOU"
            ) from e
        if (
            not stat.S_ISREG(opened.st_mode)
            or _is_reparse(named)
            or not stat.S_ISREG(named.st_mode)
            or _stat_identity(opened) != _stat_identity(named)
        ):
            raise UserFacingError(
                "[DENIED] file image_gen đổi trong lúc mở; từ chối TOCTOU"
            )

    def _atomic_write_path(self, rel_path: str, data: bytes) -> None:
        """Windows-compatible atomic replace with reparse and identity checks."""
        parent, dest_name, parent_handles = self._lock_windows_parent_chain(rel_path)
        dest = parent / dest_name
        before = self._path_dest_snapshot(dest)
        tmp = parent / f".{dest_name}.{uuid.uuid4().hex}.tmp"
        fd: int | None = None
        try:
            fd = os.open(
                tmp,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _WINDOWS_BINARY,
                0o600,
            )
            self._verify_named_fd(fd, tmp)
            written = 0
            view = memoryview(data)
            while written < len(data):
                count = os.write(fd, view[written:])
                if count <= 0:
                    raise OSError("short write while saving image")
                written += count
            os.fsync(fd)
            self._verify_named_fd(fd, tmp)
            os.close(fd)
            fd = None
            if self._path_dest_snapshot(dest) != before:
                raise UserFacingError(
                    "[DENIED] file đích đổi trong lúc tạo ảnh; từ chối TOCTOU"
                )
            os.replace(tmp, dest)
            # Verify the named result is the exact bytes written and is still non-reparse.
            verify_fd = os.open(dest, os.O_RDONLY | _WINDOWS_BINARY)
            try:
                self._verify_named_fd(verify_fd, dest)
                saved = bytearray()
                while len(saved) <= len(data):
                    chunk = os.read(verify_fd, min(1024 * 1024, len(data) + 1 - len(saved)))
                    if not chunk:
                        break
                    saved.extend(chunk)
                if bytes(saved) != data:
                    raise UserFacingError(
                        "[DENIED] file ảnh đổi ngay sau khi ghi; từ chối TOCTOU"
                    )
            finally:
                os.close(verify_fd)
        except UserFacingError:
            raise
        except OSError as e:
            raise UserFacingError("[DENIED] không ghi được file ảnh vào workspace") from e
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            except OSError:
                pass
            self._close_windows_handles(parent_handles)

    def _atomic_write_bytes(self, rel_path: str, data: bytes) -> None:
        if _supports_secure_dir_fd():
            self._atomic_write_posix(rel_path, data)
        else:
            self._atomic_write_path(rel_path, data)

    def _charged_attrs(self, size: str) -> dict[str, object]:
        attrs: dict[str, object] = {
            "provider": self._cost_provider,
            "model": self._cost_model,
            "images": 1,
            "size": size,
        }
        if self._cost_usd:
            attrs["cost_usd"] = self._cost_usd
        return attrs

    async def run(self, args: dict, ctx: ToolCtx) -> ToolResult:
        key = self._resolve_key()
        rel = args["path"].strip().replace("\\", "/").strip("/")
        dest = self._resolve_dest(rel)
        size = args.get("size", "1024x1024")
        try:
            result = await asyncio.wait_for(
                self._gen(args["prompt"].strip(), key, size=size),
                timeout=self._timeout_sec,
            )
        except ChargedImageError as e:
            return ToolResult.error(
                str(e).replace(key, "[REDACTED]"),
                span_attrs=self._charged_attrs(size),
            )
        except UserFacingError as e:
            raise UserFacingError(str(e).replace(key, "[REDACTED]")) from None
        except TimeoutError:
            raise UserFacingError("[DENIED] image_gen hết thời gian chờ backend") from None
        except Exception:
            raise UserFacingError("[DENIED] image_gen backend lỗi") from None

        # Từ đây provider đã trả lời cho một lần sinh ảnh và có thể đã tính phí. Giữ cost
        # attrs cả khi payload/file validation sau đó thất bại để ledger không bỏ sót call.
        span_attrs = self._charged_attrs(size)
        try:
            if not isinstance(result, GeneratedImage):
                raise UserFacingError(
                    "[DENIED] image_gen backend trả payload không hợp lệ"
                )
            if not result.data or not isinstance(result.mime, str) or not result.mime:
                raise UserFacingError("[DENIED] image_gen backend trả ảnh rỗng")
            actual_mime = sniff_image_mime(result.data)
            if result.mime != actual_mime:
                # Không echo MIME do provider/injected backend kiểm soát vào context.
                raise UserFacingError(
                    "[DENIED] image_gen backend khai MIME không khớp ảnh"
                )
            if not self._ext_matches_mime(dest, actual_mime):
                raise UserFacingError(
                    f"[DENIED] đuôi file không khớp MIME {actual_mime} — "
                    f"dùng {', '.join(_MIME_TO_EXTS.get(actual_mime, ()))}"
                )
            if not isinstance(result.revised_prompt, str):
                raise UserFacingError(
                    "[DENIED] image_gen backend trả revised_prompt không hợp lệ"
                )
            if len(result.revised_prompt) > _MAX_PROMPT_CHARS:
                raise UserFacingError(
                    "[DENIED] image_gen backend trả revised_prompt quá dài"
                )
            self._atomic_write_bytes(rel, result.data)
        except UserFacingError as e:
            return ToolResult.error(
                str(e).replace(key, "[REDACTED]"), span_attrs=span_attrs
            )
        except Exception:
            return ToolResult.error(
                "[DENIED] image_gen xử lý ảnh thất bại", span_attrs=span_attrs
            )

        # Nội dung trả agent: path tương đối + meta; không nhúng bytes / secret.
        revised = (result.revised_prompt or "").replace(key, "[REDACTED]")
        lines = [
            f"đã lưu ảnh {len(result.data)} byte ({actual_mime}) → {rel}",
        ]
        if revised:
            lines.append(f"revised_prompt: {revised}")
        span_attrs.update(
            bytes=len(result.data),
            mime=actual_mime,
            path=rel,
        )
        return ToolResult.success("\n".join(lines), span_attrs=span_attrs)
