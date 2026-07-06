---
title: "Harden yett harness — fix verified critique findings"
description: "Fix P0/P1/P2 findings from the code critique: real-provider loop, sandbox isolation, hardline guard bypasses, secret handling, gate defense-in-depth"
status: completed
priority: P1
branch: "claude/harness-reference-architecture-0wwuek"
tags: [security, harness, correctness]
blockedBy: []
blocks: []
created: "2026-07-05T09:59:55.694Z"
createdBy: "ck:plan"
source: skill
---

# Harden yett harness — fix verified critique findings

## Overview

Nguồn: `plans/reports/codebase-review-260705-1616-yett-harness-critique-report.md` (findings đã verify Tier A/B; vòng review sâu Codex reproduce thêm).

Vấn đề cốt lõi: yett là *tập bất biến an ninh*, nhưng test toàn chạy qua FakeProvider + CI chỉ Linux → 3 khiếm khuyết real-provider và nhiều bypass hardline **không test nào chạm tới**. Plan này sửa theo thứ tự: **dựng test để lộ lỗi trước (Phase 1) → sửa P0 → sửa hardline/secret → gate defense-in-depth → P2/cleanup**.

Nguyên tắc: fail-closed khi thiếu năng lực (Docker/secret backend), không im lặng hạ cấp. Mỗi bypass để lại 1 regression test. Không đụng quyết định người dùng (single-tenant, MIT vendoring, chuẩn skills) — chỉ sửa lỗ hổng đã verify.

## Phases

| Phase | Name | Status | Priority | Findings |
|-------|------|--------|----------|----------|
| 1 | [Test and CI Harness](./phase-01-test-and-ci-harness.md) | ✅ Completed | P1 | Meta (make invisible failures visible) |
| 2 | [Real-Provider Loop](./phase-02-real-provider-loop.md) | ✅ Completed | P1 | P0-2, P0-3 |
| 3 | [Sandbox Isolation and Exec Scope](./phase-03-sandbox-isolation-and-exec-scope.md) | ✅ Completed (e2e docker chờ daemon) | P1 | P0-1, P1-15, docker hardening |
| 4 | [Hardline Guard Bypasses](./phase-04-hardline-guard-bypasses.md) | ✅ Completed | P1 | P0-4, P1-5, P1-6 |
| 5 | [Secret Handling](./phase-05-secret-handling.md) | ✅ Completed | P1 | P0-5, P1-7, P1-13, P1-14, P1-17 |
| 6 | [Policy Gate Defense-in-Depth](./phase-06-policy-gate-defense-in-depth.md) | ✅ Completed (+ M2/H1 fix sau review) | P2 | P1-8, P1-9, P1-10, P1-11, P1-16, allowlist-anchor |
| 7 | [Web Remote Robustness and Cleanup](./phase-07-web-remote-robustness-and-cleanup.md) | ✅ Completed (salvage sau khi agent hết quota) | P2 | P2 items + P3 dead-code |

## Lịch chạy song song (3 sóng)

Suy ra từ file-ownership thực tế (Related Code Files của từng phase), không phải phỏng đoán:

- **Sóng 0 — Phase 1** (một mình): tạo test infra (`test_real_provider_contract.py`, `test_docker_sandbox.py`) + `ci.yml` + `README.md`.
- **Sóng 1 — Phase 2 ∥ 3 ∥ 4 ∥ 5** (4-way, zero overlap file giữa 4 phase — đã verify từng cặp).
- **Sóng 2 — Phase 6 ∥ 7** (2-way, 6 và 7 không đụng file nhau; cả hai phải sau Sóng 1).

Wall-clock ≈ `P1 + max(P2,P3,P4,P5) + max(P6,P7)`.

### Ma trận xung đột file (buộc thứ tự — KHÔNG được chạy song song cùng sóng)

