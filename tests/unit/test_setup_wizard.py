"""Test setup wizard logic + file secret store (không I/O thật)."""

from __future__ import annotations

from pathlib import Path

import yaml

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


def test_find_provider() -> None:
    assert find_provider("glm").label.startswith("GLM")
    assert find_provider("khong-ton-tai") is None


def test_file_secret_store(tmp_path: Path) -> None:
    store = FileSecretStore(tmp_path / "secrets")
    store.set("llm_key", "abc123")
    assert store.get("llm_key") == "abc123"
    assert store.has("llm_key")
    # quyền file 600
    import stat
    mode = (tmp_path / "secrets" / "llm_key").stat().st_mode
    assert stat.S_IMODE(mode) == 0o600


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
