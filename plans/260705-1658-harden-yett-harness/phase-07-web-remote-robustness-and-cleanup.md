---
phase: 7
title: "Web Remote Robustness and Cleanup"
status: completed
effort: "L"
---

# Phase 7: Web Remote Robustness and Cleanup

> **Resume Notes (2026-07-05 — phase deferred, chưa có code):**
> - `blockedBy: [1, 2, 3, 4]` ĐÃ THOẢ — Sóng 1 merged tại `be04fa9`. Agent worktree PHẢI `git merge claude/harness-reference-architecture-0wwuek` trước khi làm.
> - 7a→7b→7c TUẦN TỰ (7a và 7b cùng chạm `app.py`).
> - `App._wire_search()` no-op (Phase 1 phát hiện): 7c chỉ DOCUMENT trong README, KHÔNG wire (feature riêng — tinh thần RT-11). README giữ format cột "Kiểm chứng" Phase 1 tạo.
> - Dead-code 7c: RE-GREP 0-caller sau Sóng 1 (code mới có thể đã dùng symbol) trước khi xoá.
> - RT-10 ACL cho audit log: DÙNG LẠI helper trong `secrets/file_store.py` (import, không sửa file đó — Phase 5 đã fix thêm vụ ACE explicit).
> - **Sau khi code xong: adversarial review.** Chỉ thị gốc dùng `/codex:adversarial-review`; user cập nhật 2026-07-05: **codex hết quota → dùng `code-reviewer` subagent nội bộ** thay thế.

## Overview
Xử lý P2 (web/remote/robustness) + dọn dead-code P3. Priority **P2**, chạy cuối.
**[RT-24] Đã tách 3 nhóm cohesive** (7a web boundary / 7b SSH / 7c cleanup) để review được. Lưu ý: 7a và 7b **cùng chạm `app.py`** → không song song nội bộ nếu chưa khóa owner app.py (xem Risk Assessment). **[RT-10/RT-11] Đã CẮT khỏi plan:** audit-HMAC và memory review-gate (xem "Deferred / Cut" cuối file).

---

## 7a — Web boundary (server + web_fetch)
### Requirements & Architecture
- **server.py:37,52,61; cli.py:33:** validate `Host`/`Origin` (chống DNS-rebinding cho localhost service); `/api/config` GET **redact secret** dùng chung filter path Phase 5 (không tự viết lại — DRY, tránh lặp lỗ hổng sk-cp-); cap request-size; `--host` non-localhost cần cảnh báo/auth.
- **[RT-7 High] web_fetch SSRF — fix ở HTTP CLIENT, không ở WebFetchTool:** `WebFetchTool` chỉ check URL đầu rồi delegate cho `Fetcher` injected (`web_fetch.py:28-46`); **không có fetcher thật trong `src`** (`app.py:65` `fetcher=None`, chỉ có `_fake_fetch` trong test). Tool không thấy redirect hop → không thể enforce per-hop ở đây. Sửa: **tạo module fetcher thật** (urllib/httpx), tắt auto-redirect (hoặc chặn từng hop), mỗi hop re-check allowlist + denylist IP nội bộ/metadata (`169.254.0.0/16`, `127.0.0.0/8`, `::1`, RFC1918, link-local), resolve DNS chống rebinding. Thêm fetcher vào Related Code Files + wire ở `app.py`.

### Related Code Files (7a)
- Modify: `src/yett/web/server.py`, `src/yett/web/configio.py`, `src/yett/cli.py`
- Modify: `src/yett/tools/builtin/web_fetch.py` (giữ check URL đầu)
- Create: `src/yett/tools/builtin/http_fetcher.py` (fetcher thật, SSRF-safe) + wire `app.py`

### Success Criteria (7a)
- [ ] Web từ chối `Host`/`Origin` lạ; `/api/config` không trả secret plaintext (dùng filter Phase 5).
- [ ] Fetcher chặn redirect tới host ngoài allowlist + IP nội bộ/metadata (test SSRF + rebinding).

---

## 7b — SSH hardening + config wiring
### Requirements & Architecture
- **ssh_backend.py:23 known_hosts=None:** bật verify host key (known_hosts thật / pin trong host profile).
- **SSH port + user (P2 report):** thêm `port` vào `HostProfile` (`hostprofile.py:12-18`) + truyền `asyncssh.connect(..., port=host.port)`; `app.py:163-166` truyền `port` từ `HostCfg` (models.py:88 có `port=22`). Kiểm `user@addr` có được asyncssh parse không, nếu không truyền `username=` tách.

