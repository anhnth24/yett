"""Test setup wizard logic + file secret store (không I/O thật)."""

from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from yett.secrets import file_store
from yett.secrets.file_store import FileSecretStore
from yett.setup_wizard import Answers, build_config, find_provider, run_wizard


def test_build_config_glm() -> None:
    ans = Answers(provider="glm", model="glm-5.2", projects={"go-learn": "/mnt/d/go-learn"},
                  monthly_budget_usd=50)
    cfg = build_config(ans)
    assert cfg["provider"]["name"] == "glm"
    assert cfg["provider"]["model"] == "glm-5.2"
    assert cfg["provider"]["api_key_secret"] == "llm_key"  # tên, không phải giá trị
    assert cfg["projects"]["go-learn"]["path"] == "/mnt/d/go-learn"
    assert cfg["secret_backend"] == "file"
    assert "api.z.ai" in cfg["egress"]["allowlist"]
    assert cfg["budget"]["monthly_cost_alert_usd"] == 50


def test_build_config_no_secret_value_in_config() -> None:
    ans = Answers(provider="deepseek", model="deepseek-chat", api_key="SECRET-XYZ")
    cfg = build_config(ans)
    # API key KHÔNG bao giờ nằm trong config
    assert "SECRET-XYZ" not in yaml.safe_dump(cfg)


def test_build_config_custom_provider_url_uses_custom_egress_host() -> None:
    ans = Answers(
        provider="anthropic",
        model="claude-haiku-4-5",
        base_url="https://llm.internal.example/v1",
    )
    cfg = build_config(ans)
    assert cfg["provider"]["base_url"] == "https://llm.internal.example/v1"
    assert cfg["egress"]["allowlist"] == ["llm.internal.example"]


def test_find_provider() -> None:
    assert find_provider("glm").label.startswith("GLM")
    assert find_provider("khong-ton-tai") is None


def test_file_secret_store(tmp_path: Path) -> None:
    """[P1-13] File secret phải owner-only. POSIX: chmod 600 qua os.stat. Windows: os.chmod
    không map sang NTFS DACL nên verify ACL THẬT qua `icacls` (máy Windows thật, không mock)."""
    store = FileSecretStore(tmp_path / "secrets")
    store.set("llm_key", "abc123")
    assert store.get("llm_key") == "abc123"
    assert store.has("llm_key")
    secret_path = tmp_path / "secrets" / "llm_key"

    if sys.platform == "win32":
        result = subprocess.run(
            ["icacls", str(secret_path)], capture_output=True, text=True, check=True,
        )
        acl_output = result.stdout
        assert "(I)" not in acl_output, f"vẫn còn ACE kế thừa (chưa /inheritance:r): {acl_output}"
        assert ":(F)" in acl_output, f"không thấy Full Control owner-only: {acl_output}"
        for leaked_group in ("Everyone", "BUILTIN\\Users", "Authenticated Users"):
            assert leaked_group not in acl_output, f"ACL vẫn cho phép {leaked_group}: {acl_output}"
    else:
        mode = secret_path.stat().st_mode
        assert stat.S_IMODE(mode) == 0o600


def test_file_secret_store_strips_preexisting_broad_acl(tmp_path: Path) -> None:
    """[P1-13 regression] `/inheritance:r` chỉ bỏ ACE KẾ THỪA — ACE explicit có sẵn
    (vd Everyone:(F) trên thư mục tmp mở) vẫn sống sót nếu chỉ /grant owner. Store phải
    chủ động gỡ mọi principal khác owner rồi verify. Grant bằng SID `*S-1-1-0` (Everyone)
    để không phụ thuộc locale."""
    if sys.platform != "win32":
        pytest.skip("Windows DACL only")
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    leaked = secrets_dir / "llm_key"
    leaked.write_text("old", encoding="utf-8")
    subprocess.run(
        ["icacls", str(leaked), "/grant", "*S-1-1-0:F"],
        capture_output=True, text=True, check=True,
    )
    before = subprocess.run(
        ["icacls", str(leaked)], capture_output=True, text=True, check=True,
    ).stdout
    assert "S-1-1-0" in before or "Everyone" in before, f"sanity: chưa gắn được ACE rộng: {before}"

    store = FileSecretStore(secrets_dir)
    store.set("llm_key", "abc123")

    after = subprocess.run(
        ["icacls", str(leaked)], capture_output=True, text=True, check=True,
    ).stdout
    for leaked_group in ("Everyone", "S-1-1-0", "BUILTIN\\Users", "Authenticated Users"):
        assert leaked_group not in after, f"ACL vẫn cho phép {leaked_group}: {after}"
    assert ":(F)" in after, f"không thấy Full Control owner: {after}"


def test_file_secret_store_acl_failure_warns_loudly_not_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """[P1-13] icacls/chmod lỗi KHÔNG được nuốt im lặng (`except OSError: pass` cũ) —
    phải cảnh báo to qua log + stderr. Set vẫn thành công (không chặn wizard)."""
    caplog.set_level("WARNING", logger="yett.secrets.file_store")

    if sys.platform == "win32":
        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            if cmd[0] == "whoami":
                return subprocess.CompletedProcess(
                    cmd, 0, stdout='"D\\u","S-1-5-21-1-2-3-1001"\n', stderr="",
                )
            raise subprocess.CalledProcessError(1, cmd, stderr="access denied (giả lập test)")

        monkeypatch.setattr(file_store.subprocess, "run", fake_run)
    else:
        monkeypatch.setattr(sys, "platform", "linux")

        def fake_chmod(path: object, mode: int) -> None:
            raise OSError("permission denied (giả lập test)")

        monkeypatch.setattr(file_store.os, "chmod", fake_chmod)

    store = FileSecretStore(tmp_path / "secrets")
    store.set("llm_key", "abc123")  # không raise — ghi vẫn thành công

    assert store.get("llm_key") == "abc123"
    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert any("CẢNH BÁO" in w for w in warnings), f"không thấy cảnh báo to: {warnings}"


def test_run_wizard_end_to_end(tmp_path: Path) -> None:
    # Giả lập người dùng: chọn GLM(1) → model glm-5.2(1) → key → 1 project → không budget
    answers_iter = iter([
        "1",                       # provider = GLM
        "1",                       # model = glm-5.2
        "sk-test-key",             # API key
        "/mnt/d/work/duan",        # project path
        "duan",                    # project name
        "",                        # dừng thêm project
        "20",                      # budget 20 USD
    ])
    out: list[str] = []
    store = FileSecretStore(tmp_path / "secrets")
    cfg_path = tmp_path / "config" / "harness.yaml"
    ans = run_wizard(
        prompt=lambda msg: next(answers_iter),
        emit=out.append,
        config_path=cfg_path,
        secret_setter=store.set,
    )
    assert ans.provider == "glm" and ans.model == "glm-5.2"
    assert ans.projects == {"duan": "/mnt/d/work/duan"}
    # config đã ghi, không chứa key
    written = cfg_path.read_text(encoding="utf-8")
    assert "sk-test-key" not in written
    assert "glm-5.2" in written
    # key nằm trong secret store
    assert store.get("llm_key") == "sk-test-key"
