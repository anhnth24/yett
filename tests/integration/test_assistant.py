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


def test_agent_notify_via_tool(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir(parents=True)
    sec = SecurityCfg(allowlist=[ToolRule(tool="notify", effect="allow")])
    provider = FakeProvider([
        tool_result("c1", "notify", {"text": "Deploy UAT xong."}),
        text_result("Đã báo anh qua Telegram."),
    ])
    app = App(provider=provider, cfg=_cfg(tmp_path, security=sec), state_dir=tmp_path / "st",
              secrets=InMemorySecretStore(), clock=_clock())
    sent: list[str] = []
    app.set_notifier(lambda text: (sent.append(text), 1)[1])  # giả kênh: đếm 1
    res = asyncio.run(app.chat("deploy xong báo tôi"))
    assert res.status == "done"
    assert sent == ["Deploy UAT xong."]
    app.close()


def test_notify_without_channel_errors_gracefully(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir(parents=True)
    sec = SecurityCfg(allowlist=[ToolRule(tool="notify", effect="allow")])
    provider = FakeProvider([
        tool_result("c1", "notify", {"text": "hi"}),
        text_result("Kênh chưa bật."),
    ])
    app = App(provider=provider, cfg=_cfg(tmp_path, security=sec), state_dir=tmp_path / "st",
              secrets=InMemorySecretStore(), clock=_clock())
    res = asyncio.run(app.chat("báo tôi"))  # notifier chưa set → tool trả lỗi, turn vẫn done
    assert res.status == "done"
    spans = app.spanstore.get_trace(res.trace_id)
    assert any(s["name"] == "notify" for s in spans)
    app.close()


def test_telegram_dispatches_to_real_app(tmp_path: Path) -> None:
    """Tin Telegram từ chat đã ghép → App.chat thật chạy → reply gửi lại (default run_chat)."""
    import threading

    from yett.channels.gating import ChannelGate
    from yett.channels.telegram import TelegramChannel, TelegramClient

    (tmp_path / "ws").mkdir(parents=True)
    app = App(provider=FakeProvider([text_result("Chào anh, deploy đang chạy.")]),
              cfg=_cfg(tmp_path), state_dir=tmp_path / "st",
              secrets=InMemorySecretStore(), clock=_clock())

    sent = []

    def transport(url, params):
        method = url.rsplit("/", 1)[-1]
        if method == "getUpdates":
            return {"ok": True, "result": [
                {"update_id": 1, "message": {"chat": {"id": 555}, "text": "deploy tới đâu rồi?"}}
            ]}
        sent.append(params)
        return {"ok": True}

    ch = TelegramChannel(
        TelegramClient("TOK", transport=transport),
        ChannelGate(allowed_chat_ids={"555"}),
        get_app=lambda: app, chat_lock=threading.Lock(),
    )
    ch.poll_once()  # dùng _default_run_chat → app.chat thật
    assert sent and sent[-1]["chat_id"] == "555"
    assert "deploy đang chạy" in sent[-1]["text"]
    app.close()


def test_zalo_dispatches_to_real_app(tmp_path: Path) -> None:
    """Tin Zalo Official Bot từ chat đã ghép → App.chat thật → reply sendMessage (offline)."""
    import threading

    from yett.channels.gating import ChannelGate
    from yett.channels.zalo_bot import ZaloChannel, ZaloClient
    from yett.config.models import ChannelsCfg, ZaloCfg

    (tmp_path / "ws").mkdir(parents=True)
    cfg = _cfg(
        tmp_path,
        channels=ChannelsCfg(zalo=ZaloCfg(enabled=True, allowed_chat_ids=["3becaa50ae12474c1e03"])),
    )
    app = App(
        provider=FakeProvider([text_result("Zalo: deploy ổn.")]),
        cfg=cfg,
        state_dir=tmp_path / "st",
        secrets=InMemorySecretStore(),
        clock=_clock(),
    )
    assert "Zalo Bot API" in app.capabilities_summary()

    sent: list[dict] = []

    def transport(url: str, params: dict, *, timeout_sec: float = 60.0) -> dict:
        method = url.rsplit("/", 1)[-1]
        if method == "getUpdates":
            return {
                "ok": True,
                "result": {
                    "event_name": "message.text.received",
                    "message": {
                        "message_id": "mid-1",
                        "from": {"id": "3becaa50ae12474c1e03"},
                        "chat": {"id": "3becaa50ae12474c1e03", "chat_type": "PRIVATE"},
                        "date": 1,
                        "text": "deploy tới đâu rồi?",
                    },
                },
            }
        sent.append(params)
        return {"ok": True, "result": {}}

    ch = ZaloChannel(
        ZaloClient("TOK", transport=transport, max_retries=0),
        ChannelGate(allowed_chat_ids={"3becaa50ae12474c1e03"}),
        get_app=lambda: app,
        chat_lock=threading.Lock(),
    )
    ch.poll_once()
    assert sent and sent[-1]["chat_id"] == "3becaa50ae12474c1e03"
    assert "deploy ổn" in sent[-1]["text"]
    app.close()


def test_zalo_unauthorized_does_not_reach_app(tmp_path: Path) -> None:
    """Sender ngoài allowlist → không gọi App.chat; chỉ trả hướng dẫn ghép."""
    import threading

    from yett.channels.gating import ChannelGate
    from yett.channels.zalo_bot import ZaloChannel, ZaloClient

    (tmp_path / "ws").mkdir(parents=True)
    # Provider sẽ fail nếu bị gọi — FakeProvider hết script thì lỗi.
    app = App(
        provider=FakeProvider([]),
        cfg=_cfg(tmp_path),
        state_dir=tmp_path / "st",
        secrets=InMemorySecretStore(),
        clock=_clock(),
    )
    sent: list[dict] = []

    def transport(url: str, params: dict, *, timeout_sec: float = 60.0) -> dict:
        if url.rsplit("/", 1)[-1] == "sendMessage":
            sent.append(params)
        return {"ok": True, "result": {}}

    ch = ZaloChannel(
        ZaloClient("TOK", transport=transport, max_retries=0),
        ChannelGate(allowed_chat_ids=set()),
        get_app=lambda: app,
        chat_lock=threading.Lock(),
        pairing_code="PAIR-ME",
        # default run_chat would hit empty FakeProvider — must not be called
    )
    ch.handle_update(
        {
            "event_name": "message.text.received",
            "message": {
                "message_id": "m-u",
                    "from": {"id": "stranger", "is_bot": False},
                "chat": {"id": "stranger", "chat_type": "PRIVATE"},
                "text": "deploy giúp",
            },
        }
    )
    assert sent and "Chưa được cấp quyền" in sent[-1]["text"]
    app.close()
