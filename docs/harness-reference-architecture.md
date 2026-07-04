# Harness Reference — Tổng kết 3 Repo & Chiến lược Build Riêng

> **Mục đích:** Tài liệu kiến trúc tham chiếu để build Harness riêng (theo sơ đồ 9 khối: Scheduler, Memory, Provider/Model, Tools, Skills, Hooks, Security, Guardrails, Monitoring).
>
> **Quy ước nguồn:** Thông tin đánh dấu *(README)* là verified từ README của repo tại thời điểm 07/2026. Đánh giá/khuyến nghị đánh dấu **[Inference]** là nhận định kiến trúc, cần team review.

---

## 1. Tổng quan 3 repo

| | **OpenClaw** | **GoClaw** | **Hermes Agent** |
|---|---|---|---|
| Repo | github.com/openclaw/openclaw | github.com/nextlevelbuilder/goclaw | github.com/NousResearch/hermes-agent |
| **License** | ✅ **MIT** | ❌ **CC BY-NC 4.0** (cấm thương mại) | ✅ **MIT** |
| Ngôn ngữ | TypeScript/Node 22+ | Go 1.26 (single binary ~25MB) | Python 3.11 (82%) + TS |
| Quy mô *(README)* | 382k ⭐, 80k forks, ~64k commits | 3.4k ⭐, 948 forks, 584 releases | 208k ⭐, 37.7k forks, ~14k commits |
| Thiết kế cho | Personal assistant, **single-user** | **Multi-tenant** enterprise gateway | Personal agent **self-improving**, single-user |
| Storage | File-based workspace | PostgreSQL 18 + pgvector | SQLite (FTS5) |
| Channels | 23 kênh (Zalo, Zalo Personal, WeChat, QQ…) | 7 kênh (Telegram, Zalo OA/Personal, Feishu…) | 6 kênh + CLI/TUI |
| Security mặc định | ⚠️ Tool chạy trên host, full access cho session `main`; sandbox chỉ cho non-main | ✅ Permission 5 lớp, prompt-injection detection, SSRF protection, AES-256-GCM | Command approval, DM pairing, container isolation |
| Backing | Peter Steinberger + community lớn, sponsor OpenAI/GitHub/NVIDIA | Ít contributor — **[Inference]** rủi ro bus-factor | Nous Research (AI lab) |
| Điểm độc nhất | Hệ sinh thái lớn nhất, chuẩn de-facto (SOUL.md/MEMORY.md/SKILL.md), ClawHub | 8-stage pipeline, 3-tier memory, RBAC, OTel built-in | Skills tự tạo/tự cải thiện, 6 terminal backends, RPC tool-calling, trajectory datagen |

**[Inference]** Nghịch lý chính: 2 repo dùng được về pháp lý (MIT) đều thiết kế single-user; repo duy nhất thiết kế đúng cho enterprise (GoClaw) thì license cấm dùng thương mại.

---

## 2. Ràng buộc pháp lý — đọc trước khi code

| Repo | Được làm | KHÔNG được làm |
|---|---|---|
| OpenClaw (MIT) | Fork, vendor code, sửa, bán, đóng deliverable cho khách | — (giữ attribution) |
| Hermes (MIT) | Fork, vendor code, sửa, bán, đóng deliverable cho khách | — (giữ attribution) |
| GoClaw (CC BY-NC) | Đọc code, học pattern, viết lại ý tưởng bằng code của mình; chạy PoC nội bộ phi thương mại **[Inference — cần legal confirm]** | **Copy code, fork để deploy thương mại, đóng vào deliverable dự án** |

> ⚠️ **[Inference]** Khi "học pattern" từ GoClaw: viết lại từ mô tả kiến trúc/docs, không dịch code dòng-theo-dòng (derivative work). Cần quy trình clean-room nếu dùng nhiều.

---

## 3. Mapping 9 khối Harness → học từ đâu

Quy ước cột **Chiến lược**: `VENDOR` = lấy code trực tiếp (chỉ từ repo MIT) · `LEARN` = học pattern, tự viết lại · `BUILD` = tự thiết kế từ đầu.

### 3.1 Scheduler (Cron + Heartbeat)

