"""Web UI server local (stdlib, không thêm dep). `yett serve` → chat trong trình duyệt.

ThreadingHTTPServer + asyncio.run mỗi request. Bind mặc định 127.0.0.1 (chỉ máy bạn).
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING

from yett.obs import cost
from yett.web.page import INDEX_HTML

if TYPE_CHECKING:
    from yett.app import App


def make_handler(app: "App", center=None) -> type[BaseHTTPRequestHandler]:
    chat_lock = threading.Lock()  # serialize turn (single-user local)
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

        def do_GET(self) -> None:  # noqa: N802
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
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
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


def serve(app: "App", *, host: str = "127.0.0.1", port: int = 8765, center=None) -> ThreadingHTTPServer:
    """Tạo server. Nếu truyền ApprovalCenter → gắn approver để UI duyệt lệnh nhạy cảm."""
    if center is not None:
        app.set_approver(center.request)
    return ThreadingHTTPServer((host, port), make_handler(app, center))


def serve_forever(
    app: "App", *, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = False
) -> None:
    from yett.approvals import ApprovalCenter

    center = ApprovalCenter(default_timeout=float(app.cfg.security.approval_timeout_sec))
    httpd = serve(app, host=host, port=port, center=center)
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
