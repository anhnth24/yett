"""`yett doctor` — kiểm tra máy có đủ công cụ chưa; thiếu thì chỉ cách cài (theo OS).

Lệnh cài đã research 2026 (winget/brew/apt). Đánh dấu tùy chọn nếu không bắt buộc để chạy core.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Check:
    name: str
    kind: str  # "cli" | "py" | "python"
    probe: str  # tên lệnh (cli) / tên module (py)
    required: bool
    why: str
    install: dict[str, str] = field(default_factory=dict)  # os -> lệnh cài
    daemon_hint: str = ""


def _os_key() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


CHECKS: list[Check] = [
    Check("Python 3.11+", "python", "", True, "runtime của yett", {
        "windows": "winget install -e --id Python.Python.3.11",
        "macos": "brew install python@3.11",
        "linux": "sudo apt install python3.11 python3-pip python3.11-venv",
    }),
    Check("git", "cli", "git", False, "đọc lịch sử project cho báo cáo tiến độ", {
        "windows": "winget install -e --id Git.Git",
        "macos": "brew install git",
        "linux": "sudo apt install git",
    }),
    Check("Docker", "cli", "docker", False, "sandbox cô lập cho tool exec (backend=docker)", {
        "windows": "winget install -e --id Docker.DockerDesktop",
        "macos": "brew install --cask docker-desktop",
        "linux": "xem https://docs.docker.com/engine/install/ubuntu/",
    }, daemon_hint="Kiểm daemon: `docker info` (Windows/macOS cần mở Docker Desktop)"),
    Check("asyncssh", "py", "asyncssh", False, "SSH remote (deploy UAT, đọc log server)", {
        "windows": "pip install asyncssh", "macos": "pip install asyncssh", "linux": "pip install asyncssh",
    }),
    Check("psycopg", "py", "psycopg", False, "query PostgreSQL", {
        "windows": 'pip install "psycopg[binary]"', "macos": 'pip install "psycopg[binary]"',
        "linux": 'pip install "psycopg[binary]"',
    }),
    Check("pymysql", "py", "pymysql", False, "query MySQL", {
        "windows": "pip install pymysql", "macos": "pip install pymysql", "linux": "pip install pymysql",
    }),
    Check("openfortivpn", "cli", "openfortivpn", False, "VPN Fortinet (đọc log/deploy qua VPN)", {
        "windows": "KHÔNG có bản Windows — chạy trong WSL2: sudo apt install openfortivpn "
                   "(hoặc dùng FortiClient GUI)",
        "macos": "brew install openfortivpn", "linux": "sudo apt install openfortivpn",
    }),
    Check("openvpn", "cli", "openvpn", False, "VPN OpenVPN", {
        "windows": "winget install -e --id OpenVPNTechnologies.OpenVPN",
        "macos": "brew install openvpn", "linux": "sudo apt install openvpn",
    }),
    Check("pyinstaller", "py", "PyInstaller", False, "đóng gói yett.exe", {
        "windows": "pip install pyinstaller", "macos": "pip install pyinstaller",
        "linux": "pip install pyinstaller",
    }),
]


def _present(c: Check) -> bool:
    if c.kind == "python":
        return sys.version_info >= (3, 11)
    if c.kind == "py":
        return importlib.util.find_spec(c.probe.lower()) is not None
    return shutil.which(c.probe) is not None


def _version(c: Check) -> str:
    try:
        if c.kind == "python":
            return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        if c.kind == "py":
            mod = __import__(c.probe.lower())
            return str(getattr(mod, "__version__", "?"))
        out = subprocess.run([c.probe, "--version"], capture_output=True, text=True, timeout=5)
        return (out.stdout or out.stderr).strip().split("\n")[0]
    except Exception:
        return "?"


def _docker_config_problem(config_path: str | Path) -> str | None:
    """Đọc config (nếu tồn tại) để biết `sandbox.backend` thật của người dùng. Docker trong
    `CHECKS` ở trên luôn là "tùy chọn" (vì backend có thể là `local`) — nhưng nếu config nói
    `backend: docker`, Docker KHÔNG còn tùy chọn (App fail-closed khi thiếu). Trả mô tả vấn đề
    nếu backend=docker mà Docker CLI/daemon chưa sẵn sàng; trả `None` nếu không có config,
    backend khác docker, config lỗi (đã có `yett setup`/validate báo riêng), hoặc Docker ổn."""
    p = Path(config_path)
    if not p.exists():
        return None
    try:
        from yett.config.loader import load_config

        cfg = load_config(p)
    except Exception:
        return None
    if cfg.sandbox.backend != "docker":
        return None
    from yett.sandbox.docker import docker_unavailable_reason

    return docker_unavailable_reason()


def run_doctor(emit=print, config_path: str | Path = "config/harness.yaml") -> int:
    """In trạng thái từng công cụ + cách cài phần thiếu. Trả số công cụ BẮT BUỘC còn thiếu.

    Nếu tìm thấy config tại `config_path` khai `sandbox.backend: docker`, kiểm thêm Docker
    CLI/daemon — lúc đó backend không còn tùy chọn (App fail-closed nếu thiếu)."""
    osk = _os_key()
    emit(f"yett doctor — hệ điều hành: {osk}\n")
    missing_required = 0
    missing_optional: list[Check] = []
    for c in CHECKS:
        ok = _present(c)
        tag = "BẮT BUỘC" if c.required else "tùy chọn"
        if ok:
            emit(f"  ✓ {c.name:<14} {_version(c)}")
        else:
            emit(f"  ✗ {c.name:<14} THIẾU ({tag}) — {c.why}")
            if c.required:
                missing_required += 1
            else:
                missing_optional.append(c)
    if missing_optional or missing_required:
        emit("\nCách cài phần còn thiếu:")
        for c in [x for x in CHECKS if not _present(x)]:
            cmd = c.install.get(osk, "(xem tài liệu)")
            emit(f"  • {c.name}: {cmd}")
            if c.daemon_hint:
                emit(f"      {c.daemon_hint}")
    docker_problem = _docker_config_problem(config_path)
    if docker_problem:
        emit(f"\n✗ config '{config_path}' khai sandbox.backend=docker nhưng {docker_problem}.")
        emit("   → 'yett chat'/'yett serve' sẽ TỪ CHỐI khởi động (fail-closed), KHÔNG hạ cấp về host.")
        missing_required += 1
    if missing_required == 0:
        emit("\n✓ Đủ điều kiện chạy yett (core). Tool tùy chọn cài thêm khi cần.")
    else:
        emit(f"\n✗ Thiếu {missing_required} thứ BẮT BUỘC — cài rồi chạy lại 'yett doctor'.")
    return missing_required