| Nguồn | Cái hay | Chiến lược |
|---|---|---|
| OpenClaw | Khái niệm HEARTBEAT gốc (liveness + agent tự "thức dậy" làm việc định kỳ), cron jobs natural-language *(README)* | **VENDOR/LEARN** |
| Hermes | Cron scheduler built-in với delivery về mọi platform *(README)* | LEARN |
| GoClaw | Tool `cron`, `heartbeat`, `sessions_*` gọn trong toolset *(README)* | LEARN |

**Tiêu chí phải đạt:** chống overlap run (không start run mới khi run cũ chưa xong) · heartbeat = tín hiệu liveness để restart agent treo · lịch persist qua restart. **[Inference]** Khối dễ nhất, làm sau cùng cũng được.

### 3.2 Memory (Working / MEMORY.md / External / Long-term)

| Nguồn | Cái hay | Chiến lược |
|---|---|---|
| OpenClaw | Chuẩn de-facto file-based: `MEMORY.md`, `SOUL.md`, `AGENTS.md`, `TOOLS.md` inject vào prompt *(README)* — Git-diff được, human-review được | **VENDOR** format |
| Hermes | **[Inference] Phương án "do less" tốt nhất cho v1:** FTS5 search phiên cũ + LLM summarization cho cross-session recall; agent-curated memory với periodic nudge tự persist kiến thức *(README)* | **VENDOR/LEARN** |
| GoClaw | Kiến trúc đích dài hạn: 3-tier (Working → Episodic → Semantic/knowledge graph), progressive loading L0/L1/L2, consolidation chạy async qua event bus, hybrid search BM25 + pgvector *(README)* | **LEARN** |

**Tiêu chí phải đạt:** working memory vừa context window (token budget rõ) · MEMORY.md write qua review gate, không để agent ghi tự do · retrieval đo được độ liên quan · policy nén/quên để không phình vô hạn.

**[Inference] Lộ trình:** v1 = file-based (OpenClaw format) + FTS5 (Hermes pattern) → v2 = thêm episodic summaries → v3 = semantic/KG (GoClaw pattern) *chỉ khi* retrieval FTS5 chứng minh không đủ.

### 3.3 Provider / Model

| Nguồn | Cái hay | Chiến lược |
|---|---|---|
| GoClaw | Adapter interface thống nhất cho 20+ provider, capability-based routing, prompt caching Anthropic/OpenAI, API key mã hóa AES-256-GCM *(README)* | **LEARN** (thiết kế interface chuẩn nhất) |
| Hermes | Đổi model runtime bằng 1 lệnh, không đổi code; model failover *(README)* | LEARN |
| OpenClaw | Model failover, auth profile rotation *(README docs)* | LEARN |

**Tiêu chí phải đạt:** 1 interface (message + tool-call + streaming + thinking) · retry/backoff + failover · token/cost accounting per call · key mã hóa at-rest · đổi model không đụng core.

### 3.4 Tools (Built-in / MCP / CLI Runtime Packages)

| Nguồn | Cái hay | Chiến lược |
|---|---|---|
| Hermes | **RPC tool-calling từ Python script** — gom pipeline nhiều bước thành lượt chạy không tốn context *(README)* — **[Inference]** giải pháp token-optimization ở tầng kiến trúc, đúng hướng PostToolUse hook từng làm | **VENDOR/LEARN** |
| Hermes | **6 terminal backends** (local/Docker/SSH/Singularity/Modal/Daytona) *(README)* — **[Inference]** Singularity hợp môi trường gov/HPC cấm Docker daemon | **VENDOR/LEARN** |
| GoClaw | 30+ tool chia 8 nhóm rõ ràng, exec có approval workflow *(README)* — taxonomy tốt để tham khảo | LEARN |
| OpenClaw | Browser tool, nodes (iOS/Android làm tool endpoint) *(README)* | LEARN nếu cần |

**Tiêu chí phải đạt:** schema + input validation · **error message agent tự sửa được** (quan trọng nhất) · MCP: xử lý lifecycle/auth/multi-block response, MCP server chạy sandbox · CLI package pin version để reproducible · mọi tool call qua policy check trước khi thực thi.

### 3.5 Skills — khối "mực đỏ" trong sơ đồ

| Nguồn | Cái hay | Chiến lược |
|---|---|---|
| Hermes | **Reference mạnh nhất:** tự tạo skill sau task phức tạp, skill tự cải thiện khi dùng, tương thích chuẩn mở **agentskills.io** *(README)* — đúng note "codex tạo skill sử dụng kubectl" trong sơ đồ | **VENDOR** |
| OpenClaw | Format `SKILL.md` gốc, ClawHub registry, bundled/managed/workspace skill tiers *(README)* | **VENDOR** format |
| GoClaw | Hybrid search skill (BM25 + semantic) để trigger đúng skill *(README)* | LEARN |

