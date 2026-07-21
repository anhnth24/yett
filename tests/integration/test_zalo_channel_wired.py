"""Integration: Zalo channel wired into serve webhook path + notifier (offline)."""

from __future__ import annotations

import json
import threading
from http.client import HTTPConnection
from pathlib import Path

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
from yett.web.server import serve


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
                "event_name": "message.text.received",
                "message": {
                    "message_id": "wh-1",
                    "chat": {"id": "c1", "chat_type": "PRIVATE"},
                    "text": "ping",
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
        conn.close()
    finally:
        channel.stop()
        httpd.shutdown()
        app.close()
