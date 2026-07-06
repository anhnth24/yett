---
phase: 1
title: "Test and CI Harness"
status: completed
effort: "M"
---

# Phase 1: Test and CI Harness

## Overview
Dựng test làm lộ các lỗi vô hình TRƯỚC khi sửa (TDD). Ba lỗi P0 lớn nhất chỉ vô hình vì FakeProvider bỏ qua cấu trúc messages và CI chỉ Linux. Test viết dạng **`pytest.mark.xfail(strict=True)`** để chứng minh bug mà **CI vẫn XANH** (không land test đỏ lên nhánh chung); Phase 2/3/5 **gỡ marker** khi fix → test pass thường (nếu bug tái phát, strict-xfail lật thành fail). Priority **P1**. Không bị chặn.

## Requirements
- Functional: test kiểm chứng (a) provider nhận system prompt, (b) chuỗi assistant(tool_use)→tool_result đúng, (c) docker exec không network + không chạy host, (d) secret-perm đúng trên Windows.
- Non-functional: no-egress trong test (record/replay, không gọi mạng thật); docker test gated theo daemon (skip sạch nếu không có).

## Architecture
- **Real-provider contract test:** `OpenAICompatProvider` đã cho inject `http_post` (openai_compat.py:32). Dùng 1 fake `http_post` ghi lại `body` gửi đi → assert `body["messages"][0].role=="system"` và thứ tự tool_use/tool_result hợp lệ OpenAI. Đây là test làm lộ P0-2 + P0-3.
- **Docker integration test:** gate bằng `shutil.which("docker")` / ping daemon; nếu có → chạy exec `curl`/`python urllib` tới 1 host và assert fail (no network) + assert cwd/host không phải host thật. `pytest.mark.skipif` khi thiếu daemon.
- **CI matrix:** thêm `windows-latest` (và `macos-latest` nếu runner có) vào `.github/workflows/ci.yml`. Lưu ý import-linter dùng `PYTHONPATH=src` (POSIX) — trên Windows cần cú pháp khác hoặc set env qua step.

## Related Code Files
- Create: `tests/integration/test_real_provider_contract.py`
- Create: `tests/integration/test_docker_sandbox.py` (skipif no daemon)
- Modify: `.github/workflows/ci.yml` (matrix os; env cho lint-imports cross-platform)
- Modify: `README.md` (bảng status: cột "verified via fake" vs "verified e2e"; số test khớp thực tế)
- Reference: `src/yett/provider/openai_compat.py`, `src/yett/core/loop.py`, `src/yett/sandbox/docker.py`, `tests/unit/test_setup_wizard.py`

## Implementation Steps
1. Viết `test_real_provider_contract.py` (`xfail(strict=True)`): build App/loop với `OpenAICompatProvider(http_post=recorder)`, chạy 1 turn có tool_call; assert recorder thấy system message + assistant tool_use turn trước tool_result. Phase 2 gỡ marker.
2. Viết `test_docker_sandbox.py` (`skipif` no docker; body `xfail(strict=True)` khi có docker): assert exec trong container không egress + không thấy file host ngoài mount. Phase 3 gỡ marker.
3. Sửa `ci.yml`: `strategy.matrix.os: [ubuntu-latest, windows-latest]`; `runs-on: ${{ matrix.os }}`; bước lint-imports set `PYTHONPATH` cross-platform.
4. Windows leg: chạy full suite 1 lần, `skipif`/`xfail` test POSIX-only THẬT SỰ (RT-20) + test secret-perm (Phase 5 gỡ) để **leg Windows XANH**.
5. Cập nhật README: bỏ ✅ gây hiểu nhầm cho loop/docker/web_search; ghi rõ trạng thái thật; sửa số test (161/183 → số collect thực).
6. Chạy `pytest -q`: xác nhận test mới ở trạng thái `xfail` (CI XANH), xfail đúng lý do — KHÔNG để test đỏ.

## Success Criteria — ✅ DONE 2026-07-05 (commit `5c02fbc`)
- [x] `test_real_provider_contract.py` tồn tại, marker `xfail(strict=True)`, xfail đúng vì thiếu system prompt + tool_use turn (**CI XANH**, không đỏ). Phase 2 gỡ marker → pass. *(Đã xảy ra đúng vậy — Phase 2 gỡ, test pass thường.)*
- [x] `test_docker_sandbox.py` tồn tại, skip sạch khi không có daemon (verify trên máy dev — daemon tắt); có daemon → `xfail` cho tới Phase 3. *(Phase 3 đã gỡ marker.)*
- [x] **CI leg Windows XANH**: baseline Windows chỉ đỏ đúng 1 test (`test_file_secret_store`) → xfail win32 strict; không blanket-skip gì khác. *(Phase 5 đã fix + gỡ.)*
- [x] README status không còn ✅ cho hạng mục chưa verified e2e; số test khớp (227 collect tại thời điểm đó). *(Phát hiện thêm ✅ sai thứ 3: `web_search` không được app-wire — đã ghi vào README; Phase 7c document tiếp.)*

**Kết quả:** pytest sau phase: 224 passed, 1 skipped, 2 xfailed, 0 failed. Báo cáo: `reports/phase-01-report.md`.

## Risk Assessment
- Windows CI runner: Q4 đã chốt là **thêm `windows-latest`** (GitHub-hosted có sẵn). Rủi ro còn lại = phút CI Windows của tổ chức — nếu hạn chế thì chạy full suite Windows local 1 lần + `xfail`/`skipif` POSIX-only, ghi rõ giới hạn.
- import-linter/asyncio trên Windows có thể lộ lỗi phụ → xử lý trong phase tương ứng, không nuốt.

## Red Team Fixes (2026-07-05)
- **[RT-9 High] Không land test ĐỎ lên nhánh chung.** `ci.yml` chạy `pytest -q` mọi push (mọi nhánh) → test đỏ Phase-1 làm CI đỏ cho tất cả phase sau. Sửa: dùng `pytest.mark.xfail(strict=True)` cho contract test + docker test cho tới khi Phase 2/3 fix (khi fix xong, xfail strict tự chuyển thành fail nếu sau này regress). CI luôn xanh, vẫn "fail-loud" khi bug quay lại.
- **[RT-20 Med] Windows CI đỏ nhiều hơn 1 test.** Baseline cảnh báo full pytest KHÔNG tin cậy trên Windows (quyền temp/cache, process treo) và nhiều test dựa POSIX (`local.py::_adapt_shell` sh→cmd, cmdguard/immutable dùng path POSIX). Sửa: chạy full suite trên Windows 1 lần, phân loại lỗi, `skipif`/`xfail` các test POSIX-only THẬT SỰ, để leg Windows xanh và chỉ đỏ ở chỗ có chủ đích (Phase 5 sẽ xanh test secret-perm).
