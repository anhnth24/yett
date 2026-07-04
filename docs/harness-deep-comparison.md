# Harness Deep Comparison — Báo cáo Verification 3 Repo

> **Mục đích:** Verify các claim (đặc biệt các mục **[Inference]**) trong `harness-reference-architecture.md` bằng cách đọc trực tiếp README / docs / source tree của 3 repo (04/07/2026), và cập nhật chiến lược build theo 3 quyết định đã chốt.
>
> **Quyết định đã chốt:**
> 1. **Ngôn ngữ core: Python** → vendor tối đa từ Hermes (MIT)
> 2. **Deploy: on-prem, có Docker daemon** → Docker backend là mặc định cho cả dev lẫn prod
> 3. **Không multi-tenant** (deploy-per-tenant, mỗi khách một instance) → bỏ hẳn khối RBAC/tenant isolation
>
> **Phương pháp:** 3 research agent độc lập, mỗi agent một repo, fetch LICENSE/README/docs/source tree thực tế (github.com, raw.githubusercontent.com, docs sites; OpenClaw được clone shallow để xem tree đầy đủ). Mỗi claim gắn verdict: **VERIFIED / PARTIAL / REFUTED / UNVERIFIED** kèm nguồn.
>
> **Giới hạn:** api.github.com bị chặn qua proxy (403) → số contributor chính xác của OpenClaw và Hermes là [Unverified]; số stars/forks lấy từ trang GitHub render được, coi là xấp xỉ.

---

## 1. Kết quả verify theo repo

### 1.1 OpenClaw (github.com/openclaw/openclaw)

| # | Claim trong tài liệu gốc | Verdict | Ghi chú |
|---|---|---|---|
| 1 | License MIT | ✅ VERIFIED | LICENSE: "MIT License / Copyright (c) 2026 OpenClaw Foundation" |
| 2 | Memory file-based: MEMORY.md, SOUL.md, AGENTS.md, TOOLS.md | ✅ VERIFIED | Còn nhiều hơn: USER.md, IDENTITY.md, HEARTBEAT.md, BOOT.md, BOOTSTRAP.md, daily notes `memory/YYYY-MM-DD.md`; MEMORY.md inject vào context, có token budget truncation, chỉ load cho main session; tool `memory_search`/`memory_get` index phần còn lại. Docs: /concepts/agent-workspace, /concepts/memory |
| 3 | HEARTBEAT + cron natural-language | ⚠️ PARTIAL | Heartbeat VERIFIED: periodic agent turns, default 30m, contract `HEARTBEAT_OK` để suppress noise, options lightContext/activeHours. **Cron natural-language: UNVERIFIED** — docs chỉ có 3 syntax tường minh (`at` ISO-8601/relative, `every` interval, `cron` 5-6 field, persist SQLite). [Inference] Claim gốc có lẽ nhầm từ việc agent *tự tạo* cron job qua hội thoại |
| 4 | Security default: tool chạy trên host, full access session main, sandbox chỉ non-main | ✅ VERIFIED (1 nuance) | Docs nói thẳng: default cho single-operator là `security="full"`, `ask="off"` — "intentional UX". Sandbox **mặc định off**; `sandbox.mode` có 3 mức `off`/`non-main`/`all` — "chỉ non-main" là một mode, không phải default luôn bật. Có exposure-runbook, incident-response, THREAT-MODEL-ATLAS, CLI `openclaw security audit` |
| 5 | Hệ hooks | ✅ VERIFIED | Hook = thư mục chứa `HOOK.md` (frontmatter) + `handler.ts`. Events: command:*, session:compact:before/after, message:received/sent/preprocessed, agent:bootstrap, gateway:startup/shutdown |
| 6 | 23 kênh (Zalo, Zalo Personal, WeChat, QQ…) | ⚠️ PARTIAL (đếm thiếu) | Docs liệt kê **28 kênh**. Zalo (`extensions/zalo`, Bot API chính thức), Zalo Personal (`extensions/zalouser`, dùng lib unofficial `zca-js`, QR login), QQ Bot có trong tree. **WeChat KHÔNG có trong repo** — là plugin ngoài của Tencent (`@tencent-weixin/openclaw-weixin`) |
| 7 | Single-user by design | ✅ VERIFIED | Docs nói rõ: "one user/trust boundary per gateway", không phải hostile multi-tenant boundary; cần isolation thì tách gateway/OS user/host |
| 8 | SKILL.md + ClawHub, 3 tier skill | ✅ VERIFIED (tier nhiều hơn) | SKILL.md theo chuẩn AgentSkills (YAML frontmatter + markdown). ClawHub tại clawhub.ai, có verify trust envelope + provenance `.clawhub/origin.json` + security scan trước install. Precedence thực tế **6 tier** (workspace → project-agent → personal-agent → managed → bundled → extraDirs) |
| 9 | ~382k ⭐, 80k forks | ✅ VERIFIED (xấp xỉ) | 382k ⭐, 80k forks, ~64k commits hiển thị trên trang GitHub. Contributor count [Unverified] (API bị chặn) |

