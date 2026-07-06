"""Audit log append-only + hash chain (spec P3 §3.1). Chống sửa: verify_chain phát hiện thay đổi.

Mỗi entry hash = sha256(prev_hash + canonical(entry)). Sửa một dòng → hash lệch → verify fail.

[RT-10] Threat model: hash chain phát hiện sửa MỘT dòng giữa chừng mà không đụng các
dòng khác — nhưng KHÔNG chống được attacker đã có quyền ghi trực tiếp file này (họ xoá
hết rồi viết lại từ đầu, hash chain tự nó không có bí mật gì để attacker không biết).
Từng cân nhắc ký HMAC nhưng khoá HMAC muốn giấu kín lại phải nằm trong `FileSecretStore`
— CHÍNH là kho plaintext mà attacker (nếu đã đọc được file audit) nhiều khả năng cũng
đọc được luôn → HMAC không đánh bại đúng threat model của nó (gold-plating, xem
red-team review Phase 7). Phòng thủ thực tế thay vào đó: ACL owner-only cho file audit
log (REUSE `_restrict_permissions` đã cứng hoá ở Phase 5 cho secret file — DRY, tránh
tự viết lại và lặp lỗi ACE-residue trên Windows đã fix ở đó). **Tamper-evidence chống
attacker ĐÃ có quyền đọc/ghi trực tiếp file này nằm NGOÀI PHẠM VI v0.0.1** — threat
model hiện tại coi ranh giới quyền file OS là biên tin cậy (single-tenant, on-prem).
"""

from __future__ import annotations

import hashlib
import json
import stat
import threading
from pathlib import Path

from yett.secrets.file_store import _restrict_permissions

_GENESIS = "0" * 64


def _entry_hash(prev_hash: str, entry: dict) -> str:
    payload = prev_hash + json.dumps(entry, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self, path: str | Path, redactor=None) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        _restrict_permissions(self._path.parent, mode=stat.S_IRWXU)  # 700, cùng pattern FileSecretStore
        self._redact = redactor or (lambda d: d)
        # [RT-10] nhiều thread (vd nhiều request web) append đồng thời — không có lock
        # thì 2 append có thể cùng đọc `_last_hash` trước khi bên kia ghi xong, làm hỏng
        # chuỗi hash (race). `threading.Lock` (không phải asyncio.Lock) vì các caller
        # hiện tại đều đồng bộ/đa luồng (ThreadingHTTPServer), không phải coroutine.
        self._lock = threading.Lock()
        self._last_hash_cache: str | None = None
        if self._path.exists():
            _restrict_permissions(self._path, mode=stat.S_IRUSR | stat.S_IWUSR)  # 600

    def _last_hash_locked(self) -> str:
        """PHẢI gọi trong `self._lock`. Cache last-hash trong bộ nhớ: bản gốc đọc lại
        TOÀN BỘ file mỗi lần append (O(n) mỗi lần → O(n²) cho n dòng) — cache tránh việc
        này, chỉ quét file 1 lần (lần đầu tiên hoặc sau khi cache-miss)."""
        if self._last_hash_cache is not None:
            return self._last_hash_cache
        if not self._path.exists():
            self._last_hash_cache = _GENESIS
            return _GENESIS
        last = _GENESIS
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                last = json.loads(line)["hash"]
        self._last_hash_cache = last
        return last

    def append(self, *, ts: float, actor: str, kind: str, detail: dict) -> str:
        detail = self._redact(detail)
        with self._lock:
            prev = self._last_hash_locked()
            body = {"ts": ts, "actor": actor, "kind": kind, "detail": detail, "prev_hash": prev}
            h = _entry_hash(prev, body)
            record = {**body, "hash": h}
            is_new_file = not self._path.exists()
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            if is_new_file:
                _restrict_permissions(self._path, mode=stat.S_IRUSR | stat.S_IWUSR)
            self._last_hash_cache = h
        return h

    def verify_chain(self) -> bool:
        """True nếu chuỗi hash toàn vẹn (không dòng nào bị sửa/xoá giữa chừng). Luôn đọc
        thẳng từ đĩa (không dùng cache) — đây là thao tác audit tường minh, không phải
        đường nóng append, và phải phát hiện được tamper xảy ra NGOÀI instance này."""
        if not self._path.exists():
            return True
        prev = _GENESIS
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            body = {k: rec[k] for k in ("ts", "actor", "kind", "detail", "prev_hash")}
            if rec["prev_hash"] != prev:
                return False
            if _entry_hash(prev, body) != rec["hash"]:
                return False
            prev = rec["hash"]
        return True

    def entries(self) -> list[dict]:
        if not self._path.exists():
            return []
        return [json.loads(ln) for ln in self._path.read_text(encoding="utf-8").splitlines() if ln.strip()]
