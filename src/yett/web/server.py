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
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from yett.obs import cost
from yett.web.page import INDEX_HTML

if TYPE_CHECKING:
    from yett.app import App

# Tên host coi là "chỉ máy này" — dùng cả ở đây (allowlist Host/Origin) và ở cli.py
# (cảnh báo khi `--host` bind ra ngoài các tên này).
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

_MAX_BODY_BYTES = 2 * 1024 * 1024  # 2MB — đủ cho message chat + config text, chặn DoS bộ nhớ
_MAX_HTTP_WORKERS = 32
_CLIENT_SOCKET_TIMEOUT_SEC = 30.0


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Bound request threads and slow-client lifetime for the externally reachable webhook."""

    daemon_threads = True
    request_queue_size = 64

    def __init__(self, *args, max_workers: int = _MAX_HTTP_WORKERS, **kwargs) -> None:
        self._worker_slots = threading.BoundedSemaphore(max_workers)
        super().__init__(*args, **kwargs)

    def get_request(self):
        request, client_address = super().get_request()
        request.settimeout(_CLIENT_SOCKET_TIMEOUT_SEC)
        return request, client_address

    def process_request(self, request, client_address) -> None:
        # Backpressure happens in the accept loop; no unbounded Python thread/queue is created.
        self._worker_slots.acquire()
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._worker_slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._worker_slots.release()


def make_handler(
    app: "App", center=None, config_path: str | None = None, *,
    bind_host: str = "127.0.0.1", chat_lock=None, rebuild=None,
) -> type[BaseHTTPRequestHandler]:
    # serialize turn (single-user local). Chia sẻ với scheduler tick nếu được truyền vào.
    chat_lock = chat_lock or threading.Lock()
    initial_app = app  # fallback nếu server chưa gắn _app; hot-reload đọc self.server._app

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

        def _read_body(self, *, max_bytes: int = _MAX_BODY_BYTES) -> bytes | None:
            """Đọc body POST có cap kích thước; trả None + đã trả response lỗi nếu
            Content-Length thiếu hợp lệ hoặc vượt trần (không đọc hết body trong TH đó
            — tránh giữ request khổng lồ trong bộ nhớ)."""
            if self.headers.get("Transfer-Encoding"):
                self._json(400, {"error": "Transfer-Encoding không được hỗ trợ"})
                return None
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) > 1:
                self._json(400, {"error": "Content-Length bị lặp"})
                return None
            try:
                length = int(lengths[0]) if lengths else 0
            except ValueError:
                self._json(400, {"error": "Content-Length không hợp lệ"})
                return None
            if length < 0:
                self._json(400, {"error": "Content-Length không hợp lệ"})
                return None
            if length > max_bytes:
                # Drain only a bounded prefix. Draining an attacker-declared multi-GB body
                # would tie up one worker despite bounded memory.
                self._drain(min(length, max_bytes))
                self.close_connection = True
                self._json(413, {"error": f"request quá lớn (> {max_bytes} byte)"})
                return None
            body = self.rfile.read(length)
            if len(body) != length:
                self._json(400, {"error": "request body bị thiếu"})
                return None
            return body

        def _drain(self, total: int) -> None:
            remaining = total
            chunk_size = 65536
            while remaining > 0:
                chunk = self.rfile.read(min(chunk_size, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)

        def _drain_declared_body(self, *, max_bytes: int = _MAX_BODY_BYTES) -> None:
            """Drain body theo Content-Length trước khi trả lỗi sớm (vd Host/Origin
            không hợp lệ) — cap ở `_MAX_BODY_BYTES` (đủ cho client thật; Content-Length
            giả mạo khổng lồ thì không cần drain hết, client đó vốn không hợp lệ)."""
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                return
            if length > 0:
                self._drain(min(length, max_bytes))
                if length > max_bytes:
                    self.close_connection = True

        def do_GET(self) -> None:  # noqa: N802
            if not self._host_origin_ok():
                self._json(403, {"error": "Host/Origin không hợp lệ"})
                return
            app = getattr(self.server, "_app", None) or initial_app  # hot-reload: app hiện tại
            if self.path == "/" or self.path.startswith("/index"):
                self._send(200, INDEX_HTML.encode("utf-8"), "text/html")
            elif self.path == "/api/health":
                self._json(200, {"ok": True, "provider": app.cfg.provider.name,
                                 "model": app.cfg.provider.model})
            elif self.path.startswith("/api/usage"):
                spans: list[dict] = []
                for t in app.spanstore.list_traces(limit=1000):
                    spans.extend(app.spanstore.get_trace(t["trace_id"]))
                self._json(200, {"rows": cost.aggregate(spans, by="provider")})
            elif self.path.startswith("/api/traces"):
                self._json(200, {"traces": app.spanstore.list_traces(limit=50)})
            elif self.path.startswith("/api/trace?"):
                tid = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
                self._json(200, {"spans": app.spanstore.get_trace(tid) if tid else []})
            elif self.path == "/api/pending":
                self._json(200, {"pending": center.list_pending() if center else []})
            elif self.path == "/api/config":
                from yett.web.configio import read_config_text_redacted

                # [RT] GET không bao giờ trả secret plaintext — dùng chung filter Phase 5.
                text = read_config_text_redacted(config_path) if config_path else ""
                self._json(200, {"text": text, "path": config_path or ""})
            elif self.path == "/api/meta":
                from yett.web.consoledata import overview
                self._json(200, overview(app))
            elif self.path == "/api/secrets":
                from yett.web.consoledata import secrets_status
                self._json(200, {"secrets": secrets_status(app)})
            elif self.path == "/api/subagents":
                from yett.web.consoledata import subagents
                self._json(200, {"subagents": subagents(app)})
            elif self.path == "/api/projects":
                from yett.web.consoledata import projects
                self._json(200, projects(app))
            elif self.path == "/api/jobs":
                self._json(200, {"jobs": app.cron.list_all()})
            elif self.path.startswith("/api/tasks"):
                qs = parse_qs(urlparse(self.path).query)
                status = qs["status"][0] if qs.get("status") else None
                project = qs["project"][0] if qs.get("project") else None
                tasks = app.tasks.list_tasks(status=status, project=project)
                self._json(200, {"tasks": [t.as_dict() for t in tasks]})
            elif self.path == "/api/briefing":
                from yett.brief import briefing_data
                self._json(200, {"text": app.briefing(), "data": briefing_data(app)})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            # Zalo Official Bot webhook: auth bằng X-Bot-Api-Secret-Token (không dựa Host
            # loopback — Zalo gọi từ ngoài). Path khớp config mới nhận; secret sai → 401.
            zalo_wh = getattr(self.server, "_zalo_webhook", None)
            req_path = urlparse(self.path).path
            if (
                isinstance(zalo_wh, dict)
                and req_path == zalo_wh.get("path")
                and zalo_wh.get("channel") is not None
            ):
                channel = zalo_wh["channel"]
                # Authenticate before allocating/parsing a body. The channel repeats this
                # check to keep its direct-call contract fail closed.
                if not channel.authorize_webhook(self.headers):
                    self._drain_declared_body(max_bytes=channel.webhook_max_bytes)
                    self._json(401, {"ok": False, "error": "unauthorized"})
                    return
                content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0]
                if content_type.strip().lower() != "application/json":
                    self._drain_declared_body(max_bytes=channel.webhook_max_bytes)
                    self._json(415, {"ok": False, "error": "application/json required"})
                    return
                body = self._read_body(max_bytes=channel.webhook_max_bytes)
                if body is None:
                    return
                result = channel.handle_webhook(self.headers, body)
                self._json(result.status, {"ok": result.ok, "error": result.error})
                return

            if not self._host_origin_ok():
                # Trả lỗi sớm (trước khi đọc body) vẫn phải drain body đã khai báo qua
                # Content-Length trước khi đóng — nếu không, client (đang ghi dở body)
                # sẽ thấy reset kết nối thay vì đọc được response 403 sạch (HTTP/1.0
                # không có 100-continue để báo trước "đừng gửi body").
                self._drain_declared_body()
                self._json(403, {"error": "Host/Origin không hợp lệ"})
                return
            app = getattr(self.server, "_app", None) or initial_app  # hot-reload: app hiện tại
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
                    return
                note = "Đã lưu. Khởi động lại yett để áp dụng."
                if rebuild is not None:
                    # Hot-reload: dựng App mới từ config vừa lưu, swap dưới chat_lock (không
                    # race với chat/scheduler), rồi đóng App cũ. Lỗi build → file đã lưu, báo rõ.
                    try:
                        _hot_swap(self.server, chat_lock, center, rebuild)
                        # Cập nhật mtime đã-biết để watcher không rebuild lại lần nữa cho chính
                        # lần ghi này (chỉ external edit mới kích watcher).
                        try:
                            self.server._cfg_mtime = os.path.getmtime(config_path)  # type: ignore[attr-defined]
                        except OSError:
                            pass
                        note = "Đã lưu & áp dụng ngay (không cần khởi động lại)."
                    except Exception as e:  # noqa: BLE001
                        note = f"Đã lưu, nhưng áp dụng nóng lỗi ({e}) — khởi động lại để chắc."
                self._json(200, {"ok": True, "note": note})
                return

            if self.path == "/api/cancel":
                self._json(200, {"ok": app.cancel(data.get("session", "main"))})
                return

            if self.path == "/api/jobs":  # tạo job cron
                import time as _t
                from yett.sched.cron import CronJob, initial_next_run
                jid = (data.get("id") or "").strip()
                spec = (data.get("spec") or "").strip()
                prompt = (data.get("prompt") or "").strip()
                if not (jid and spec and prompt):
                    self._json(400, {"error": "cần id, spec, prompt"})
                    return
                nxt = initial_next_run(spec, _t.time(), app.cfg.timezone)
                if nxt is None:
                    self._json(400, {"error": "spec không hợp lệ hoặc đã qua (dùng at:/every:/cron:)"})
                    return
                app.cron.add(CronJob(id=jid, spec=spec, tz=app.cfg.timezone, prompt=prompt,
                                     session_key=data.get("session", "main")), next_run=nxt)
                self._json(200, {"ok": True, "note": "Đã thêm job."})
                return
            if self.path == "/api/jobs/run":
                self._json(200, {"ok": app.cron.trigger(data.get("id", ""))})
                return
            if self.path == "/api/jobs/delete":
                self._json(200, {"ok": app.cron.delete(data.get("id", ""))})
                return

            if self.path == "/api/tasks":  # thêm việc
                title = (data.get("title") or "").strip()
                if not title:
                    self._json(400, {"error": "cần title"})
                    return
                try:
                    t = app.tasks.add(
                        title, project=(data.get("project") or None),
                        priority=data.get("priority", "normal"),
                        due=(data.get("due") or None), notes=(data.get("notes") or None),
                    )
                except ValueError as e:
                    self._json(400, {"error": str(e)})
                    return
                self._json(200, {"ok": True, "task": t.as_dict()})
                return
            if self.path == "/api/tasks/update":
                tid = data.get("id")
                if not isinstance(tid, int) or app.tasks.get(tid) is None:
                    self._json(404, {"error": "không có việc"})
                    return
                try:
                    t = app.tasks.update(
                        tid, status=data.get("status"), title=data.get("title"),
                        priority=data.get("priority"), due=data.get("due"), notes=data.get("notes"),
                    )
                except ValueError as e:
                    self._json(400, {"error": str(e)})
                    return
                self._json(200, {"ok": True, "task": t.as_dict() if t else None})
                return
            if self.path == "/api/tasks/delete":
                self._json(200, {"ok": app.tasks.delete(int(data.get("id", 0)))})
                return

            if self.path != "/api/chat":
                self._json(404, {"error": "not found"})
                return
            msg = (data.get("message") or "").strip()
            if not msg:
                self._json(400, {"error": "thiếu message"})
                return
            session = data.get("session", "main")
            turn_id = data.get("turn_id") or None
            project = data.get("project") or None
            try:
                with chat_lock:
                    res = asyncio.run(app.chat(msg, session_key=session, turn_id=turn_id,
                                               project=project))
                self._json(200, {"text": res.text, "status": res.status,
                                 "iterations": res.iterations, "trace_id": res.trace_id,
                                 "turn_id": turn_id})
            except Exception as e:  # noqa: BLE001
                self._json(500, {"error": str(e)})

    return Handler


def serve(app: "App", *, host: str = "127.0.0.1", port: int = 8765, center=None,
          config_path: str | None = None, chat_lock=None, rebuild=None) -> ThreadingHTTPServer:
    """Tạo server. Nếu truyền ApprovalCenter → gắn approver để UI duyệt lệnh nhạy cảm.
    chat_lock chia sẻ với scheduler tick. rebuild: callable dựng App mới (hot-reload config)."""
    if center is not None:
        app.set_approver(center.request)
    lock = chat_lock or threading.Lock()
    httpd = BoundedThreadingHTTPServer(
        (host, port),
        make_handler(app, center, config_path, bind_host=host, chat_lock=lock, rebuild=rebuild),
    )
    httpd._chat_lock = lock  # type: ignore[attr-defined]  # để serve_forever dùng cho ticker
    httpd._app = app  # type: ignore[attr-defined]  # App hiện tại; hot-reload swap tại đây
    try:
        httpd._cfg_mtime = os.path.getmtime(config_path) if config_path else None  # type: ignore[attr-defined]
    except OSError:
        httpd._cfg_mtime = None  # type: ignore[attr-defined]
    return httpd


def _run_scheduler(httpd, chat_lock, stop) -> None:
    """Thread nền: mỗi 3s chạy job cron tới hạn. Đọc App HIỆN TẠI từ httpd._app (có thể đã
    hot-reload) và giữ chat_lock suốt tick → serialize với chat + an toàn khi rebuild swap App."""
    import time as _t

    from yett.sched.cron import compute_next

    while not stop.wait(3.0):  # wait trả True khi stop set → thoát vòng
        with chat_lock:
            app = getattr(httpd, "_app", None)
            if app is None:
                continue
            try:
                due = app.cron.due(_t.time())
            except Exception:  # noqa: BLE001 — lỗi store 1 tick không được giết thread
                continue
            for job in due:
                if not app.cron.mark_running(job.id):
                    continue  # overlap → skip
                try:
                    asyncio.run(app.chat(job.prompt, session_key=job.session_key))
                except Exception:  # noqa: BLE001 — job lỗi vẫn phải finish để không kẹt running
                    pass
                app.cron.finish(job.id, now=_t.time(),
                                next_run=compute_next(job.spec, _t.time(), job.tz))


def _hot_swap(httpd, chat_lock, center, rebuild) -> None:
    """Dựng App mới rồi swap httpd._app dưới chat_lock (không race chat/scheduler), đóng App cũ."""
    with chat_lock:
        new_app = rebuild()
        old = getattr(httpd, "_app", None)
        # Channel threads own credentials, pairing state, webhook routes, and polling mode.
        # They are not rebuilt by an App swap. Refuse to claim channel config was hot-applied
        # while old credentials remain live; the saved config takes effect after restart.
        if old is not None and new_app.cfg.channels != old.cfg.channels:
            new_app.close()
            raise RuntimeError("thay đổi channels cần khởi động lại yett")
        httpd._app = new_app
        if center is not None:
            new_app.set_approver(center.request)
        # Giữ kênh thông báo (Telegram/Zalo) sau hot-reload: App mới cũng cần notifier.
        notifier = getattr(httpd, "_notifier", None)
        if notifier is not None:
            new_app.set_notifier(notifier)
        if old is not None:
            old.close()


def _compose_notifier(httpd, notify_fn) -> None:
    """Gộp nhiều kênh notify (Telegram + Zalo) thành một notifier duy nhất cho App."""
    prev = getattr(httpd, "_notifier", None)
    if prev is None:
        setattr(httpd, "_notifier", notify_fn)
        return

    def _both(text: str) -> int:
        n = 0
        try:
            n += int(prev(text) or 0)
        except Exception:  # noqa: BLE001
            pass
        try:
            n += int(notify_fn(text) or 0)
        except Exception:  # noqa: BLE001
            pass
        return n

    setattr(httpd, "_notifier", _both)


def _start_telegram(httpd, app, lock, stop) -> None:
    """Bật kênh Telegram nếu config enabled: dựng channel, gắn notifier, chạy thread poll +
    (tùy chọn) thread briefing tự động. Token thiếu/không hợp lệ → cảnh báo, KHÔNG sập web."""
    import sys

    tg = app.cfg.channels.telegram
    if not tg.enabled:
        return
    from yett.channels.gating import ChannelGate
    from yett.channels.telegram import TelegramChannel, TelegramClient
    from yett.errors import YettError

    try:
        token = app._secrets.get(tg.token_secret)
    except (YettError, Exception) as e:  # noqa: BLE001 — token chưa đặt → tắt Telegram, web vẫn chạy
        print(f"[yett] Telegram bật nhưng chưa lấy được token ({tg.token_secret}): {e} — bỏ qua.",
              file=sys.stderr)
        return
    gate = ChannelGate(allowed_chat_ids=set(tg.allowed_chat_ids))
    channel = TelegramChannel(
        TelegramClient(token), gate,
        get_app=lambda: getattr(httpd, "_app", app), chat_lock=lock,
        pairing_code=tg.pairing_code, poll_timeout=tg.poll_timeout_sec,
    )
    _compose_notifier(httpd, channel.notify)
    app.set_notifier(getattr(httpd, "_notifier", None))
    threading.Thread(target=channel.run, args=(stop,), daemon=True).start()
    print(f"[yett] Telegram: bật (poll). Chat đã ghép: {len(gate.allowed_chat_ids)}.")
    if tg.briefing_hour is not None:
        threading.Thread(
            target=_run_briefing, args=(httpd, lock, channel, tg.briefing_hour, app.cfg.timezone, stop),
            daemon=True,
        ).start()
        print(f"[yett] Briefing tự động {tg.briefing_hour}h ({app.cfg.timezone}) → Telegram.")


def _start_zalo(httpd, app, lock, stop) -> None:
    """Bật kênh Zalo Official Bot API nếu config enabled. Fail-closed: webhook thiếu secret/HTTPS
    đã bị chặn ở pydantic load; token thiếu → cảnh báo, không sập web. Không gọi API thật trong test."""
    import sys

    zl = app.cfg.channels.zalo
    if not zl.enabled:
        return
    from yett.channels.gating import ChannelGate
    from yett.channels.zalo_bot import (
        ZaloChannel,
        ZaloClient,
        validate_zalo_runtime_cfg,
    )
    try:
        token = app._secrets.get(zl.token_secret)
    except Exception as e:  # noqa: BLE001 — token chưa đặt → tắt Zalo, web vẫn chạy
        print(f"[yett] Zalo bật nhưng chưa lấy được token ({zl.token_secret}): {e} — bỏ qua.",
              file=sys.stderr)
        return
    pairing_code = zl.pairing_code
    if zl.pairing_code_secret:
        try:
            pairing_code = app._secrets.get(zl.pairing_code_secret)
        except Exception as e:  # noqa: BLE001
            print(
                f"[yett] Zalo không lấy được pairing code ({zl.pairing_code_secret}): "
                f"{e} — tắt pairing.",
                file=sys.stderr,
            )
            pairing_code = ""
    webhook_secret = zl.webhook_secret
    if zl.mode == "webhook" and zl.webhook_secret_secret:
        try:
            webhook_secret = app._secrets.get(zl.webhook_secret_secret)
        except Exception as e:  # noqa: BLE001
            print(
                f"[yett] Zalo không lấy được webhook secret "
                f"({zl.webhook_secret_secret}): {e} — bỏ qua.",
                file=sys.stderr,
            )
            return
    cfg_err = validate_zalo_runtime_cfg(
        enabled=zl.enabled,
        mode=zl.mode,
        webhook_url=zl.webhook_url,
        webhook_secret=webhook_secret,
        webhook_path=zl.webhook_path,
        pairing_code=pairing_code,
    )
    if cfg_err:
        print(f"[yett] Zalo config fail-closed: {cfg_err} — bỏ qua.", file=sys.stderr)
        return
    gate = ChannelGate(allowed_chat_ids=set(zl.allowed_chat_ids))
    client = ZaloClient(
        token,
        http_timeout_sec=zl.http_timeout_sec,
        max_retries=zl.max_retries,
    )
    channel = ZaloChannel(
        client, gate,
        get_app=lambda: getattr(httpd, "_app", app), chat_lock=lock,
        pairing_code=pairing_code, poll_timeout=zl.poll_timeout_sec,
        webhook_secret=webhook_secret if zl.mode == "webhook" else "",
    )
    _compose_notifier(httpd, channel.notify)
    app.set_notifier(getattr(httpd, "_notifier", None))
    setattr(httpd, "_zalo_channel", channel)

    if zl.mode == "webhook":
        path = zl.webhook_path
        setattr(httpd, "_zalo_webhook", {"path": path, "channel": channel})
        try:
            client.set_webhook(zl.webhook_url, webhook_secret)
        except Exception as e:  # noqa: BLE001 — đăng ký webhook lỗi → không nhận inbound; web vẫn chạy
            print(f"[yett] Zalo setWebhook lỗi: {e} — webhook path vẫn lắng nghe local.",
                  file=sys.stderr)
        print(f"[yett] Zalo: bật (webhook {path}). Chat đã ghép: {len(gate.allowed_chat_ids)}.")
    else:
        try:
            client.delete_webhook()
        except Exception as e:  # noqa: BLE001
            # Official API says getUpdates cannot work while a webhook is configured.
            print(
                f"[yett] Zalo deleteWebhook lỗi: {e} — không khởi động poll.",
                file=sys.stderr,
            )
        else:
            threading.Thread(target=channel.run, args=(stop,), daemon=True).start()
            print(f"[yett] Zalo: bật (poll). Chat đã ghép: {len(gate.allowed_chat_ids)}.")

    if zl.briefing_hour is not None:
        threading.Thread(
            target=_run_briefing, args=(httpd, lock, channel, zl.briefing_hour, app.cfg.timezone, stop),
            daemon=True,
        ).start()
        print(f"[yett] Briefing tự động {zl.briefing_hour}h ({app.cfg.timezone}) → Zalo.")

    def _shutdown_zalo() -> None:
        channel.stop()
        # Keep webhook registration across normal restarts so Zalo can retry queued events.
        # Poll mode removes it explicitly at startup.

    hooks: list = getattr(httpd, "_channel_shutdown_hooks", None) or []
    hooks.append(_shutdown_zalo)
    setattr(httpd, "_channel_shutdown_hooks", hooks)


def _run_briefing(httpd, chat_lock, channel, hour, tz, stop) -> None:
    """Mỗi phút kiểm: tới giờ briefing & chưa gửi hôm nay → gửi briefing App hiện tại qua kênh."""
    from yett.channels.telegram import briefing_due, now_in_tz

    last_sent: str | None = None
    while not stop.wait(60.0):
        now = now_in_tz(tz)
        if not briefing_due(now, hour, last_sent):
            continue
        last_sent = now.strftime("%Y-%m-%d")
        with chat_lock:
            app = getattr(httpd, "_app", None)
            text = app.briefing() if app is not None else None
        if text:
            channel.notify(text)


def _watch_config(httpd, chat_lock, center, rebuild, config_path, stop) -> None:
    """Theo dõi mtime file config; operator sửa file mount (kể cả mount :ro từ host — :ro chỉ
    chặn CONTAINER ghi, vẫn đọc được thay đổi từ host) → hot-reload live, không cần restart,
    agent/web vẫn không ghi được config. Bỏ qua lần web tự lưu (đã cập nhật _cfg_mtime)."""
    while not stop.wait(3.0):
        try:
            m = os.path.getmtime(config_path)
        except OSError:
            continue
        if m != getattr(httpd, "_cfg_mtime", m):
            httpd._cfg_mtime = m
            try:
                _hot_swap(httpd, chat_lock, center, rebuild)
            except Exception:  # noqa: BLE001 — config sửa lỗi 1 lần không được giết watcher
                pass


def serve_forever(
    app: "App", *, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = False,
    config_path: str | None = None, rebuild=None,
) -> None:
    from yett.approvals import ApprovalCenter

    center = ApprovalCenter(default_timeout=float(app.cfg.security.approval_timeout_sec))
    httpd = serve(app, host=host, port=port, center=center, config_path=config_path,
                  rebuild=rebuild)
    url = f"http://{host}:{port}"
    print(f"[yett] Web UI: {url}  (Ctrl+C để dừng)")
    if open_browser:
        import webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    stop = threading.Event()
    lock = httpd._chat_lock  # type: ignore[attr-defined]
    sched = threading.Thread(target=_run_scheduler, args=(httpd, lock, stop), daemon=True)
    sched.start()
    _start_telegram(httpd, app, lock, stop)  # kênh Telegram + briefing tự động (nếu config bật)
    _start_zalo(httpd, app, lock, stop)      # kênh Zalo Official Bot API (poll hoặc webhook)
    if config_path and rebuild is not None:
        # Watcher: sửa file config (trên host, kể cả mount :ro) → hot-reload live trong prod.
        threading.Thread(
            target=_watch_config, args=(httpd, lock, center, rebuild, config_path, stop),
            daemon=True,
        ).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[yett] đã dừng.")
    finally:
        stop.set()
        for hook in list(getattr(httpd, "_channel_shutdown_hooks", []) or []):
            try:
                hook()
            except Exception:  # noqa: BLE001
                pass
        httpd.shutdown()
        httpd.server_close()
        getattr(httpd, "_app", app).close()  # đóng App hiện tại (có thể đã hot-reload swap)
