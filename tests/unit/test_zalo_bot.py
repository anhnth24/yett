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
    monotonic=None,
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
        monotonic=monotonic,
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


def test_pairing_rejects_groups_and_rate_limits_failures() -> None:
    now = [10.0]
    tr = _FakeTransport()
    ch = _chan(
        tr,
        pairing="OPEN-SESAME",
        monotonic=lambda: now[0],
    )
    group = _upd("g1", "group-1", "OPEN-SESAME")
    group["message"]["chat"]["chat_type"] = "GROUP"
    assert ch.handle_update(group)
    assert not ch._gate.is_allowed("group-1")

    for index in range(5):
        assert ch.handle_update(_upd(f"bad-{index}", "private-1", "wrong"))
    assert ch.handle_update(_upd("locked", "private-1", "OPEN-SESAME"))
    assert not ch._gate.is_allowed("private-1")
    now[0] += 301
    assert ch.handle_update(_upd("after-lock", "private-1", "OPEN-SESAME"))
    assert ch._gate.is_allowed("private-1")


def test_malformed_updates_consumed_without_send() -> None:
    # A non-empty malformed API result is surfaced, not silently treated as an empty poll.
    tr = _FakeTransport(get_sequence=[{"no_event": True}])
    ch = _chan(tr, allowed=["x"])
    with pytest.raises(ZaloApiError, match="invalid update"):
        ch.poll_once()
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


def test_same_message_id_in_different_chats_does_not_collide() -> None:
    tr = _FakeTransport()
    ch = _chan(tr, allowed=["a", "b"])
    assert ch.handle_update(_upd("same-id", "a", "one"))
    assert ch.handle_update(_upd("same-id", "b", "two"))
    assert [item["text"] for item in tr.sent] == ["echo:one", "echo:two"]


def test_concurrent_duplicate_is_claimed_once() -> None:
    tr = _FakeTransport()
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def run_chat(text: str, chat_id: str) -> str:
        calls.append(text)
        entered.set()
        assert release.wait(2)
        return "ok"

    ch = _chan(tr, allowed=["c"], run_chat=run_chat)
    update = _upd("race-id", "c", "hello")
    worker = threading.Thread(target=ch.handle_update, args=(update,))
    worker.start()
    assert entered.wait(2)
    assert ch.handle_update(update)
    release.set()
    worker.join(2)
    assert calls == ["hello"]
    assert ch.processed_cursor == 1


def test_actionable_update_requires_string_ids_and_message_id() -> None:
    tr = _FakeTransport()
    ch = _chan(tr, allowed=["c", "123"])
    missing_mid = _upd("m", "c", "one")
    del missing_mid["message"]["message_id"]
    numeric_chat = _upd("m2", "c", "two")
    numeric_chat["message"]["chat"]["id"] = 123
    object_text = _upd("m3", "c", "three")
    object_text["message"]["text"] = {"coerce": "me"}
    missing_sender = _upd("m4", "c", "four")
    del missing_sender["message"]["from"]
    for update in (missing_mid, numeric_chat, object_text, missing_sender):
        assert ch.handle_update(update)
    assert tr.sent == []


def test_bot_origin_is_ignored() -> None:
    tr = _FakeTransport()
    ch = _chan(tr, allowed=["c"])
    update = _upd("m1", "c", "loop")
    update["message"]["from"]["is_bot"] = True
    assert ch.handle_update(update)
    assert tr.sent == []


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
    assert ch.notify("") == 0


def test_normalize_and_chunk_helpers() -> None:
    assert normalize_inbound_text("  hi\x00  ") == "hi"
    assert len(normalize_inbound_text("a" * 5000)) == ZALO_TEXT_LIMIT
    parts = chunk_outbound_text("a" * 4500)
    assert len(parts) == 3 and all(len(p) <= ZALO_TEXT_LIMIT for p in parts)
    emoji_parts = chunk_outbound_text("😀" * 2000)
    assert [len(part) for part in emoji_parts] == [1000, 1000]


