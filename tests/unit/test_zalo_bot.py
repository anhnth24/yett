"""Offline contract tests — Zalo Official Bot API channel (transport inject, no egress)."""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest
from pydantic import ValidationError

from yett.channels.gating import ChannelGate
from yett.channels.zalo_bot import (
    WEBHOOK_SECRET_HEADER,
    ZALO_TEXT_LIMIT,
    ZaloApiError,
    ZaloChannel,
    ZaloClient,
    chunk_outbound_text,
    normalize_inbound_text,
    validate_zalo_runtime_cfg,
)
from yett.config.models import ChannelsCfg, HarnessCfg, ProviderCfg, ZaloCfg


class _FakeTransport:
    """Ghi call + trả response lập trình sẵn theo method. Không đụng mạng."""

    def __init__(
        self,
        *,
        updates: list[Any] | None = None,
        send_error: Exception | None = None,
        get_error: Exception | None = None,
        get_sequence: list[Any] | None = None,
    ) -> None:
        self.sent: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.urls: list[str] = []
        self._updates = list(updates or [])
        self._get_sequence = list(get_sequence) if get_sequence is not None else None
        self._send_error = send_error
        self._get_error = get_error
        self.timeouts: list[float] = []

    def __call__(self, url: str, params: dict, *, timeout_sec: float = 60.0) -> dict:
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, dict(params or {})))
        self.urls.append(url)
        self.timeouts.append(timeout_sec)
        if method == "getUpdates":
            if self._get_error is not None:
                raise self._get_error
            if self._get_sequence is not None:
                if not self._get_sequence:
                    return {"ok": True, "result": None}
                item = self._get_sequence.pop(0)
                if isinstance(item, Exception):
                    raise item
                return {"ok": True, "result": item}
            if not self._updates:
                return {"ok": True, "result": None}
            u = self._updates.pop(0)
            return {"ok": True, "result": u}
        if method == "sendMessage":
            if self._send_error is not None:
                raise self._send_error
            self.sent.append(dict(params))
            return {"ok": True, "result": {"message_id": "out-1"}}
        if method in ("deleteWebhook", "setWebhook", "getMe", "getWebhookInfo"):
            return {"ok": True, "result": {}}
        return {"ok": True, "result": {}}


def _upd(
    message_id: str,
    chat_id: str,
    text: str,
    *,
    event: str = "message.text.received",
) -> dict:
    return {
        "event_name": event,
        "message": {
            "message_id": message_id,
            "from": {"id": chat_id, "display_name": "User"},
            "chat": {"id": chat_id, "chat_type": "PRIVATE"},
            "date": 1749632637199,
            "text": text,
        },
    }


def _chan(
    transport: _FakeTransport,
    *,
    allowed: list[str] | None = None,
    pairing: str = "",
    run_chat=None,
    webhook_secret: str = "",
    poll_timeout: int = 1,
    seen_cap: int = 64,
) -> ZaloChannel:
    client = ZaloClient("TOK-SECRET-VALUE", transport=transport, max_retries=0, http_timeout_sec=5.0)
    gate = ChannelGate(allowed_chat_ids=set(allowed or []))
    return ZaloChannel(
        client,
        gate,
        get_app=lambda: None,
        chat_lock=threading.Lock(),
        pairing_code=pairing,
        run_chat=run_chat or (lambda t, c: f"echo:{t}"),
        poll_timeout=poll_timeout,
        webhook_secret=webhook_secret,
        seen_cap=seen_cap,
    )


def test_client_url_and_get_updates_timeout_string() -> None:
    tr = _FakeTransport(updates=[_upd("m1", "abc.xyz", "hi")])
    c = ZaloClient("TOK", transport=tr, max_retries=0)
    upd = c.get_updates(timeout=30)
    assert upd is not None and upd["message"]["text"] == "hi"
    method, params = tr.calls[0]
    assert method == "getUpdates"
    assert params == {"timeout": "30"}  # official: timeout as string
    assert "bot-api.zaloplatforms.com/botTOK/getUpdates" in tr.urls[0]


def test_client_send_message_shape() -> None:
    tr = _FakeTransport()
    c = ZaloClient("TOK", transport=tr, max_retries=0)
    c.send_message("3becaa50ae12474c1e03", "xin chào")
    assert tr.sent[-1] == {"chat_id": "3becaa50ae12474c1e03", "text": "xin chào"}


def test_authorized_sender_dispatches_and_replies() -> None:
    tr = _FakeTransport(updates=[_upd("m5", "chat-42", "trạng thái deploy?")])
    ch = _chan(tr, allowed=["chat-42"], run_chat=lambda t, c: f"trả lời: {t}")
    n = ch.poll_once()
    assert n == 1
    assert tr.sent[-1]["chat_id"] == "chat-42"
    assert "trả lời: trạng thái deploy?" in tr.sent[-1]["text"]
    assert ch.processed_cursor == 1


