---
phase: 4
title: "Hardline Guard Bypasses"
status: completed
effort: "S"
---

# Phase 4: Hardline Guard Bypasses

## Overview
Vá các bypass classifier đã verify: cmdguard bỏ sót toán tử `&`, redirect không-space, và sqlguard strip MySQL exec-comment. Priority **P1** (hardline "cấm xóa OS" và "DB read-only"). Mỗi bypass để lại 1 regression test đỏ→xanh.

## Requirements
- Functional: `ls & rm -rf /data` → DENY; `echo x>/etc/hostname` → DENY (overwrite); SQL `/*!...*/` MySQL không được lọt qua như read-only.
- Non-functional: giữ fail-closed (parse fail = deny); không false-deny lệnh hợp lệ (rà lại regex Windows denylist đang match `git format-patch`).

## Architecture
- **P0-4 cmdguard `&`:** `cmdguard.py:70` `re.split(r"(?:&&|\|\||;|\||\n)", cmd)` thiếu lone `&`. `cmdguard.py:142` (`_is_readonly`) cùng lỗi. Thêm `&` vào cả hai regex (cẩn thận không nuốt `&&` — dùng thứ tự/alternation đúng, `\n` đã có ở :70 nhưng thiếu ở :142).
- **P1-6 redirect no-space + traversal:** `cmdguard.py:86,150` regex `(^|\s)>` bỏ sót `x>` và fd-prefix `1>`/`2>`. Sửa: nhận fd digit tùy chọn (`(?:^|\s|\d)>`), xử lý `>>` append; canonicalize target, chỉ miễn `/dev/null` + tmp thật (chống traversal `>/tmp/../etc/passwd`).
- **P1-5 sqlguard MySQL comment:** `sqlguard.py:30` `_normalize` strip cả `/*!...*/` (MySQL exec-comment) trước parse. **[RT-5/RT-18 High] BẮT BUỘC dùng phương án fail-closed:** reject mọi SQL chứa `/*!` trong `_normalize` **bất kể dialect**. Lý do: có 2 call-site — `db_query.py:54` truyền `prof.driver` đúng, nhưng `immutable.py:33` gọi `classify_sql(sql)` với default `dialect='postgres'` (sqlguard.py:52); postgres coi `/*!...*/` là comment thường và drop nội dung → fix "classify theo dialect" để hở đường immutable. Reject `/*!` phủ cả hai. **Đồng thời** sửa `immutable.py:33` nhận driver thật (thread `prof.driver` vào `ImmutableCore.check` cho db_query) và thêm regression test **tại lớp immutable-core**, không chỉ db_query.
- **Phụ (map security):** `denylist.py:24` Windows regex match bare `format`/`del` như substring → false-deny `git format-patch`. Chọn denylist theo target OS thay vì union, hoặc anchor token command-head.

## Related Code Files
- Modify: `src/yett/security/cmdguard.py` (split regex + redirect regex + traversal canonicalize)
- Modify: `src/yett/tools/db/sqlguard.py` (reject `/*!` bất kể dialect)
- Modify: `src/yett/policy/immutable.py` (**RT-5/18** — `immutable.py:33` thread `prof.driver` vào `classify_sql`, không để default postgres)
- Modify: `src/yett/tools/db/db_query.py` (truyền driver vào `ImmutableCore.check` để immutable path biết dialect)
- Modify: `src/yett/security/denylist.py` (chọn theo OS / anchor)
- Modify: `tests/unit/test_cmdguard.py`, `tests/unit/test_sqlguard.py` (payload bypass), `tests/unit/test_phase3.py` (**regression `/*!` tại lớp immutable-core**, không chỉ db_query)

## Implementation Steps
1. cmdguard: thêm `&` vào split ở :70 và :142; thêm test `ls & rm -rf /data`, `echo hi & rm -rf /data`.
2. cmdguard redirect: sửa regex fd-prefix + `>>`; canonicalize target; test `echo x>/etc/hostname`, `1>/etc/x`, `>/tmp/../etc/passwd`.
3. sqlguard: `_normalize` **reject mọi SQL chứa `/*!`** (fail-closed, bất kể dialect); test `SELECT ... /*!INTO OUTFILE ...*/` → deny; SELECT thường vẫn pass.
4. **immutable-core dialect (RT-5/18):** sửa `immutable.py:33` nhận `prof.driver` (thread driver từ `db_query.py` vào `ImmutableCore.check`), không để default postgres; thêm regression `/*!` **tại lớp immutable-core** (`test_phase3.py`), không chỉ db_query.
5. denylist: fix false-deny `git format-patch`; test lệnh hợp lệ chứa `format`/`del` không bị chặn sai.
6. Chạy `test_cmdguard.py`, `test_sqlguard.py`, `test_ssh_gate.py`, `test_phase3.py` — xanh.

## Success Criteria — ✅ DONE 2026-07-05 (merge `33dd8f7`, 35 regression test mới)
- [x] Payload `ls & rm -rf /data` (+ biến thể echo/cat) → DENY qua gate_ssh; regression test đỏ→xanh. *(Kèm lookbehind `(?<![<>])` để KHÔNG vỡ `2>&1` fd-duplication.)*
- [x] Redirect `echo x>/etc/hostname`, fd-prefix, traversal `>/tmp/../` → DENY.
- [x] SQL chứa `/*!` bị reject ở **cả hai** call-site: `db_query` VÀ **immutable-core** (regression test riêng). *(`db_query.py` không cần sửa — call hiện có đã đúng; xem report.)*
- [x] `git format-patch` KHÔNG bị false-deny (fix trailing-boundary; leading-boundary giữ nguyên vì siết nó làm hỏng detect `sudo rm -rf /`).
- [x] Fail-closed giữ nguyên: input parse fail vẫn deny.

**Kết quả:** report `reports/phase-04-report.md`. **HANDOFF → Phase 6:** thread driver thật vào gate-time `ImmutableCore.check` cần `wiring.py` (ngoài ownership Phase 4) — Phase 4 để lại driver-hint + multi-dialect fail-closed fallback trong `immutable.py`; Phase 6 wire driver thật, GIỮ fallback khi hint vắng.

## Risk Assessment
- Regex sửa dễ gây false-positive/negative mới — mỗi thay đổi kèm test cả positive (deny đúng) lẫn negative (không chặn lệnh hợp lệ).
- sqlguard: cần biết dialect executor thật dùng (sqlite/postgres/mysql). Nếu chưa chắc, fail-closed (reject `/*!`) an toàn hơn.
- Song song: Phase 4 tách bạch với 2/3/5 → cùng **Sóng 1** (2∥3∥4∥5). NHƯNG chung `policy/immutable.py` + `test_phase3.py` với Phase 6 và `db_query.py` với Phase 7 → **6 và 7 phải sau 4** (Sóng 2). Xem ma trận xung đột ở `plan.md`.
