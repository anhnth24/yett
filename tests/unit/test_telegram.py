"""Test kênh Telegram offline (transport inject) — gating, pairing, dispatch, notify, briefing."""

from __future__ import annotations

import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from yett.channels.gating import ChannelGate
from yett.channels.telegram import (
    TelegramChannel,
    TelegramClient,
    briefing_due,
)


class _FakeTransport:
    """Ghi lại call + trả response lập trình sẵn theo method."""

    def __init__(self, updates: list[dict] | None = None) -> None:
        self.sent: list[dict] = []
        self._updates = updates or []
        self.calls: list[str] = []

    def __call__(self, url: str, params: dict) -> dict:
        method = url.rsplit("/", 1)[-1]
        self.calls.append(method)
        if method == "getUpdates":
            u, self._updates = self._updates, []  # trả 1 lần rồi hết
            return {"ok": True, "result": u}
        if method == "sendMessage":
            self.sent.append(params)
            return {"ok": True}
        return {"ok": True}


def _msg(update_id: int, chat_id: int, text: str) -> dict:
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


def _chan(transport, *, allowed=None, pairing="", run_chat=None) -> TelegramChannel:
    client = TelegramClient("TOKEN", transport=transport)
    gate = ChannelGate(allowed_chat_ids=set(allowed or []))
    return TelegramChannel(client, gate, get_app=lambda: None, chat_lock=threading.Lock(),
                           pairing_code=pairing, run_chat=run_chat or (lambda t, c: f"echo:{t}"))


def test_client_get_and_send() -> None:
    tr = _FakeTransport(updates=[_msg(1, 99, "hi")])
    c = TelegramClient("TOKEN", transport=tr)
    assert c.get_updates(0)[0]["update_id"] == 1
    c.send_message("99", "xin chào")
    assert tr.sent[-1] == {"chat_id": "99", "text": "xin chào"}


def test_allowed_chat_dispatches_and_replies() -> None:
    tr = _FakeTransport(updates=[_msg(5, 42, "trạng thái deploy?")])
    ch = _chan(tr, allowed=["42"], run_chat=lambda t, c: f"trả lời: {t}")
    n = ch.poll_once()
    assert n == 1
    assert tr.sent[-1]["chat_id"] == "42"
    assert "trả lời: trạng thái deploy?" in tr.sent[-1]["text"]


def test_unauthorized_without_code_denied() -> None:
    tr = _FakeTransport(updates=[_msg(1, 7, "làm giúp tôi")])
    ch = _chan(tr, allowed=[], pairing="OPEN-SESAME")
    ch.poll_once()
    assert "Chưa được cấp quyền" in tr.sent[-1]["text"]


def test_pairing_code_adds_chat() -> None:
    tr = _FakeTransport(updates=[_msg(1, 7, "OPEN-SESAME")])
    ch = _chan(tr, allowed=[], pairing="OPEN-SESAME")
    ch.poll_once()
    assert "Đã ghép" in tr.sent[-1]["text"]
    # lần sau đã được phép
    tr2 = _FakeTransport(updates=[_msg(2, 7, "việc gì?")])
    ch._client._transport = tr2  # type: ignore[attr-defined]
    ch.poll_once()
    assert "echo:việc gì?" in tr2.sent[-1]["text"]


def test_offset_advances() -> None:
    tr = _FakeTransport(updates=[_msg(10, 42, "a"), _msg(11, 42, "b")])
    ch = _chan(tr, allowed=["42"])
    ch.poll_once()
    assert ch._offset == 12  # max update_id + 1


def test_run_chat_error_isolated() -> None:
    def boom(t, c):
        raise RuntimeError("turn lỗi")
    tr = _FakeTransport(updates=[_msg(1, 42, "x")])
    ch = _chan(tr, allowed=["42"], run_chat=boom)
    ch.poll_once()  # không raise ra ngoài
    assert "[lỗi xử lý]" in tr.sent[-1]["text"]


def test_notify_sends_to_all_paired() -> None:
    tr = _FakeTransport()
    ch = _chan(tr, allowed=["1", "2", "3"])
    n = ch.notify("briefing sáng")
    assert n == 3
    assert {s["chat_id"] for s in tr.sent} == {"1", "2", "3"}
    assert all(s["text"] == "briefing sáng" for s in tr.sent)


def test_briefing_due_logic() -> None:
    tz = ZoneInfo("Asia/Ho_Chi_Minh")
    at8 = datetime(2026, 7, 7, 8, 30, tzinfo=tz)
    at7 = datetime(2026, 7, 7, 7, 0, tzinfo=tz)
    assert briefing_due(at8, 8, None) is True            # tới giờ, chưa gửi
    assert briefing_due(at8, 8, "2026-07-07") is False   # đã gửi hôm nay
    assert briefing_due(at7, 8, None) is False           # chưa tới giờ
    assert briefing_due(at8, 8, "2026-07-06") is True    # hôm qua đã gửi, hôm nay chưa