### 1.2 GoClaw (github.com/nextlevelbuilder/goclaw)

| # | Claim | Verdict | Ghi chú |
|---|---|---|---|
| 1 | License CC BY-NC 4.0 | ✅ VERIFIED — **pháp lý then chốt** | LICENSE nguyên văn CC BY-NC 4.0. Xác nhận ràng buộc: KHÔNG copy code, chỉ học pattern, cần clean-room |
| 2 | 8-stage pipeline pluggable | ⚠️ PARTIAL | README nói 8 stage (context→…→summarize) nhưng docs V3 thực tế mô tả khác: **ContextStage → [loop tối đa 20 vòng: Think → Prune → Tool → Observe → Checkpoint] → FinalizeStage**; memory xử lý trong Context/Finalize. Pluggability được README claim nhưng docs không có bằng chứng interface. README marketing lệch docs |
| 3 | 3-tier memory, L0/L1/L2, BM25 + pgvector | ⚠️ PARTIAL (gần đúng) | 3-tier + L0/L1/L2 + async consolidation qua worker pool (có dedup/retry) VERIFIED. Nhưng hybrid search là **Postgres FTS + RRF** (reciprocal rank fusion), không phải BM25 đúng nghĩa |
| 4 | Provider adapter 20+, capability routing, caching, AES-256-GCM | ✅ VERIFIED (1 nuance) | Interface `Provider` chỉ 4 method (`Chat`, `ChatStream`, `Name`, `DefaultModel`), 9 canonical failover reasons, capability registry. Nuance: chỉ **6 adapter cụ thể** — con số "20+" đạt được qua adapter OpenAI-compatible |
| 5 | Security 5 lớp, prompt-injection, SSRF, rate limit | ✅ VERIFIED | Có docs riêng `09-security.md`, `23-ai-agent-permission-matrix.md`. Lưu ý: verify ở mức docs, chưa audit độ sâu implementation |
| 6 | Self-evolution guardrail (mutable style / immutable identity) | ✅ VERIFIED | "Never change identity, name, or core purpose"; docs `21-agent-evolution-and-skill-management.md` |
| 7 | Tracing spans + cache metrics, OTLP, CLI traces | ✅ VERIFIED | 5 loại span (agent, llm_call, tool_call, embedding, event), buffer 1000 span flush 5s vào Postgres, công thức cost per-span, OTLP gRPC/HTTP ra Jaeger/Tempo/Datadog, CLI `traces list/get/follow/export/timeline` |
| 8 | 30+ tools 8 nhóm, exec approval, cron/heartbeat | ✅ VERIFIED | Docs `03-tools-system.md`, `08-scheduling-cron.md`, `22-heartbeat-system.md` |
| 9 | Bus-factor thấp | ⚠️ PARTIAL (đúng hướng) | ~8 author gần đây trong đó 2 là bot/agent → thực chất **~5-6 người, 1 maintainer chiếm đa số (mrgoonie)**. Vẫn active (commit 03/07/2026), 599 releases. Nhận định [Inference] rủi ro bus-factor trong tài liệu gốc: **đứng vững** |

**Clean-room:** bộ `docs/` (~38 file) đủ để học pattern **không cần đọc code Go** — docs providers và tracing tự chứa (interface signature, công thức, contract). ⚠️ Một số docs nhúng Go signature/snippet: quy trình clean-room phải là *trích khái niệm ra spec riêng*, không chép signature nguyên văn.

### 1.3 Hermes Agent (github.com/NousResearch/hermes-agent)

