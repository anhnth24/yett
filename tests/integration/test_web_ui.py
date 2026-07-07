"""Integration: web UI local (health, chat, index) qua HTTP thật trên localhost."""

from __future__ import annotations

import itertools
import json
import threading
import time
import urllib.request
from pathlib import Path

from yett.app import App
from yett.config.models import BudgetCfg, HarnessCfg, ProviderCfg, SandboxCfg
from yett.provider.fake import FakeProvider, text_result
from yett.secrets.backends import InMemorySecretStore
from yett.web.server import serve


def _clock():
    c = itertools.count(1)
    return lambda: float(next(c))


def _build_app(tmp_path: Path) -> App:
    (tmp_path / "ws").mkdir()
    cfg = HarnessCfg(
        provider=ProviderCfg(name="fake", model="fake-1"),
        workspace_root=tmp_path / "ws",
        sandbox=SandboxCfg(backend="local"),
        budget=BudgetCfg(max_loop_iterations=3),
    )
    return App(
        provider=FakeProvider([text_result("Chào từ web UI")] * 5), cfg=cfg,
        state_dir=tmp_path / "st", secrets=InMemorySecretStore(), clock=_clock(),
    )


def _get(url: str) -> dict:
    return json.loads(urllib.request.urlopen(url, timeout=5).read())


def _post(url: str, obj: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(obj).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    return json.loads(urllib.request.urlopen(req, timeout=5).read())


def test_web_ui_health_chat_index(tmp_path: Path) -> None:
    app = _build_app(tmp_path)
    # port=0 → OS tự chọn cổng trống (tránh flaky trên Windows khi dải cổng bị WSL2/Hyper-V
    # loại trừ -> WinError 10013); đọc cổng thật từ server_address.
    httpd = serve(app, host="127.0.0.1", port=0)
    port = httpd.server_address[1]
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    time.sleep(0.3)
    try:
        base = f"http://127.0.0.1:{port}"
        health = _get(f"{base}/api/health")
        assert health["ok"] and health["provider"] == "fake"

        chat = _post(f"{base}/api/chat", {"message": "xin chào"})
        assert chat["status"] == "done"
        assert "web UI" in chat["text"]

        html = urllib.request.urlopen(f"{base}/", timeout=5).read().decode()
        assert "<title>yett</title>" in html

        # message rỗng → 400
        try:
            _post(f"{base}/api/chat", {"message": ""})
            assert False, "phải trả lỗi 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400
    finally:
        httpd.shutdown()
        app.close()
