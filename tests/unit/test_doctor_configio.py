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


def test_config_redacts_nonpattern_api_key_by_field(tmp_path: Path) -> None:
    """Key GLM dạng `id.secret` (KHÔNG có prefix sk-) không khớp pattern redactor → phải
    được che theo TÊN field `api_key`. Bảo vệ đúng provider mặc định của yett."""
    glm_key = "a1b2c3d4e5f6g7h8.i9j0k1l2m3n4o5p6"
    p = tmp_path / "harness.yaml"
    p.write_text(
        f'provider:\n  name: glm\n  model: glm-5.2\n  api_key: "{glm_key}"\n'
        "workspace_root: ./workspace\n",
        encoding="utf-8",
    )
    redacted = read_config_text_redacted(p)
    assert glm_key not in redacted  # không lộ ra trình duyệt
    assert "[REDACTED]" in redacted
    # api_key_secret (TÊN, không phải value) KHÔNG bị che
    assert write_config_text(p, redacted) is None
    assert glm_key in p.read_text(encoding="utf-8")  # lưu lại vẫn giữ key thật


def test_config_does_not_redact_secret_name_fields(tmp_path: Path) -> None:
    """`api_key_secret` là TÊN tham chiếu (không phải value) → KHÔNG bị che."""
    p = tmp_path / "harness.yaml"
    p.write_text(
        "provider:\n  name: glm\n  model: glm-5.2\n  api_key_secret: llm_key\n"
        "workspace_root: ./workspace\n",
        encoding="utf-8",
    )
    redacted = read_config_text_redacted(p)
    assert "llm_key" in redacted  # tên secret vẫn hiện để người dùng biết tham chiếu gì


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


def test_channel_bearer_secrets_are_redacted_and_restored_by_mapping_path(
    tmp_path: Path,
) -> None:
    p = tmp_path / "harness.yaml"
    telegram_code = "telegram-pair-123"
    zalo_code = "zalo-pair-456"
    webhook_secret = "zalo-webhook-secret-789"
    p.write_text(
        "provider:\n  name: fake\n  model: fake\n"
        "workspace_root: ./workspace\n"
        "channels:\n"
        "  telegram:\n"
        f"    pairing_code: {telegram_code}\n"
        "  zalo:\n"
        "    enabled: true\n"
        f"    pairing_code: {zalo_code}\n"
        "    mode: webhook\n"
        "    webhook_url: https://example.test/api/channels/zalo/webhook\n"
        f"    webhook_secret: {webhook_secret}\n",
        encoding="utf-8",
    )
    redacted = read_config_text_redacted(p)
    for secret in (telegram_code, zalo_code, webhook_secret):
        assert secret not in redacted
    assert redacted.count("[REDACTED]") == 3
    assert write_config_text(p, redacted) is None
    restored = p.read_text(encoding="utf-8")
    assert telegram_code in restored
    assert zalo_code in restored
    assert webhook_secret in restored


def test_config_restore_does_not_move_secret_between_channel_blocks(tmp_path: Path) -> None:
    """Removing Telegram must not make its same-indented pairing_code overwrite Zalo."""
    p = tmp_path / "harness.yaml"
    telegram_code = "telegram-pair-123"
    zalo_code = "zalo-pair-456"
    p.write_text(
        "provider:\n  name: fake\n  model: fake\n"
        "workspace_root: ./workspace\n"
        "channels:\n"
        "  telegram:\n"
        f"    pairing_code: {telegram_code}\n"
        "  zalo:\n"
        "    enabled: true\n"
        f"    pairing_code: {zalo_code}\n",
        encoding="utf-8",
    )
    redacted = read_config_text_redacted(p)
    # Simulate deleting the whole Telegram block in the browser while retaining Zalo's marker.
    submitted = redacted.replace(
        '  telegram:\n    pairing_code: "[REDACTED]"\n',
        "",
    )
    assert write_config_text(p, submitted) is None
    restored = p.read_text(encoding="utf-8")
    assert telegram_code not in restored
    assert zalo_code in restored


def test_config_validation_error_does_not_echo_webhook_secret() -> None:
    invalid_secret = "leakme"
    text = (
        "provider:\n  name: fake\n  model: fake\n"
        "workspace_root: ./workspace\n"
        "channels:\n"
        "  zalo:\n"
        "    enabled: true\n"
        "    mode: webhook\n"
        "    webhook_url: https://example.test/hook\n"
        f"    webhook_secret: {invalid_secret}\n"
    )
    error = validate_config_text(text)
    assert error is not None
    assert invalid_secret not in error
