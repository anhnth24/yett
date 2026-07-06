---
phase: 5
title: "Secret Handling"
status: completed
effort: "M"
---

# Phase 5: Secret Handling

## Overview
Vá bất biến "secret không bao giờ vào context" + bảo vệ secret tại chỗ. Priority **P1**. Gồm: filter lọt body private key, pattern key mới/DSN, chmod Windows no-op, backend downgrade im lặng, và xử lý key đã lộ.

## Requirements
- Functional: private key/PII/DSN bị redact HOÀN TOÀN trước khi vào context; **implement backend `keyring` thật** (fallback `age`) mã hoá secret tại chỗ; **bỏ silent-fallback plaintext** — chỉ fail startup (có remediation) khi library của backend đã chọn thiếu ở runtime; secret file bảo vệ owner-only trên Windows.
- Non-functional: fail-loud (không nuốt lỗi chmod/backend); không nhúng secret vào log/span/checkpoint.

> **Quyết định keyring (2026-07-05) — nhất quán toàn phase:** `keyring`/`age` được **implement thật**, GIỮ trong enum. `resolve.py` KHÔNG tụt plaintext im lặng nữa; nếu backend chọn nhưng thư viện chưa cài → **fail startup có remediation** ("cài `keyring`, hoặc đổi sang `env`/`file`"). Không có mâu thuẫn "implement vs fail-startup": implement là mặc định; fail-startup chỉ là nhánh khi thiếu library.

## Architecture
- **P0-5 private key body:** `filters.py:19` pattern chỉ khớp header PEM (không DOTALL) → body lọt. Sửa: khớp cả block `-----BEGIN...-----` … `-----END...PRIVATE KEY-----` (DOTALL) và redact toàn khối. **[RT-15 Med] + fallback output bị cắt:** khi thiếu footer END (`head -c 400 id_rsa`, log phân trang, exec bị cap size) block-match không khớp → body lọt. Thêm pattern (2) redact từ header BEGIN tới hết-chuỗi khi không có footer, + heuristic redact chuỗi base64 dài ngay sau header PRIVATE KEY.
- **P1-7 + [RT-6/RT-19 High] pattern phải phủ `sk-cp-`:** `filters.py:16` `sk-[A-Za-z0-9]{32,}` **không khớp** `sk-cp-...` (đứt ở dấu `-`) — đây CHÍNH là format key MiniMax đã lộ (`config/harness.yaml:22`). Phase gốc chỉ thêm sk-proj-/svcacct-/admin-, vẫn quên sk-cp-. Sửa: pattern generic tha prefix provider, vd `sk-(?:[a-z]+-)?[A-Za-z0-9]{20,}` (hoặc `sk-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+` có sàn độ dài), **fixture test dùng đúng shape `sk-cp-` khẳng định bị redact**. `filters.py:18` thêm ODBC/ADO DSN `Pwd=/Password=`; `filters.py:39` `redact_attrs` redact **đệ quy** dict/list lồng. `/api/config` redaction (Phase 7) dùng chung path này.
- **P1-13 Windows chmod:** `file_store.py:29` `os.chmod(0o600)` no-op trên Windows + `except OSError: pass` nuốt lỗi. Sửa: trên Windows set DACL owner-only (icacls hoặc pywin32); nếu không set được → **cảnh báo to** (log/stderr), không im lặng. POSIX giữ nguyên. (Fix này làm `test_setup_wizard.py::test_file_secret_store` xanh trên Windows — Phase 1 CI leg.)
- **P1-17 backend downgrade → IMPLEMENT `keyring` thật (Q2/Q3 đã chốt).** Quyết định 2026-07-05: at-rest dùng backend `keyring` (OS credential manager — Windows Credential Manager / macOS Keychain / Linux Secret Service, mã hoá tại chỗ, không thêm hạ tầng), fallback `age` (file mã hoá). KHÔNG tự build vault; HashiCorp Vault chỉ khi khách có sẵn. `resolve.py:15-18` thay silent-fallback bằng: nếu backend chọn nhưng thư viện/adapter chưa sẵn → **fail startup có remediation** (`cli.py:70,195`), không tụt plaintext câm. Giữ `keyring`/`age` trong `models.py` enum (ta muốn dùng), chỉ fail khi runtime thiếu.

