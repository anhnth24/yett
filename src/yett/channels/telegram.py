"""Kênh Telegram (spec P3 §4): nhắn tin với yett như trợ lý.

Long-polling (bot tự gọi api.telegram.org) → KHÔNG cần mở port vào máy, hợp on-prem sau
firewall (chỉ cần outbound HTTPS + thêm api.telegram.org vào egress.allowlist).

Bảo mật: chỉ chat_id trong allowlist mới sai khiến agent (ChannelGate). Người lạ gửi đúng
pairing_code → tự ghép. Cùng core, cùng Policy Gate — kênh chỉ là entry adapter.

Transport inject được (http) → test offline, không gọi mạng (giữ nguyên tắc no-egress test).
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from yett.channels.gating import ChannelGate

# transport(url, params) -> dict JSON đã parse. Tách ra để test inject.
Transport = Callable[[str, dict], dict]


def _urllib_transport(url: str, params: dict) -> dict:
    data = json.dumps(params).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 — URL cố định api.telegram.org
        result: dict = json.loads(r.read())
        return result


class TelegramClient:
    """Wrapper mỏng Bot API. Chỉ 2 method cần: getUpdates (nhận) + sendMessage (gửi)."""

    def __init__(self, token: str, *, transport: Transport | None = None) -> None:
        self._token = token
        self._transport = transport or _urllib_transport

    def _call(self, method: str, params: dict) -> dict:
        return self._transport(f"https://api.telegram.org/bot{self._token}/{method}", params)

    def get_updates(self, offset: int, *, timeout: int = 25) -> list[dict]:
        r = self._call("getUpdates", {"offset": offset, "timeout": timeout})
        return r.get("result", []) if r.get("ok", True) else []

    def send_message(self, chat_id: str, text: str) -> dict:
        return self._call("sendMessage", {"chat_id": chat_id, "text": text})


class TelegramChannel:
    """Vòng lặp poll + dispatch. get_app() trả App HIỆN TẠI (an toàn với hot-reload);
    chat_lock serialize với web chat + scheduler. run_chat inject được cho test."""

    def __init__(
        self, client: TelegramClient, gate: ChannelGate, *,
        get_app: Callable[[], Any], chat_lock, pairing_code: str = "",
        run_chat: Callable[[str, str], str] | None = None,
        poll_timeout: int = 25,
    ) -> None:
        self._client = client
        self._gate = gate
        self._get_app = get_app
        self._lock = chat_lock
        self._pairing_code = pairing_code
        self._run_chat = run_chat or self._default_run_chat
        self._poll_timeout = poll_timeout
        self._offset = 0

    def _default_run_chat(self, text: str, chat_id: str) -> str:
        import asyncio

        with self._lock:
            app = self._get_app()
            res = asyncio.run(app.chat(text, session_key=f"tg:{chat_id}"))
        return res.text or "(không có nội dung)"

    def handle_update(self, upd: dict) -> None:
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id", "")).strip()
        text = (msg.get("text") or "").strip()
        if not chat_id or not text:
            return
        if not self._gate.is_allowed(chat_id):
            # Chưa ghép: chỉ chấp nhận đúng pairing_code (nếu bật), còn lại hướng dẫn.
            if self._pairing_code and text == self._pairing_code:
                self._gate.allowed_chat_ids.add(chat_id)
                self._safe_send(chat_id, "Đã ghép! Từ giờ anh nhắn trực tiếp cho yett ở đây.")
            else:
                self._safe_send(chat_id, "Chưa được cấp quyền. Gửi đúng mã ghép để dùng yett.")
            return
        try:
            reply = self._run_chat(text, chat_id)
        except Exception as e:  # noqa: BLE001 — lỗi 1 turn không được giết vòng poll
            reply = f"[lỗi xử lý] {e}"
        self._safe_send(chat_id, reply)

    def _safe_send(self, chat_id: str, text: str) -> None:
        try:
            self._client.send_message(chat_id, text)
        except Exception:  # noqa: BLE001 — gửi lỗi không được giết vòng poll
            pass

    def poll_once(self) -> int:
        """Lấy updates từ offset hiện tại, xử lý từng cái, cập nhật offset. Trả số update."""
        updates = self._client.get_updates(self._offset, timeout=self._poll_timeout)
        for upd in updates:
            uid = upd.get("update_id")
            if isinstance(uid, int):
                self._offset = uid + 1
            self.handle_update(upd)
        return len(updates)

    def run(self, stop) -> None:
        """Vòng lặp cho tới khi stop.set(). Lỗi mạng 1 lần → nghỉ ngắn rồi thử lại."""
        while not stop.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 — mạng chập chờn không được giết thread
                stop.wait(3.0)

    def notify(self, text: str) -> int:
        """Đẩy tin cho MỌI chat đã ghép (briefing tự động, thông báo job xong…). Trả số đã gửi."""
        sent = 0
        for cid in sorted(self._gate.allowed_chat_ids):
            self._safe_send(cid, text)
            sent += 1
        return sent


def briefing_due(now: datetime, hour: int, last_sent_date: str | None) -> bool:
    """Đến giờ gửi briefing chưa: đã tới/qua `hour` trong ngày VÀ chưa gửi hôm nay.
    Tách pure để test không cần thread/thời gian thật."""
    today = now.strftime("%Y-%m-%d")
    return now.hour >= hour and last_sent_date != today


def now_in_tz(tz: str) -> datetime:
    try:
        return datetime.now(ZoneInfo(tz))
    except Exception:  # noqa: BLE001
        return datetime.now(ZoneInfo("UTC"))
