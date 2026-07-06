---
phase: 6
title: "Policy Gate Defense-in-Depth"
status: completed
effort: "M"
---

# Phase 6: Policy Gate Defense-in-Depth

> **Resume Notes (2026-07-05 — phase deferred, chưa có code):**
> - `blockedBy: [2, 4]` ĐÃ THOẢ — Sóng 1 merged tại `be04fa9`. Agent worktree PHẢI `git merge claude/harness-reference-architecture-0wwuek` trước khi làm (worktree snapshot cũ hơn Sóng 0).
> - **User đã verify bằng snippet (2026-07-05)** hai lỗ P1-11/P1-16 là THẬT trên code hiện tại: `wiring.py:56` Gate allow `echo safe` → hook mutate thành `rm -rf /` → tool vẫn chạy args mới (không re-gate); `wiring.py:76` hook inject `sk-cp-...` sau filter → trả ra không redact. Regression test phải tái hiện đúng 2 kịch bản này.
> - **Handoff Phase 4:** đọc `reports/phase-04-report.md` — thread driver thật vào gate-time `ImmutableCore.check` qua `wiring.py`; GIỮ multi-dialect fail-closed fallback khi hint vắng.
> - **Sau khi code xong: adversarial review.** Chỉ thị gốc dùng `/codex:adversarial-review`; user cập nhật 2026-07-05: **codex hết quota → dùng `code-reviewer` subagent nội bộ** thay thế.
> - **Review Sóng 0+1 xác nhận M2 = mục P1-8 của phase này (ImmutableCore inert — `app.py:164` wire BasicGate, PolicyEngine không được khởi tạo).** Wiring ImmutableCore vào gate đang chạy là ưu tiên #1. Liên quan: **M1** (denylist.check_exec bypass quote/interpreter — cân nhắc áp `cmdguard.classify` cho tool `exec`, KHÔNG chỉ ssh), **L1** (journalctl không gate sub-command), **M3** (resume at-least-once khi crash — quyết + document guarantee). Chi tiết: `reports/from-advisor-to-orchestrator-260705-2146-merged-hardening-adversarial-review.md`. M1/L1 nếu vượt file-ownership (cmdguard/denylist) → để cổng cuối, đừng phá ownership đang chạy.

## Overview
Đóng các đường vòng qua Policy Gate và siết enforcement thật (không chỉ hiển thị). Priority **P2** (đa số cần config/hook trusted mới khai thác, nhưng phá bất biến "gate là đường duy nhất"). Gồm 6 finding gate/scope.

## Requirements
- Functional: mọi tool call THỰC THI phải qua gate với args cuối; subagent toolset con enforce lúc chạy; immutable core bảo vệ path bằng so khớp canonical; log_read không đọc file ngoài allowlist.
- Non-functional: giữ fail-closed; không nới quyền qua hook/subagent.

## Architecture
- **P1-8 immutable path — [RT-2/RT-15 High] tách 2 case:** `immutable.py:38` `target = path or cmd`; với `write_file` target là path thật → `Path.resolve()` + `is_relative_to` OK. Nhưng với `exec` target là **cả command string** (`sed -i policy.yaml`) → `Path('sed -i policy.yaml').resolve()` vô nghĩa, is_relative_to KHÔNG bắt (còn tệ hơn substring cho case path tuyệt đối). Sửa: **write_file** → canonicalize `args['path']` + is_relative_to; **exec/ssh_exec** → shlex-parse cmd lấy các file operand, canonicalize từng cái so với protected dir (hoặc đơn giản+fail-closed: deny exec nếu token nào chạm basename protected). Regression test CẢ hai vector (`write_file` + `exec sed -i policy.yaml`).
- **P1-10 subagent subset — [RT-8 High] không dựa ctx.allowed_tools:** `SessionCtx` Protocol (`gate.py:24-27`) và `_SimpleCtx` (`loop.py:85,193-195`) CHỈ có `session_key` — không có `allowed_tools`. Check `tc.name ∈ ctx.allowed_tools` sẽ AttributeError/luôn-sai. Sửa: truyền `allowed_tools` làm **tham số tường minh** vào `execute_tool` (run_turn đã nhận nó ở loop.py:81), `None` = không giới hạn; enforce subset từ param đó, không duck-type qua ctx.
- **P1-11 PreToolUse re-gate:** `wiring.py:59` hook mutate args SAU gate, args mới không re-gate → denylist bị vượt. Sửa: re-run `safe_evaluate` trên `mutated_args` trước validate/run. **[RT-7 phụ] Xử lý verdict lần 2 đầy đủ:** nếu re-gate trên mutated_args cho `deny` → trả `[DENIED]`; nếu cho `need_approval` (mutate làm allow→need_approval) → **request approval LẠI với mutated_args**, approval cũ (của args gốc) KHÔNG dùng lại; approver vắng/timeout → deny fail-closed. Không được "re-gate nhưng vẫn chạy vì approval cũ".
- **P1-16 PostToolUse re-filter:** `wiring.py:70,77-78` mutate result SAU filter, return thẳng. Sửa: chạy PostToolUse TRƯỚC filter, hoặc filter lại `mutated_result` trước return (khớp thứ tự `runner.py:3` document).
- **P1-9 log_read fnmatch — [RT-13 Med] path là REMOTE POSIX:** `ssh_exec.py:96` so path remote; controller có thể chạy Windows (platform quảng cáo, Phase 1 thêm CI Windows). Đừng dùng `Path.resolve()` local (sẽ diễn giải theo Windows, sai + chạm FS local). Sửa: dùng `PurePosixPath` + chuẩn hóa lexical (gộp `..`/`.` không đụng FS), reject path thoát prefix khai báo; KHÔNG glob lỏng.
- **Phụ (map):** `allowlist.py:26` + `schema.py:21` dùng `re.search` (substring) → allow over-broad (`{"cmd":"ls"}` khớp `ls; rm`). Sửa: anchor (`fullmatch` hoặc document `^...$`) + normalize path trước match.

