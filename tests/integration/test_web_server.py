"""Integration: web UI server phục vụ chat qua HTTP (offline, FakeProvider)."""

from __future__ import annotations

import itertools
import json
import threading
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


def _app(tmp_path: Path) -> App:
    (tmp_path / "ws").mkdir(exist_ok=True)
    cfg = HarnessCfg(
        provider=ProviderCfg(name="fake", model="fake-1"),
        workspace_root=tmp_path / "ws",
        sandbox=SandboxCfg(backend="local"),
        budget=BudgetCfg(max_loop_iterations=3),
    )
    return App(provider=FakeProvider([text_result("Chào anh từ web!")]),
               cfg=cfg, state_dir=tmp_path / "st", secrets=InMemorySecretStore(), clock=_clock())


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, json.loads(r.read())


def _post(url, obj):
    req = urllib.request.Request(url, data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read())


def test_web_chat_flow(tmp_path: Path) -> None:
    app = _app(tmp_path)
    httpd = serve(app, host="127.0.0.1", port=0)  # port 0 = tự chọn cổng trống
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        # health
        code, h = _get(base + "/api/health")
        assert code == 200 and h["ok"] and h["provider"] == "fake"
        # trang chủ trả HTML
        with urllib.request.urlopen(base + "/", timeout=5) as r:
            assert r.status == 200 and b"<title>yett</title>" in r.read()
        # chat
        code, j = _post(base + "/api/chat", {"message": "xin chào", "session": "main"})
        assert code == 200 and "web" in j["text"] and j["status"] == "done"
        # usage đọc được
        code, u = _get(base + "/api/usage")
        assert code == 200 and "rows" in u
    finally:
        httpd.shutdown()
        app.close()


def test_web_empty_message_rejected(tmp_path: Path) -> None:
    app = _app(tmp_path)
    httpd = serve(app, host="127.0.0.1", port=0)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        import urllib.error
        try:
            _post(f"http://127.0.0.1:{port}/api/chat", {"message": "  "})
            assert False, "phải trả lỗi 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400
    finally:
        httpd.shutdown()
        app.close()
