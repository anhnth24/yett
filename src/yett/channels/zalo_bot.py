"""Kênh Zalo Bot API chính thức (spec P3 §4) — KHÔNG phải Zalo Personal / unofficial.

Kiến trúc mirror TelegramChannel: long-poll (hoặc webhook) → gating/pairing → App.chat →
sendMessage. Transport inject được → test offline, không gọi mạng.

Official API assumptions (docs.zaloplatforms.com / bot-api.zaloplatforms.com — verified
from public docs + community SDKs mirroring the same surface; live credential calls NOT
made in this repo):

* Base URL: ``https://bot-api.zaloplatforms.com/bot{TOKEN}/{method}`` (POST, JSON body).
* ``getUpdates``: body ``{"timeout": "<seconds as string>"}``. Official docs list only
  ``timeout`` (no Telegram-style ``offset``). Response ``result`` is a **single** update
  object (not an array). ``getUpdates`` and webhook are mutually exclusive — call
  ``deleteWebhook`` before polling.
* Update shape: ``{"event_name": "message.text.received", "message": {...}}``.
  Message fields used here: ``message_id``, ``chat.id`` (string; may be hex-like),
  ``from.id``, ``text``. Other event types are ignored safely.
* ``sendMessage``: ``chat_id`` (string) + ``text`` (1..2000 chars).
* Webhook: ``setWebhook`` with ``url`` (HTTPS) + ``secret_token`` (8..256 chars).
  Zalo sends header ``X-Bot-Api-Secret-Token``.
* Local cursor: because official ``getUpdates`` has no documented offset, we dedupe by
  ``message_id`` (bounded set) so duplicate deliveries / webhook retries are fail-safe.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from yett.channels.gating import ChannelGate

# Official hard limit (sendMessage text length).
ZALO_TEXT_LIMIT = 2000
ZALO_API_HOST = "bot-api.zaloplatforms.com"
ZALO_API_BASE = f"https://{ZALO_API_HOST}/bot"
WEBHOOK_SECRET_HEADER = "X-Bot-Api-Secret-Token"
# Bound in-memory dedupe / webhook body / retries — DoS & memory safety.
DEFAULT_SEEN_CAP = 2048
DEFAULT_WEBHOOK_MAX_BYTES = 256 * 1024
DEFAULT_MAX_RETRIES = 2
DEFAULT_HTTP_TIMEOUT_SEC = 60.0
POLL_ERROR_BACKOFF_SEC = 3.0

# transport(url, params, *, timeout_sec) -> parsed JSON dict.
Transport = Callable[..., dict]


class ZaloApiError(Exception):
    """Lỗi API Zalo (ok=false hoặc HTTP lỗi). description đã redact token nếu có."""

    def __init__(
        self, message: str, *, error_code: int | None = None, retryable: bool = False
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable

    @property
    def is_polling_timeout(self) -> bool:
        # Community SDKs treat 408 as "no updates" on long-poll.
        return self.error_code == 408


def _redact(text: str, token: str) -> str:
    if not token:
        return text
    return text.replace(token, "[REDACTED_ZALO_TOKEN]")


def _urllib_transport(url: str, params: dict, *, timeout_sec: float = DEFAULT_HTTP_TIMEOUT_SEC) -> dict:
    data = json.dumps(params).encode("utf-8") if params else b"{}"
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as r:  # noqa: S310 — host cố định Zalo API
            result: dict = json.loads(r.read())
            return result
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:  # noqa: BLE001
            body = ""
        raise ZaloApiError(
            f"HTTP {e.code}: {body or e.reason}", error_code=e.code, retryable=e.code >= 500
        ) from None
    except (TimeoutError, socket.timeout) as e:
        raise ZaloApiError(f"timeout: {e}", error_code=408, retryable=True) from None
    except OSError as e:
        raise ZaloApiError(f"network: {e}", retryable=True) from None


def normalize_inbound_text(text: str, *, max_len: int = ZALO_TEXT_LIMIT) -> str:
    """Chuẩn hoá text inbound: strip + cắt trần độ dài (tránh payload quá lớn vào App.chat)."""
    t = (text or "").replace("\x00", "").strip()
    if len(t) > max_len:
        return t[:max_len]
    return t


def chunk_outbound_text(text: str, *, limit: int = ZALO_TEXT_LIMIT) -> list[str]:
    """Chia text outbound theo giới hạn 2000 ký tự của sendMessage."""
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def extract_update(payload: Any) -> dict[str, Any] | None:
    """Nhận webhook body hoặc getUpdates result → update dict hoặc None (malformed)."""
    if not isinstance(payload, dict):
        return None
    data: dict[str, Any] = payload
    # getUpdates wraps: {"ok": true, "result": {event_name, message}}
    nested = data.get("result")
    if "event_name" not in data and isinstance(nested, dict):
        data = nested
    if not isinstance(data.get("event_name"), str):
        return None
    return data


def chat_id_from_update(upd: dict[str, Any]) -> str:
    raw_msg = upd.get("message")
    msg: dict[str, Any] = raw_msg if isinstance(raw_msg, dict) else {}
    raw_chat = msg.get("chat")
    chat: dict[str, Any] = raw_chat if isinstance(raw_chat, dict) else {}
    return str(chat.get("id", "")).strip()


def message_id_from_update(upd: dict[str, Any]) -> str:
    raw_msg = upd.get("message")
    msg: dict[str, Any] = raw_msg if isinstance(raw_msg, dict) else {}
    mid = msg.get("message_id")
    return str(mid).strip() if mid is not None else ""


def text_from_update(upd: dict[str, Any]) -> str:
    if upd.get("event_name") != "message.text.received":
        return ""
    raw_msg = upd.get("message")
    msg: dict[str, Any] = raw_msg if isinstance(raw_msg, dict) else {}
    return normalize_inbound_text(str(msg.get("text") or ""))

@dataclass(frozen=True)
class WebhookResult:
    status: int
    ok: bool
    error: str = ""


class ZaloClient:
    """Wrapper mỏng Official Bot API. Transport inject; retry/timeout/redact token."""

    def __init__(
        self,
        token: str,
        *,
        transport: Transport | None = None,
        http_timeout_sec: float = DEFAULT_HTTP_TIMEOUT_SEC,
        max_retries: int = DEFAULT_MAX_RETRIES,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if not (token or "").strip():
            raise ValueError("Zalo bot token rỗng")
        self._token = token.strip()
        self._transport = transport or _urllib_transport
        self._http_timeout_sec = http_timeout_sec
        self._max_retries = max(0, max_retries)
        self._sleep = sleep or time.sleep

    def _url(self, method: str) -> str:
        return f"{ZALO_API_BASE}{self._token}/{method}"

    def _call(self, method: str, params: dict | None = None) -> dict:
        url = self._url(method)
        body = params if params is not None else {}
        last: Exception | None = None
        attempts = self._max_retries + 1
        for attempt in range(attempts):
            try:
                raw = self._transport(url, body, timeout_sec=self._http_timeout_sec)
                if not isinstance(raw, dict):
                    raise ZaloApiError("malformed API response (not object)")
                if raw.get("ok", True) is False:
                    code = raw.get("error_code")
                    code_i = int(code) if isinstance(code, int) else None
                    desc = _redact(str(raw.get("description") or "ok=false"), self._token)
                    # 408 on getUpdates = empty poll, not a hard failure for caller.
                    raise ZaloApiError(
                        desc,
                        error_code=code_i,
                        retryable=bool(code_i is not None and code_i >= 500),
                    )
                return raw
            except ZaloApiError as e:
                last = ZaloApiError(
                    _redact(str(e), self._token),
                    error_code=e.error_code,
                    retryable=e.retryable,
                )
                if e.is_polling_timeout or not e.retryable or attempt + 1 >= attempts:
                    raise last from None
                self._sleep(0.05 * (2**attempt))
            except Exception as e:  # noqa: BLE001 — bọc lỗi lạ, redact token
                last = ZaloApiError(_redact(str(e), self._token), retryable=True)
                if attempt + 1 >= attempts:
                    raise last from None
                self._sleep(0.05 * (2**attempt))
        raise last or ZaloApiError("unknown API failure")

    def get_updates(self, *, timeout: int = 30) -> dict | None:
        """Long-poll một update. Trả update dict hoặc None (hết/timeout/rỗng)."""
        try:
            # Official docs: timeout is a string. No documented offset parameter.
            r = self._call("getUpdates", {"timeout": str(int(timeout))})
        except ZaloApiError as e:
            if e.is_polling_timeout:
                return None
            raise
        result = r.get("result")
        if result is None:
            return None
        if isinstance(result, list):
            # Defensive: some proxies may array-wrap; take first object only (bounded).
            result = result[0] if result and isinstance(result[0], dict) else None
        if not isinstance(result, dict):
            return None
        return extract_update(result)

    def send_message(self, chat_id: str, text: str) -> dict:
        return self._call("sendMessage", {"chat_id": str(chat_id), "text": text})

    def delete_webhook(self) -> dict:
        return self._call("deleteWebhook", {})

    def set_webhook(self, url: str, secret_token: str) -> dict:
        return self._call("setWebhook", {"url": url, "secret_token": secret_token})


class ZaloChannel:
    """Poll/webhook + gating + dispatch. get_app() trả App hiện tại (hot-reload an toàn)."""

    def __init__(
        self,
        client: ZaloClient,
        gate: ChannelGate,
        *,
        get_app: Callable[[], Any],
        chat_lock: Any,
        pairing_code: str = "",
        run_chat: Callable[[str, str], str] | None = None,
        poll_timeout: int = 30,
        seen_cap: int = DEFAULT_SEEN_CAP,
        webhook_secret: str = "",
        webhook_max_bytes: int = DEFAULT_WEBHOOK_MAX_BYTES,
    ) -> None:
        self._client = client
        self._gate = gate
        self._get_app = get_app
        self._lock = chat_lock
        self._pairing_code = pairing_code
        self._run_chat = run_chat or self._default_run_chat
        self._poll_timeout = poll_timeout
        self._seen_cap = max(1, seen_cap)
        self._webhook_secret = webhook_secret
        self._webhook_max_bytes = max(1024, webhook_max_bytes)
        # Local cursor: ordered message_ids already processed (official API has no offset).
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._processed = 0  # monotonic local cursor for tests/observability
        self._stopped = False

    @property
    def processed_cursor(self) -> int:
        """Số update đã xử lý (local cursor; không phải Telegram offset)."""
        return self._processed

    def _default_run_chat(self, text: str, chat_id: str) -> str:
        import asyncio

        with self._lock:
            app = self._get_app()
            res = asyncio.run(app.chat(text, session_key=f"zalo:{chat_id}"))
        return res.text or "(không có nội dung)"

    def _mark_seen(self, message_id: str) -> bool:
        """True nếu message_id MỚI (chưa thấy). Bounded OrderedDict = cursor cửa sổ."""
        if not message_id:
            # Không có id → vẫn xử lý nhưng không dedupe được; dùng synthetic cursor key.
            message_id = f"anon:{self._processed}"
        if message_id in self._seen:
            return False
        self._seen[message_id] = None
        while len(self._seen) > self._seen_cap:
            self._seen.popitem(last=False)
        return True

    def handle_update(self, upd: dict | None) -> bool:
        """Xử lý một update. Trả True nếu đã consume (kể cả skip duplicate/malformed)."""
        if not isinstance(upd, dict):
            return False
        parsed = extract_update(upd)
        if parsed is None:
            return False
        mid = message_id_from_update(parsed)
        if mid and not self._mark_seen(mid):
            return True  # duplicate — advance local understanding, no re-dispatch
        if not mid:
            self._mark_seen("")  # advance anon cursor
        self._processed += 1

        chat_id = chat_id_from_update(parsed)
        text = text_from_update(parsed)
        if not chat_id or not text:
            return True  # malformed/unsupported event: consume, no reply
        if not self._gate.is_allowed(chat_id):
            if self._pairing_code and text == self._pairing_code:
                self._gate.allowed_chat_ids.add(chat_id)
                self._safe_send(chat_id, "Đã ghép! Từ giờ anh nhắn trực tiếp cho yett ở đây.")
            else:
                self._safe_send(chat_id, "Chưa được cấp quyền. Gửi đúng mã ghép để dùng yett.")
            return True
        try:
            reply = self._run_chat(text, chat_id)
        except Exception as e:  # noqa: BLE001 — lỗi 1 turn không được giết vòng poll
            reply = f"[lỗi xử lý] {e}"
        self._safe_send(chat_id, reply)
        return True

    def _safe_send(self, chat_id: str, text: str) -> None:
        for chunk in chunk_outbound_text(text or ""):
            try:
                self._client.send_message(chat_id, chunk)
            except Exception:  # noqa: BLE001 — gửi lỗi không được giết vòng poll
                return

    def poll_once(self) -> int:
        """Lấy tối đa 1 update (API trả single object), xử lý, cập nhật cursor. Trả 0/1."""
        upd = self._client.get_updates(timeout=self._poll_timeout)
        if upd is None:
            return 0
        self.handle_update(upd)
        return 1

    def run(self, stop: Any) -> None:
        """Vòng poll tới khi stop.set() hoặc stop(). Lỗi mạng → nghỉ ngắn rồi thử lại."""
        while not self._stopped and not _stop_is_set(stop):
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 — mạng chập chờn không được giết thread
                if _stop_is_set(stop) or self._stopped:
                    break
                _stop_wait(stop, POLL_ERROR_BACKOFF_SEC)

    def stop(self) -> None:
        """Fail-closed shutdown: vòng run thoát ở lần lặp kế; webhook từ chối xử lý mới."""
        self._stopped = True

    def notify(self, text: str) -> int:
        """Đẩy tin cho mọi chat đã ghép. Trả số chat đã gửi (không đếm chunk)."""
        sent = 0
        for cid in sorted(self._gate.allowed_chat_ids):
            before = sent
            try:
                for chunk in chunk_outbound_text(text or ""):
                    self._client.send_message(cid, chunk)
                sent += 1
            except Exception:  # noqa: BLE001
                if sent == before:
                    continue
        return sent

    def handle_webhook(
        self,
        headers: Mapping[str, str],
        body: bytes,
    ) -> WebhookResult:
        """Xử lý 1 webhook POST (bounded). Secret sai → 401; body lỗi → 400; dừng → 503."""
        if self._stopped:
            return WebhookResult(503, False, "shutting down")
        if not self._webhook_secret:
            return WebhookResult(503, False, "webhook not configured")
        # Header lookup case-insensitive (HTTP headers).
        got = ""
        for k, v in headers.items():
            if k.lower() == WEBHOOK_SECRET_HEADER.lower():
                got = v
                break
        if got != self._webhook_secret:
            return WebhookResult(401, False, "unauthorized")
        if len(body) > self._webhook_max_bytes:
            return WebhookResult(413, False, "payload too large")
        try:
            raw = json.loads(body.decode("utf-8") if body else b"{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return WebhookResult(400, False, "invalid json")
        upd = extract_update(raw)
        if upd is None:
            return WebhookResult(400, False, "invalid update")
        self.handle_update(upd)
        return WebhookResult(200, True)


def _stop_is_set(stop: Any) -> bool:
    if stop is None:
        return False
    if isinstance(stop, bool):
        return stop
    is_set = getattr(stop, "is_set", None)
    return bool(is_set()) if callable(is_set) else bool(stop)


def _stop_wait(stop: Any, seconds: float) -> None:
    wait = getattr(stop, "wait", None)
    if callable(wait):
        wait(seconds)
    else:
        time.sleep(seconds)


def validate_zalo_runtime_cfg(
    *,
    enabled: bool,
    mode: str,
    webhook_url: str,
    webhook_secret: str,
) -> str | None:
    """Fail-closed preflight. Trả message lỗi hoặc None nếu OK / kênh tắt."""
    if not enabled:
        return None
    if mode not in ("poll", "webhook"):
        return f"channels.zalo.mode không hợp lệ: {mode!r} (poll|webhook)"
    if mode == "webhook":
        if not webhook_url.startswith("https://"):
            return "channels.zalo.webhook_url phải là HTTPS khi mode=webhook"
        try:
            u = urlparse(webhook_url)
            if not u.hostname:
                return "channels.zalo.webhook_url thiếu hostname"
        except Exception:  # noqa: BLE001
            return "channels.zalo.webhook_url không parse được"
        n = len(webhook_secret or "")
        if n < 8 or n > 256:
            return "channels.zalo.webhook_secret phải 8–256 ký tự khi mode=webhook"
    return None