| # | Claim | Verdict | Ghi chú |
|---|---|---|---|
| 1 | License MIT | ✅ VERIFIED | LICENSE: MIT, "Copyright (c) 2025 Nous Research"; khớp pyproject.toml + README |
| 2 | Skills tự tạo/tự cải thiện, chuẩn agentskills.io | ✅ VERIFIED | Tự tạo skill sau task "5+ tool calls" thành công; patch skill khi dùng thấy lỗi/thiếu; SKILL.md tương thích agentskills.io. Engine rải ở `agent/skill_*.py` + `tools/skills_*.py`; thư mục `skills/` chỉ là *content* bundled. Có repo phụ `hermes-agent-self-evolution` (DSPy+GEPA) |
| 3 | 6 terminal backends | ✅ VERIFIED | `tools/environments/`: `local.py`, `docker.py`, `ssh.py`, `singularity.py`, `modal.py`, `daytona.py` + ABC `base.py` + `file_sync.py`. Chọn backend qua `TERMINAL_ENV` trong `terminal_tool.py` |
| 4 | RPC tool-calling từ Python script | ✅ VERIFIED (⚠️ có CVE-style issues) | `tools/code_execution_tool.py`: sinh stub `hermes_tools.py`, Unix socket RPC, child process env-stripped; limits 300s / 50KB stdout / 50 tool calls. **Issues #41, #7071: sandbox-bypass / PYTHONPATH leak** — không vendor nguyên code |
| 5 | SQLite FTS5 + LLM summarization + curated memory | ⚠️ PARTIAL | FTS5 VERIFIED: `hermes_state.py`, `~/.hermes/state.db` WAL, bảng `messages_fts` + `messages_fts_trigram` (CJK/substring), tool `session_search`. Curated memory + nudge VERIFIED (MEMORY.md/USER.md, `agent/memory_manager.py`). **"LLM summarization cho recall" bị chính docs phủ nhận** ("Search queries return actual messages — no LLM summarization") — README marketing |
| 6 | trajectory_compressor + /compress | ✅ VERIFIED (2 thứ khác nhau) | `/compress` = compaction context runtime (`agent/conversation_compression.py`, `context_compressor.py`). `trajectory_compressor.py` = nén trajectory cho **RL training data (Atropos)**, không phải compaction runtime |
| 7 | Cron scheduler built-in | ✅ VERIFIED | Gateway tick 60s; CLI `hermes cron` + `/cron`; `tools/cronjob_tools.py`; croniter là core dep |
| 8 | /usage, /insights --days N | ✅ VERIFIED | Kèm CLI `hermes insights [--days N] [--source]`; code `agent/account_usage.py`, `credits_tracker.py` |
| 9 | 6 kênh + security model | ⚠️ PARTIAL (đếm thiếu) | README nói 6 nhưng docs messaging liệt kê **24 platform** (đa số là optional extras). Security VERIFIED: approval 3 mức (manual/smart/YOLO) + hardline blocklist, DM pairing mã 8 ký tự (1h expiry, rate limit), Docker isolation drop ALL caps + chặn privilege escalation |
| 10 | ~208k ⭐, Python 3.11 | ✅ VERIFIED | 209k ⭐, 38.1k forks, 14,388 commits, v0.18.0 (01/07/2026). Python `>=3.11,<3.14`. **31 direct deps pinned** + ~40 optional extras, heavy deps lazy-load (`tools/lazy_deps.py`) — chính sách pin sau sự cố supply-chain. Contributor count [Unverified] |

---

## 2. Đánh giá vendorability từ Hermes (quyết định Python)

Đây là phần quan trọng nhất sau khi chốt Python. Xếp hạng theo độ "vendor được":