**Tiêu chí phải đạt (giữ nguyên note gốc + bổ sung):**
- 80% dựa trên public CLI · Agent-friendly · error → descriptive message *(note gốc trong sơ đồ)*
- **Progressive disclosure**: chỉ nạp SKILL.md liên quan, không nạp hết (vỡ context)
- **Chất lượng description** quyết định trigger đúng — đầu tư nhiều nhất vào đây
- **Composability**: skill gọi skill (ví dụ kubectl)
- Adopt chuẩn **agentskills.io** thay vì tự định nghĩa format **[Inference]**

### 3.6 Hooks

| Nguồn | Cái hay | Chiến lược |
|---|---|---|
| OpenClaw | Hệ hooks + `examples/hooks` trong GoClaw kế thừa từ đây | **VENDOR/LEARN** |
| GoClaw | 8-stage pipeline (context → history → prompt → think → act → observe → memory → summarize) với pluggable stages *(README)* — **[Inference]** hooks = điểm cắm vào từng stage, mô hình sạch hơn hooks rời rạc | **LEARN** |

**Tiêu chí phải đạt:** lifecycle points rõ (Pre/PostToolUse, PrePrompt, PostResponse…) · hook nhanh, không block loop · được phép **mutate/deny** (để enforce policy + token optimization) · lỗi hook không giết agent · hook có trace riêng.

### 3.7 Security (Blocks / Filters)

| Nguồn | Cái hay | Chiến lược |
|---|---|---|
| GoClaw | **Reference tốt nhất về mô hình:** permission 5 lớp, prompt-injection detection, SSRF protection, rate limiting *(README)* | **LEARN** (không copy code) |
| OpenClaw | Security runbook + exposure runbook *(README)* — **[Inference]** đọc để biết *những gì phải làm ngược lại*: default của nó là trust-owner/full-host-access, enterprise cần deny-by-default | LEARN (bài học ngược) |
| Hermes | Command approval + container isolation *(README)* | LEARN |

**Tiêu chí phải đạt:** **Blocks** = hard deny (`rm -rf`, network egress ngoài whitelist, secret access) · **Filters** = redact PII/secret + lọc prompt-injection trên tool result · enforce **trong execution path, trước khi chạy** — nếu chỉ log thì coi như chưa có · **fail-closed** · auditable.

### 3.8 Guardrails

| Nguồn | Cái hay | Chiến lược |
|---|---|---|
| GoClaw | Self-evolution có guardrail: agent được sửa communication style/CAPABILITIES.md nhưng **không bao giờ** đổi identity/name/core purpose *(README)* — **[Inference]** pattern "mutable vs immutable config" rất đáng học | LEARN |
| — | Policy engine as code (đánh giá trước mọi side-effect) | **BUILD** |

**Tiêu chí phải đạt:** policy tách khỏi code (config/DSL) · human-in-the-loop gate cho hành động nguy hiểm (đúng mô hình review gate F-Project) · immutable core (identity, deny-list gốc) agent không tự sửa được.

### 3.9 Monitoring (Analytics / Logs / Traces)

| Nguồn | Cái hay | Chiến lược |
|---|---|---|
| GoClaw | **Reference tốt nhất:** LLM call tracing built-in với spans + prompt-cache metrics, OTel OTLP export, CLI `traces list/get/follow` *(README)* — traces là first-class, query được bằng CLI (đúng note `goclaw traces read abc` trong sơ đồ) | **LEARN** |
| Hermes | `/usage`, `/insights --days N` — usage analytics ở tầng conversation *(README)* | LEARN |

**Tiêu chí phải đạt:** mọi model/tool/hook call có trace + correlation ID, **replay được** · logs JSON không lộ secret · analytics: token/cost/success-rate per agent/session · **on-prem** (gov) · thiết kế từ ngày đầu, không bolt-on.

---

## 4. Tiêu chí xuyên suốt (cross-cutting) — quyết định sống/chết

Không nằm trong khối nào của sơ đồ nhưng khó hơn mọi khối:

