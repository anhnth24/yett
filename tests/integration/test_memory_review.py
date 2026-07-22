"""Integration: memory_propose wired qua App + CLI list/approve/reject (RG1-8)."""

from __future__ import annotations

import asyncio
import itertools
from pathlib import Path

import pytest

from yett.app import App
from yett.cli import main
from yett.config.models import (
    BudgetCfg, HarnessCfg, ProviderCfg, SandboxCfg, SecurityCfg, ToolRule,
)
from yett.provider.fake import FakeProvider, text_result, tool_result
from yett.secrets.backends import InMemorySecretStore


def _clock():
    c = itertools.count(1)
    return lambda: float(next(c))


def _cfg(tmp_path: Path, **kw) -> HarnessCfg:
    return HarnessCfg(
        provider=ProviderCfg(name="fake", model="fake-1"),
        workspace_root=tmp_path / "ws",
        sandbox=SandboxCfg(backend="local"),
        budget=BudgetCfg(max_loop_iterations=6),
        **kw,
    )


def test_agent_proposes_memory_via_app_no_direct_write(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir(parents=True)
    sec = SecurityCfg(allowlist=[ToolRule(tool="memory_propose", effect="allow")])
    provider = FakeProvider([
        tool_result("c1", "memory_propose", {"content": "Ưu tiên báo cáo ngắn."}),
        text_result("Đã đề xuất memory, chờ anh duyệt."),
    ])
    app = App(
        provider=provider, cfg=_cfg(tmp_path, security=sec), state_dir=tmp_path / "st",
        secrets=InMemorySecretStore(), clock=_clock(),
    )
    res = asyncio.run(app.chat("nhớ giúp anh thích báo cáo ngắn"))
    assert res.status == "done"
    assert not (tmp_path / "ws" / "MEMORY.md").exists()
    pending = app.memory_gate.list_pending()
    assert len(pending) == 1
    assert "báo cáo ngắn" in pending[0][1]
    # approve → vào MEMORY.md; chat turn sau nạp được
    assert app.memory_gate.approve(pending[0][0])
    assert "báo cáo ngắn" in (tmp_path / "ws" / "MEMORY.md").read_text(encoding="utf-8")
    prompt = app.workspace.build_system_prompt("BASE", token_budget=2000)
    assert "báo cáo ngắn" in prompt
    assert "memory_propose" in app.capabilities_summary()
    app.close()


def test_app_write_file_memory_md_denied_by_immutable(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir(parents=True)
    cfg_file = tmp_path / "harness.yaml"
    cfg_file.write_text("provider: {}\n", encoding="utf-8")
    sec = SecurityCfg(allowlist=[ToolRule(tool="write_file", effect="allow")])
    app = App(
        provider=FakeProvider([text_result("ok")]),
        cfg=_cfg(tmp_path, security=sec), state_dir=tmp_path / "st",
        secrets=InMemorySecretStore(), clock=_clock(), config_path=cfg_file,
    )
    try:
        dec = app.gate.evaluate(
            "write_file",
            {"path": str(tmp_path / "ws" / "MEMORY.md"), "content": "x"},
            type("C", (), {"session_key": "main"})(),
        )
        assert dec.verdict == "deny" and dec.rule_id == "IMMUTABLE_WRITE"
    finally:
        app.close()


@pytest.mark.parametrize(
    "control_path",
    [
        "memory/pending/forged.md",
        "memory/review-audit.jsonl",
        ".yett-memory-review.lock",
    ],
)
def test_app_exec_cannot_direct_write_memory_control_state(
    tmp_path: Path, control_path: str
) -> None:
    (tmp_path / "ws").mkdir(parents=True)
    sec = SecurityCfg(
        allowlist=[ToolRule(tool="exec", arg_patterns={"cmd": ".*"}, effect="allow")]
    )
    app = App(
        provider=FakeProvider([text_result("ok")]),
        cfg=_cfg(tmp_path, security=sec),
        state_dir=tmp_path / "st",
        secrets=InMemorySecretStore(),
        clock=_clock(),
    )
    try:
        command = f"python -c \"open('{control_path}','w').write('forged')\""
        dec = app.gate.evaluate(
            "exec",
            {"cmd": command},
            type("C", (), {"session_key": "main"})(),
        )
        assert dec.verdict == "deny" and dec.rule_id == "IMMUTABLE_WRITE"
    finally:
        app.close()


def test_cli_memory_list_approve_reject(tmp_path: Path, capsys) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    from yett.memory.review_gate import MemoryReviewGate

    gate = MemoryReviewGate(ws)
    pid = gate.propose("nội dung chờ duyệt qua CLI")

    assert main(["memory", "list", "--workspace", str(ws)]) == 0
    out = capsys.readouterr().out
    assert pid in out and "chờ duyệt" in out

    assert main(["memory", "reject", pid, "--workspace", str(ws)]) == 0
    assert not (ws / "MEMORY.md").exists()
    assert gate.list_pending() == []

    pid2 = gate.propose("duyệt qua CLI")
    assert main(["memory", "approve", pid2, "--workspace", str(ws)]) == 0
    assert "duyệt qua CLI" in (ws / "MEMORY.md").read_text(encoding="utf-8")
    assert gate.list_pending() == []


def test_cli_memory_approve_unknown_fail_closed(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    assert main(["memory", "approve", "deadbeef", "--workspace", str(ws)]) == 1


def test_cli_memory_malformed_config_fails_without_traceback(tmp_path: Path, capsys) -> None:
    config = tmp_path / "bad.yaml"
    config.write_text("provider: [not-a-provider]\n", encoding="utf-8")
    assert main(["memory", "list", "--config", str(config)]) == 1
    assert "không đọc được" in capsys.readouterr().err


def test_cli_memory_list_sanitizes_terminal_controls(tmp_path: Path, capsys) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    from yett.memory.review_gate import MemoryReviewGate

    pid = MemoryReviewGate(ws).propose("safe\x1b[31mred")
    assert main(["memory", "list", "--workspace", str(ws)]) == 0
    output = capsys.readouterr().out
    assert pid in output
    assert "\x1b" not in output
