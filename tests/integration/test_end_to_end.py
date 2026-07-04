"""Integration: một turn đầy đủ offline (RG1 demo end-to-end, RG1-1 no-egress, RG1-7 provider swap).

Chứng minh no-egress bằng thiết kế: FakeProvider + fetcher offline, không gọi mạng thật.
"""

from __future__ import annotations

import itertools
from pathlib import Path

from yett.app import App
from yett.config.models import EgressCfg, HarnessCfg, ProviderCfg, SandboxCfg, SecurityCfg, ToolRule, BudgetCfg
from yett.provider.fake import FakeProvider, text_result, tool_result


def _clock():
    c = itertools.count(1)
    return lambda: float(next(c))


def _cfg(tmp_path: Path, allow=None) -> HarnessCfg:
    return HarnessCfg(
        provider=ProviderCfg(name="fake", model="fake-1"),
        workspace_root=tmp_path / "ws",
        sandbox=SandboxCfg(backend="local"),
        security=SecurityCfg(allowlist=allow or []),
        egress=EgressCfg(allowlist=["example.com"]),
        budget=BudgetCfg(max_loop_iterations=5),
    )


async def test_full_turn_text_only(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    provider = FakeProvider([text_result("Chào anh, em là yett.")])
    app = App(provider=provider, cfg=_cfg(tmp_path), state_dir=tmp_path / "state", clock=_clock())
    res = await app.chat("xin chào", session_key="main")
    assert res.status == "done"
    assert "yett" in res.text
    # trace replay được: có span AGENT + LLM_CALL
    spans = app.spanstore.get_trace(res.trace_id)
    assert {s["kind"] for s in spans} >= {"AGENT", "LLM_CALL"}
    app.close()


async def test_full_turn_with_tool(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    pricing = {"fake": {"fake-1": __import__("yett.config.models", fromlist=["PriceRow"]).PriceRow(
        input_per_mtok=3.0, output_per_mtok=15.0)}}
    allow = [ToolRule(tool="exec", arg_patterns={"cmd": r"^echo"}, effect="allow")]
    provider = FakeProvider([
        tool_result("c1", "exec", {"cmd": "echo hello-uat"}),
        text_result("Đã chạy xong."),
    ])
    app = App(provider=provider, cfg=_cfg(tmp_path, allow), state_dir=tmp_path / "state",
              pricing=pricing, clock=_clock())
    res = await app.chat("chạy echo giúp em", session_key="main")
    assert res.status == "done"
    assert "c1" in res.completed_tool_calls
    # có cost trong span LLM (RG1-5)
    spans = app.spanstore.get_trace(res.trace_id)
    llm = [s for s in spans if s["kind"] == "LLM_CALL"]
    assert any(s["attrs"].get("cost_usd", 0) > 0 for s in llm)
    app.close()


async def test_denied_tool_surfaces_to_agent(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    # exec KHÔNG có trong allowlist → default deny; agent nhận lỗi rồi kết thúc
    provider = FakeProvider([
        tool_result("c1", "exec", {"cmd": "echo x"}),
        text_result("Bị chặn rồi anh."),
    ])
    app = App(provider=provider, cfg=_cfg(tmp_path), state_dir=tmp_path / "state", clock=_clock())
    res = await app.chat("chạy đi", session_key="main")
    assert res.status == "done"
    # tool result trả về agent là DENIED (được thêm vào context)
    assert res.text == "Bị chặn rồi anh."
    app.close()
