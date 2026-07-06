"""Test AuditLog hardening (phase 7c, [RT-10]): append O(1) (không re-scan cả file mỗi
lần), thread-safe dưới ghi đồng thời, và file/thư mục audit log được khoá owner-only
(REUSE helper `_restrict_permissions` từ `secrets/file_store.py` — không assert chi
tiết OS-ACL ở đây, Phase 5 đã test riêng; chỉ xác nhận audit.py THẬT SỰ gọi helper đó).
"""

from __future__ import annotations

import threading
from pathlib import Path

import yett.compliance.audit as audit_mod
from yett.compliance.audit import AuditLog


def test_append_does_not_rescan_whole_file_each_call(tmp_path: Path, monkeypatch) -> None:
    """[O(n²) fix] Trước fix, mỗi `append()` gọi `Path.read_text()` để dò last-hash —
    O(n) mỗi lần, O(n²) cho n dòng. Sau fix, cache in-memory nên chỉ đọc file tối đa 1
    lần (khi khởi tạo hoặc lần append đầu, nếu file đã có sẵn từ trước)."""
    log = AuditLog(tmp_path / "audit.jsonl")
    read_calls = {"n": 0}
    real_read_text = Path.read_text

    def counting_read_text(self, *a, **k):
        read_calls["n"] += 1
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", counting_read_text)
    for i in range(20):
        log.append(ts=float(i), actor="agent", kind="gate", detail={"i": i})
    # append thứ 2..20 KHÔNG được đọc lại file (cache đã có last-hash trong bộ nhớ).
    assert read_calls["n"] == 0
    assert log.verify_chain() is True
    assert len(log.entries()) == 20


def test_append_thread_safe_under_concurrency(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    n_threads, per_thread = 8, 10

    def worker(idx: int) -> None:
        for j in range(per_thread):
            log.append(ts=float(idx * 100 + j), actor=f"t{idx}", kind="gate", detail={"j": j})

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert log.verify_chain() is True  # không race làm gãy chuỗi hash
    assert len(log.entries()) == n_threads * per_thread


def test_audit_log_file_and_dir_get_owner_only_acl(tmp_path: Path, monkeypatch) -> None:
    calls: list[Path] = []

    def spy(path: Path, *, mode: int) -> None:
        calls.append(Path(path))

    monkeypatch.setattr(audit_mod, "_restrict_permissions", spy)
    log_dir = tmp_path / "nested" / "audit"
    log = AuditLog(log_dir / "audit.jsonl")
    log.append(ts=1.0, actor="agent", kind="gate", detail={})

    assert log_dir in calls  # thư mục chứa (700)
    assert (log_dir / "audit.jsonl") in calls  # file audit log (600)


def test_audit_log_reuses_file_store_helper_not_reimplemented() -> None:
    """[DRY] Đảm bảo audit.py IMPORT helper từ secrets/file_store.py thay vì tự viết lại
    logic ACL (tránh lặp lỗi ACE-residue Phase 5 đã fix)."""
    from yett.secrets.file_store import _restrict_permissions as canonical

    assert audit_mod._restrict_permissions is canonical
