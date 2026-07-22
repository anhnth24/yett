"""Kênh Zalo Bot API chính thức (spec P3 §4) — KHÔNG phải Zalo Personal / unofficial.

Kiến trúc mirror TelegramChannel: long-poll (hoặc webhook) → gating/pairing → App.chat →
sendMessage. Transport inject được → test offline, không gọi mạng.

Official API assumptions (docs.zaloplatforms.com / bot-api.zaloplatforms.com — verified
from public docs + community SDKs mirroring the same surface; live credential calls NOT
made in this repo):

* Base URL: ``https://bot-api.zaloplatforms.com/bot{TOKEN}/{method}`` (POST, JSON body).
* ``getUpdates``: official parameter table declares ``timeout`` as String while its code
  samples send a JSON number. This adapter follows the declared type and community
  implementations with ``{"timeout": "<seconds>"}``; live acceptance is unverified.
  There is no documented Telegram-style ``offset``. Response ``result`` is a **single**
  update object (not an array). ``getUpdates`` and webhook are mutually exclusive — call
  ``deleteWebhook`` before polling.
* Webhook/update envelope: ``{"ok": true, "result": {"event_name": ..., "message": ...}}``.
  ``getUpdates.result`` is the update itself. Message fields used here are
  ``message_id``, ``chat.id`` (strings), ``from.id``, and ``text``. Other event types
  are ignored safely. The webhook sample includes ``message_id`` although the prose
  table omits it; actionable text without one is rejected because it cannot be deduped.
* ``sendMessage``: ``chat_id`` (string) + ``text`` (1..2000 chars). The docs do not
  define whether astral Unicode characters count as one code point or two UTF-16 units,
  so outbound chunks conservatively stay within 2000 UTF-16 units.
* Webhook: ``setWebhook`` with ``url`` (HTTPS) + ``secret_token`` (8..256 chars).
  Zalo sends header ``X-Bot-Api-Secret-Token``.
* Local cursor: because official ``getUpdates`` has no documented offset, we dedupe by
  ``(chat.id, message_id)`` in a bounded in-memory window. This gives at-most-once
  dispatch only within one process lifetime; the public docs do not specify replay
  retention or an idempotency key.
* The public docs do not document 429 bodies, ``Retry-After``, request idempotency, or
  webhook retry timing. HTTP ``Retry-After`` is honored defensively. ``sendMessage`` is
  retried only after an explicit 429, never after ambiguous network/5xx failures.
"""

from __future__ import annotations

import hmac
import json
import socket
import threading
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
DEFAULT_API_MAX_BYTES = 1024 * 1024
POLL_HTTP_GRACE_SEC = 5.0
POLL_ERROR_BACKOFF_SEC = 3.0
EMPTY_POLL_BACKOFF_SEC = 0.1
RETRY_BACKOFF_BASE_SEC = 0.5
RETRY_BACKOFF_MAX_SEC = 30.0
MAX_ID_LENGTH = 256
PAIRING_MAX_FAILURES = 5
PAIRING_LOCKOUT_SEC = 300.0
PAIRING_STATE_CAP = 2048

# transport(url, params, *, timeout_sec) -> parsed JSON dict.
Transport = Callable[..., dict]


class ZaloApiError(Exception):
    """Lỗi API Zalo (ok=false hoặc HTTP lỗi). description đã redact token nếu có."""

    def __init__(
        self,
        message: str,
        *,
        error_code: int | None = None,
        retryable: bool = False,
        retry_after_sec: float | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable
        self.retry_after_sec = retry_after_sec

    @property
    def is_polling_timeout(self) -> bool:
        # Community SDKs treat 408 as "no updates" on long-poll.
        return self.error_code == 408


def _redact(text: str, *secrets: str) -> str:
    result = text
    for secret in secrets:
        if secret:
            result = result.replace(secret, "[REDACTED_ZALO_SECRET]")
    return result[:1000]


def _retry_after_from_headers(headers: Any) -> float | None:
    value = headers.get("Retry-After") if headers is not None else None
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return min(max(seconds, 0.0), RETRY_BACKOFF_MAX_SEC)


def _urllib_transport(
    url: str, params: dict, *, timeout_sec: float = DEFAULT_HTTP_TIMEOUT_SEC
) -> dict:
    data = json.dumps(params, allow_nan=False).encode("utf-8") if params else b"{}"
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as r:  # noqa: S310 — host cố định Zalo API
            declared = r.headers.get("Content-Length")
            if declared is not None and int(declared) > DEFAULT_API_MAX_BYTES:
                raise ZaloApiError("API response too large")
            raw_body = r.read(DEFAULT_API_MAX_BYTES + 1)
            if len(raw_body) > DEFAULT_API_MAX_BYTES:
                raise ZaloApiError("API response too large")
            result: dict = json.loads(raw_body)
            return result
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read(501).decode("utf-8", errors="replace")[:500]
        except Exception:  # noqa: BLE001
            body = ""
        raise ZaloApiError(
            f"HTTP {e.code}: {body or e.reason}",
            error_code=e.code,
            retryable=e.code == 429 or e.code >= 500,
            retry_after_sec=_retry_after_from_headers(e.headers),
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


def normalize_zalo_id(value: Any) -> str:
    """Validate an official string ID without coercing bool/number/object identities."""
    if not isinstance(value, str):
        return ""
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > MAX_ID_LENGTH
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in normalized)
    ):
        return ""
    return normalized


