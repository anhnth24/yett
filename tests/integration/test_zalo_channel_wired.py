"""Integration: Zalo channel wired into serve webhook path + notifier (offline)."""

from __future__ import annotations

import json
import threading
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace

import pytest

import yett.channels.zalo_bot as zalo_module
from yett.app import App
from yett.channels.gating import ChannelGate
from yett.channels.zalo_bot import WEBHOOK_SECRET_HEADER, ZaloChannel, ZaloClient
from yett.config.models import (
    BudgetCfg,
    ChannelsCfg,
    HarnessCfg,
    ProviderCfg,
    SandboxCfg,
    ZaloCfg,
)
from yett.provider.fake import FakeProvider, text_result
from yett.secrets.backends import InMemorySecretStore
from yett.web.server import _hot_swap, _start_zalo, serve


def _cfg(tmp_path: Path, **kw) -> HarnessCfg:
    return HarnessCfg(
        provider=ProviderCfg(name="fake", model="fake-1"),
        workspace_root=tmp_path / "ws",
        sandbox=SandboxCfg(backend="local"),
        budget=BudgetCfg(max_loop_iterations=4),
        **kw,
    )


def test_zalo_webhook_path_on_httpd(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir(parents=True)
    app = App(
        provider=FakeProvider([text_result("webhook ok")]),
        cfg=_cfg(tmp_path, channels=ChannelsCfg(zalo=ZaloCfg(enabled=False))),
        state_dir=tmp_path / "st",
        secrets=InMemorySecretStore(),
    )
    sent: list[dict] = []

    def transport(url: str, params: dict, *, timeout_sec: float = 60.0) -> dict:
        if url.rsplit("/", 1)[-1] == "sendMessage":
            sent.append(params)
        return {"ok": True, "result": {}}

    channel = ZaloChannel(
        ZaloClient("TOK", transport=transport, max_retries=0),
        ChannelGate(allowed_chat_ids={"c1"}),
        get_app=lambda: app,
        chat_lock=threading.Lock(),
        webhook_secret="webhook-secret-ok",
    )
    httpd = serve(app, host="127.0.0.1", port=0)
    httpd._zalo_webhook = {  # type: ignore[attr-defined]
        "path": "/api/channels/zalo/webhook",
        "channel": channel,
    }
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        port = httpd.server_address[1]
        body = json.dumps(
            {
                "ok": True,
                "result": {
                "event_name": "message.text.received",
                "message": {
                    "message_id": "wh-1",
                    "from": {"id": "c1", "is_bot": False},
                    "chat": {"id": "c1", "chat_type": "PRIVATE"},
                    "text": "ping",
                },
                },
            }
        ).encode()
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST",
            "/api/channels/zalo/webhook",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                WEBHOOK_SECRET_HEADER: "webhook-secret-ok",
                "Host": f"127.0.0.1:{port}",
            },
        )
        resp = conn.getresponse()
        raw = resp.read()
        assert resp.status == 200, raw
        assert sent and "webhook ok" in sent[-1]["text"]

        # secret sai → 401, không dispatch
        sent.clear()
        conn.request(
            "POST",
            "/api/channels/zalo/webhook",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                WEBHOOK_SECRET_HEADER: "wrong",
                "Host": f"127.0.0.1:{port}",
            },
        )
        resp2 = conn.getresponse()
        resp2.read()
        assert resp2.status == 401
        assert sent == []

        # Public webhook contract requires application/json.
        conn.request(
            "POST",
            "/api/channels/zalo/webhook",
            body=body,
            headers={
                "Content-Type": "text/plain",
                WEBHOOK_SECRET_HEADER: "webhook-secret-ok",
                "Host": f"127.0.0.1:{port}",
            },
        )
        resp3 = conn.getresponse()
        resp3.read()
        assert resp3.status == 415

        # The route enforces the channel's 256 KiB cap before JSON parsing.
        huge = b"x" * (256 * 1024 + 1)
        conn.request(
            "POST",
            "/api/channels/zalo/webhook",
            body=huge,
            headers={
                "Content-Type": "application/json",
                WEBHOOK_SECRET_HEADER: "webhook-secret-ok",
                "Host": f"127.0.0.1:{port}",
            },
        )
        resp4 = conn.getresponse()
        resp4.read()
        assert resp4.status == 413
        conn.close()
    finally:
        channel.stop()
        httpd.shutdown()
        app.close()


def test_start_zalo_resolves_secrets_and_wires_notifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ws").mkdir(parents=True)
    cfg = _cfg(
        tmp_path,
        channels=ChannelsCfg(
            zalo=ZaloCfg(
                enabled=True,
                allowed_chat_ids=["c1"],
                pairing_code_secret="zalo_pairing",
                mode="webhook",
                webhook_url="https://example.test/api/channels/zalo/webhook",
                webhook_secret_secret="zalo_webhook",
            )
        ),
    )
    secrets = InMemorySecretStore(
        {
            "zalo_bot_token": "BOT-TOKEN",
            "zalo_pairing": "PAIR-CODE-123",
            "zalo_webhook": "WEBHOOK-SECRET-123",
        }
    )
    app = App(
        provider=FakeProvider([text_result("unused")]),
        cfg=cfg,
        state_dir=tmp_path / "st",
        secrets=secrets,
    )
    calls: list[tuple[str, dict]] = []

    def transport(url: str, params: dict, *, timeout_sec: float = 60.0) -> dict:
        calls.append((url.rsplit("/", 1)[-1], dict(params)))
        return {"ok": True, "result": {}}

    real_client = zalo_module.ZaloClient

    def client_factory(token: str, **kwargs):
        assert token == "BOT-TOKEN"
        return real_client(token, transport=transport, max_retries=0)

    monkeypatch.setattr(zalo_module, "ZaloClient", client_factory)
    httpd = serve(app, host="127.0.0.1", port=0)
    stop = threading.Event()
    try:
        _start_zalo(httpd, app, httpd._chat_lock, stop)  # type: ignore[attr-defined]
        assert calls[0] == (
            "setWebhook",
            {
                "url": "https://example.test/api/channels/zalo/webhook",
                "secret_token": "WEBHOOK-SECRET-123",
            },
        )
        assert getattr(httpd, "_zalo_webhook")["path"] == "/api/channels/zalo/webhook"
        assert app._notifier is not None
        assert app._notifier("notify") == 1
        assert calls[-1] == ("sendMessage", {"chat_id": "c1", "text": "notify"})
        for hook in getattr(httpd, "_channel_shutdown_hooks"):
            hook()
        assert all(method != "deleteWebhook" for method, _ in calls)
    finally:
        stop.set()
        httpd.server_close()
        app.close()


def test_hot_swap_rejects_channel_config_change_and_closes_candidate() -> None:
    class DummyApp:
        def __init__(self, channels: ChannelsCfg) -> None:
            self.cfg = SimpleNamespace(channels=channels)
            self.closed = False

        def close(self) -> None:
            self.closed = True

    old = DummyApp(ChannelsCfg())
    candidate = DummyApp(
        ChannelsCfg(zalo=ZaloCfg(enabled=True, allowed_chat_ids=["c1"]))
    )
    server = SimpleNamespace(_app=old)
    with pytest.raises(RuntimeError, match="khởi động lại"):
        _hot_swap(server, threading.Lock(), None, lambda: candidate)
    assert server._app is old
    assert candidate.closed
