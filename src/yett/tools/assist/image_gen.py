"""image_gen (spec P3 §5.3, WP3.6). API key từ secret store; ảnh lưu trong workspace.

Backend injectable để test offline / local không-egress. Cost span ghi vào ledger
(per_call) qua `span_attrs` trên ToolResult — loop gắn vào TOOL_CALL span (không chứa secret).
"""

from __future__ import annotations

import os
import re
import stat
import uuid
from pathlib import Path

from yett.errors import SecretNotFound, UserFacingError
from yett.tools.assist.image_backend import GeneratedImage, ImageGenFn
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
    ) -> None:
        self._gen = image_fn
        self._secrets = secrets
        self._key_name = api_key_secret
        self._scope = scope
        self._cost_usd = cost_usd
        self._cost_provider = cost_provider
        self._cost_model = cost_model

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
        if ".." in Path(path).parts:
            raise UserFacingError("'path' không được chứa '..'")
        if "\x00" in path:
            raise UserFacingError("'path' không hợp lệ")
        norm = path.replace("\\", "/").strip("/")
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
        if not isinstance(key, str) or not key:
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
        lexical = ws.joinpath(*rel.split("/"))
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
        if dest.is_symlink():
            raise UserFacingError("[DENIED] image_gen từ chối ghi đè symlink")
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

    @staticmethod
    def _atomic_write_bytes(dest: Path, data: bytes) -> None:
        """Ghi atomic: temp O_EXCL|O_NOFOLLOW trong cùng dir → os.replace. Không follow symlink."""
        parent = dest.parent
        parent.mkdir(parents=True, exist_ok=True)
        flags_dir = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags_dir |= os.O_DIRECTORY
        # O_NOFOLLOW trên directory: fail nếu parent là symlink (chống swap dir).
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        try:
            dir_fd = os.open(parent, flags_dir | nofollow)
        except OSError as e:
            raise UserFacingError(
                f"[DENIED] không mở được thư mục đích cho image_gen: {dest.parent.name}"
            ) from e
        tmp_name = f".{dest.name}.{uuid.uuid4().hex}.tmp"
        fd: int | None = None
        try:
            # Nếu đích đang là symlink — từ chối (không ghi đè qua link).
            try:
                st = os.lstat(dest.name, dir_fd=dir_fd)
                if stat.S_ISLNK(st.st_mode):
                    raise UserFacingError(
                        "[DENIED] image_gen từ chối ghi đè symlink"
                    )
                if not stat.S_ISREG(st.st_mode):
                    raise UserFacingError(
                        "[DENIED] image_gen chỉ ghi regular file"
                    )
            except FileNotFoundError:
                pass
            fd = os.open(
                tmp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow,
                0o600,
                dir_fd=dir_fd,
            )
            written = 0
            view = memoryview(data)
            while written < len(data):
                written += os.write(fd, view[written:])
            os.fsync(fd)
            os.close(fd)
            fd = None
            os.replace(tmp_name, dest.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
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

    async def run(self, args: dict, ctx: ToolCtx) -> ToolResult:
        key = self._resolve_key()
        rel = args["path"].strip().replace("\\", "/").strip("/")
        dest = self._resolve_dest(rel)
        size = args.get("size", "1024x1024")
        try:
            result = await self._gen(args["prompt"].strip(), key, size=size)
        except UserFacingError as e:
            raise UserFacingError(str(e).replace(key, "[REDACTED]")) from e
        except Exception as e:
            raise UserFacingError("[DENIED] image_gen backend lỗi") from e

        if not isinstance(result, GeneratedImage):
            raise UserFacingError("[DENIED] image_gen backend trả payload không hợp lệ")
        if not result.data or not result.mime:
            raise UserFacingError("[DENIED] image_gen backend trả ảnh rỗng")
        if not self._ext_matches_mime(dest, result.mime):
            raise UserFacingError(
                f"[DENIED] đuôi file không khớp MIME {result.mime} — "
                f"dùng {', '.join(_MIME_TO_EXTS.get(result.mime, ()))}"
            )

        self._atomic_write_bytes(dest, result.data)

        # Nội dung trả agent: path tương đối + meta; không nhúng bytes / secret.
        revised = (result.revised_prompt or "").replace(key, "[REDACTED]")
        lines = [
            f"đã lưu ảnh {len(result.data)} byte ({result.mime}) → {rel}",
        ]
        if revised:
            lines.append(f"revised_prompt: {revised}")
        span_attrs: dict[str, object] = {
            "provider": self._cost_provider,
            "model": self._cost_model,
            "images": 1,
            "bytes": len(result.data),
            "mime": result.mime,
            "path": rel,
        }
        if self._cost_usd:
            span_attrs["cost_usd"] = self._cost_usd
        return ToolResult.success("\n".join(lines), span_attrs=span_attrs)