| Module | Verdict | Chi tiết |
|---|---|---|
| **(b) Terminal backends + Docker** | 🟢 **VENDOR nguyên module — ứng viên tốt nhất** | `tools/environments/` tự chứa, ABC sạch (`BaseEnvironment`: subclass chỉ implement `_run_bash()` + `cleanup()`; base lo session lifecycle, wait/kill, CWD tracking). `docker.py` ~1.200 dòng chỉ import stdlib + base + 2 helper từ local; 4 lazy import config-plumbing thay bằng config riêng. **Vendor: `base.py` + `local.py` + `docker.py` (+ `file_sync.py`), bỏ 4 backend còn lại.** Bonus: hardening flags Docker (drop ALL caps, no-new-privileges, resource limits) đi kèm sẵn |
| **(d) FTS5 memory store** | 🟢 **VENDOR schema/store gần nguyên văn** | Schema `hermes_state.py` generic (sessions, messages, messages_fts, trigram, schema_version migrations) — lift được, kể cả workaround FTS5-availability cho Python 3.11 (issue #13029). Phần curated memory (nudge, MEMORY.md) dính conversation loop → **học pattern, tự viết** |
| **(c) RPC tool-calling** | 🟡 **VENDOR design, VIẾT LẠI code** | Kiến trúc đơn giản đáng copy: sinh stub typed từ tool registry → child process → Unix socket → caps (timeout/stdout/số call) → strip secrets. Nhưng: coupled với registry Hermes, và có lỗ hổng đã báo (#41, #7071). Với sandbox Docker của ta, child chạy **trong container** → transport đổi anyway. Viết lại ~vài trăm dòng trên registry riêng; **dùng issue #41/#7071 làm test checklist** |
| **(a) Skills engine** | 🟡 **Adopt format, tự implement engine** | Format = chuẩn agentskills.io → adopt trực tiếp. Engine rải 2 package, dính `conversation_loop.py`/context engine/gateway. Chỉ port 2 behavior: hook "5+ tool calls → draft skill" và prompt patch-khi-dùng (là policy/prompt logic, không phải hạ tầng). Tham khảo `skills_guard.py` + `skill_provenance.py` cho khâu review skill do agent tự viết |

---

## 3. Cập nhật chiến lược theo 3 quyết định đã chốt

### 3.1 Những gì thay đổi so với tài liệu gốc

| Hạng mục | Trước | Sau khi verify + chốt quyết định |
|---|---|---|
| Multi-tenant + RBAC (§5 tài liệu gốc) | "Bắt buộc tự viết" | **BỎ HẲN** — deploy-per-tenant. Học GoClaw bỏ qua: `23-multi-tenant-architecture.md`, RBAC, `WithUserID()`, lane-based scheduler (worker pool thường là đủ) |
| Sandbox backend | Docker vs Singularity chưa chốt | **Docker chốt** (khách cho phép daemon). Vendor Docker backend Hermes; abstraction `BaseEnvironment` giữ lại nên thêm Singularity sau vẫn rẻ |
| Ngôn ngữ | Chưa chốt | **Python** — vendor path từ Hermes đã verify là khả thi (mục 2) |
| Storage v1 | SQLite, "sẵn đường lên Postgres cho multi-tenant" | SQLite **giữ nguyên**; lý do lên Postgres chỉ còn là pgvector cho semantic memory v3 (nếu FTS5 không đủ) — không còn áp lực multi-tenant |
| "BM25 + pgvector" (học GoClaw) | BM25 | Thực tế GoClaw dùng **Postgres FTS + RRF**. Bài học đúng: hybrid = FTS + vector + **RRF fusion** — RRF cũng áp được lên SQLite FTS5 + vector lib sau này |
| 8-stage pipeline (học GoClaw) | 8 stage README | Học theo **docs V3**: setup-một-lần → vòng lặp có giới hạn (Think→Prune→Tool→Observe→**Checkpoint**) → Finalize. Hai stage đáng giá mà đa số harness thiếu: **Prune** (tỉa context mỗi vòng) và **Checkpoint** (resume được) |
| Cron natural-language (học OpenClaw) | Tưởng là feature | Không phải parser feature — chỉ cần 3 syntax `at`/`every`/`cron` + persist SQLite là đủ ngang OpenClaw |
| Channel Zalo Personal | "Vendor từ OpenClaw nếu cần" | ⚠️ **Không plan** — phụ thuộc lib unofficial `zca-js` (reverse-engineered, QR login), không có lib Python tương đương trong docs, [Inference] rủi ro ToS/ban. Zalo Bot API chính thức thì OK (moderate, cấu trúc giống Telegram). WeChat: không portable (plugin đóng của Tencent) |
| "LLM summarization recall" (học Hermes) | Tính năng để học | README nói quá — search trả message gốc. v1 của ta: FTS5 trả raw message là đủ, đừng thêm summarization vì tưởng Hermes có |

### 3.2 Nguồn học/vendor cụ thể cho MVP v0.1 (cập nhật)

| Khối v0.1 | Hành động | Nguồn cụ thể (đã verify tồn tại) |
|---|---|---|
| Exec sandbox Docker | **VENDOR** | Hermes `tools/environments/base.py`, `local.py`, `docker.py`, `file_sync.py` |
| Provider adapter | **LEARN** (clean-room) | GoClaw `docs/02-providers.md`: interface 4 method, 9 failover reasons, capability registry, context-overflow → compaction thay vì failover |
| Traces | **LEARN** (clean-room) | GoClaw `docs/10-tracing-observability.md`: 5 span types, buffer-flush, token chỉ aggregate từ llm_call span (tránh đếm đôi), cost formula per-span, OTLP là module tách rời |
| Working memory file-based | **VENDOR format** | OpenClaw docs /concepts/agent-workspace + /concepts/memory: danh sách file, thời điểm load, token budget truncation, daily-note rotation, MEMORY.md main-session-only |
| Agent loop | **LEARN** (clean-room) | GoClaw `docs/01-agent-loop.md` (bản V3, không phải README): bounded iterations (max 20), Prune stage, Checkpoint stage |
| Security deny-list | **LEARN** | Hermes security docs: approval 3 mức + hardline blocklist + allowlist config; GoClaw `docs/09-security.md` + `23-ai-agent-permission-matrix.md` |
| Cross-session memory (v0.2) | **VENDOR schema** | Hermes `hermes_state.py`: schema FTS5 + trigram + migrations |
| Skills (v0.2) | **Adopt chuẩn + tự viết engine** | agentskills.io spec; OpenClaw precedence 6-tier làm mẫu; Hermes `skills_guard.py`/`skill_provenance.py` làm reference an toàn |
| Hooks (v0.2) | **LEARN** | OpenClaw event taxonomy (/automation/hooks); handler đổi từ TS sang Python entry point |
| Cron + heartbeat (v0.2) | **LEARN** | OpenClaw: 3 syntax + SQLite persist + contract `HEARTBEAT_OK`; Python dùng croniter/APScheduler |
| RPC tool-calling (v0.2+) | **Viết lại từ design** | Hermes `code_execution_tool.py` làm reference; issues #41/#7071 làm security test checklist; child chạy trong container |

### 3.3 Ràng buộc pháp lý — sau verify

- **OpenClaw MIT** ("OpenClaw Foundation") + **Hermes MIT** ("Nous Research"): vendor thoải mái, giữ copyright notice trong file vendored.
- **GoClaw CC BY-NC 4.0**: xác nhận nguyên văn từ LICENSE. Quy trình clean-room cụ thể hoá được: **chỉ đọc `docs/` (không đọc `*.go`)**, người đọc docs viết spec riêng bằng lời của mình (không chép Go signature nhúng trong docs), dev implement từ spec đó. Docs GoClaw đủ tự chứa để cách này khả thi (đã kiểm chứng với docs providers/tracing/agent-loop).

---

## 3b. Repo khác đã scout

### blogminhquy/javis-os (scout 04/07/2026 theo yêu cầu)

**Verdict: KHÔNG vendor — pháp lý không cho phép; gần như không có gì để học.**

| Tiêu chí | Kết quả |
|---|---|
| Bản chất | KHÔNG phải agent harness — là web UI/wrapper FastAPI bọc **Claude Code CLI chạy subprocess** làm "brain" (frontend → FastAPI → `claude` CLI → MCP). Agent loop là của Anthropic, không phải của repo |
| License | **Không có file LICENSE** (đã check LICENSE/COPYING các biến thể — 404) → mặc định all-rights-reserved, **không được vendor bất kỳ dòng code nào** |
| Quy mô/chất lượng | Repo **8 ngày tuổi** (v0.1.0 26/06 → v0.9.5 04/07/2026), 68⭐, 1 contributor người + Claude co-author, ~80 commits, **không có test**, CI chỉ publish Docker image |
| Security | Chỉ ở tầng web-app (login, token, rate limit); Claude chạy "toàn quyền", **zero sandbox** — ngược hoàn toàn posture fail-closed của ta |
| Liên quan đến plan | Không có VPN/SSH tooling, không WSL2, không policy layer, không tracing. Không giải bài toán khó nào của ta |

**Hai điểm duy nhất đáng liếc qua (học ý tưởng, không copy):**
1. `server/zalo_login.py` — pattern QR-login Zalo bằng cách shell-out CLI ngoài với `HOME` cô lập per-session (multi-account). Là reference implementation kênh Zalo Việt Nam duy nhất thấy được ngoài OpenClaw — ghi nhớ nếu v0.3+ làm Zalo adapter.
2. `git_brain.py` — memory dạng markdown vault sync 2 chiều qua git (kiểu Obsidian). Cross-check cho workspace memory của ta, nhưng concept generic, không cần code của repo này.

Trạng thái: **watch-list, không phải dependency hay template.** Đáng chú ý: README của nó cũng credit Hermes Agent làm cảm hứng — củng cố lựa chọn nguồn của ta.

---

## 4. Các mục còn [Unverified] / cần theo dõi

1. Số contributor chính xác của OpenClaw và Hermes (api.github.com bị chặn qua proxy — có thể verify lại từ máy khác).
2. Độ sâu implementation của security 5 lớp GoClaw (mới verify ở mức docs, chưa audit code — mà theo clean-room thì cũng không audit code).
3. Ngưỡng "5+ tool calls" tạo skill của Hermes: có trong docs, chưa trace trong code.
4. Trạng thái vá của issues #41/#7071 (Hermes RPC) tại thời điểm vendor design — kiểm tra lại trước khi implement.
5. Stars/forks là giá trị hiển thị trên trang, xấp xỉ.

---

*Báo cáo tổng hợp từ 3 research agent độc lập, 04/07/2026. Tài liệu gốc: `harness-reference-architecture.md`.*
