"""Audit log append-only + hash chain (spec P3 §3.1). Chống sửa: verify_chain phát hiện thay đổi.

Mỗi entry hash = sha256(prev_hash + canonical(entry)). Sửa một dòng → hash lệch → verify fail.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

_GENESIS = "0" * 64


def _entry_hash(prev_hash: str, entry: dict) -> str:
    payload = prev_hash + json.dumps(entry, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self, path: str | Path, redactor=None) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._redact = redactor or (lambda d: d)

    def _last_hash(self) -> str:
        if not self._path.exists():
            return _GENESIS
        last = _GENESIS
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                last = json.loads(line)["hash"]
        return last

    def append(self, *, ts: float, actor: str, kind: str, detail: dict) -> str:
        detail = self._redact(detail)
        prev = self._last_hash()
        body = {"ts": ts, "actor": actor, "kind": kind, "detail": detail, "prev_hash": prev}
        h = _entry_hash(prev, body)
        record = {**body, "hash": h}
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return h

    def verify_chain(self) -> bool:
        """True nếu chuỗi hash toàn vẹn (không dòng nào bị sửa/xoá giữa chừng)."""
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