1. **Context & state management** — ghép context mỗi turn deterministic · token budgeting + chiến lược truncate/summarize (tham khảo `trajectory_compressor` của Hermes, `/compress` command) · state persist qua session không mất ngầm.
2. **Sandbox thực thi** — mọi tool/CLI chạy cô lập · timeout + resource limit · stdout/stderr → structured result · **không egress data** (gov). Lấy terminal-backend abstraction của Hermes làm khung.
3. **Failure & idempotency** — mỗi step resume/retry được · side-effect idempotent · dedup + retry cho async pipeline (GoClaw dùng typed domain event bus + worker pool *(README)* — pattern đáng học).
4. **Provider abstraction** — xem §3.3.
5. **Observability từ ngày đầu** — xem §3.9.
6. **Security = enforcement, không phải advice** — xem §3.7.

**Cổng quyết định "làm được":** (a) mọi data — memory, traces, tool I/O — ở on-prem; (b) guardrails enforce được trong execution path, fail-closed. Pass 2 cổng này, phần còn lại là kỹ thuật.

---

## 5. Phần BẮT BUỘC tự viết (không repo nào cho)

| Hạng mục | Lý do |
|---|---|
| **Multi-tenant isolation + RBAC** | 2 repo MIT đều single-user; GoClaw có nhưng không dùng code được. **[Inference]** Retrofit vào OpenClaw/Hermes là rework sâu — thiết kế từ đầu rẻ hơn |
| **Guardrails policy engine fail-closed** | Cả 3 repo đều thiên trust-owner ở mức độ khác nhau |
| **Monitoring on-prem, không egress** | Yêu cầu data sovereignty gov |
| **VN/gov compliance layer** | Audit log theo yêu cầu khách, phân loại dữ liệu, retention policy |
| **Domain retrieval** | Chunking/embedding cho văn bản pháp luật/nghiệp vụ tiếng Việt |

---

## 6. Lộ trình build đề xuất **[Inference]**

### MVP (v0.1) — chứng minh 2 cổng ở §4
- Provider adapter (1 interface, 2 provider) — pattern GoClaw
- Tools: exec sandbox (Docker backend, khung Hermes) + read/write file + web_fetch
- Working memory file-based (format OpenClaw) + token budget
- Traces: mọi call có span, CLI đọc được — pattern GoClaw
- Security: deny-list hard-coded, fail-closed, enforce trước execution

### v0.2
- Skills engine (chuẩn agentskills.io, vendor từ Hermes) + progressive disclosure
- Hooks Pre/PostToolUse với quyền mutate/deny
- FTS5 cross-session memory (pattern Hermes)
- Cron + heartbeat

### v0.3
- Multi-tenant + RBAC (tự thiết kế, tham khảo mô hình GoClaw)
- Guardrails policy engine (policy-as-config)
- Channel adapters (vendor Zalo/Telegram từ OpenClaw nếu cần)
- Analytics dashboard on-prem

### Defer có chủ đích (YAGNI)
- Semantic memory / knowledge graph — chỉ khi FTS5 không đủ
- Multi-agent orchestration/teams
- Self-evolution — chỉ sau khi guardrails immutable-core vững
- Trajectory datagen (pattern Hermes) — chỉ khi có kế hoạch fine-tune

---

## 7. Quyết định mở cần chốt trước khi code

| Quyết định | Lựa chọn | Khuyến nghị **[Inference]** |
|---|---|---|
| Ngôn ngữ core | Python (gần Hermes, vendor dễ) vs Go (ops gọn, concurrency) vs TS (gần OpenClaw) | Python nếu team thạo — vendor được nhiều nhất từ Hermes; Go nếu ưu tiên single-binary deploy |
| Storage v1 | SQLite (zero-ops) vs PostgreSQL (sẵn cho multi-tenant) | SQLite cho v0.1–0.2, thiết kế schema sẵn đường lên Postgres |
| Skills format | agentskills.io vs tự định nghĩa | agentskills.io — tương thích hệ sinh thái |
| Sandbox backend đầu tiên | Docker vs Singularity | Docker cho dev; xác nhận sớm môi trường khách có cho Docker daemon không — nếu không, ưu tiên Singularity |
| Legal | Quy trình clean-room cho pattern học từ GoClaw | Bắt buộc có trước khi dev đọc code GoClaw |

---

*Tài liệu tổng hợp từ phân tích sơ đồ Harness + README 3 repo (07/2026). Số liệu stars/forks/release thay đổi theo thời gian.*
