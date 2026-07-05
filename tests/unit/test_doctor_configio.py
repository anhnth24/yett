"""Test yett doctor + config IO (validate/save)."""

from __future__ import annotations

from pathlib import Path

from yett.doctor import CHECKS, run_doctor
from yett.web.configio import validate_config_text, write_config_text

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
