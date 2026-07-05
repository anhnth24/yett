"""list_dir / grep (spec P0-P1) — điều hướng code, giới hạn trong ProjectScope.

Cùng ranh giới bảo mật với read_file: mọi path resolve rồi kiểm thuộc một root
cho phép; ra ngoài → từ chối. An toàn hơn shell qua exec (không đọc lọt ngoài scope,
output có giới hạn, bỏ qua thư mục/nhiễu và file nhị phân).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from yett.errors import UserFacingError
from yett.tools.base import ToolCtx, ToolResult
from yett.tools.projects import ProjectScope

# Thư mục bỏ qua khi duyệt/grep (nhiễu, nặng, không phải source người đọc).
_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", ".idea", ".vscode",
    "vendor", "target", ".next", ".gradle", "bin", "obj",
})
_MAX_ENTRIES = 500  # trần số dòng list_dir
_MAX_MATCHES = 200  # trần số match grep
_MAX_FILE_BYTES = 2_000_000  # bỏ qua file > 2MB khi grep


def _skip(name: str) -> bool:
    return name in _SKIP_DIRS


class ListDirTool:
    """Liệt kê cây thư mục trong project/workspace đã đăng ký."""

    name = "list_dir"
    schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "thư mục cần liệt kê (trong scope)"},
            "depth": {"type": "integer", "description": "độ sâu tối đa (mặc định 2)"},
        },
        "required": ["path"],
    }

    def __init__(self, scope: ProjectScope) -> None:
        self._scope = scope

    def validate(self, args: dict) -> None:
        if not args.get("path"):
            raise UserFacingError("thiếu 'path'")

    async def run(self, args: dict, ctx: ToolCtx) -> ToolResult:
        root = self._scope.resolve_in_scope(args["path"])  # raise nếu ngoài scope
        if not root.exists():
            return ToolResult.error(f"không tồn tại: {args['path']}")
        if not root.is_dir():
            return ToolResult.error(f"không phải thư mục: {args['path']}")
        depth = max(1, int(args.get("depth", 2)))
        lines: list[str] = []
        truncated = self._walk(root, root, depth, lines)
        body = "\n".join(lines) if lines else "(rỗng)"
        if truncated:
            body += f"\n… (đã cắt ở {_MAX_ENTRIES} mục)"
        return ToolResult.success(body)

    def _walk(self, base: Path, cur: Path, depth: int, out: list[str]) -> bool:
        try:
            entries = sorted(cur.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError as e:
            out.append(f"  (không đọc được {cur.name}: {e})")
            return False
        for entry in entries:
            if len(out) >= _MAX_ENTRIES:
                return True
            rel = entry.relative_to(base)
            indent = "  " * (len(rel.parts) - 1)
            if entry.is_dir() and _skip(entry.name):
                out.append(f"{indent}{entry.name}/ (bỏ qua)")
                continue
            out.append(f"{indent}{entry.name}{'/' if entry.is_dir() else ''}")
            if entry.is_dir() and len(rel.parts) < depth:
                if self._walk(base, entry, depth, out):
                    return True
        return False


class GrepTool:
    """Tìm regex trong source, giới hạn trong project/workspace đã đăng ký."""

    name = "grep"
    schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "regex Python"},
            "path": {"type": "string", "description": "thư mục/file để tìm (mặc định: mọi root)"},
            "glob": {"type": "string", "description": "lọc theo đuôi/tên, vd '*.go' hoặc '*.py'"},
            "ignore_case": {"type": "boolean"},
            "max_results": {"type": "integer"},
        },
        "required": ["pattern"],
    }

    def __init__(self, scope: ProjectScope) -> None:
        self._scope = scope

    def validate(self, args: dict) -> None:
        if not args.get("pattern"):
            raise UserFacingError("thiếu 'pattern'")
        try:
            re.compile(args["pattern"])
        except re.error as e:
            raise UserFacingError(f"regex không hợp lệ: {e}")

    async def run(self, args: dict, ctx: ToolCtx) -> ToolResult:
        flags = re.IGNORECASE if args.get("ignore_case") else 0
        rx = re.compile(args["pattern"], flags)
        cap = min(int(args.get("max_results", _MAX_MATCHES)), _MAX_MATCHES)
        glob = args.get("glob")

        if args.get("path"):
            roots = [self._scope.resolve_in_scope(args["path"])]
        else:
            roots = self._scope.roots()

        hits: list[str] = []
        for root in roots:
            if self._search(root, rx, glob, cap, hits):
                break
        if not hits:
            return ToolResult.success("(không có match)")
        body = "\n".join(hits)
        if len(hits) >= cap:
            body += f"\n… (đạt trần {cap} match — thu hẹp pattern/path để xem thêm)"
        return ToolResult.success(body)

    def _search(self, root: Path, rx, glob, cap: int, hits: list[str]) -> bool:
        files: list[Path] = [root] if root.is_file() else []
        if root.is_dir():
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if not _skip(d)]
                for fn in sorted(filenames):
                    files.append(Path(dirpath) / fn)
        for f in files:
            if glob and not f.match(glob):
                continue
            if self._grep_file(f, rx, cap, hits):
                return True
        return False

    def _grep_file(self, f: Path, rx, cap: int, hits: list[str]) -> bool:
        try:
            if f.stat().st_size > _MAX_FILE_BYTES:
                return False
            with f.open("r", encoding="utf-8", errors="strict") as fh:
                for i, line in enumerate(fh, 1):
                    if rx.search(line):
                        hits.append(f"{f}:{i}: {line.rstrip()[:300]}")
                        if len(hits) >= cap:
                            return True
        except (OSError, UnicodeDecodeError):
            return False  # nhị phân/không đọc được → bỏ qua
        return False