| File | Phase cùng đụng | Ràng buộc |
|------|-----------------|-----------|
| `core/loop.py` | 2, 6 | 6 sau 2 |
| `policy/immutable.py` | 4, 6 | 6 sau 4 |
| `tests/unit/test_phase3.py` | 4, 6 | 6 sau 4 |
| `app.py` | 3, 7 | 7 sau 3 |
| `provider/base.py` | 2, 7 (dead-code) | 7 sau 2 |
| `tools/db/db_query.py` | 4, 7 (dead-code) | 7 sau 4 |
| `README.md` | 1, 7 | 7 sau 1 |
| `tests/integration/test_*` | 1 tạo, 2/3 sửa | 2,3 sau 1 |

Các sóng được thiết kế để mọi ràng buộc trên đều thoả (6 và 7 đều nằm Sóng 2 = sau toàn bộ Sóng 1).

**Ownership test-integration (tránh đụng file setup):** Phase 2 sở hữu `test_real_provider_contract.py`; Phase 3 sở hữu `test_docker_sandbox.py` — hai file KHÁC nhau nên không conflict. **Không tạo conftest/helper dùng chung** giữa 2 và 3; nếu cần fixture chung thì Phase 1 tạo sẵn, 2/3 chỉ đọc không sửa.

**Điều kiện an toàn khi chạy đồng thời:** mỗi phase-agent chỉ sửa đúng file nó sở hữu; nhiều agent cùng lúc → **git worktree cô lập mỗi phase** rồi merge theo sóng (tránh race git index / test).

### Thứ tự merge & verify (bắt buộc)

1. **Sóng 0:** làm Phase 1 một mình, merge.
2. **Sóng 1:** chạy Phase 2 ∥ 3 ∥ 4 ∥ 5 (worktree riêng). **Merge theo thứ tự 2 → 3 → 4 → 5**, để **Phase 5 cuối cùng** vì nó thêm dep `keyring` vào `pyproject.toml` (đổi môi trường cho mọi phase khác). Sau merge Sóng 1 → chạy full test 1 lần.
3. **Sóng 2:** chạy Phase 6 ∥ 7. **Merge Phase 6 TRƯỚC, Phase 7 SAU** — vì Phase 7 có dead-code cleanup (xoá symbol callerless); merge 6 trước để chắc 6/tests không (gián tiếp) dùng symbol 7 sắp xoá, rồi 7 xoá trên trạng thái đã biết.
4. **Cổng cuối:** chạy full verification — `ruff` + `mypy` + `import-linter` + `pytest` + docker-gated tests + **leg CI Windows** (secret-perm giờ phải xanh sau Phase 5, gỡ xfail).

> **Phase 7 nội bộ KHÔNG tự ý song song:** 7a (wire `http_fetcher` vào `app.py`) và 7b (wire SSH port vào `app.py`) đều chạm `app.py`. Trong Phase 7 phải hoặc chỉ-định-một-owner cho `app.py`, hoặc chạy 7a/7b/7c **tuần tự**. Xem `phase-07`.

## Dependencies

- Phase 2 `blockedBy: [1]` — cần real-provider contract test để verify system prompt + tool_use turn (+ sửa chung `test_real_provider_contract.py` do 1 tạo).
- Phase 3 `blockedBy: [1]` — cần docker integration test để verify no-egress + non-host exec.
- Phase 6 `blockedBy: [2, 4]` — chung `core/loop.py` (với 2), `policy/immutable.py` + `test_phase3.py` (với 4).
- Phase 7 `blockedBy: [1, 2, 3, 4]` — chung `README.md` (1), `provider/base.py` (2), `app.py` (3), `db_query.py` (4).
- Phase 4, 5 chỉ cần Sóng 0 xong (không hard-depend file của 1, nhưng chạy sau 1 để có CI infra).
- Cross-plan: không có plan nào khác trong `plans/` (chỉ có `reports/`).

## Red Team Review