def test_unauthorized_without_code_denied() -> None:
    tr = _FakeTransport(updates=[_upd("m1", "stranger", "làm giúp tôi")])
    ch = _chan(tr, allowed=[], pairing="OPEN-SESAME")
    ch.poll_once()
    assert "Chưa được cấp quyền" in tr.sent[-1]["text"]


def test_pairing_code_adds_chat() -> None:
    tr = _FakeTransport(updates=[_upd("m1", "stranger", "OPEN-SESAME")])
    ch = _chan(tr, allowed=[], pairing="OPEN-SESAME")
    ch.poll_once()
    assert "Đã ghép" in tr.sent[-1]["text"]
    assert ch._gate.is_allowed("stranger")
    tr2 = _FakeTransport(updates=[_upd("m2", "stranger", "việc gì?")])
    ch._client._transport = tr2  # type: ignore[attr-defined]
    ch.poll_once()
    assert "echo:việc gì?" in tr2.sent[-1]["text"]


def test_malformed_updates_consumed_without_send() -> None:
    # Client extract_update drops payloads thiếu event_name → get_updates = None (0).
    tr = _FakeTransport(get_sequence=[{"no_event": True}])
    ch = _chan(tr, allowed=["x"])
    assert ch.poll_once() == 0
    # event có event_name nhưng không text / chat_id → consume, không gửi.
    tr2 = _FakeTransport(
        get_sequence=[
            {"event_name": "message.sticker.received", "message": {"chat": {"id": "x"}}},
            {"event_name": "message.text.received", "message": {"chat": {"id": ""}, "text": "hi"}},
            None,
        ]
    )
    ch2 = _chan(tr2, allowed=["x"])
    assert ch2.poll_once() == 1  # sticker
    assert ch2.poll_once() == 1  # empty chat_id
    assert ch2.poll_once() == 0  # None result
    assert tr2.sent == []
    assert ch2.handle_update({"garbage": True}) is False


def test_cursor_advances_and_duplicates_skipped() -> None:
    u = _upd("mid-9", "chat-42", "a")
    tr = _FakeTransport(get_sequence=[u, u, _upd("mid-10", "chat-42", "b")])
    ch = _chan(tr, allowed=["chat-42"])
    assert ch.poll_once() == 1
    assert ch.processed_cursor == 1
    assert ch.poll_once() == 1  # duplicate message_id — no second dispatch
    assert ch.processed_cursor == 1
    assert ch.poll_once() == 1
    assert ch.processed_cursor == 2
    texts = [s["text"] for s in tr.sent]
    assert texts == ["echo:a", "echo:b"]


def test_seen_cap_bounds_memory() -> None:
    seq = [_upd(f"m{i}", "chat-42", f"t{i}") for i in range(5)]
    tr = _FakeTransport(get_sequence=seq)
    ch = _chan(tr, allowed=["chat-42"], seen_cap=2)
    for _ in range(5):
        ch.poll_once()
    assert len(ch._seen) <= 2


def test_run_chat_error_isolated() -> None:
    def boom(t: str, c: str) -> str:
        raise RuntimeError("turn lỗi")

    tr = _FakeTransport(updates=[_upd("m1", "chat-42", "x")])
    ch = _chan(tr, allowed=["chat-42"], run_chat=boom)
    ch.poll_once()
    assert "[lỗi xử lý]" in tr.sent[-1]["text"]


def test_notify_and_outbound_chunking() -> None:
    tr = _FakeTransport()
    ch = _chan(tr, allowed=["1", "2"])
    long = "x" * (ZALO_TEXT_LIMIT + 50)
    n = ch.notify(long)
    assert n == 2
    # mỗi chat bị chia ≥2 chunk
    assert len(tr.sent) >= 4
    assert all(len(s["text"]) <= ZALO_TEXT_LIMIT for s in tr.sent)


def test_normalize_and_chunk_helpers() -> None:
    assert normalize_inbound_text("  hi\x00  ") == "hi"
    assert len(normalize_inbound_text("a" * 5000)) == ZALO_TEXT_LIMIT
    parts = chunk_outbound_text("a" * 4500)
    assert len(parts) == 3 and all(len(p) <= ZALO_TEXT_LIMIT for p in parts)


def test_api_error_retryable_then_success() -> None:
    sleeps: list[float] = []
    calls = {"n": 0}

    def flaky(url: str, params: dict, *, timeout_sec: float = 60.0) -> dict:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ZaloApiError("upstream", error_code=503, retryable=True)
        return {"ok": True, "result": {"message_id": "ok"}}

    c = ZaloClient("TOK", transport=flaky, max_retries=2, sleep=sleeps.append)
    r = c.send_message("c", "hi")
    assert r["ok"] is True
    assert sleeps  # backoff occurred


