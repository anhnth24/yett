"""Test web server boundary hardening (phase 7a): Host/Origin allowlist (chống
DNS-rebinding cho dịch vụ localhost), cap kích thước request, và `/api/config` GET
không lộ secret plaintext (redact dùng chung filter Phase 5).

Dùng `port=0` (OS tự chọn cổng trống) — cổng cố định bị chặn bởi permission trên máy
Windows này (xác nhận đây là hạn chế môi trường có sẵn, không phải do thay đổi ở đây).
"""

from __future__ import annotations

import itertools
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from yett.app import App
from yett.config.models import BudgetCfg, HarnessCfg, ProviderCfg, SandboxCfg
from yett.provider.fake import FakeProvider, text_result
from yett.secrets.backends import InMemorySecretStore
from yett.web.server import LOOPBACK_HOSTS, serve


def _clock():
    c = itertools.count(1)
    return lambda: float(next(c))


def _app(tmp_path: Path) -> App:
    (tmp_path / "ws").mkdir(exist_ok=True)
    cfg = HarnessCfg(
        provider=ProviderCfg(name="fake", model="fake-1"),
        workspace_root=tmp_path / "ws",
        sandbox=SandboxCfg(backend="local"),
        budget=BudgetCfg(max_loop_iterations=2),
    )
    return App(provider=FakeProvider([text_result("ok")]), cfg=cfg,
               state_dir=tmp_path / "st", secrets=InMemorySecretStore(), clock=_clock())


class _Server:
    def __init__(self, tmp_path: Path, config_path: str | None = None) -> None:
        self.app = _app(tmp_path)
        self.httpd = serve(self.app, host="127.0.0.1", port=0, config_path=config_path)
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.app.close()

    def request(self, path: str, *, method: str = "GET", body: bytes | None = None,
                headers: dict | None = None):
        req = urllib.request.Request(
            self.base + path, data=body, method=method, headers=headers or {},
        )
        return urllib.request.urlopen(req, timeout=5)


@pytest.fixture
def server(tmp_path: Path):
    srv = _Server(tmp_path)
    yield srv
    srv.close()


def test_loopback_hosts_constant_covers_common_forms() -> None:
    assert {"127.0.0.1", "localhost", "::1"} <= LOOPBACK_HOSTS


def test_default_client_request_allowed(server: _Server) -> None:
    # urllib đặt Host tự động khớp URL (127.0.0.1:<port>) — phải qua được allowlist.
    with server.request("/api/health") as r:
        assert r.status == 200
        assert json.loads(r.read())["ok"] is True


def test_wrong_host_header_rejected(server: _Server) -> None:
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        server.request("/api/health", headers={"Host": "evil.com"})
    assert exc_info.value.code == 403


def test_dns_rebinding_style_host_rejected(server: _Server) -> None:
    """Mô phỏng DNS rebinding: trang JS trên domain khác gọi fetch tới cổng loopback —
    trình duyệt gửi Host trùng URL nó gọi (domain attacker), KHÔNG phải 127.0.0.1."""
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        server.request("/api/health", headers={"Host": "attacker-controlled.example:1234"})
    assert exc_info.value.code == 403


def test_mismatched_origin_rejected_on_post(server: _Server) -> None:
    body = json.dumps({"message": "xin chào"}).encode()
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        server.request(
            "/api/chat", method="POST", body=body,
            headers={"Content-Type": "application/json", "Origin": "http://evil.com"},
        )
    assert exc_info.value.code == 403


def test_matching_origin_allowed_on_post(server: _Server) -> None:
    body = json.dumps({"message": "xin chào"}).encode()
    with server.request(
        "/api/chat", method="POST", body=body,
        headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{server.port}"},
    ) as r:
        assert r.status == 200


def test_oversized_post_body_rejected(server: _Server) -> None:
    huge = json.dumps({"message": "x" * (3 * 1024 * 1024)}).encode()  # > 2MB cap
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        server.request(
            "/api/chat", method="POST", body=huge, headers={"Content-Type": "application/json"},
        )
    assert exc_info.value.code == 413


def test_malformed_content_length_rejected(tmp_path: Path, server: _Server) -> None:
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    conn.putrequest("POST", "/api/chat")
    conn.putheader("Content-Length", "abc")
    conn.endheaders()
    resp = conn.getresponse()
    assert resp.status == 400
    conn.close()


def test_duplicate_content_length_and_transfer_encoding_rejected(server: _Server) -> None:
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    conn.putrequest("POST", "/api/chat")
    conn.putheader("Content-Length", "0")
    conn.putheader("Content-Length", "0")
    conn.endheaders()
    response = conn.getresponse()
    response.read()
    assert response.status == 400
    conn.close()

    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    conn.putrequest("POST", "/api/chat")
    conn.putheader("Transfer-Encoding", "chunked")
    conn.endheaders()
    response = conn.getresponse()
    response.read()
    assert response.status == 400
    conn.close()


def test_config_get_redacts_secret(tmp_path: Path) -> None:
    cfg_path = tmp_path / "harness.yaml"
    cfg_path.write_text(
        "provider:\n  name: openai\n  api_key: sk-ant-abcdefghijklmnopqrstuvwxyz012345\n",
        encoding="utf-8",
    )
    srv = _Server(tmp_path, config_path=str(cfg_path))
    try:
        with srv.request("/api/config") as r:
            payload = json.loads(r.read())
        assert "sk-ant-abcdefghijklmnopqrstuvwxyz012345" not in payload["text"]
        assert "[REDACTED]" in payload["text"]
        assert payload["path"] == str(cfg_path)
    finally:
        srv.close()


def test_config_get_empty_when_no_path(server: _Server) -> None:
    with server.request("/api/config") as r:
        payload = json.loads(r.read())
    assert payload["text"] == "" and payload["path"] == ""


# --- cli.py: cảnh báo --host non-localhost (dùng chung LOOPBACK_HOSTS với server.py) ---
def test_non_loopback_warning_silent_for_loopback_hosts() -> None:
    from yett.cli import non_loopback_warning

    for h in ("127.0.0.1", "localhost", "::1"):
        assert non_loopback_warning(h) is None


def test_non_loopback_warning_fires_for_lan_bind() -> None:
    from yett.cli import non_loopback_warning

    msg = non_loopback_warning("0.0.0.0")
    assert msg is not None and "0.0.0.0" in msg and "CẢNH BÁO" in msg