### Session — 2026-07-05
**Findings:** 15 gộp (26 thô từ 4 reviewer) — **15 accepted, 0 rejected**. Mọi finding có bằng chứng `file:line` từ codebase thật.
**Severity:** 2 Critical, 10 High, 3 Medium.

| # | Finding | Sev | Disp | Applied |
|---|---------|-----|------|---------|
| RT-1 | DockerSandbox wire không mount workspace + thiếu dịch host→container cwd | Critical | Accept | Phase 3 |
| RT-2 | Resume không khôi phục message history (checkpoint chỉ lưu ids; id provider non-deterministic) | Critical | Accept | Phase 2 |
| RT-3 | Checkpoint `canceled` không resumable (guard chỉ đọc `running`) | High | Accept | Phase 2 |
| RT-4 | Fix immutable-core không phủ vector exec (target là command string) | High | Accept | Phase 6 |
| RT-5/18 | sqlguard fix bỏ sót immutable.py:33 (default postgres) → bắt buộc reject `/*!` | High | Accept | Phase 4 |
| RT-6/19 | Filter quên format `sk-cp-` (key đã lộ); regex đứt ở `-` | High | Accept | Phase 5 |
| RT-7 | SSRF fix sai lớp — fetcher không tồn tại trong src; enforce ở HTTP client | High | Accept | Phase 7a |
| RT-8 | Subagent subset dựa `ctx.allowed_tools` — không có trên SessionCtx/_SimpleCtx | High | Accept | Phase 6 |
| RT-9 | Phase 1 land test ĐỎ lên nhánh chung → CI đỏ; dùng `xfail(strict=True)` | High | Accept | Phase 1 |
| RT-10 | Audit HMAC gold-plating (khóa trong plaintext store) — cắt, dùng ACL | High | Accept | Phase 7 (cut) |
| RT-11 | Wire memory review-gate là feature mới, không phải hardening — cắt | High | Accept | Phase 7 (cut) |
| RT-12/24 | Phase 7 quá tải → tách 7a/7b/7c; token estimate bỏ qua tool_call payload | High/Med | Accept | Phase 7, Phase 2 |
| RT-13 | log_read containment phải `PurePosixPath` (remote POSIX, controller Windows) | Med | Accept | Phase 6 |
| RT-14 | exec.cwd scope-check an toàn giả (command body thoát) | Med | Accept | Phase 3 |
| RT-15 | Redact private key lọt body khi output bị cắt (thiếu footer) | Med | Accept | Phase 5 |

Chi tiết fix nằm inline trong từng phase file dưới mục "Red Team Fixes" / "[RT-N]".

### Whole-Plan Consistency Sweep
Xem cuối file (chạy sau khi áp findings).

## Acceptance Criteria (toàn plan) — cập nhật 2026-07-05 sau Sóng 0+1

