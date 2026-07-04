# Harness Master Plan — Kế hoạch tổng quan

> **Trạng thái:** Draft để team review · 04/07/2026
> **Đầu vào:** `harness-reference-architecture.md` (kiến trúc 9 khối) + `harness-deep-comparison.md` (verification 3 repo)
> **Quyết định đã chốt:** Core **Python** (>=3.11) · Deploy **on-prem có Docker** · **Single-tenant** (deploy-per-tenant) · Storage v1 **SQLite**
> **Quy ước:** Nhận định/ước lượng đánh dấu **[Inference]**. Sự kiện từ repo đã verify trong `harness-deep-comparison.md` không đánh dấu lại.

---

## 1. Vì sao nên tự build (thay vì fork một repo có sẵn)

Câu hỏi phải trả lời trước khi tốn công: *sao không fork?* Sau khi verify 3 repo, câu trả lời cụ thể:

| Phương án | Vì sao KHÔNG |
|---|---|
| Fork **OpenClaw** | TypeScript (đã chốt Python). Quan trọng hơn: security model của nó là **trust-owner by design** — docs nói thẳng default `security="full"`, `ask="off"` là "intentional UX", sandbox mặc định off, và "không phải hostile multi-tenant boundary". Đảo ngược một default ăn sâu vào thiết kế là rework lớn hơn viết mới phần đó. **[Inference]** |
| Fork **Hermes** | Đúng ngôn ngữ, nhưng là monolith personal-agent: skills engine, memory, RPC đều dính vào `conversation_loop.py` của nó. Gỡ personal-agent assumptions (DM pairing, ~/.hermes home, YOLO mode) để nhét compliance/fail-closed vào core loop ≈ viết lại core loop. **[Inference]** |
| Fork **GoClaw** | Thiết kế đúng nhất cho enterprise nhưng **CC BY-NC 4.0 — cấm thương mại**. Không bàn thêm. |
| Build từ số 0 tuyệt đối | Không cần thiết — 2 repo MIT cho phép vendor code, GoClaw docs cho phép học pattern. Tự viết 100% là bỏ phí nguồn đã verify. |

**Kết luận:** build harness riêng với core loop + security + compliance tự viết, **vendor module hạ tầng từ Hermes (MIT)**, **vendor format/spec từ OpenClaw (MIT)**, **học pattern kiến trúc từ GoClaw docs (clean-room)**. Đây là con đường duy nhất thỏa đồng thời: pháp lý sạch, Python, fail-closed, on-prem gov.

**Vì sao đáng làm (giá trị):**
1. Không sản phẩm nào trên thị trường OSS thỏa bộ ràng buộc gov on-prem + fail-closed + compliance VN (đã khảo sát 3 repo đại diện nhất — cả 3 đều trust-owner ở mức độ khác nhau).
2. Deploy-per-tenant + single binary/container → mô hình bán theo dự án của F-Project khớp tự nhiên. **[Inference]**
3. Kiến thức harness tích lũy là tài sản dài hạn của team, không phụ thuộc license bên thứ ba.

---

## 2. Nguyên tắc chỉ đạo (không thương lượng)

1. **Hai cổng sống/chết** (từ tài liệu gốc §4) — mọi phase phải giữ:
   - (a) Mọi data (memory, traces, tool I/O) ở on-prem, không egress.
   - (b) Guardrails enforce trong execution path, fail-closed — chỉ log thì coi như chưa có.
2. **Security là enforcement, không phải advice** — policy check chạy *trước* khi tool thực thi, deny khi không chắc.
3. **Observability từ ngày đầu** — span/trace là first-class từ commit đầu tiên, không bolt-on (bài học GoClaw: tracing built-in là thứ làm nó nổi bật).
4. **Vendor > Learn > Build** — theo thứ tự đó. Chỉ tự viết khi không vendor/học được (xem ma trận §3).
5. **YAGNI có kỷ luật** — semantic memory/KG, multi-agent, self-evolution: chỉ mở khi có bằng chứng cần (xem §4 Defer).
6. **Clean-room với GoClaw** — chỉ đọc `docs/`, không đọc `*.go`; người đọc viết spec bằng lời của mình, dev implement từ spec (xem §5.1).

