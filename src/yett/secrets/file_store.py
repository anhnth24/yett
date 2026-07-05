"""File secret store (spec P0-P1 §3.2). Lưu secret vào secrets/<name>, quyền 600.

Cho wizard setup: key nằm trên đĩa trong thư mục secrets/ (gitignored, chmod 600) —
KHÔNG nằm trong config, KHÔNG vào context/span/log. Đơn giản hơn quản lý env cho user local.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from yett.errors import SecretNotFound


class FileSecretStore:
    def __init__(self, root: str | Path = "secrets") -> None:
        self._root = Path(root)

    def set(self, name: str, value: str) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self._root, stat.S_IRWXU)  # 700
        except OSError:
            pass
        p = self._root / name
        p.write_text(value, encoding="utf-8")
        try:
            os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)  # 600
        except OSError:
            pass

    def get(self, name: str) -> str:
        p = self._root / name
        if not p.exists():
            raise SecretNotFound(f"secret '{name}' không tồn tại tại {p}")
        return p.read_text(encoding="utf-8").strip()

    def has(self, name: str) -> bool:
        return (self._root / name).exists()
