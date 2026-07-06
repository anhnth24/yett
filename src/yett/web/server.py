"""Web UI server local (stdlib, không thêm dep). `yett serve` → chat trong trình duyệt.

ThreadingHTTPServer + asyncio.run mỗi request. Bind mặc định 127.0.0.1 (chỉ máy bạn).

[RT web boundary] Vì server chỉ có Content-Type check, KHÔNG có auth, nó vốn dựa vào
"chỉ máy bạn nghe được" để an toàn — nhưng một trang web độc hại mở trong CÙNG trình
duyệt vẫn gọi `fetch()` tới `127.0.0.1:<port>` được (không bị Same-Origin-Policy chặn ở
mức network), và DNS rebinding (domain attacker trỏ tạm về 127.0.0.1) có thể qua mặt
mọi allowlist dựa trên IP đích. Phòng thủ: validate header `Host`/`Origin` khớp đúng
`host:port` server đang bind — 2 header này phản ánh URL người dùng/trang gõ, KHÔNG bị
ảnh hưởng bởi việc DNS resolve đi đâu.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from yett.obs import cost
from yett.web.page import INDEX_HTML

if TYPE_CHECKING:
    from yett.app import App

# Tên host coi là "chỉ máy này" — dùng cả ở đây (allowlist Host/Origin) và ở cli.py
# (cảnh báo khi `--host` bind ra ngoài các tên này).
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

_MAX_BODY_BYTES = 2 * 1024 * 1024  # 2MB — đủ cho message chat + config text, chặn DoS bộ nhớ


def make_handler(
    app: "App", center=None, config_path: str | None = None, *,
    bind_host: str = "127.0.0.1",
) -> type[BaseHTTPRequestHandler]:
    chat_lock = threading.Lock()  # serialize turn (single-user local)

    # Bind loopback -> allowlist Host/Origin cố định (127.0.0.1/localhost/::1 + đúng
    # port). Bind ra ngoài loopback (LAN/0.0.0.0) là lựa chọn tường minh của người vận
    # hành (cli.py đã cảnh báo) — không đoán trước được hostname/IP hợp lệ nên bỏ qua
    # allowlist Host cứng trong trường hợp này (không phải lỗ hổng mới: threat model
    # DNS-rebinding chỉ áp dụng cho dịch vụ tự nhận là "chỉ localhost").
    _is_loopback_bind = bind_host in LOOPBACK_HOSTS

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # tắt log ồn ra stderr
            return

        def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: dict) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

        def _expected_hosts(self) -> set[str] | None:
            """`None` = bind ngoài loopback -> không áp allowlist cứng. Cổng lấy từ
            `self.server.server_address` (giá trị THẬT sau khi bind) chứ không phải
            cổng truyền vào `serve()` — cần thiết cho `port=0` (OS tự chọn cổng trống,
            dùng trong test) vì lúc tạo Handler class còn chưa biết cổng thật."""
            if not _is_loopback_bind:
                return None
            # `server_address` khai báo kiểu rộng (Any/tuple/str/Buffer tuỳ address
            # family) trong typeshed; `ThreadingHTTPServer` luôn dùng AF_INET/AF_INET6
            # nên thực tế luôn là tuple (host, port).
            addr = self.server.server_address
            actual_port = addr[1] if isinstance(addr, tuple) else 0
            return {f"{h}:{actual_port}" for h in LOOPBACK_HOSTS}

        def _host_origin_ok(self) -> bool:
            """True nếu request hợp lệ theo threat model DNS-rebinding."""
            expected = self._expected_hosts()
            if expected is None:
                return True
            host_hdr = (self.headers.get("Host") or "").strip().lower()
            if host_hdr not in expected:
                return False
            origin = self.headers.get("Origin")
            if origin:  # Origin vắng mặt với curl/top-level GET — chỉ kiểm khi có
                o = urlparse(origin)
                default_port = 443 if o.scheme == "https" else 80
                origin_host = f"{(o.hostname or '').lower()}:{o.port or default_port}"
                if origin_host not in expected:
                    return False
            return True

        def _read_body(self) -> bytes | None:
            """Đọc body POST có cap kích thước; trả None + đã trả response lỗi nếu
            Content-Length thiếu hợp lệ hoặc vượt trần (không đọc hết body trong TH đó
            — tránh giữ request khổng lồ trong bộ nhớ)."""
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                self._json(400, {"error": "Content-Length không hợp lệ"})
                return None
            if length < 0:
                self._json(400, {"error": "Content-Length không hợp lệ"})
                return None
            if length > _MAX_BODY_BYTES:
                # Đọc-rồi-bỏ theo chunk nhỏ (không giữ cả body khổng lồ trong bộ nhớ)
                # thay vì đóng kết nối ngay — HTTP/1.0 không có cơ chế 100-continue nên
                # đóng sớm giữa lúc client còn đang ghi sẽ làm client thấy reset kết nối
                # thay vì đọc được response 413 sạch.
                self._drain(length)
                self._json(413, {"error": f"request quá lớn (> {_MAX_BODY_BYTES} byte)"})
                return None
            return self.rfile.read(length)

        def _drain(self, total: int) -> None:
            remaining = total
            chunk_size = 65536
            while remaining > 0:
                chunk = self.rfile.read(min(chunk_size, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)

        def _drain_declared_body(self) -> None:
            """Drain body theo Content-Length trước khi trả lỗi sớm (vd Host/Origin
            không hợp lệ) — cap ở `_MAX_BODY_BYTES` (đủ cho client thật; Content-Length
            giả mạo khổng lồ thì không cần drain hết, client đó vốn không hợp lệ)."""
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                return
            if length > 0:
                self._drain(min(length, _MAX_BODY_BYTES))

        def do_GET(self) -> None:  # noqa: N802
            if not self._host_origin_ok():
                self._json(403, {"error": "Host/Origin không hợp lệ"})
                return
            if self.path == "/" or self.path.startswith("/index"):
                self._send(200, INDEX_HTML.encode("utf-8"), "text/html")
            elif self.path == "/api/health":
                self._json(200, {"ok": True, "provider": app.cfg.provider.name,
                                 "model": app.cfg.provider.model})
            elif self.path.startswith("/api/usage"):
                spans = []
                for t in app.spanstore.list_traces(limit=1000):
                    spans.extend(app.spanstore.get_trace(t["trace_id"]))
                self._json(200, {"rows": cost.aggregate(spans, by="provider")})
            elif self.path.startswith("/api/traces"):
                self._json(200, {"traces": app.spanstore.list_traces(limit=50)})
            elif self.path == "/api/pending":
                self._json(200, {"pending": center.list_pending() if center else []})
            elif self.path == "/api/config":
                from yett.web.configio import read_config_text_redacted

                # [RT] GET không bao giờ trả secret plaintext — dùng chung filter Phase 5.
                text = read_config_text_redacted(config_path) if config_path else ""
                self._json(200, {"text": text, "path": config_path or ""})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._host_origin_ok():
                # Trả lỗi sớm (trước khi đọc body) vẫn phải drain body đã khai báo qua
                # Content-Length trước khi đóng — nếu không, client (đang ghi dở body)
                # sẽ thấy reset kết nối thay vì đọc được response 403 sạch (HTTP/1.0
                # không có 100-continue để báo trước "đừng gửi body").
                self._drain_declared_body()
                self._json(403, {"error": "Host/Origin không hợp lệ"})
                return
            body = self._read_body()
            if body is None:
                return
            try:
                data = json.loads(body or b"{}")
            except json.JSONDecodeError:
                self._json(400, {"error": "JSON không hợp lệ"})
                return

            if self.path == "/api/approve":
                if center is None:
                    self._json(404, {"error": "approval không bật"})
                    return
                ok = center.resolve(data.get("id", ""), bool(data.get("approved")))
                self._json(200 if ok else 404, {"ok": ok})
                return

            if self.path == "/api/config":
                if not config_path:
                    self._json(404, {"error": "không có config path"})
                    return
                from yett.web.configio import write_config_text

                err = write_config_text(config_path, data.get("text", ""))
                if err:
                    self._json(400, {"error": err})
                else:
                    self._json(200, {"ok": True, "note": "Đã lưu. Khởi động lại yett để áp dụng."})
                return

            if self.path != "/api/chat":
                self._json(404, {"error": "not found"})
                return
            msg = (data.get("message") or "").strip()
            if not msg:
                self._json(400, {"error": "thiếu message"})
                return
            session = data.get("session", "main")
            try:
                with chat_lock:
                    res = asyncio.run(app.chat(msg, session_key=session))
                self._json(200, {"text": res.text, "status": res.status,
                                 "iterations": res.iterations, "trace_id": res.trace_id})
            except Exception as e:  # noqa: BLE001
                self._json(500, {"error": str(e)})

    return Handler


def serve(app: "App", *, host: str = "127.0.0.1", port: int = 8765, center=None,
          config_path: str | None = None) -> ThreadingHTTPServer:
    """Tạo server. Nếu truyền ApprovalCenter → gắn approver để UI duyệt lệnh nhạy cảm."""
    if center is not None:
        app.set_approver(center.request)
    httpd = ThreadingHTTPServer(
        (host, port), make_handler(app, center, config_path, bind_host=host)
    )
    return httpd


def serve_forever(
    app: "App", *, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = False,
    config_path: str | None = None,
) -> None:
    from yett.approvals import ApprovalCenter

    center = ApprovalCenter(default_timeout=float(app.cfg.security.approval_timeout_sec))
    httpd = serve(app, host=host, port=port, center=center, config_path=config_path)
    url = f"http://{host}:{port}"
    print(f"[yett] Web UI: {url}  (Ctrl+C để dừng)")
    if open_browser:
        import threading
        import webbrowser

        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[yett] đã dừng.")
    finally:
        httpd.shutdown()
        app.close()