---

## 3. Ma trận tổng thể: Làm gì — Học từ đâu — Vì sao

Cột "Vì sao nguồn này" là lý do chọn nguồn, đã kiểm chứng trong `harness-deep-comparison.md`.

| # | Khối | Làm gì | Nguồn | Vì sao nguồn này |
|---|---|---|---|---|
| 1 | **Exec sandbox** | VENDOR `tools/environments/` của Hermes (`base.py` + `local.py` + `docker.py` + `file_sync.py`), bỏ 4 backend còn lại | Hermes (MIT) | Module tự chứa duy nhất vendor được nguyên vẹn trong cả 3 repo; ABC sạch (subclass chỉ cần `_run_bash()` + `cleanup()`); Docker hardening (drop ALL caps, no-new-privileges, resource limits) có sẵn; giữ abstraction nên thêm Singularity sau này rẻ |
| 2 | **Provider adapter** | Tự viết theo pattern: interface 4 method, 9 canonical failover reasons, capability registry, context-overflow → compaction (không failover) | GoClaw `docs/02-providers.md` (clean-room) | Interface thiết kế chuẩn nhất trong 3 repo; doc tự chứa (signature + contract, ít code) nên clean-room khả thi; Hermes/OpenClaw không có tài liệu interface tương đương |
| 3 | **Agent loop** | Tự viết theo pattern V3: setup một lần → vòng lặp bounded (max N) [Think → **Prune** → Tool → Observe → **Checkpoint**] → Finalize | GoClaw `docs/01-agent-loop.md` (clean-room) | Prune (tỉa context mỗi vòng) và Checkpoint (resume được giữa chừng) là 2 stage đa số harness thiếu — giải trực tiếp bài toán token budget + failure recovery của §4 tài liệu gốc. Lưu ý: theo docs V3, không theo README 8-stage |
| 4 | **Working memory** | Tự viết loader theo format OpenClaw: bộ file workspace (AGENTS.md, SOUL.md, MEMORY.md, TOOLS.md, daily notes), token budget truncation, MEMORY.md qua review gate | OpenClaw docs `/concepts/agent-workspace`, `/concepts/memory` (spec, MIT) | Chuẩn de-facto (382k⭐), git-diff được, human-review được; docs mô tả đủ semantics (file nào, load lúc nào, budget, rotation) để implement từ spec không cần đọc TS. "No hidden state" — triết lý đúng cho auditability gov |
| 5 | **Cross-session memory** | VENDOR schema SQLite/FTS5 từ `hermes_state.py` (messages_fts + trigram + migrations), kể cả workaround FTS5-availability | Hermes (MIT) | Schema generic, lift gần nguyên văn; trigram table giải luôn bài CJK/substring (tiếng Việt có dấu hưởng lợi tương tự **[Inference]**); tránh thiết kế lại từ đầu một bài đã giải |
| 6 | **Traces/Monitoring** | Tự viết theo pattern: 5 span types (agent/llm_call/tool_call/embedding/event), buffer-flush, token chỉ aggregate từ llm_call (tránh đếm đôi), cost formula per-span, OTLP export là module tách rời, CLI đọc traces | GoClaw `docs/10-tracing-observability.md` (clean-room) | Reference duy nhất coi traces là first-class + query bằng CLI (đúng note sơ đồ gốc); tách OTLP thành module lẻ khớp yêu cầu on-prem (không bắt buộc collector ngoài) |
| 7 | **Security blocks/filters** | Tự viết, fail-closed: hardline blocklist + allowlist config + approval 3 mức (manual/smart — KHÔNG có YOLO cho gov); prompt-injection filter trên tool result; permission matrix | Hermes security docs (mô hình approval) + GoClaw `docs/09-security.md`, `23-ai-agent-permission-matrix.md` (clean-room) + OpenClaw security docs (**bài học ngược**: những default phải đảo) | Không repo nào fail-closed sẵn → phải tự viết; nhưng taxonomy (blocklist/allowlist/approval tiers, permission matrix) đã có người nghĩ hộ. OpenClaw THREAT-MODEL-ATLAS + exposure-runbook dùng làm threat checklist |
| 8 | **Skills** | Adopt chuẩn **agentskills.io** (SKILL.md + frontmatter, progressive disclosure); tự viết engine; precedence tiers theo mẫu OpenClaw; review gate cho skill do agent tạo | agentskills.io spec + OpenClaw docs `/tools/skills` (MIT) + Hermes `skills_guard.py`/`skill_provenance.py` làm reference | Cả OpenClaw lẫn Hermes đều theo chuẩn này → tương thích hệ sinh thái skill có sẵn; engine Hermes dính conversation loop nên không vendor được — chỉ port 2 behavior (auto-draft sau task 5+ tool calls, patch khi dùng) là policy/prompt logic |
| 9 | **Hooks** | Tự viết: event taxonomy theo OpenClaw (command:*, session:compact:*, message:*, bootstrap, startup/shutdown) + quyền **mutate/deny** cho Pre/PostToolUse; handler = Python entry point | OpenClaw docs `/automation/hooks` (MIT) | Taxonomy event đầy đủ nhất, đã chạy thật ở quy mô lớn; quyền mutate/deny là chỗ ta vượt lên (OpenClaw hooks không phải policy enforcement point — của ta thì phải là) |
| 10 | **Scheduler** | Tự viết: 3 syntax `at`/`every`/`cron` (croniter) + persist SQLite + heartbeat với contract kiểu `HEARTBEAT_OK` + chống overlap run | OpenClaw docs `/automation/cron-jobs`, `/gateway/heartbeat` (spec) | Semantics documented đủ chi tiết (kể cả gotcha croner OR-matching, suppression contract); khối đơn giản nhất, làm cuối |
| 11 | **RPC tool-calling** | Tự viết từ design: stub typed từ registry riêng → child process **trong container** → socket → caps (timeout/stdout/tool-call count) → strip secrets | Hermes `code_execution_tool.py` làm reference design (MIT) | Giải bài token-optimization ở tầng kiến trúc; KHÔNG vendor code vì có issues sandbox-escape #41/#7071 — dùng chính 2 issue đó làm security test checklist; chạy trong container đổi transport anyway |
| 12 | **Channels** | v0.3: Telegram trước (Bot API, dễ), Zalo Bot API sau (moderate, cấu trúc giống Telegram). **Loại Zalo Personal** (lib unofficial `zca-js`, rủi ro ToS) và WeChat (plugin đóng Tencent, không portable) | OpenClaw `docs/channels/*` làm mẫu config/gating (MIT) | Đã verify adapter nào port được từ spec, adapter nào không — tránh hứa với khách tính năng không build nổi |
| 13 | **Guardrails policy engine** | **Tự thiết kế 100%**: policy-as-config (YAML/DSL), immutable core (identity + deny-list gốc agent không sửa được), human-in-the-loop gate | Pattern mutable/immutable từ GoClaw docs 21 (clean-room); phần còn lại BUILD | Không repo nào có fail-closed policy engine — đây là differentiator chính của ta (§5 tài liệu gốc) |
| 14 | **Compliance layer VN/gov** | **Tự thiết kế 100%**: audit log theo yêu cầu khách, phân loại dữ liệu, retention policy | BUILD | Không có nguồn — đặc thù thị trường |