def test_api_error_ok_false_and_polling_timeout() -> None:
    def bad_ok(url: str, params: dict, *, timeout_sec: float = 60.0) -> dict:
        return {"ok": False, "error_code": 400, "description": "bad request TOK-LEAK"}

    c = ZaloClient("TOK-LEAK", transport=bad_ok, max_retries=0)
    with pytest.raises(ZaloApiError) as ei:
        c.send_message("c", "hi")
    assert "TOK-LEAK" not in str(ei.value)
    assert "[REDACTED_ZALO_TOKEN]" in str(ei.value)

    def timeout_poll(url: str, params: dict, *, timeout_sec: float = 60.0) -> dict:
        raise ZaloApiError("idle", error_code=408, retryable=True)

    c2 = ZaloClient("TOK", transport=timeout_poll, max_retries=0)
    assert c2.get_updates() is None  # 408 → empty, not raised


def test_secret_redaction_in_transport_exception() -> None:
    def boom(url: str, params: dict, *, timeout_sec: float = 60.0) -> dict:
        raise RuntimeError(f"conn failed for token=SECRETTOKEN99 in {url}")

    c = ZaloClient("SECRETTOKEN99", transport=boom, max_retries=0)
    with pytest.raises(ZaloApiError) as ei:
        c.send_message("c", "hi")
    assert "SECRETTOKEN99" not in str(ei.value)


def test_webhook_authorized_and_unauthorized() -> None:
    tr = _FakeTransport()
    ch = _chan(tr, allowed=["chat-42"], webhook_secret="supersecret")
    body = json.dumps(_upd("w1", "chat-42", "hello webhook")).encode()
    bad = ch.handle_webhook({"X-Bot-Api-Secret-Token": "wrong"}, body)
    assert bad.status == 401 and tr.sent == []
    ok = ch.handle_webhook({WEBHOOK_SECRET_HEADER: "supersecret"}, body)
    assert ok.status == 200 and ok.ok
    assert tr.sent[-1]["text"] == "echo:hello webhook"


def test_webhook_malformed_and_oversized() -> None:
    tr = _FakeTransport()
    ch = _chan(tr, allowed=["c"], webhook_secret="supersecret")
    r = ch.handle_webhook({WEBHOOK_SECRET_HEADER: "supersecret"}, b"{not-json")
    assert r.status == 400
    huge = b"x" * (256 * 1024 + 10)
    ch2 = ZaloChannel(
        ZaloClient("TOK", transport=tr, max_retries=0),
        ChannelGate(),
        get_app=lambda: None,
        chat_lock=threading.Lock(),
        webhook_secret="supersecret",
        webhook_max_bytes=1024,
    )
    r2 = ch2.handle_webhook({WEBHOOK_SECRET_HEADER: "supersecret"}, huge)
    assert r2.status == 413


def test_shutdown_stops_poll_and_rejects_webhook() -> None:
    tr = _FakeTransport(get_sequence=[_upd("m1", "chat-42", "hi")])
    stop = threading.Event()
    ch = _chan(tr, allowed=["chat-42"], webhook_secret="supersecret")
    ch.stop()
    stop.set()
    ch.run(stop)  # must return promptly
    r = ch.handle_webhook({WEBHOOK_SECRET_HEADER: "supersecret"}, json.dumps(_upd("m2", "chat-42", "x")).encode())
    assert r.status == 503


def test_safe_send_swallows_api_errors() -> None:
    tr = _FakeTransport(
        updates=[_upd("m1", "chat-42", "hi")],
        send_error=ZaloApiError("down", error_code=500, retryable=True),
    )
    ch = _chan(tr, allowed=["chat-42"])
    ch.poll_once()  # must not raise


def test_fail_closed_config_webhook() -> None:
    assert validate_zalo_runtime_cfg(
        enabled=True, mode="webhook", webhook_url="http://insecure", webhook_secret="abcdefgh"
    )
    assert validate_zalo_runtime_cfg(
        enabled=True, mode="webhook", webhook_url="https://ok.example/hook", webhook_secret="short"
    )
    assert (
        validate_zalo_runtime_cfg(
            enabled=True, mode="webhook",
            webhook_url="https://ok.example/hook", webhook_secret="abcdefgh",
        )
        is None
    )
    with pytest.raises(ValidationError):
        ZaloCfg(enabled=True, mode="webhook", webhook_url="https://x", webhook_secret="short")
    with pytest.raises(ValidationError):
        ZaloCfg(enabled=True, mode="webhook", webhook_url="http://x", webhook_secret="abcdefgh")
    # disabled → no webhook constraints
    ZaloCfg(enabled=False, mode="webhook", webhook_url="", webhook_secret="")


def test_channels_cfg_default_zalo_disabled() -> None:
    cfg = HarnessCfg(
        provider=ProviderCfg(name="fake", model="m"),
        workspace_root=".",
        channels=ChannelsCfg(),
    )
    assert cfg.channels.zalo.enabled is False
    assert cfg.channels.zalo.token_secret == "zalo_bot_token"