> **Q3 chốt — phân lớp rõ:** *at-rest* (keyring/age, mục này) bảo vệ **đĩa**; *không lộ lên LLM* là việc của **resolve-at-point-of-use + Result Filters** (P0-5/P1-7/RT-6/RT-15 ở trên). Vault KHÔNG chặn leak-to-LLM nếu code nhét secret vào tool result — filter mới chặn. Ưu tiên vá filter. Key MiniMax đã **rotate** (xong).
- **P1-14 key đã lộ:** key MiniMax `config/harness.yaml:22` đã **rotate** (xong). Q3 đã chốt: nghiêng **bỏ inline `api_key` / bắt buộc secret store**; nếu giữ làm local-convenience thì `/api/config` GET **phải redact** (dùng chung filter path — Phase 7a) + doc cảnh báo. Chốt cụ thể giữ/bỏ khi cook Phase 5.

## Related Code Files
- Modify: `src/yett/security/filters.py` (private key block, pattern key mới, redact đệ quy)
- Modify: `src/yett/secrets/file_store.py` (Windows ACL + fail-loud)
- Modify: `src/yett/secrets/resolve.py` (dựng KeyringSecretStore/AgeSecretStore; bỏ silent-fallback; fail startup có remediation khi lib thiếu)
- Create: `src/yett/secrets/backends.py` bổ sung `KeyringSecretStore` (dùng lib `keyring`) + `AgeSecretStore` (nếu làm `age`)
- Modify: `src/yett/config/models.py` (**GIỮ** keyring/age trong enum — đã chốt dùng)
- Modify: `pyproject.toml` (thêm dep `keyring` — pin exact theo chính sách supply-chain)
- Modify: `tests/unit/test_filters.py`, `tests/unit/test_setup_wizard.py`
- Ops (ngoài code): rotate key MiniMax; xoá/chuyển inline key sang secret store

## Implementation Steps
1. filters: pattern private key block (DOTALL) + redact đệ quy nested; test với key PEM thật (fixture), sk-proj-, ODBC DSN, dict lồng.
2. file_store: Windows DACL owner-only (icacls) hoặc pywin32; fail-loud khi không set được; POSIX giữ chmod 600.
3. **Implement `KeyringSecretStore`** (dùng lib `keyring`, mã hoá qua OS store) trong `backends.py`; `resolve.py` dựng nó cho `backend=="keyring"`. (Age tuỳ chọn — làm nếu cần portable.)
4. `resolve.py`: bỏ silent-fallback; nếu library backend chọn thiếu → `UserFacingError` có remediation. GIỮ keyring/age trong `models.py` enum.
5. `pyproject.toml`: thêm `keyring` (pin exact).
6. Chạy `test_filters.py`, `test_setup_wizard.py`, + test mới cho KeyringSecretStore (mock OS store) (Windows CI leg sẽ xanh).

## Success Criteria — ✅ DONE 2026-07-05 (merge `1c3393f`; keyring==25.6.0 pinned)
- [x] `cat id_rsa` qua tool → context chỉ thấy `[REDACTED]`, KHÔNG có body base64 — kể cả output bị cắt thiếu footer (RT-15).
- [x] Key `sk-cp-`/`sk-proj-` + ODBC DSN Pwd= bị redact; secret trong dict/list lồng bị redact đệ quy.
- [x] `test_file_secret_store` xanh trên Windows (icacls + SID từ `whoami /user` — tránh nhập nhằng tên máy trùng tên user); fail-loud khi không set được.
- [x] `secret_backend=keyring` hoạt động thật (KeyringSecretStore, test mock — `keyring.testing` ImportKiller hỏng trên Py3.13, dùng `sys.modules[name]=None`); lib thiếu → fail startup có remediation; `age` trong enum → UserFacingError (chưa implement, có chủ đích).
- [x] Key MiniMax đã rotate (trước plan); **quyết định inline key: GIỮ** làm local-convenience + warning comment trong `harness.example.yaml`; redact `/api/config` GET → Phase 7a.

**Kết quả:** report `reports/phase-05-report.md`.

### Addendum — review finding 2026-07-05 (đã fix cùng ngày)
`icacls /inheritance:r + /grant:r *SID:F` KHÔNG xoá ACE explicit có sẵn → `Everyone:(F)` sống sót trên thư mục mở. Fix trong `file_store.py`: sau grant owner, liệt kê DACL thật, `/remove` mọi principal khác owner (theo đúng tên icacls in ra — không phụ thuộc locale), verify lại, còn sót → cảnh báo to. Regression test mới: file gắn sẵn ACE `*S-1-1-0:F` (Everyone) → sau `store.set` phải sạch.

## Risk Assessment
- Windows ACL: `icacls` subprocess vs pywin32 (thêm dep). Ưu tiên `icacls` (stdlib subprocess, không thêm dep). Test trên CI Windows.
- Đổi filter pattern có thể over-redact nội dung hợp lệ → test cả trường hợp không nên redact.
- File tách bạch với Phase 4/6 → chạy song song Phase 4 an toàn.
