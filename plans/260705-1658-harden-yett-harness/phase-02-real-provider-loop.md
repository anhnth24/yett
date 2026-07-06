---
phase: 2
title: "Real-Provider Loop"
status: completed
effort: "M"
---

# Phase 2: Real-Provider Loop

## Overview
Sửa các khiếm khuyết khiến agent loop KHÔNG chạy được với LLM thật (chỉ FakeProvider chịu): (P0-3) system prompt không tới model, (P0-2) assistant tool_use turn không vào context, và resume không khôi phục được ngữ cảnh. Priority **P1**. `blockedBy: [1]` (verify bằng contract test Phase 1). Ưu tiên #1.

## Requirements
- Functional: (P0-3) system prompt/capabilities/safety vào request; (P0-2) assistant message chứa tool_use ghi vào context TRƯỚC tool_result, đúng thứ tự OpenAI/Anthropic; resume khôi phục đủ message history (assistant tool_use + tool_result), không lặp side-effect.
- Non-functional: deterministic context assembly (RG); token estimate phản ánh cả tool_call payload.

## Architecture
- **P0-3 system prompt:** `Message` đã hỗ trợ `role="system"` (`provider/base.py:31`). Prepend `Message(role="system", content=system)` vào `messages` trong `assemble_context` (`context.py:58-64`); `_to_openai_msg` map role system đúng. (Không cần đổi chữ ký `chat()`.)
- **P0-2 tool_use turn:** mở rộng `Message` thêm `tool_calls: list[ToolCall] | None`; `Context.add_assistant_tool_calls(tool_calls)`; `_to_openai_msg` phát `{"role":"assistant","tool_calls":[...]}`. tool_result giữ `tool_call_id` (đã có).
- **[RT-2] Resume provider-agnostic:** checkpoint hiện chỉ lưu `{"completed_tool_calls": completed}` (ids); `app.py:238` rebuild Context mới mỗi turn; tool_call id do provider cấp (non-deterministic). Sửa: persist **cả assistant tool_use turn + nội dung tool_result** vào checkpoint; rebuild Context từ đó khi resume; khóa idempotency theo **signature ổn định `tool name + hash(args)`**, KHÔNG theo id provider.
- **[RT-3] Canceled resumable:** cancel mark `status='canceled'` (loop.py:170-173) nhưng resume guard chỉ nhận `'running'` (loop.py:96) → turn hủy load `completed=[]` và chạy lại tool. Sửa: coi `canceled` là resumable (giữ `completed`) hoặc dùng trạng thái `interrupted` riêng.
- **[RT-12] Token estimate:** `context.py:35-39` chỉ tính `m.content`; assistant tool_use content rỗng nhưng args lớn → ước lượng ~0, `prune()` chỉ nhắm role `tool` → ContextOverflow có thể livelock. Sửa: cộng args serialize của tool_call vào `estimate_tokens`; `prune` loại cặp tool_use↔tool_result cùng nhau (giữ tính hợp lệ cặp).

## Related Code Files
- Modify: `src/yett/core/context.py` (system message + `add_assistant_tool_calls` + estimate_tokens tính tool_calls + prune theo cặp)
- Modify: `src/yett/core/loop.py` (ghi assistant tool_use turn; resume restore; canceled resumable)
- Modify: `src/yett/core/checkpoint.py` (**RT-2** — persist message history, không chỉ ids)
- Modify: `src/yett/provider/base.py` (`Message.tool_calls`)
- Modify: `src/yett/provider/openai_compat.py` (`_to_openai_msg` phát assistant tool_calls)
- Modify: `src/yett/provider/fake.py` (tương thích test cũ)
- Modify: `tests/integration/test_real_provider_contract.py` (Phase 1 — **gỡ/flip `xfail`** sau khi fix, xem Step 6)

## Implementation Steps
1. Thêm system message vào `assemble_context`; contract test phần system → xanh.
2. Mở rộng `Message.tool_calls` + `_to_openai_msg` xử lý assistant tool_calls.
3. `loop.py`: trước `for tc in res.tool_calls` gọi `ctx.add_assistant_tool_calls(res.tool_calls)`; tránh double khi `force_text`.
4. **Checkpoint (RT-2):** đổi schema `checkpoint.py` persist assistant tool_use + tool_result content; rebuild Context khi resume; idempotency theo signature `tool+hash(args)`.
5. **Canceled resumable (RT-3):** resume guard nhận cả `running` và `canceled` (hoặc `interrupted`); test resume-sau-cancel không chạy lại tool đã completed.
6. **Token estimate (RT-12):** cộng tool_call args vào `estimate_tokens`; `prune` theo cặp.
7. Cập nhật `fake.py`/test cũ; **gỡ marker `xfail(strict=True)`** khỏi contract test (Phase 1) để nó pass thường (nếu để nguyên, XPASS strict làm CI fail); chạy full loop test + contract test.

## Success Criteria — ✅ DONE 2026-07-05 (merge `66cb0b6`)
- [x] Contract test (Phase 1) **pass thường sau khi gỡ xfail**: `body["messages"][0].role=="system"` và mọi tool_result có assistant tool_use đứng trước, id khớp.
- [x] `test_core_loop.py` cũ vẫn xanh (1 assertion cập nhật theo schema checkpoint mới — có chủ đích, ghi trong report).
- [x] **Checkpoint persist message history** (assistant tool_use + tool_result content), không chỉ ids; idempotency theo signature `tool+hash(args)`.
- [x] **Resume sau CANCEL**: context có đủ assistant/tool history, KHÔNG chạy lại tool đã completed (`tests/unit/test_resume_checkpoint.py`, 4 test — RT-2/3/12).
- [x] Token estimate tính cả tool_call payload; ContextOverflow không livelock (RT-12).
- [x] mypy strict xanh sau khi đổi `Message`.

**Kết quả:** report `reports/phase-02-report.md` (gồm 2 design decision hoà giải RT-2 idempotency vs anti-loop test).

## Risk Assessment
- Đổi `Message` là contract nội bộ → rà mọi caller (context, loop, provider, fake, tests). import-linter giữ core không import adapter.
- Anthropic adapter chưa có; chỉ cần openai_compat đúng; ghi chú cho Anthropic native sau.

## Red Team Fixes log (2026-07-05)
RT-2 (checkpoint schema + signature idempotency), RT-3 (canceled resumable), RT-12 (token estimate) đã **fold vào Requirements/Steps/Success/Related Files** ở trên. Cross-phase RT-9: Step 7 gỡ `xfail` của contract test Phase 1.