def _utf16_units(text: str) -> int:
    return sum(2 if ord(char) > 0xFFFF else 1 for char in text)


def chunk_outbound_text(text: str, *, limit: int = ZALO_TEXT_LIMIT) -> list[str]:
    """Chunk conservatively by UTF-16 units without splitting a Python code point."""
    if not text or limit < 1:
        return []
    chunks: list[str] = []
    start = 0
    units = 0
    for index, char in enumerate(text):
        char_units = 2 if ord(char) > 0xFFFF else 1
        if units and units + char_units > limit:
            chunks.append(text[start:index])
            start = index
            units = 0
        units += char_units
    chunks.append(text[start:])
    return chunks


def extract_update(payload: Any) -> dict[str, Any] | None:
    """Nhận webhook body hoặc getUpdates result → update dict hoặc None (malformed)."""
    if not isinstance(payload, dict):
        return None
    data: dict[str, Any] = payload
    if "ok" in data and data.get("ok") is not True:
        return None
    # Official webhook wraps: {"ok": true, "result": {event_name, message}}.
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
    return normalize_zalo_id(chat.get("id"))


def message_id_from_update(upd: dict[str, Any]) -> str:
    raw_msg = upd.get("message")
    msg: dict[str, Any] = raw_msg if isinstance(raw_msg, dict) else {}
    return normalize_zalo_id(msg.get("message_id"))


def sender_id_from_update(upd: dict[str, Any]) -> str:
    raw_msg = upd.get("message")
    msg: dict[str, Any] = raw_msg if isinstance(raw_msg, dict) else {}
    raw_from = msg.get("from")
    sender: dict[str, Any] = raw_from if isinstance(raw_from, dict) else {}
    return normalize_zalo_id(sender.get("id"))


def chat_type_from_update(upd: dict[str, Any]) -> str:
    raw_msg = upd.get("message")
    msg: dict[str, Any] = raw_msg if isinstance(raw_msg, dict) else {}
    raw_chat = msg.get("chat")
    chat: dict[str, Any] = raw_chat if isinstance(raw_chat, dict) else {}
    value = chat.get("chat_type")
    return value.strip().upper() if isinstance(value, str) else ""


def is_bot_update(upd: dict[str, Any]) -> bool:
    raw_msg = upd.get("message")
    msg: dict[str, Any] = raw_msg if isinstance(raw_msg, dict) else {}
    raw_from = msg.get("from")
    sender: dict[str, Any] = raw_from if isinstance(raw_from, dict) else {}
    return sender.get("is_bot") is True