- [x] Real-provider contract test (record/replay) xanh: provider nhận system prompt + đúng chuỗi assistant(tool_use)→tool_result. *(Phase 2 — pass thường, đã gỡ xfail.)*
- [~] Docker sandbox integration test (gated theo daemon) xanh: exec không có network, không chạy trên host, **workspace được mount + exec chạy được trong container**; backend=docker mà Docker thiếu → fail-closed (không hạ cấp). *(Phase 3 — code + test đã land; fail-closed/mounts/hardening + writability verify bằng unit test trên argv; **e2e CHƯA chạy** — máy dev không có daemon. Còn lại DUY NHẤT: chạy integration test trên máy/CI có Docker.)*
- [x] CI có leg Windows (+ macOS nếu khả thi); test secret-perm Windows xanh. *(Phase 1 thêm `windows-latest`; macOS không thêm — tùy chọn. Secret-perm xanh trên Windows local sau Phase 5. Lưu ý: CI cloud chưa quan sát vì nhánh chưa push.)*
- [x] Mọi bypass đã verify (cmdguard `&`/redirect, sqlguard `/*!*/`, filter private key, immutable path, log_read fnmatch) có regression test đỏ→xanh. *(cmdguard ✓ + sqlguard ✓ Phase 4; filter private key ✓ Phase 5; immutable path (write_file + exec, kể cả redirect + con của protected dir) + log_read ✓ Phase 6 + M2/H1 fix.)*
- [x] `secret_backend` chưa implement → fail startup, không silent downgrade plaintext. *(Phase 5 — keyring implement thật; age trong enum → UserFacingError có remediation.)*
- [x] Key MiniMax đã lộ được rotate; inline `api_key` chuyển secret store hoặc document trade-off tường minh. *(Key đã rotate trước plan. Quyết định khi cook: GIỮ inline làm local-convenience + warning comment trong `harness.example.yaml`; redact `/api/config` GET → Phase 7a, deferred.)*
- [~] mypy strict + ruff + import-linter + pytest xanh trên cả Linux và Windows CI. *(Windows local sau toàn bộ Wave 0-2 + fix: pytest **394 passed/4 skipped**, mypy strict ✓, ruff ✓, import-linter 2 kept ✓. Linux CI + Windows CI cloud chưa quan sát — nhánh chưa push.)*
- [x] README status table phân biệt "logic tested via fake" vs "verified end-to-end"; số test khớp thực tế. *(Phase 1; Phase 7c sẽ rà lại lần cuối khi chạy.)*

## Decisions (chốt 2026-07-05)

1. **LocalSandbox = CHỈ dev/test; production chạy Docker Desktop.** `backend: docker` là đường dùng thật (mount workspace + `--network none`). Docker thiếu → **fail-closed** (từ chối exec, báo lỗi rõ), KHÔNG tụt về host. Hệ quả: RT-14 (exec.cwd) nhẹ đi (containment thật đến từ Docker); RT-25 (env-allowlist LocalSandbox) → **CẮT** (LocalSandbox không phải runtime prod nên không có phơi nhiễm cần vá).
2. **Secret at-rest: implement backend `keyring` thật (fallback `age`), KHÔNG tự build vault.** `keyring` = OS credential manager (Windows Credential Manager / macOS Keychain / Linux Secret Service), mã hoá tại chỗ, không thêm hạ tầng. HashiCorp Vault chỉ khi khách đã có sẵn. Bỏ hành vi tụt-plaintext im lặng: backend chưa sẵn sàng → fail startup có remediation.
3. **Chống "lỡ gửi secret lên LLM" = kiến trúc + filter, KHÔNG phải vault.** Giữ resolve-at-point-of-use (model chỉ thấy tên profile) + **ưu tiên vá Result Filters (Phase 5)**: lọt body private key (P0-5), miss `sk-cp-` (RT-6), redact đệ quy (P1-7). Key MiniMax **đã rotate** (xong). Vault bảo vệ đĩa, filter bảo vệ context — hai lớp bổ sung.
4. **CI thêm chân `windows-latest`** (repo dùng GitHub Actions nên có sẵn hosted runner). Nếu phút CI Windows hạn chế → tối thiểu chạy full suite Windows local 1 lần + `xfail`/`skipif` test POSIX-only, ghi rõ giới hạn (Phase 1).

*(4 câu hỏi mở đã được người dùng chốt — plan sẵn sàng cook.)*

## Whole-Plan Consistency Sweep — 2026-07-05

Chạy sau khi áp 15 red-team finding. Đọc lại plan.md + 7 phase.