---

## 4. Lộ trình theo phase — mỗi phase có exit criteria

### Phase 0 — Chuẩn bị (trước khi viết code harness)

**Vì sao cần phase này:** 2 việc dưới đây làm sai thì không sửa được về sau (nhiễm license, spec trôi).

| Việc | Chi tiết | Exit criteria |
|---|---|---|
| 0.1 Quy trình clean-room GoClaw | Chỉ định 1 người "reader" chỉ đọc `docs/` của GoClaw (cấm mở `*.go`), viết spec nội bộ bằng lời của mình cho 4 tài liệu: provider interface, tracing model, agent loop V3, permission matrix. Dev khác chỉ đọc spec nội bộ | 4 spec nội bộ được viết xong; có ghi chú nguồn doc URL; legal/lead sign-off quy trình |
| 0.2 Vendor intake | Copy từ Hermes: `tools/environments/{base,local,docker,file_sync}.py` + schema `hermes_state.py` vào `vendor/` kèm LICENSE gốc + ghi commit SHA nguồn | Code vendored chạy standalone (test: mở Docker session, chạy lệnh, cleanup) |
| 0.3 Repo skeleton | Cấu trúc package Python, pyproject.toml **pin exact deps** (học chính sách supply-chain của Hermes sau sự cố của họ), CI (lint + test + build container), pre-commit | CI xanh trên skeleton rỗng |
| 0.4 Threat model v0 | Dùng OpenClaw THREAT-MODEL-ATLAS + exposure-runbook làm checklist, viết threat model cho posture của ta (on-prem, single-tenant, fail-closed) | Threat model 1-2 trang được review |