def text_from_update(upd: dict[str, Any]) -> str:
    if upd.get("event_name") != "message.text.received":
        return ""
    raw_msg = upd.get("message")
    msg: dict[str, Any] = raw_msg if isinstance(raw_msg, dict) else {}
    text = msg.get("text")
    return normalize_inbound_text(text) if isinstance(text, str) else ""


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

    @staticmethod
    def _error_code(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return None

    @staticmethod
    def _api_retry_after(raw: dict[str, Any]) -> float | None:
        # Not documented by Zalo. Accept common shapes defensively without depending on them.
        parameters = raw.get("parameters")
        nested = parameters.get("retry_after") if isinstance(parameters, dict) else None
        value = raw.get("retry_after", nested)
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            return None
        return min(max(seconds, 0.0), RETRY_BACKOFF_MAX_SEC)

    def _call(
        self,
        method: str,
        params: dict | None = None,
        *,
        timeout_sec: float | None = None,
        retry_transient: bool = False,
        polling: bool = False,
        sensitive_values: tuple[str, ...] = (),
    ) -> dict:
        url = self._url(method)
        body = params if params is not None else {}
        secrets = (self._token, *sensitive_values)
        last: ZaloApiError | None = None
        attempts = self._max_retries + 1
        for attempt in range(attempts):
            try:
                raw = self._transport(
                    url, body, timeout_sec=timeout_sec or self._http_timeout_sec
                )
                if not isinstance(raw, dict):
                    raise ZaloApiError("malformed API response (not object)")
                if raw.get("ok") is not True:
                    if "ok" not in raw:
                        raise ZaloApiError("malformed API response (missing ok=true)")
                    code_i = self._error_code(raw.get("error_code"))
                    desc = _redact(
                        str(raw.get("description") or "ok=false"), *secrets
                    )
                    raise ZaloApiError(
                        desc,
                        error_code=code_i,
                        retryable=bool(
                            code_i is not None
                            and (code_i == 408 or code_i == 429 or code_i >= 500)
                        ),
                        retry_after_sec=self._api_retry_after(raw),
                    )
                return raw
            except ZaloApiError as e:
                last = ZaloApiError(
                    _redact(str(e), *secrets),
                    error_code=e.error_code,
                    retryable=e.retryable,
                    retry_after_sec=e.retry_after_sec,
                )
                explicit_rate_limit = e.error_code == 429
                can_retry = e.retryable and (explicit_rate_limit or retry_transient)
                if (
                    (polling and e.is_polling_timeout)
                    or not can_retry
                    or attempt + 1 >= attempts
                ):
                    raise last from None
                delay = e.retry_after_sec
                if delay is None:
                    delay = min(
                        RETRY_BACKOFF_BASE_SEC * (2**attempt), RETRY_BACKOFF_MAX_SEC
                    )
                self._sleep(delay)
            except Exception as e:  # noqa: BLE001 — bọc lỗi lạ, redact token
                last = ZaloApiError(_redact(str(e), *secrets), retryable=True)
                if not retry_transient or attempt + 1 >= attempts:
                    raise last from None
                self._sleep(
                    min(RETRY_BACKOFF_BASE_SEC * (2**attempt), RETRY_BACKOFF_MAX_SEC)
                )
        raise last or ZaloApiError("unknown API failure")

    def get_updates(self, *, timeout: int = 30) -> dict | None:
        """Long-poll một update. Trả update dict hoặc None (hết/timeout/rỗng)."""
        try:
            # Official docs: timeout is a string. No documented offset parameter.
            poll_timeout = max(0, int(timeout))
            r = self._call(
                "getUpdates",
                {"timeout": str(poll_timeout)},
                timeout_sec=max(
                    self._http_timeout_sec, poll_timeout + POLL_HTTP_GRACE_SEC
                ),
                retry_transient=True,
                polling=True,
            )
        except ZaloApiError as e:
            if e.is_polling_timeout:
                return None
            raise
        result = r.get("result")
        if result is None:
            return None
        if isinstance(result, list):
            # Official result is one object. Taking element zero would silently lose a batch.
            raise ZaloApiError("malformed getUpdates result (array is not supported)")
        if not isinstance(result, dict):
            raise ZaloApiError("malformed getUpdates result (not object)")
        update = extract_update(result)
        if update is None and result:
            raise ZaloApiError("malformed getUpdates result (invalid update)")
        return update

    def send_message(self, chat_id: str, text: str) -> dict:
        normalized_chat_id = normalize_zalo_id(chat_id)
        if not normalized_chat_id:
            raise ValueError("Zalo chat_id không hợp lệ")
        if (
            not isinstance(text, str)
            or not text
            or _utf16_units(text) > ZALO_TEXT_LIMIT
        ):
            raise ValueError("Zalo text phải dài 1..2000 ký tự")
        # Retrying an ambiguous network/5xx failure can duplicate a message. Only _call's
        # explicit 429 path retries sendMessage.
        return self._call(
            "sendMessage", {"chat_id": normalized_chat_id, "text": text}
        )

    def delete_webhook(self) -> dict:
        return self._call("deleteWebhook", {}, retry_transient=True)

    def set_webhook(self, url: str, secret_token: str) -> dict:
        return self._call(
            "setWebhook",
            {"url": url, "secret_token": secret_token},
            retry_transient=True,
            sensitive_values=(secret_token,),
        )


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
        monotonic: Callable[[], float] | None = None,
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
        self._monotonic = monotonic or time.monotonic
        # Local cursor: ordered message_ids already processed (official API has no offset).
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._state_lock = threading.RLock()
        self._webhook_slot = threading.BoundedSemaphore(1)
        self._pairing_failures: OrderedDict[str, tuple[int, float]] = OrderedDict()
        self._processed = 0  # monotonic local cursor for tests/observability
        self._stopped = False

    @property
    def processed_cursor(self) -> int:
        """Số update đã xử lý (local cursor; không phải Telegram offset)."""
        with self._state_lock:
            return self._processed

    @property
    def webhook_max_bytes(self) -> int:
        return self._webhook_max_bytes

    def _default_run_chat(self, text: str, chat_id: str) -> str:
        import asyncio

        with self._lock:
            app = self._get_app()
            res = asyncio.run(app.chat(text, session_key=f"zalo:{chat_id}"))
        return res.text or "(không có nội dung)"

    def _mark_seen(self, chat_id: str, message_id: str) -> bool:
        """Atomically claim a new update inside the bounded process-local replay window."""
        key = (chat_id, message_id)
        with self._state_lock:
            if key in self._seen:
                return False
            self._seen[key] = None
            while len(self._seen) > self._seen_cap:
                self._seen.popitem(last=False)
            self._processed += 1
            return True

    def _pair(self, chat_id: str, text: str, chat_type: str) -> bool:
        """Rate-limit bearer-code pairing and never auto-pair a group conversation."""
        if not self._pairing_code or chat_type != "PRIVATE":
            return False
        now = self._monotonic()
        with self._state_lock:
            count, locked_until = self._pairing_failures.get(chat_id, (0, 0.0))
            if locked_until > now:
                return False
            matches = hmac.compare_digest(
                text.encode("utf-8"), self._pairing_code.encode("utf-8")
            )
            if matches:
                self._pairing_failures.pop(chat_id, None)
                self._gate.allowed_chat_ids.add(chat_id)
                return True
            count += 1
            locked_until = (
                now + PAIRING_LOCKOUT_SEC if count >= PAIRING_MAX_FAILURES else 0.0
            )
            self._pairing_failures[chat_id] = (count, locked_until)
            self._pairing_failures.move_to_end(chat_id)
            while len(self._pairing_failures) > PAIRING_STATE_CAP:
                self._pairing_failures.popitem(last=False)
            return False

    def handle_update(self, upd: dict | None) -> bool:
        """Xử lý một update. Trả True nếu đã consume (kể cả skip duplicate/malformed)."""
        with self._state_lock:
            if self._stopped:
                return False
        if not isinstance(upd, dict):
            return False
        parsed = extract_update(upd)
        if parsed is None:
            return False
        mid = message_id_from_update(parsed)
        chat_id = chat_id_from_update(parsed)
        sender_id = sender_id_from_update(parsed)
        chat_type = chat_type_from_update(parsed)
        text = text_from_update(parsed)
        if is_bot_update(parsed):
            return True
        # Actionable text without a stable message_id cannot be replay-protected. Fail closed.
        if text and (
            not chat_id
            or not sender_id
            or not mid
            or chat_type not in {"PRIVATE", "GROUP"}
        ):
            return True
        if mid and not self._mark_seen(chat_id, mid):
            return True
        if not mid:
            with self._state_lock:
                self._processed += 1
        if not chat_id or not text:
            return True  # malformed/unsupported event: consume, no reply
        if not self._gate.is_allowed(chat_id):
            if self._pair(chat_id, text, chat_type):
                self._safe_send(
                    chat_id,
                    "Đã ghép cho phiên chạy hiện tại! Anh có thể nhắn trực tiếp cho yett.",
                )
            else:
                self._safe_send(
                    chat_id,
                    "Chưa được cấp quyền. Chỉ ghép trong chat riêng bằng mã hợp lệ.",
                )
            return True
        try:
            reply = self._run_chat(text, chat_id)
        except Exception:  # noqa: BLE001 — lỗi 1 turn không được giết vòng poll
            # Internal/provider exceptions can contain credentials or infrastructure details.
            reply = "[lỗi xử lý] Không thể hoàn tất yêu cầu."
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
        with self._state_lock:
            if self._stopped:
                return 0
        return 1 if self.handle_update(upd) else 0

    def run(self, stop: Any) -> None:
        """Vòng poll tới khi stop.set() hoặc stop(). Lỗi mạng → nghỉ ngắn rồi thử lại."""
        while not self._stopped and not _stop_is_set(stop):
            try:
                if self.poll_once() == 0:
                    _stop_wait(stop, EMPTY_POLL_BACKOFF_SEC)
            except Exception:  # noqa: BLE001 — mạng chập chờn không được giết thread
                if _stop_is_set(stop) or self._stopped:
                    break
                _stop_wait(stop, POLL_ERROR_BACKOFF_SEC)

    def stop(self) -> None:
        """Fail-closed shutdown: vòng run thoát ở lần lặp kế; webhook từ chối xử lý mới."""
        with self._state_lock:
            self._stopped = True

    def notify(self, text: str) -> int:
        """Đẩy tin cho mọi chat đã ghép. Trả số chat đã gửi (không đếm chunk)."""
        chunks = chunk_outbound_text(text or "")
        if not chunks:
            return 0
        sent = 0
        with self._state_lock:
            chat_ids = sorted(self._gate.allowed_chat_ids)
        for cid in chat_ids:
            try:
                for chunk in chunks:
                    self._client.send_message(cid, chunk)
                sent += 1
            except Exception:  # noqa: BLE001
                continue
        return sent

    def authorize_webhook(self, headers: Mapping[str, str]) -> bool:
        """Constant-time auth; duplicate secret headers are rejected as ambiguous."""
        if not self._webhook_secret:
            return False
        values = [
            value
            for key, value in headers.items()
            if key.lower() == WEBHOOK_SECRET_HEADER.lower() and isinstance(value, str)
        ]
        if len(values) != 1:
            return False
        return hmac.compare_digest(
            values[0].encode("utf-8"), self._webhook_secret.encode("utf-8")
        )

    def handle_webhook(
        self,
        headers: Mapping[str, str],
        body: bytes,
    ) -> WebhookResult:
        """Xử lý 1 webhook POST (bounded). Secret sai → 401; body lỗi → 400; dừng → 503."""
        with self._state_lock:
            if self._stopped:
                return WebhookResult(503, False, "shutting down")
        if not self._webhook_secret:
            return WebhookResult(503, False, "webhook not configured")
        if not self.authorize_webhook(headers):
            return WebhookResult(401, False, "unauthorized")
        if len(body) > self._webhook_max_bytes:
            return WebhookResult(413, False, "payload too large")
        if not self._webhook_slot.acquire(blocking=False):
            return WebhookResult(503, False, "webhook busy")
        try:
            try:
                raw = json.loads(body.decode("utf-8") if body else "{}")
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                return WebhookResult(400, False, "invalid json")
            upd = extract_update(raw)
            if upd is None:
                return WebhookResult(400, False, "invalid update")
            if not self.handle_update(upd):
                with self._state_lock:
                    if self._stopped:
                        return WebhookResult(503, False, "shutting down")
                return WebhookResult(400, False, "invalid update")
            return WebhookResult(200, True)
        finally:
            self._webhook_slot.release()


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
    webhook_path: str = "/api/channels/zalo/webhook",
    pairing_code: str = "",
) -> str | None:
    """Fail-closed preflight. Trả message lỗi hoặc None nếu OK / kênh tắt."""
    if not enabled:
        return None
    if mode not in ("poll", "webhook"):
        return f"channels.zalo.mode không hợp lệ: {mode!r} (poll|webhook)"
    if pairing_code and (
        not 8 <= len(pairing_code) <= 256
        or pairing_code != pairing_code.strip()
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in pairing_code)
    ):
        return "channels.zalo pairing code phải 8–256 ký tự hợp lệ"
    if mode == "webhook":
        try:
            u = urlparse(webhook_url)
            if u.scheme != "https" or not u.hostname:
                return "channels.zalo.webhook_url phải là HTTPS và có hostname"
            if u.username or u.password or u.query or u.fragment:
                return "channels.zalo.webhook_url không được chứa credential/query/fragment"
        except Exception:  # noqa: BLE001
            return "channels.zalo.webhook_url không parse được"
        if (
            not webhook_path.startswith("/")
            or webhook_path == "/"
            or "?" in webhook_path
            or "#" in webhook_path
        ):
            return "channels.zalo.webhook_path phải là absolute path riêng"
        n = len(webhook_secret or "")
        if (
            n < 8
            or n > 256
            or webhook_secret != webhook_secret.strip()
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in webhook_secret)
        ):
            return "channels.zalo.webhook_secret phải 8–256 ký tự khi mode=webhook"
    return None