**Decision deltas đã reconcile:**
- Scope mở rộng: Phase 2 **+`checkpoint.py`** (RT-2, persist message history); Phase 7a **+`http_fetcher.py` mới** (RT-7, SSRF ở HTTP client); Phase 6 subagent-subset qua **param tường minh** vào `execute_tool` (RT-8, chạm thêm `loop.py`/`gate.py` cho SessionCtx).
- Scope cắt: **audit-HMAC** (RT-10) → thay bằng ACL owner-only + document; **memory review-gate wiring** (RT-11) → đẩy ra plan feature riêng. Hai mục này KHÔNG còn là acceptance của plan.
- Phase 7 tái cấu trúc **7a/7b/7c**; "fix-vs-delete" callerless resolve theo per-symbol (RT-23): mặc định xóa, chỉ fix khi wire.
- Guard fix chuyển sang **fail-closed mạnh hơn**: sqlguard reject `/*!` bất kể dialect (RT-5); immutable tách write_file/exec (RT-4); Phase 1 test đỏ dùng `xfail(strict=True)` để CI không đỏ nhánh chung (RT-9).

**Kiểm tra stale/mâu thuẫn:** Acceptance criteria vẫn đúng (không nhắc HMAC/memory-gate nên không stale); bảng Phases khớp; Unresolved Questions Q1 giờ chi phối RT-1/RT-14/RT-25 (docker mount + exec containment). Bổ sung acceptance: docker test phải xác nhận **workspace được mount + exec chạy được trong container** (không chỉ "không network").

**Sweep round 1** (sơ sài — nhận lỗi): chỉ ghi delta ở block "Red Team Fixes", KHÔNG fold vào Requirements/Steps/Success/Related Files → còn mâu thuẫn nội-phase.

**Sweep round 2** (2026-07-05, sau feedback + Codex tìm 7 issue + tự soát tìm thêm 2): đã **fold** mọi RT-fix + quyết định vào phần chính của từng phase:
- Phase 1: bỏ ngôn ngữ "test ĐỎ" → `xfail(strict=True)` (CI xanh), Phase 2/3/5 gỡ marker.
- Phase 2: `checkpoint.py` vào Related Files; resume-after-cancel + token-estimate + signature-idempotency vào Steps/Success.
- Phase 3: docker mount + host→container cwd vào Requirements/Steps/Success; env-allowlist cắt; exec.cwd hạ mức (không phải containment).
- Phase 4: `immutable.py`/`db_query.py` vào Related Files; reject `/*!` + immutable-core dialect test vào Steps/Success.
- Phase 5: keyring **implement thật** (không mâu thuẫn "fail-startup vs implement"); `backends.py`+`pyproject.toml` vào scope.
- Phase 6: `loop.py`/`broker.py` vào Related Files (truyền param `allowed_tools`); need_approval-lần-2 vào Steps/Success.

**Quyết định (chốt 2026-07-05):** Q1 LocalSandbox dev/test + Docker Desktop; Q2/Q3 keyring/age + ưu tiên vá filter (key đã rotate); Q4 windows-latest. RT-25 cắt.

**Mâu thuẫn chưa giải quyết:** KHÔNG (0) sau round 2. → **Plan sẵn sàng cook.**

## Execution Log — 2026-07-05 (cook --parallel, Sóng 0 + Sóng 1)

Nhánh: `claude/harness-reference-architecture-0wwuek`. Sóng chạy đúng thiết kế (worktree cô lập mỗi phase, merge đúng thứ tự 2→3→4→5).

| Commit | Nội dung |
|--------|----------|
| `5c02fbc` | Sóng 0 — Phase 1: contract/docker test (xfail strict), CI windows leg, README status |
| `66cb0b6` | Merge Phase 2: system prompt + assistant tool_use + checkpoint/resume (RT-2/3/12) |
| `bc69154` | Merge Phase 3: DockerSandbox wire + mounts + hardening + fail-closed (RT-1/14) |
| `33dd8f7` | Merge Phase 4: cmdguard `&`/redirect, sqlguard `/*!`, denylist false-deny (RT-5/18) |
| `1c3393f` | Merge Phase 5: PEM/`sk-cp-` redaction, Windows ACL, keyring backend (RT-6/15/19) |
| `be04fa9` | Fix tích hợp sau merge: doctor test hermetic (đọc config máy thật → non-hermetic), `PYTHONUTF8=1` cho import-linter Windows |
| `3975c49` | Fix review finding (user): strip ACE explicit rộng trên secret file; `_container_user` map host UID |
| `a7165e7` | Merge Phase 6: re-gate mutated args, subagent toolset enforce (loop+RPC), canonical immutable path, log_read PurePosixPath |

