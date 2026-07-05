# PyInstaller spec — đóng gói yett thành 1 file .exe (chạy trên Windows không cần cài Python).
# Build (trên Windows, trong venv đã `pip install -e . pyinstaller`):
#   pyinstaller packaging/yett.spec
# Kết quả: dist/yett.exe  — double-click sẽ mở web UI (yett serve --open).
# Lưu ý: config/harness.yaml + secrets/ nằm CẠNH yett.exe khi chạy (không nhúng secret vào exe).

# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hidden = collect_submodules("yett") + collect_submodules("pydantic") + ["sqlglot", "yaml"]

a = Analysis(
    ["launcher.py"],
    pathex=["../src"],
    binaries=[],
    datas=[("../skills", "skills")],   # bundle skill mặc định
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "mypy", "ruff"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="yett",
    console=True,          # giữ console để xem log/URL; đổi False nếu muốn ẩn
    disable_windowed_traceback=False,
    icon=None,
)