## Related Code Files
- Modify: `src/yett/policy/immutable.py` (canonical path — tách write_file/exec)
- Modify: `src/yett/tools/wiring.py` (`execute_tool` nhận param `allowed_tools`; subagent subset enforce; Pre re-gate + need_approval lần 2; Post re-filter)
- Modify: `src/yett/core/loop.py` (**RT-8** — truyền `allowed_tools` (đã có ở loop.py:81) vào `execute_tool`)
- Modify: `src/yett/rpc/broker.py` (**RT-8** — broker cũng gọi `execute_tool` → phải truyền `allowed_tools` của subagent, không thì RPC vượt subset)
- Modify: `src/yett/tools/remote/ssh_exec.py` (log_read containment PurePosixPath)
- Modify: `src/yett/security/allowlist.py`, `src/yett/policy/schema.py` (anchor matching)
- Modify: `tests/unit/test_phase3.py`, `test_ssh_gate.py`, `test_skills_hooks_sched.py`, `test_security_basic.py`

## Implementation Steps
1. immutable.py: canonical path compare + fail-closed; test `sed -i policy.yaml` (relative) → deny.
2. `execute_tool` nhận param `allowed_tools`; `loop.py` (loop.py:81) và `rpc/broker.py` truyền nó vào; enforce `tc.name ∈ allowed_tools` (None=không giới hạn); test subagent (kể cả qua RPC) gọi tool ngoài tập → deny.
3. wiring.py: Pre-hook re-gate `mutated_args`; nếu verdict lần 2 = deny → `[DENIED]`; = need_approval → request approval LẠI với mutated_args (không dùng approval cũ), vắng approver → deny; test hook đổi `ls`→`rm -rf /` → deny, và hook đổi sang lệnh cần duyệt → phải hỏi duyệt lại.
4. wiring.py: Post-hook filter lại mutated_result; test hook chèn secret sau filter → bị redact.
5. ssh_exec.py: log_read containment; test path traversal → deny.
6. allowlist/schema: anchor match; test over-broad `{"cmd":"ls"}` không khớp `ls; rm`.
7. Chạy toàn bộ test gate/security/hooks.

## Success Criteria — ✅ DONE 2026-07-05/06 (merge `a7165e7` + M2/H1 fix `9b29f35`)
- [x] `sed -i policy.yaml` (path tương đối) → immutable core DENY. **+ [M2] chạy qua gate THẬT của App** (không chỉ PolicyEngine dựng tay): `_ImmutableFirstGate` wrap `BasicGate` (`app.py`), test `test_app_wires_immutable_core_into_running_gate`. **+ [H1]** bóc redirect (`>policy.yaml`) + so khớp mọi path component (con của protected dir).
- [x] Subagent gọi tool ngoài toolset con → DENY lúc execute (qua cả loop VÀ RPC broker).
- [x] PreToolUse re-gate: mutate `ls`→`rm -rf /` → chặn; mutate sang lệnh cần duyệt → hỏi approval lại với args mới. **Lưu ý fix `a55859a`:** re-gate/re-approve CHỈ khi hook thực sự đổi args (empty HookRunner trước đây trả copy → hỏi duyệt 2 lần).
- [x] PostToolUse mutate chèn secret → filter lại redact trước khi vào context.
- [x] log_read path traversal → DENY (PurePosixPath); allowlist arg match anchored (fullmatch).

**Review sau merge:** M2 (ImmutableCore inert) phát hiện bởi advisor → đã fix (`9b29f35`). Chi tiết: `reports/from-advisor-to-orchestrator-260705-2215-phase6-gate-adversarial-review.md`.

## Risk Assessment
- Anchor matching có thể phá allow-rule mẫu đang dựa vào substring → rà `config/harness.example.yaml` và cập nhật rule cho khớp; test không làm hỏng luồng hợp lệ.
- Re-gate/re-filter thêm chi phí mỗi call — chấp nhận (đúng đắn > tốc độ ở đường security).
- Song song: Phase 6 chung `core/loop.py` với Phase 2 và `policy/immutable.py` + `test_phase3.py` với Phase 4 → **6 phải sau 2 và 4** (`blockedBy: [2,4]`, Sóng 2). `wiring.py`/`broker.py`/`ssh_exec.py`/`allowlist.py`/`schema.py` là của riêng Phase 6. Phase 6 và 7 KHÔNG đụng file nhau → chạy song song trong Sóng 2 được. Xem ma trận xung đột ở `plan.md`.