**Flake môi trường (KHÔNG phải regression):** `test_web_ui.py`/`test_web_approval.py` bind **cứng port 8791/8811/8812**; dải port Windows loại trừ (WSL2/Hyper-V) dịch chuyển trong phiên nên có lúc `WinError 10013`. Không phải fail của phase nào. **Fix triệt để chưa làm = bind port 0 (ephemeral)** — ghi vào "việc còn lại".

### Sóng 2 + fix sau review (2026-07-06)

| Commit | Nội dung |
|--------|----------|
| `a7165e7` | Merge Phase 6: re-gate mutated args, subagent toolset (loop+RPC), canonical immutable path, log_read PurePosixPath |
| `d41721a` | Merge Phase 7 (salvage sau khi agent hết quota giữa chừng — code đã xong, chưa commit): http_fetcher SSRF-safe, SSH known_hosts + port, ReDoS guard, dead-code, audit ACL |
| `a55859a` | Fix merge-regression: `HookRunner` chỉ báo `mutate` khi hook THỰC SỰ đổi args/result (empty runner trả copy → wiring re-gate mọi call → tool need_approval bị hỏi duyệt 2 lần → treo tới timeout). + sửa rule test web-approval sang fullmatch. |
| `9b29f35` | **M2 Critical fix:** wire `ImmutableCore` vào gate chạy thật (`_ImmutableFirstGate` trước `BasicGate`, protect file config); **H1:** bóc redirect operator + so khớp mọi path component (`>policy.yaml`, con của protected dir). |
| `0f7a148` | M4: redact ODBC password có quote/braces; L2: chặn đệ quy `EXPLAIN(...)` (fail-closed, hết log-flood). |

**Review findings đã xử lý:** M1 (đã bị M2 wiring bao phủ một phần: ImmutableCore chạy classify_cmd+denylist cho exec ở gate — xem ghi chú dưới), **M2 ✅ fixed**, M3 (documented — resume at-least-once khi crash cứng, chưa đổi), **M4 ✅ fixed**, **H1 ✅ fixed**, **L2 ✅ fixed**. **Còn lại (Low, chưa fix, đã ghi rõ):** L1 (journalctl sub-command), L3/L4 (documented, chấp nhận), M1 sâu (áp cmdguard.classify đầy đủ cho exec — hiện exec qua BasicGate chỉ chạy denylist; ImmutableCore thêm DELETE_FILE+denylist hardline nhưng chưa chặn compound `git ; curl|sh` cho exec local).

**Kết quả cuối (Windows local):** pytest **394 passed, 4 skipped**, 0 failed; mypy ✓; ruff ✓; import-linter 2 kept ✓.

**Kết quả verify (Windows local):** pytest **311 passed, 4 skipped** (docker — không daemon), 0 failed; mypy strict ✓; ruff ✓; import-linter 2 contracts kept ✓. Cả 3 marker xfail của Phase 1 đã được Phase 2/3/5 gỡ như thiết kế.

**Conflict merge đã xử lý (đều do worktree snapshot trước Sóng 0):** `test_real_provider_contract.py` + `test_docker_sandbox.py` (add/add → lấy bản post-fix của phase), `test_setup_wizard.py` (xfail marker vs platform-aware rewrite → lấy bản Phase 5).

**Phase 6 + 7: DEFERRED** — user dừng 2026-07-05 khi 2 agent Sóng 2 chưa sửa dòng code nào (chỉ mới sync worktree/đọc file). Xem "Resume Notes" trong từng phase file để chạy tiếp.

