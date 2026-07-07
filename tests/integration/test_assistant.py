"""Integration: lớp trợ lý — agent tạo task qua tool + briefing đọc state thật."""

from __future__ import annotations

import asyncio
import itertools
from pathlib import Path

from yett.app import App
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


def test_agent_creates_task_via_tool(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir(parents=True)
    sec = SecurityCfg(allowlist=[ToolRule(tool="task_add", effect="allow")])
    provider = FakeProvider([
        tool_result("c1", "task_add", {"title": "deploy UAT", "priority": "high"}),
        text_result("Đã ghi việc deploy UAT (ưu tiên cao)."),
    ])
    app = App(provider=provider, cfg=_cfg(tmp_path, security=sec), state_dir=tmp_path / "st",
              secrets=InMemorySecretStore(), clock=_clock())
    res = asyncio.run(app.chat("nhắc tôi deploy UAT"))
    assert res.status == "done"
    tasks = app.tasks.list_tasks()
    assert len(tasks) == 1 and tasks[0].title == "deploy UAT" and tasks[0].priority == "high"
    app.close()


def test_briefing_reads_real_tasks(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir(parents=True)
    app = App(provider=FakeProvider([text_result("ok")]), cfg=_cfg(tmp_path),
              state_dir=tmp_path / "st", secrets=InMemorySecretStore(), clock=_clock())
    # briefing rỗng khi chưa có việc
    assert "chưa có việc" in app.briefing().lower()
    # có việc quá hạn → briefing nêu ra
    app.tasks.add("việc cũ", due="2000-01-01")
    app.tasks.add("việc thường")
    b = app.briefing()
    assert "việc chưa xong" in b.lower()
    assert "QUÁ HẠN" in b and "việc cũ" in b
    app.close()


def test_capabilities_mentions_tasks(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir(parents=True)
    app = App(provider=FakeProvider([text_result("ok")]), cfg=_cfg(tmp_path),
              state_dir=tmp_path / "st", secrets=InMemorySecretStore(), clock=_clock())
    cap = app.capabilities_summary()
    assert "task_add" in cap and "task_list" in cap
    assert "TRỢ LÝ CÁ NHÂN" in cap
    app.close()
