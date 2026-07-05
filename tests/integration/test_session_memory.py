"""Integration: nhớ hội thoại xuyên turn theo session_key + độc lập provider."""

from __future__ import annotations

import asyncio
import itertools
from pathlib import Path

from yett.app import App
from yett.config.models import BudgetCfg, HarnessCfg, ProviderCfg, SandboxCfg
from yett.provider.fake import FakeProvider, text_result
from yett.secrets.backends import InMemorySecretStore


def _clock():
    c = itertools.count(1)
    return lambda: float(next(c))


def _cfg(tmp_path: Path) -> HarnessCfg:
    return HarnessCfg(
        provider=ProviderCfg(name="fake", model="fake-1"),
        workspace_root=tmp_path / "ws",
        sandbox=SandboxCfg(backend="local"),
        budget=BudgetCfg(max_loop_iterations=4),
    )


def test_history_carried_across_turns(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir(parents=True)
    provider = FakeProvider([text_result("Ừ, tên anh là Nam."), text_result("Anh tên Nam.")])
    app = App(provider=provider, cfg=_cfg(tmp_path), state_dir=tmp_path / "st",
              secrets=InMemorySecretStore(), clock=_clock())

    r1 = asyncio.run(app.chat("Tên tôi là Nam", session_key="s1"))
    assert r1.status == "done"
    r2 = asyncio.run(app.chat("Tôi tên gì?", session_key="s1"))
    assert r2.status == "done"

    # Ở lần gọi provider của turn 2, context phải chứa lại lượt turn 1.
    msgs_turn2 = provider.calls[-1]
    contents = [m.content for m in msgs_turn2]
    assert "Tên tôi là Nam" in contents  # user turn 1
    assert "Ừ, tên anh là Nam." in contents  # assistant turn 1
    assert "Tôi tên gì?" in contents  # user turn 2
    app.close()


def test_history_isolated_per_session(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir(parents=True)
    provider = FakeProvider([text_result("ok1"), text_result("ok2")])
    app = App(provider=provider, cfg=_cfg(tmp_path), state_dir=tmp_path / "st",
              secrets=InMemorySecretStore(), clock=_clock())

    asyncio.run(app.chat("bí mật session A", session_key="A"))
    asyncio.run(app.chat("xin chào", session_key="B"))

    # Session B KHÔNG thấy lịch sử session A.
    contents = [m.content for m in provider.calls[-1]]
    assert "bí mật session A" not in contents
    app.close()


def test_history_survives_provider_switch(tmp_path: Path) -> None:
    """Đổi provider (dựng App mới, provider khác) nhưng cùng state_dir → vẫn nhớ."""
    (tmp_path / "ws").mkdir(parents=True)
    state = tmp_path / "st"

    p1 = FakeProvider([text_result("Ghi nhận: dự án X đang deploy UAT.")])
    app1 = App(provider=p1, cfg=_cfg(tmp_path), state_dir=state,
               secrets=InMemorySecretStore(), clock=_clock())
    asyncio.run(app1.chat("Dự án X đang deploy UAT", session_key="prj"))
    app1.close()

    # Provider khác, App mới, cùng state_dir (mô phỏng đổi GLM→MiniMax).
    p2 = FakeProvider([text_result("Dự án X đang deploy UAT.")], model="other-1")
    app2 = App(provider=p2, cfg=_cfg(tmp_path), state_dir=state,
               secrets=InMemorySecretStore(), clock=_clock())
    asyncio.run(app2.chat("Dự án X đang làm gì?", session_key="prj"))

    contents = [m.content for m in p2.calls[-1]]
    assert "Dự án X đang deploy UAT" in contents  # lịch sử còn nguyên qua provider mới
    app2.close()
