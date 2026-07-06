"""Test yett doctor + config IO (validate/save)."""

from __future__ import annotations

from pathlib import Path

from yett.doctor import CHECKS, run_doctor
from yett.web.configio import (
    read_config_text_redacted,
    validate_config_text,
    write_config_text,
)

_VALID = """
provider:
  name: glm
  model: glm-5.2
  api_key: "k"
workspace_root: ./workspace
"""


def test_doctor_runs_and_reports(tmp_path: Path) -> None:
    lines: list[str] = []
    # config_path cô lập: doctor đọc config thật của máy dev (backend=docker → rc phụ thuộc
    # daemon đang chạy) nên test phải trỏ vào path không tồn tại để hermetic.
    rc = run_doctor(emit=lines.append, config_path=tmp_path / "no-config.yaml")
    # Python luôn có (ta đang chạy) → 0 required thiếu
    assert rc == 0
    joined = "\n".join(lines)
    assert "Python 3.11+" in joined
    # mọi check có lệnh cài cho cả 3 OS
    for c in CHECKS:
        assert set(c.install) >= {"windows", "macos", "linux"}


def test_config_validate_ok() -> None:
    assert validate_config_text(_VALID) is None


def test_config_validate_bad_yaml() -> None:
    err = validate_config_text("provider: [unclosed")
    assert err and "YAML" in err


def test_config_validate_bad_schema() -> None:
    err = validate_config_text("provider:\n  name: glm\n")  # thiếu model + workspace_root
    assert err and "không hợp lệ" in err


def test_config_write_valid(tmp_path: Path) -> None:
    p = tmp_path / "config" / "harness.yaml"
    err = write_config_text(p, _VALID)
    assert err is None
    assert p.exists() and "glm-5.2" in p.read_text(encoding="utf-8")


def test_config_write_invalid_not_saved(tmp_path: Path) -> None:
    p = tmp_path / "harness.yaml"
    err = write_config_text(p, "provider:\n  name: glm\n")
    assert err is not None
    assert not p.exists()  # config hỏng → KHÔNG ghi


def test_config_save_redacted_preserves_secret(tmp_path: Path) -> None:
    """Lưu lại đúng text đã redact (người dùng không sửa dòng secret) KHÔNG được ghi đè
    key thật bằng `[REDACTED]` — nếu không, tab Cấu hình sẽ phá config mỗi lần Lưu."""
    real_key = "sk-cp-abcdefghijklmnopqrstuvwxyz0123456789"
    p = tmp_path / "harness.yaml"
    p.write_text(
        f"provider:\n  name: glm\n  model: glm-5.2\n  api_key: {real_key}\n"
        "workspace_root: ./workspace\n",
        encoding="utf-8",
    )
    redacted = read_config_text_redacted(p)
    assert real_key not in redacted and "[REDACTED]" in redacted  # GET đã che
    err = write_config_text(p, redacted)  # POST y nguyên bản đã che
    assert err is None  # không còn pydantic list-error
    assert real_key in p.read_text(encoding="utf-8")  # key thật được khôi phục


def test_config_save_can_change_secret(tmp_path: Path) -> None:
    """Người dùng gõ key mới (không có marker) vẫn ghi đè bình thường."""
    p = tmp_path / "harness.yaml"
    p.write_text(
        "provider:\n  name: glm\n  model: glm-5.2\n  api_key: old-secret-value-1234567890\n"
        "workspace_root: ./workspace\n",
        encoding="utf-8",
    )
    new = (
        "provider:\n  name: glm\n  model: glm-5.2\n  api_key: brand-new-key-value\n"
        "workspace_root: ./workspace\n"
    )
    assert write_config_text(p, new) is None
    assert "brand-new-key-value" in p.read_text(encoding="utf-8")
