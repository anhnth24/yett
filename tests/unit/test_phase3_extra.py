"""Test retention, channel gating, analytics, RPC broker (P3 §3/§4/§6, P2 §8)."""

from __future__ import annotations

from pathlib import Path

from yett.analytics import insights
from yett.channels.gating import ChannelGate
from yett.compliance.retention import purge_old_spans
from yett.obs.spanstore import SpanStore
from yett.obs.tracer import SpanKind
from yett.rpc.broker import RpcCaps, RpcSession, strip_secret_env
from yett.tools.base import ToolResult


# --- retention ---
def test_retention_purges_old(tmp_path: Path) -> None:
    store = SpanStore(tmp_path / "traces.db")
    old = store.start_span(SpanKind.AGENT, "old", start_ts=0.0)
    store.end_span(old, end_ts=1.0)
    new = store.start_span(SpanKind.AGENT, "new", start_ts=1_000_000.0)
    store.end_span(new, end_ts=1_000_001.0)
    store.flush(); store.close()
    audit = []
    n = purge_old_spans(tmp_path / "traces.db", now=1_000_100.0, keep_days=1,
                        auditor=lambda **k: audit.append(k))
    assert n == 1  # span cũ bị xóa
    assert audit[0]["kind"] == "retention"


# --- channel gating ---
def test_channel_pairing_flow() -> None:
    gate = ChannelGate()
    assert not gate.is_allowed("123")
    gate.request_pairing("123", "ABCD1234")
    assert not gate.is_allowed("123")  # chưa duyệt
    assert gate.approve_pairing("ABCD1234") == "123"
    assert gate.is_allowed("123")
    gate.revoke("123")
    assert not gate.is_allowed("123")


def test_channel_bad_code() -> None:
    gate = ChannelGate()
    assert gate.approve_pairing("WRONG") is None


# --- analytics ---
def test_budget_alert_levels() -> None:
    spans = [{"attrs": {"cost_usd": 8.5, "provider": "anthropic"}, "start_ts": 1.0}]
    r = insights.check_budget(spans, monthly_limit=10.0)
    assert r["alert"] == "warn" and r["pct"] == 85.0
    r2 = insights.check_budget([{"attrs": {"cost_usd": 12.0, "provider": "a"}, "start_ts": 1.0}], 10.0)
    assert r2["alert"] == "over"
    r3 = insights.check_budget(spans, None)
    assert r3["alert"] is None


# --- RPC broker ---
async def test_rpc_call_goes_through_handler() -> None:
    async def handler(tool, args):
        return ToolResult.success(f"{tool}:{args}")

    sess = RpcSession(handler)
    r = await sess.call("read_file", {"path": "x"})
    assert "read_file" in r.content


async def test_rpc_blocks_recursive_and_cap() -> None:
    async def handler(tool, args):
        return ToolResult.success("ok")

    sess = RpcSession(handler, caps=RpcCaps(max_calls=2))
    assert (await sess.call("execute_code", {})).is_error  # recursive cấm
    await sess.call("read_file", {})
    await sess.call("read_file", {})
    over = await sess.call("read_file", {})  # vượt cap
    assert over.is_error and "giới hạn" in over.content


async def test_rpc_denied_tool_recorded() -> None:
    async def handler(tool, args):
        return ToolResult.error("[DENIED] hardline")

    sess = RpcSession(handler)
    await sess.call("ssh_exec", {"cmd": "rm x"})
    assert "ssh_exec" in sess.denied_tools


def test_strip_secret_env() -> None:
    env = {"PATH": "/usr/bin", "YETT_SECRET_X": "abc", "API_TOKEN": "t", "PYTHONPATH": "/x", "HOME": "/h"}
    clean = strip_secret_env(env)
    assert "PATH" in clean and "HOME" in clean
    assert "YETT_SECRET_X" not in clean and "API_TOKEN" not in clean and "PYTHONPATH" not in clean


def test_rpc_stub_generation() -> None:
    from yett.provider.base import ToolSchema
    from yett.rpc.stubgen import generate_stub

    stub = generate_stub([ToolSchema("read_file", "đọc file", {"properties": {"path": {}}})])
    assert "def read_file(path):" in stub
    assert "_rpc('read_file'" in stub