### Related Code Files (7b)
- Modify: `src/yett/tools/remote/ssh_backend.py`, `src/yett/tools/remote/hostprofile.py`, `src/yett/app.py`

### Success Criteria (7b)
- [ ] SSH verify host key (known_hosts), không còn `known_hosts=None`.
- [ ] SSH dùng đúng `port`/`user` từ config (test port ≠ 22).

---

## 7c — Robustness cleanup + dead-code + README
### Requirements & Architecture
- **codenav.py ReDoS:** GrepTool/SearchTool compile regex agent-cấp, scan đồng bộ (SearchTool gom ×10). Thêm timeout/giới hạn độ phức tạp hoặc chạy trong executor; `_section` bắt cả exception ngoài `UserFacingError`.
- **loader.py:57 / cron.py:83:** 1 SKILL.md hỏng không crash cả discovery; cron `compute_next` tôn trọng timezone.
- **[RT-23] Quyết định 1 lần mỗi symbol callerless — XÓA hoặc WIRE, KHÔNG vừa-fix-vừa-xóa:** `insights.py::check_budget`, `retention.py::purge_old_messages`, `obs/cost.py::compute_call_cost`, `core/session.py::_lock/lock()`, `Usage.cache_write_tokens`, `SpanKind.EMBEDDING`, `DbProfile.readonly`. Grep xác nhận 0 caller trong `src`. Mặc định: **xóa** (không fix business-logic cho hàm sắp xóa). Chỉ WIRE (rồi mới fix) nếu có caller thật land ngay chu kỳ này — vd nếu wire `check_budget` thì mới sửa bug monthly-vs-all-time; nếu wire `retention` thì mới bắt buộc auditor.
- **README/docs:** `web_search` ghi "implemented, chưa app-wired" (không ✅); rà bảng status khớp thực tế.

### Related Code Files (7c)
- Modify: `src/yett/tools/builtin/codenav.py`, `src/yett/skills/loader.py`, `src/yett/sched/cron.py`, `README.md`
- Delete-or-wire: các symbol callerless liệt kê trên (quyết định per-symbol)

### Success Criteria (7c)
- [ ] ReDoS guard (timeout/executor) cho grep/search; `_section` không để exception lạ crash turn.
- [ ] 1 SKILL.md hỏng không chặn discovery; cron tôn trọng timezone.
- [ ] Không còn symbol dead-code callerless (grep/import-linter sạch); README khớp thực tế.

---

## Deferred / Cut khỏi plan này
- **[RT-10] audit-HMAC — CẮT.** Threat là "attacker có quyền file". Khóa HMAC chỉ có thể nằm trong FileSecretStore — chính là plaintext store attacker đọc được (Phase 5 xác nhận), nên HMAC KHÔNG đánh bại threat của nó → gold-plating. Thay bằng: **ACL owner-only cho file audit log** (dùng lại việc ACL ở Phase 5) + document rõ "tamper-evidence chống attacker local-file là ngoài phạm vi v0.0.1". `audit.py:26` O(n²) append + thiếu lock: có thể fix riêng (rẻ), giữ lại như robustness nhỏ.
- **[RT-11] memory review-gate wiring — CẮT.** Invariant "agent không ghi thẳng MEMORY.md" ĐÃ đúng-by-construction: `MemoryReviewGate` 0 caller, không có tool ghi memory trong registry, `workspace.py:15` chỉ ĐỌC. "Wire" = XÂY tính năng ghi-memory mới → không phải hardening. Đưa ra plan feature riêng có threat review riêng. Ở đây chỉ thêm 1 dòng note invariant đang được giữ.

## Risk Assessment
- **7a/7b/7c KHÔNG tự ý chạy song song nội bộ:** 7a (wire `http_fetcher` → `app.py`) và 7b (wire SSH `port` → `app.py`) đều sửa `app.py`. Nếu muốn song song trong Phase 7 thì phải chỉ-định-một-owner cho `app.py`; còn không thì chạy 7a→7b→7c **tuần tự**. (Toàn bộ Phase 7 vẫn nằm Sóng 2, sau Sóng 1.)
- **Dead-code cleanup (7c) merge sau Phase 6:** 7c xoá symbol callerless; merge Phase 6 trước rồi 7 sau + full regression, để không xoá symbol mà 6/tests (gián tiếp) còn dùng. Xem thứ tự merge ở `plan.md`.
- Fetcher SSRF-safe là code mới — test kỹ redirect/rebinding/metadata; đây là lớp đúng để enforce (không phải WebFetchTool).
- Dead-code: xác nhận 0 caller (grep cả tests) trước khi xóa; symbol có kế hoạch dùng gần thì wire thay vì xóa.