**Việc còn lại (sau khi TẤT CẢ 7 phase đã xong + merge + fix review):**
1. Docker integration e2e trên máy/CI có daemon (unit-verified rồi, e2e chưa — acceptance item docker còn `[~]`).
2. Push nhánh → quan sát CI Linux + Windows thực tế (local đã xanh; cloud chưa chạy).
3. Fix triệt để flake port web-test: `serve(port=0)` ephemeral thay port cứng.
4. Low findings chưa fix (không chặn): L1 journalctl sub-command gate; M1 sâu (cmdguard.classify đầy đủ cho exec local); M3 (document/đổi resume guarantee). L3/L4 đã accept.

## Adversarial Review — Sóng 0+1 (2026-07-05)

Advisor `code-reviewer` nội bộ (thay `/codex:adversarial-review` — codex hết quota) review diff `2cc89a8..3975c49`. Báo cáo đầy đủ: `reports/from-advisor-to-orchestrator-260705-2146-merged-hardening-adversarial-review.md`. **Verdict: SOUND trên bất biến chính — 0 Critical, 0 High.** 4 Medium + 4 Low = khe hở defense-in-depth/đảm-bảo, ngoài tầm với của happy-path + FakeProvider suite.

| # | Sev | Tóm tắt | Route |
|---|-----|---------|-------|
| M1 | Med | `denylist.check_exec` regex-only, bypass bằng quote/interpreter (`'rm'`, `python -c shutil.rmtree`); là hardline xóa DUY NHẤT cho tool `exec` local (cmdguard chỉ chạy cho ssh_exec). Docker read-only rootfs giảm nhẹ. | **Phase 6** (tokenize/áp cmdguard.classify vào exec) |
| M2 | Med | ImmutableCore **inert** — `app.py:164` wire BasicGate; PolicyEngine không được khởi tạo trong `src`. Bất biến "immutable core không override được" CHƯA thoả bởi code đã merge. | **Phase 6** (đã trong scope — wire ImmutableCore) |
| M3 | Med | Resume = at-least-once khi crash cứng (effect chạy trước ckpt.save, `loop.py:232-245`); `consult_cache=is_resume` phục vụ stale cả turn. | **Phase 6** — quyết + document guarantee thật |
| M4 | Med | Filter `odbc_dsn_pwd` (`filters.py:25`) lọt password có quote/braces (`Pwd='sec;ret'`). Backstop — resolve-at-point-of-use vẫn giữ. | **Cổng cuối** — fix nhỏ filters.py (merged, unowned) |
| L1 | Low | `journalctl` trong `_READONLY_BINS` không gate sub-command → `--vacuum-time`/`--rotate` xóa journal. | **Phase 6/7** cmdguard |
| L2 | Low | `EXPLAIN(SELECT 1)` (no space) → self-recursion ~980 parse (`sqlguard.py:87-92`); RecursionError bị catch → deny (fail-closed) nhưng DoS/log flood nhẹ. | **Cổng cuối** — fix nhỏ sqlguard.py |
| L3 | Low | Truncated-PEM fallback over-redact nội dung sau header lẻ; `generic_sk_key` cần ≥20 ký tự. Hướng an toàn. | Chấp nhận / document |
| L4 | Low | `SandboxCfg.network="proxy"` chỉ đặt tên docker network, không enforce hạn chế egress. | Document controlled-egress mode |

**Đã verify HOLDING (không giả định):** P0-3 system prompt tới provider; P0-2 tool_use ordering hợp lệ (kể cả sau prune); idempotency **sha256** (không phải builtin hash); SSH delete fail-closed (eval/exec/perl/nested-`$()` → deny); SQL read-only đúng driver; Docker fail-closed + `--network none` + hardening flags + `_container_user` map host UID; keyring fail-loud, resolve.py không silent-plaintext.
