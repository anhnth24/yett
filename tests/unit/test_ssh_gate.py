"""Test Gate phân lớp lệnh SSH theo host profile + App đăng ký ssh_exec/log_read."""

from __future__ import annotations

from pathlib import Path

from yett.config.models import HostCfg, RemoteCfg, SecurityCfg
from yett.security.basic_gate import BasicGate
from yett.security.gate import safe_evaluate
from yett.tools.remote.hostprofile import HostProfile, HostRegistry


class _Ctx:
    session_key = "t"


def _gate() -> BasicGate:
    hosts = HostRegistry({
        "uat": HostProfile(address="10.0.0.1", auth="keyfile:k",
                           log_paths=["/var/log/app/*.log"], deploy_script="/opt/deploy/run.sh"),
        "prod": HostProfile(address="10.0.0.9", auth="keyfile:k", tier="restricted"),
    })
    return BasicGate(SecurityCfg(), hosts=hosts)


def test_ssh_readonly_allowed() -> None:
    dec = safe_evaluate(_gate(), "ssh_exec", {"host": "uat", "cmd": "tail -n 100 /var/log/app/x.log"}, _Ctx())
    assert dec.verdict == "allow"


def test_ssh_deploy_needs_approval() -> None:
    dec = safe_evaluate(_gate(), "ssh_exec", {"host": "uat", "cmd": "/opt/deploy/run.sh"}, _Ctx())
    assert dec.verdict == "need_approval"


def test_ssh_delete_hardline_denied() -> None:
    for cmd in ["rm -rf /data", "find /var -delete", "xargs rm < list"]:
        dec = safe_evaluate(_gate(), "ssh_exec", {"host": "uat", "cmd": cmd}, _Ctx())
        assert dec.verdict == "deny", cmd


def test_ssh_unknown_host_denied() -> None:
    dec = safe_evaluate(_gate(), "ssh_exec", {"host": "khong-co", "cmd": "tail x"}, _Ctx())
    assert dec.verdict == "deny" and dec.rule_id == "SSH_UNKNOWN_HOST"


def test_ssh_restricted_no_deploy() -> None:
    dec = safe_evaluate(_gate(), "ssh_exec", {"host": "prod", "cmd": "/opt/deploy/run.sh"}, _Ctx())
    assert dec.verdict == "deny"  # tier restricted không cho deploy


def test_log_read_known_host_allowed() -> None:
    dec = safe_evaluate(_gate(), "log_read", {"host": "uat", "path": "/var/log/app/e.log"}, _Ctx())
    assert dec.verdict == "allow"


def test_app_registers_ssh_tools(tmp_path: Path) -> None:
    import itertools

    from yett.app import App
    from yett.config.models import BudgetCfg, HarnessCfg, ProviderCfg, SandboxCfg
    from yett.provider.fake import FakeProvider, text_result
    from yett.secrets.backends import InMemorySecretStore

    (tmp_path / "ws").mkdir()
    cfg = HarnessCfg(
        provider=ProviderCfg(name="fake", model="fake-1"), workspace_root=tmp_path / "ws",
        sandbox=SandboxCfg(backend="local"), budget=BudgetCfg(max_loop_iterations=2),
        remote=RemoteCfg(hosts={"uat": HostCfg(address="10.0.0.1", auth="keyfile:k",
                                               log_paths=["/var/log/*.log"])}),
    )
    c = itertools.count(1)
    app = App(provider=FakeProvider([text_result("ok")]), cfg=cfg, state_dir=tmp_path / "st",
              secrets=InMemorySecretStore({"k": "KEY"}), clock=lambda: float(next(c)))
    assert app.registry.has("ssh_exec")
    assert app.registry.has("log_read")
    # gate của app phân lớp SSH: xóa file bị chặn
    dec = safe_evaluate(app.gate, "ssh_exec", {"host": "uat", "cmd": "rm -rf /"}, _Ctx())
    assert dec.verdict == "deny"
    app.close()