**[Inference] Ước lượng:** 1–2 tuần với 2 người.

### Phase 1 — MVP v0.1: chứng minh 2 cổng sống/chết

**Vì sao phạm vi này:** nhỏ nhất có thể mà vẫn chứng minh được (a) data không egress, (b) guardrails fail-closed trong execution path. Chưa pass 2 cổng thì mọi thứ khác vô nghĩa.

| Việc | Nguồn (§3) | Definition of Done |
|---|---|---|
| 1.1 Provider adapter (interface + 2 provider: Anthropic, OpenAI-compatible) | #2 | Đổi provider bằng config, không sửa core; retry/backoff; token+cost ghi vào span |
| 1.2 Agent loop bounded + Prune + Checkpoint | #3 | Kill process giữa chừng → resume từ checkpoint; context không vượt budget qua N vòng |
| 1.3 Exec sandbox Docker (vendored) + read/write file + web_fetch | #1 | Lệnh chạy trong container drop-caps; timeout + resource limit; stdout/stderr thành structured result |
| 1.4 Security fail-closed v0 | #7 | Deny-list hard-coded chặn TRƯỚC execution; test: lệnh bị cấm không bao giờ chạm Docker; policy engine lỗi → deny (fail-closed test bắt buộc) |
| 1.5 Working memory file-based | #4 | Bộ file workspace load đúng thời điểm, đúng budget; MEMORY.md chỉ ghi qua review gate |
| 1.6 Traces + CLI | #6 | Mọi model/tool call có span + correlation ID; `harness traces list/get` chạy được; **zero kết nối ra ngoài** (kiểm bằng network monitor trong test) |
| **Gate review** | — | Demo end-to-end: prompt → model → tool trong sandbox → trace đọc lại được; đủ 2 cổng (a)+(b) có bằng chứng test |

**[Inference] Ước lượng:** 4–6 tuần với 2–3 dev, phần lớn rơi vào 1.2 và 1.4.

### Phase 2 — v0.2: khả năng mở rộng cho agent

**Vì sao thứ tự này:** skills/hooks/memory dài hạn chỉ có nghĩa khi core loop đã an toàn; đây là các khối biến harness từ "chạy được" thành "dùng được".

| Việc | Nguồn (§3) | Definition of Done |
|---|---|---|
| 2.1 Skills engine (agentskills.io) + progressive disclosure | #8 | Chỉ SKILL.md liên quan được nạp (đo token); skill do agent draft phải qua review gate mới active |
| 2.2 Hooks Pre/PostToolUse với mutate/deny | #9 | Hook deny được tool call (test); hook lỗi/timeout không giết agent; hook có span riêng |
| 2.3 FTS5 cross-session memory (schema vendored) | #5 | `session_search` trả message gốc; test với tiếng Việt có dấu (trigram) |
| 2.4 Cron + heartbeat | #10 | Lịch persist qua restart; không overlap run; heartbeat phát hiện agent treo |
| 2.5 RPC tool-calling (viết lại từ design) | #11 | Pass toàn bộ checklist từ Hermes issues #41/#7071; child chạy trong container |

