"""Project registry + path scoping (spec P0-P1, P1.3.6).

File/exec chỉ được phép trong workspace root + các project root đã đăng ký.
Chống path-traversal: resolve tuyệt đối rồi kiểm thuộc một root cho phép.
"""

from __future__ import annotations

from pathlib import Path

from yett.errors import UserFacingError


class ProjectScope:
    def __init__(self, workspace_root: Path, projects: dict[str, Path]) -> None:
        self._roots = [workspace_root.resolve()] + [p.resolve() for p in projects.values()]
        self._projects = {name: p.resolve() for name, p in projects.items()}

    def roots(self) -> list[Path]:
        return list(self._roots)

    def project_path(self, name: str) -> Path:
        if name not in self._projects:
            raise UserFacingError(f"project '{name}' chưa đăng ký")
        return self._projects[name]

    def resolve_in_scope(self, path: str | Path) -> Path:
        """Resolve path và bảo đảm nằm trong một root cho phép; nếu không → raise."""
        p = Path(path).resolve()
        for root in self._roots:
            if p == root or root in p.parents:
                return p
        raise UserFacingError(
            f"đường dẫn '{path}' nằm ngoài workspace và mọi project đã đăng ký — bị từ chối"
        )

    def is_in_scope(self, path: str | Path) -> bool:
        try:
            self.resolve_in_scope(path)
            return True
        except UserFacingError:
            return False