def test_api_error_retryable_then_success() -> None:
    sleeps: list[float] = []
    calls = {"n": 0}

    def flaky(url: str, params: dict, *, timeout_sec: float = 60.0) -> dict:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ZaloApiError("upstream", error_code=503, retryable=True)
        return {"ok": True, "result": _upd("m1", "c", "hi")}

    c = ZaloClient("TOK", transport=flaky, max_retries=2, sleep=sleeps.append)
    r = c.get_updates(timeout=1)
    assert r is not None and r["message"]["message_id"] == "m1"
    assert sleeps  # backoff occurred


def test_api_error_ok_false_and_polling_timeout() -> None:
    def bad_ok(url: str, params: dict, *, timeout_sec: float = 60.0) -> dict:
        return {"ok": False, "error_code": 400, "description": "bad request TOK-LEAK"}

    c = ZaloClient("TOK-LEAK", transport=bad_ok, max_retries=0)
    with pytest.raises(ZaloApiError) as ei:
        c.send_message("c", "hi")
    assert "TOK-LEAK" not in str(ei.value)
    assert "[REDACTED_ZALO_SECRET]" in str(ei.value)

    def timeout_poll(url: str, params: dict, *, timeout_sec: float = 60.0) -> dict:
        raise ZaloApiError("idle", error_code=408, retryable=True)

    c2 = ZaloClient("TOK", transport=timeout_poll, max_retries=0)
    assert c2.get_updates() is None  # 408 → empty, not raised


def test_send_retries_explicit_429_but_not_ambiguous_503() -> None:
    sleeps: list[float] = []
    calls = 0

    def rate_limited(url: str, params: dict, *, timeout_sec: float = 60.0) -> dict:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "ok": False,
                "error_code": 429,
                "description": "slow down",
                "parameters": {"retry_after": 1.25},
            }
        return {"ok": True, "result": {"message_id": "ok"}}

    client = ZaloClient("TOK", transport=rate_limited, max_retries=2, sleep=sleeps.append)
    client.send_message("c", "hello")
    assert calls == 2
    assert sleeps == [1.25]

    attempts = 0

    def ambiguous(url: str, params: dict, *, timeout_sec: float = 60.0) -> dict:
        nonlocal attempts
        attempts += 1
        raise ZaloApiError("upstream", error_code=503, retryable=True)

    no_duplicate_retry = ZaloClient(
        "TOK", transport=ambiguous, max_retries=3, sleep=sleeps.append
    )
    with pytest.raises(ZaloApiError):
        no_duplicate_retry.send_message("c", "hello")
    assert attempts == 1


def test_poll_http_timeout_includes_long_poll_grace() -> None:
    tr = _FakeTransport()
    client = ZaloClient("TOK", transport=tr, http_timeout_sec=1, max_retries=0)
    assert client.get_updates(timeout=30) is None
    assert tr.timeouts == [35.0]


def test_array_poll_result_is_rejected_without_dropping_tail() -> None:
    tr = _FakeTransport(get_sequence=[[_upd("m1", "c", "one"), _upd("m2", "c", "two")]])
    client = ZaloClient("TOK", transport=tr, max_retries=0)
    with pytest.raises(ZaloApiError, match="array"):
        client.get_updates()


def test_secret_redaction_in_transport_exception() -> None:
    def boom(url: str, params: dict, *, timeout_sec: float = 60.0) -> dict:
        raise RuntimeError(f"conn failed for token=SECRETTOKEN99 in {url}")

    c = ZaloClient("SECRETTOKEN99", transport=boom, max_retries=0)
    with pytest.raises(ZaloApiError) as ei:
        c.send_message("c", "hi")
    assert "SECRETTOKEN99" not in str(ei.value)


def test_webhook_registration_secret_is_redacted_from_errors() -> None:
    secret = "webhook-secret-value"

    def boom(url: str, params: dict, *, timeout_sec: float = 60.0) -> dict:
        raise RuntimeError(f"request failed: {params!r}")

    client = ZaloClient("BOT-TOKEN", transport=boom, max_retries=0)
    with pytest.raises(ZaloApiError) as exc_info:
        client.set_webhook("https://example.test/hook", secret)
    assert secret not in str(exc_info.value)
    assert "BOT-TOKEN" not in str(exc_info.value)


