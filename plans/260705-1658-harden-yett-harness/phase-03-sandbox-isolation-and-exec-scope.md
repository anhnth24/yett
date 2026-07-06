---
phase: 3
title: "Sandbox Isolation and Exec Scope"
status: completed
effort: "M"
---

# Phase 3: Sandbox Isolation and Exec Scope

## Overview
Khôi phục bất biến no-egress: wire DockerSandbox thật (mount workspace + `--network none` + hardening), fail-closed khi Docker thiếu. Priority **P1**. `blockedBy: [1]`. Cam kết "sống/chết" #1.

> **Q1 đã chốt (2026-07-05):** LocalSandbox = CHỈ dev/test; production chạy **Docker Desktop** (`backend: docker`). Docker thiếu → **fail-closed** (từ chối exec), KHÔNG hạ cấp host. Containment thật đến từ Docker (mount + network none), KHÔNG từ scope-check cwd. Vì vậy: exec.cwd scope-check vẫn giữ nhưng chỉ là vệ sinh (không phải hàng rào chính); mục env-allowlist LocalSandbox **CẮT** (không có phơi nhiễm prod).

## Requirements
- Functional: `backend=docker` → DockerSandbox với **workspace được mount** + `--network none` + hardening; exec chạy được trong container. Docker không sẵn sàng → **fail startup/từ chối exec** (không host fallback).
- Non-functional: hardening đúng docstring (drop caps, no-new-privileges, read-only rootfs + tmpfs scratch, non-root, pids-limit, mem/cpu limit, `--rm`, timeout).

## Architecture
- **P0-1 wire docker:** `app.py:48` `sandbox = LocalSandbox() if backend=="local" else None`; `app.py:81` `sb = sandbox or LocalSandbox()` → docker=None→host. Sửa: khởi tạo `DockerSandbox` khi `backend=="docker"`; Docker/daemon vắng → raise `UserFacingError` rõ (bỏ `or LocalSandbox()`). Sửa docstring sai `local.py:4`.
- **[RT-1] Mount workspace:** `DockerSandbox.__init__(..., mounts=None→{})` (docker.py:19-22) không mount gì; chỉ ExecTool dùng sandbox. `build_app` phải **tính & truyền `mounts`**: host `workspace_root` + project roots → path container cố định (vd `/workspace`, rw); tmpfs cho scratch. Không mount → exec không thấy file project (+ `--read-only` càng chặn).
- **[RT-1/RT-14] Dịch host→container cho cwd:** `ProjectScope.resolve_in_scope(cwd)` trả path **host**; DockerSandbox `-w` cần path **container** (docker.py:42-43). Luồng đúng: scope-check path host TRƯỚC → map sang path container theo mount → truyền `-w`.
- **P1 docker hardening:** `docker.py:24-27` thiếu flag; `--network` ternary no-op. Bổ sung đủ flag (xem Requirements non-functional).
- **exec.cwd (P1-15, hạ mức):** `exec.py:36` truyền `cwd` thẳng. Scope-check `cwd` qua ProjectScope (vệ sinh) — nhưng **không claim** nó bao được truy cập file: `sh -c <cmd>` (exec.py:35) vẫn cho `cd /`, path tuyệt đối thoát. Containment thật = Docker isolation. Trên LocalSandbox (dev/test) exec không được coi là an toàn.

## Related Code Files
- Modify: `src/yett/app.py` (khởi tạo DockerSandbox theo backend; **tính & truyền mounts**; fail-closed; inject scope + map host→container cwd cho ExecTool)
- Modify: `src/yett/sandbox/docker.py` (hardening flags thật; nhận mounts)
- Modify: `src/yett/sandbox/local.py` (sửa docstring; đánh dấu dev/test-only)
- Modify: `src/yett/tools/builtin/exec.py` (scope-check cwd + map sang container path)
- Modify: `src/yett/doctor.py` (báo rõ backend=docker mà thiếu Docker → fail)
- Verify: `tests/integration/test_docker_sandbox.py` (Phase 1 — **gỡ/flip `xfail`** sau khi fix)

## Implementation Steps
1. `build_app`: `backend=="docker"` → `DockerSandbox(cfg.sandbox, mounts=<map>)`; Docker vắng → raise `UserFacingError` (không `or LocalSandbox()`).
2. **Mount (RT-1):** tính `mounts` host workspace_root/project roots → `/workspace` (rw) + tmpfs scratch; truyền vào DockerSandbox.
3. `docker.py`: đủ hardening flags; bỏ ternary no-op; nhận & phát `-v` cho mounts.
4. **cwd host→container (RT-1/RT-14):** ExecTool scope-check `cwd` (host) → map sang path container theo mount → truyền `-w`; cwd ngoài scope → `UserFacingError`.
5. `local.py`: sửa docstring, đánh dấu dev/test-only (không env-allowlist — đã cắt).
6. `doctor.py`: cảnh báo backend=docker nhưng Docker vắng.
7. Chạy docker integration test (nếu có daemon) + unit test exec scope/cwd-mapping; **gỡ marker `xfail`** của docker test (Phase 1) để pass thường.

## Success Criteria — ✅ DONE 2026-07-05 (merge `bc69154`; e2e docker CHƯA chạy — máy dev không có daemon)
- [ ] `backend=docker` + Docker có → **workspace mounted, exec chạy được trong container**, `--network none`, không egress (integration test pass sau khi gỡ xfail). *(Code + test đã land, xfail đã gỡ; **e2e chưa chạy** vì không có daemon — verify bằng unit test trên argv. Còn lại: chạy trên máy/CI có Docker.)*
- [x] cwd host hợp lệ được **map đúng sang path container**; cwd ngoài scope → từ chối (unit test — `tests/unit/test_docker_wiring.py`, 26 test).
- [x] `backend=docker` + Docker thiếu → fail-closed (`probe_docker` → `UserFacingError`), KHÔNG chạy trên host.
- [x] docker.py có đủ hardening flags docstring hứa; không còn ternary no-op.
- [x] `local.py` docstring đúng (dev/test-only); không claim exec.cwd bao truy cập file.

**Kết quả:** report `reports/phase-03-report.md`.

### Addendum — review finding 2026-07-05 (đã fix cùng ngày)
`--user 65534:65534` cứng + mount `:rw` → trên Linux/macOS native, workspace do UID user host sở hữu sẽ KHÔNG ghi được từ container. Fix: `_container_user()` — POSIX dùng UID/GID host thật (uid 0 → ép 65534 giữ non-root), Windows host giữ 65534 (Docker Desktop tự map quyền). Integration test bổ sung bước **ghi** file trong mounted cwd + đọc lại từ host; unit test map UID POSIX + fallback root/Windows.

## Risk Assessment
- Q1 đã chốt → không còn phụ thuộc câu hỏi mở. Thiết kế mount (host→`/workspace`) cần khớp ProjectScope roots; test mapping cw.
- Docker test cần daemon trong CI → gate skip; đừng để đỏ giả trên runner không có Docker.

## Red Team Fixes log (2026-07-05)
RT-1 (mount workspace + host→container cwd) và RT-14 (exec.cwd không phải containment) đã **fold vào Requirements/Architecture/Steps/Success**. RT-25 (env-allowlist) **CẮT** theo Q1. Cross-phase RT-9: Step 7 gỡ `xfail` docker test Phase 1.
