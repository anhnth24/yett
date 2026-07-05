"""Entry point cho bản đóng gói exe. Double-click yett.exe → mở web UI + trình duyệt.

Nếu chưa có config/harness.yaml cạnh exe → chạy wizard setup trước.
Truyền tham số dòng lệnh vẫn hoạt động như CLI (yett.exe chat "...", yett.exe setup...).
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    from yett.cli import main as cli_main

    # Có tham số → hành xử như CLI bình thường.
    if len(sys.argv) > 1:
        return cli_main(sys.argv[1:])

    # Không tham số (double-click): chưa có config → setup; có rồi → serve --open.
    cfg = Path("config/harness.yaml")
    if not cfg.exists():
        print("[yett] Chưa có cấu hình — chạy wizard cài đặt...")
        cli_main(["setup"])
    return cli_main(["serve", "--open"])


if __name__ == "__main__":
    raise SystemExit(main())