def test_webhook_authorized_and_unauthorized() -> None:
    tr = _FakeTransport()
    ch = _chan(tr, allowed=["chat-42"], webhook_secret="supersecret")
    body = json.dumps(_upd("w1", "chat-42", "hello webhook")).encode()
    bad = ch.handle_webhook({"X-Bot-Api-Secret-Token": "wrong"}, body)
    assert bad.status == 401 and tr.sent == []
    ok = ch.handle_webhook({WEBHOOK_SECRET_HEADER: "supersecret"}, body)
    assert ok.status == 200 and ok.ok
    assert tr.sent[-1]["text"] == "echo:hello webhook"

    class DuplicateHeaders:
        def items(self):
            return [
                (WEBHOOK_SECRET_HEADER, "supersecret"),
                (WEBHOOK_SECRET_HEADER.lower(), "supersecret"),
            ]

    assert not ch.authorize_webhook(DuplicateHeaders())  # type: ignore[arg-type]


def test_official_webhook_envelope_contract() -> None:
    tr = _FakeTransport()
    ch = _chan(tr, allowed=["chat-42"], webhook_secret="supersecret")
    body = json.dumps({"ok": True, "result": _upd("w1", "chat-42", "official")}).encode()
    result = ch.handle_webhook({WEBHOOK_SECRET_HEADER: "supersecret"}, body)
    assert result.status == 200
    assert tr.sent[-1]["text"] == "echo:official"


def test_webhook_has_bounded_concurrency_without_queueing() -> None:
    tr = _FakeTransport()
    entered = threading.Event()
    release = threading.Event()

    def slow_chat(text: str, chat_id: str) -> str:
        entered.set()
        assert release.wait(2)
        return "ok"

    ch = _chan(
        tr,
        allowed=["c"],
        run_chat=slow_chat,
        webhook_secret="supersecret",
    )
    headers = {WEBHOOK_SECRET_HEADER: "supersecret"}
    body = json.dumps({"ok": True, "result": _upd("w1", "c", "slow")}).encode()
    results = []
    worker = threading.Thread(target=lambda: results.append(ch.handle_webhook(headers, body)))
    worker.start()
    assert entered.wait(2)
    busy = ch.handle_webhook(
        headers,
        json.dumps({"ok": True, "result": _upd("w2", "c", "second")}).encode(),
    )
    assert busy.status == 503 and busy.error == "webhook busy"
    release.set()
    worker.join(2)
    assert results and results[0].status == 200


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


def test_update_returning_after_stop_is_not_dispatched() -> None:
    entered = threading.Event()
    release = threading.Event()
    sent: list[dict] = []

    def transport(url: str, params: dict, *, timeout_sec: float = 60.0) -> dict:
        if url.endswith("/getUpdates"):
            entered.set()
            assert release.wait(2)
            return {"ok": True, "result": _upd("late", "c", "too late")}
        sent.append(params)
        return {"ok": True, "result": {}}

    channel = ZaloChannel(
        ZaloClient("TOK", transport=transport, max_retries=0),
        ChannelGate(allowed_chat_ids={"c"}),
        get_app=lambda: None,
        chat_lock=threading.Lock(),
        run_chat=lambda text, chat_id: "reply",
    )
    result: list[int] = []
    worker = threading.Thread(target=lambda: result.append(channel.poll_once()))
    worker.start()
    assert entered.wait(2)
    channel.stop()
    release.set()
    worker.join(2)
    assert result == [0]
    assert sent == []


def test_empty_poll_loop_uses_stop_aware_backoff() -> None:
    waits: list[float] = []

    class Stop:
        def is_set(self) -> bool:
            return bool(waits)

        def wait(self, seconds: float) -> bool:
            waits.append(seconds)
            return True

    ch = _chan(_FakeTransport())
    ch.run(Stop())
    assert waits == [0.1]


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
    with pytest.raises(ValidationError):
        ZaloCfg(
            enabled=True,
            mode="webhook",
            webhook_url="https://user:pass@x/hook?token=leak",
            webhook_secret="abcdefgh",
        )
    with pytest.raises(ValidationError):
        ZaloCfg(enabled=True, pairing_code="short")
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