**[Inference] Ước lượng:** 4–6 tuần.

### Phase 3 — v0.3: sản phẩm hóa

| Việc | Nguồn (§3) | Ghi chú |
|---|---|---|
| 3.1 Guardrails policy engine (policy-as-config) + immutable core | #13 | Differentiator chính — dành effort thiết kế |
| 3.2 Compliance layer (audit log, phân loại dữ liệu, retention) | #14 | Theo yêu cầu khách cụ thể đầu tiên |
| 3.3 Channel: Telegram → Zalo Bot API | #12 | Không Zalo Personal, không WeChat |
| 3.4 Analytics on-prem (mẫu `/usage`, `/insights` của Hermes) | Hermes docs | Đọc từ traces đã có, không thu thập thêm |
| 3.5 Packaging deploy-per-tenant | — | 1 khách = 1 compose stack/container set; upgrade path documented |

### Defer có chủ đích — và điều kiện mở lại

| Hạng mục | Mở lại khi |
|---|---|
| Semantic memory / KG (pattern GoClaw 3-tier) | FTS5 đo được là không đủ trên use-case thật (định nghĩa metric relevance trước) |
| Multi-agent orchestration/teams | Có yêu cầu khách trả tiền cụ thể |
| Self-evolution (agent tự sửa config) | Chỉ sau khi immutable-core guardrail (3.1) chạy ổn định ≥1 quý **[Inference]** |
| Singularity backend | Gặp khách cấm Docker daemon (abstraction #1 đã chừa sẵn chỗ) |
| Trajectory datagen | Có kế hoạch fine-tune thật |

---

## 5. Rủi ro chính & giảm thiểu

| Rủi ro | Giảm thiểu |
|---|---|
| **Nhiễm license GoClaw** (dev đọc code Go rồi viết lại) | Quy trình 0.1: reader duy nhất, chỉ đọc docs, spec nội bộ là nguồn duy nhất cho dev. Ghi log ai đọc gì |
| **Vendored code trôi upstream** (Hermes vá bug/security mà ta không biết) | Ghi commit SHA lúc vendor; việc định kỳ (hàng quý **[Inference]**): diff upstream `tools/environments/` + theo dõi security advisories của Hermes — đặc biệt trạng thái issues #41/#7071 |
| **Security fail-closed làm agent "cùn"** (deny quá nhiều, không dùng được) | Approval tier "smart" + allowlist per-deployment; đo tỉ lệ deny trong traces để tune — nhưng default luôn là deny |
| **Scope creep channels** (khách đòi Zalo Personal/WeChat) | Đã có bằng chứng kỹ thuật vì sao không làm (deep-comparison §3.1) — đưa vào phụ lục hợp đồng **[Inference]** |
| **README ≠ thực tế** (đã gặp 3 lần khi verify) | Mọi pattern học từ repo phải trỏ vào docs/source cụ thể, không trích README |
| **1 maintainer chi phối GoClaw** (bus-factor) | Không sao với ta — chỉ học pattern từ docs snapshot; không phụ thuộc runtime |

---

## 6. Đo thành công

- **Gate v0.1:** demo end-to-end + bằng chứng test cho 2 cổng (no-egress, fail-closed). Không pass → dừng, xem lại thiết kế, không sang phase 2.
- **Gate v0.2:** một task thật của team (vd: quy trình nghiệp vụ nội bộ) chạy hoàn toàn trên harness với skills + memory.
- **Gate v0.3:** một deployment khách (hoặc pilot nội bộ mô phỏng khách) qua được checklist compliance + threat model review.
- **Xuyên suốt:** token/cost per task đo được từ traces ngay v0.1 — đây là số liệu bán hàng và tối ưu về sau.

---

*Plan này là bản tổng quan để review; mỗi việc trong Phase 1 sẽ có design doc riêng khi bắt đầu (theo spec nội bộ từ Phase 0). Nguồn verify: `harness-deep-comparison.md`.*
