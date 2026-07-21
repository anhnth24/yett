# yett

> Agent harness Python cho môi trường on-prem — fail-closed, dữ liệu tại chỗ, deploy-per-tenant.
>
> **Tên:** *yett* = cổng lưới sắt của thành lũy, loại thả xuống đóng kín theo mặc định — đúng triết lý fail-closed của Policy Gate (mọi tool call qua một cổng, mặc định từ chối). Lệnh CLI: `yett`.
>
> **Trạng thái:** Bộ khung code đã implement (hardening P0–P2 theo `plans/260705-1658-harden-yett-harness/`); pytest, mypy strict, ruff và import-linter đều sạch. Một số hạng mục mới chỉ **kiểm chứng qua fake/logic đơn lẻ, chưa verify end-to-end** — xem cột "Kiểm chứng" trong [Trạng thái implement](#trạng-thái-implement) bên dưới. *(Test web UI đã bind cổng 0 (ephemeral) — hết flaky trên Windows do dải cổng bị WSL2/Hyper-V loại trừ.)*

## Chạy thử nhanh

```bash
pip install -e ".[dev]"
yett demo            # turn agent offline (FakeProvider), không cần key
yett setup           # wizard cài đặt từng bước: provider → model → key → project
yett serve --open    # 🖥️ mở giao diện chat WEB (localhost) — giống "app"
yett chat "báo cáo tiến độ tuần này của các project"   # hoặc dùng CLI
yett usage --by provider --state state
pytest -q
```

**Muốn dùng như một app Windows:** `yett serve --open` mở UI chat trong trình duyệt; hoặc đóng gói `yett.exe` (double-click chạy, không cần Python) — xem [`packaging/BUILD_EXE.md`](packaging/BUILD_EXE.md).

**Hướng dẫn cài đặt đầy đủ (WSL2 + Docker Desktop): [`INSTALL.md`](INSTALL.md).**
Chọn 1 trong 10+ model top (GLM 5.2, MiniMax M3, DeepSeek, Gemini, GPT-5.5, Claude, Grok, Qwen, Mistral) ngay trong wizard hoặc sửa `config/harness.yaml` — không đụng code. Giá thật ở [`config/pricing.yaml`](config/pricing.yaml).

## Trạng thái implement

Đã có code chạy được + test cho lõi cả 3 phase. Phần cần tài nguyên ngoài (LLM API thật, Docker daemon, SSH/VPN tới server thật, Telegram/Zalo Bot API) được thiết kế qua interface + backend inject được, và test bằng fake/offline — đúng nguyên tắc no-egress trong test.

**Chú giải cột "Kiểm chứng":** ✅ e2e = có test lắp ráp thật (App/loop thật, không chỉ gọi hàm lẻ). 🟡 fake/wired = logic đúng khi chạy qua `FakeProvider`/stand-in hoặc đã wire vào `App` nhưng backend thật (provider/API/Docker daemon) **chưa** được quan sát chạy trên máy này — có thể còn lệch khi nối thật. ❌ chưa wire = cấu hình cho phép nhưng đường chạy thật (`App`/`yett chat`) chưa thực sự dùng tới (tool/gate tồn tại, không có caller thật).

| Khối | Kiểm chứng | Test |
|---|---|---|
| Policy Gate fail-closed + deny-list + allowlist + filters | ✅ e2e | `test_security_basic`, `test_filters` |
| **Hardline cấm xóa file OS qua SSH/VPN** (cmdguard) | ✅ e2e | `test_cmdguard` (45 case né tránh) |
| **Hardline DB read-only** (sqlguard) | ✅ e2e | `test_sqlguard` (26 case) |
| Provider interface + failover + FakeProvider | ✅ e2e (với FakeProvider) | `test_provider_failover` |
| Tool registry + wiring bất biến + builtin tools + project scope | ✅ e2e | `test_tools_wiring` |
| Sandbox: local (dev/test), chạy trực tiếp trên host | ✅ e2e | `test_tools_wiring` |
| Sandbox: **docker (production, hardened)**, `App` wire thật (mount workspace + host→container cwd) **(wired)** | 🟡 wired, logic-verified qua unit test trên argv/mounts; backend=docker thiếu daemon → fail-closed (từ chối, không tụt về host) — **e2e (exec chạy thật trong container) chưa quan sát trên máy dev này** (không có Docker daemon) | `test_docker_wiring` (unit, argv/mounts), `test_docker_sandbox` (integration, `skipif` không có daemon) |
| Agent loop: bounded + prune + checkpoint/resume + cancel | 🟡 fake — đúng với `FakeProvider`; contract test xác nhận system prompt được gửi + đúng thứ tự message assistant(tool_use)→tool_result qua transport inject, nhưng **chưa gọi API thật** | `test_core_loop` (fake), `test_real_provider_contract` (transport inject, không phải API thật) |
| Tracing + span store + cost ledger | ✅ e2e | `test_obs` |
| Memory: workspace file (AGENTS/SOUL/MEMORY.md → system prompt) **(wired vào `yett chat`)** | ✅ e2e | `test_memory`, verify `App.chat` |
| Memory: review gate (staging cho agent đề xuất ghi MEMORY.md) **(wired vào `yett chat` + CLI)** | ✅ e2e — tool `memory_propose` đăng ký qua App → Gate→Registry→Filters, chỉ ghi `memory/pending/`; `write_file`/`ImmutableCore` chặn ghi thẳng `MEMORY.md`; người vận hành `yett memory list\|approve\|reject` | `test_memory`, `test_memory_review`, `test_memory_write_invariant` |
| Remote ops: host profile + ssh_exec + log_read + vpn | 🟡 fake/wired — SSH/VPN CLI runner inject được; App wire `SubprocessVpnRunner` (openfortivpn/openvpn, argv-only, secret qua file tạm 0600); **chưa verify tunnel thật** trên máy này | `test_remote_db`, `test_vpn_runner` |
| DB tools: db_query/db_config read-only 4 lớp | 🟡 fake (executor inject, chưa DB thật ngoài sqlite) | `test_remote_db` |
| Skills: loader + disclosure + lint + review gate **(wired vào `yett chat`)** | ✅ e2e | `test_skills_hooks_sched`, `test_group2_wired` |
| Hooks: mutate/deny + cách ly lỗi **(wired)** | ✅ e2e | `test_skills_hooks_sched` |
| Scheduler: cron + overlap + unattended approval timeout | ✅ e2e | `test_skills_hooks_sched` |
| Eval suite golden tasks | ✅ e2e | `test_eval_websearch` |
| **`web_search` tool** **(wired vào `yett chat` / `build_app`)** | 🟡 fake — đăng ký qua `App._wire_search()` khi có `search:` + secret store; invoke qua `App.chat` với transport inject (Brave backend offline); key thiếu/rỗng fail an toàn; kết quả lọc egress allowlist; secret không vào context/span. **Chưa gọi Brave API thật** | `test_eval_websearch`, `test_web_search_wired` |
| **`image_gen` tool** **(wired vào `yett chat` / `build_app`)** | 🟡 fake — đăng ký qua `App._wire_image()` khi có `image:` + secret store; invoke qua `App.chat` với backend/transport inject (OpenAI-compatible Images offline); ảnh decode + sniff MIME, ghi atomic trong workspace; path traversal/symlink/oversize/malformed base64 bị từ chối; URL download mode chặn host ngoài allowlist + redirect; key thiếu/rỗng fail an toàn; secret không vào context/span; cost `per_call` vào ledger. **Chưa gọi Images API thật** | `test_image_gen`, `test_image_backend`, `test_image_gen_wired` |
| Policy engine (policy-as-config, drop-in) + immutable core | ✅ e2e | `test_phase3` |
| Audit hash-chain + retention + channel gating + analytics | ✅ e2e | `test_phase3`, `test_phase3_extra` |
| Subagent delegation 1 cấp (không leo thang quyền) **(wired)** | ✅ e2e | `test_phase3`, `test_group2_wired` |
| **DB query read-only (sqlite thật + lazy postgres/mysql), wired** | ✅ e2e | `test_group2_wired` |
| RPC code execution (broker + caps + secret strip) | 🟡 fake (in-process; production dùng socket-in-container, chưa test) | `test_phase3_extra` |
| **Adapter LLM thật (OpenAI-compatible: GLM 5.2, MiniMax M3...)** | 🟡 fake — parse response/lỗi đúng qua transport inject, **chưa gọi API thật**; xem thêm dòng "Agent loop" ở trên cho bug system-prompt/tool-ordering khi loop dùng provider này | `test_openai_compat` |
| **Adapter Anthropic Messages API native** | 🟡 fake — request shape/system/`tool_use`→`tool_result`/usage/lỗi/timeout/malformed/secret-redact đúng qua transport inject + loop contract; **chưa gọi api.anthropic.com thật** | `test_anthropic`, `test_anthropic_provider_contract` |
| `yett chat` nối config thật + provider factory + secret store | ✅ e2e (build_app lắp ráp đúng); provider vẫn cần key thật để gọi mạng | `test_openai_compat`, `test_anthropic`, verify build_app |
| Packaging: config mẫu, pricing, bundled skills, subagent defs, deploy+runbook, backup/restore | ✅ e2e | — |
| **Trợ lý cá nhân: task/goal store + tool (task_add/list/update) + briefing chủ động** **(wired vào `yett chat` + web tab "Việc")** | ✅ e2e | `test_tasks`, `test_assistant`, `test_web_console_endpoints` |
| **Code navigation: list_dir/grep/search scoped** + complexity router + anti-loop step-budget | ✅ e2e | `test_codenav`, `test_complexity`, `test_core_loop` |
| **Kênh Telegram: long-poll + gating/pairing + dispatch→App.chat + notify tool + briefing tự động** **(wired vào `yett serve`)** | 🟡 fake — logic đúng qua transport inject (getUpdates/sendMessage), dispatch chạy App.chat thật; **chưa gọi api.telegram.org thật** (cần bot token) | `test_telegram`, `test_assistant` (notify + e2e dispatch) |
| **Kênh Zalo Official Bot API** (không Zalo Personal): poll/webhook + gating/pairing + normalize/chunk ≤2000 + dispatch→App.chat + notify + token redact + fail-closed webhook config **(wired vào `yett serve` / config / capabilities)** | 🟡 fake — contract offline qua transport inject (`bot-api.zaloplatforms.com` shape: `getUpdates` single-object + timeout string, `sendMessage`, webhook `X-Bot-Api-Secret-Token`); **chưa gọi Bot API thật** (cần token từ [bot.zaloplatforms.com](https://bot.zaloplatforms.com); assumptions ghi trong `channels/zalo_bot.py`) | `test_zalo_bot`, `test_zalo_channel_wired`, `test_assistant` (zalo dispatch) |
| VPN/SSH CLI thật | 🟡 wired — `SubprocessVpnRunner` + tool `vpn` + `yett vpn` + SSH pre-connect `ensure`; adversarial offline tests; **chưa quan sát openfortivpn/openvpn live** | `test_vpn_runner` |

## Repo này sẽ làm gì

Xây dựng một **agent harness** (nền tảng chạy AI agent) bằng **Python**, thiết kế cho môi trường **on-prem/gov**: mọi dữ liệu ở tại chỗ, an ninh **fail-closed** (mặc định từ chối, chặn trước khi thực thi), triển khai **mỗi khách một instance** (deploy-per-tenant, không multi-tenant).

Harness = phần hạ tầng bao quanh model: nhận yêu cầu → ghép context → gọi LLM → cho agent dùng tool trong sandbox → ghi nhớ → giám sát. Model có thể thay đổi; harness là thứ team sở hữu lâu dài.

**Vì sao tự build:** đã khảo sát và verify 3 harness OSS đại diện (OpenClaw, GoClaw, Hermes Agent — xem `docs/harness-deep-comparison.md`). Không sản phẩm nào thỏa đồng thời: pháp lý sạch để đóng vào deliverable thương mại + fail-closed + on-prem không egress. Chiến lược: **vendor module hạ tầng từ Hermes (MIT), vendor format/spec từ OpenClaw (MIT), học pattern kiến trúc từ GoClaw docs (clean-room, license CC BY-NC cấm copy code)** — chỉ tự viết phần không nguồn nào có (policy engine, compliance).

## Hai cam kết kiến trúc (sống/chết)

1. **No-egress:** memory, traces, tool I/O đều lưu local (file + SQLite); container sandbox mặc định không có network; đường ra ngoài duy nhất là web_fetch qua whitelist và OTLP export (optional, mặc định tắt).
2. **Fail-closed trong execution path:** mọi tool call xuyên qua Policy Gate *trước khi chạy* — không có trong allowlist thì từ chối, Gate lỗi cũng từ chối; mọi tool result bị lọc secret/PII/prompt-injection *trước khi* vào context. Chỉ log mà không chặn thì coi như chưa có security.

## Các chức năng sẽ làm

Nhãn phase theo lộ trình: **[v0.1]** MVP → **[v0.2]** mở rộng cho agent → **[v0.3]** sản phẩm hóa.

### Lõi agent
- **[v0.1] Agent loop bounded** — vòng lặp Think → Prune → Tool → Observe → Checkpoint có trần số vòng; **Prune** tỉa context mỗi vòng theo token budget; **Checkpoint** persist trạng thái để kill giữa chừng vẫn resume được. Hủy (Ctrl+C) dừng sạch ở ranh giới stage, kill container đang chạy, resume không lặp side-effect.
- **[v0.1] Provider layer** — một interface thống nhất (chat/stream/tool-call/usage), 2 adapter đầu: Anthropic + OpenAI-compatible; đổi model/provider bằng config không sửa core; retry/backoff, failover theo lý do lỗi chuẩn hóa, prompt caching; token + cost ghi vào trace từng call.
- **[v0.2] RPC code execution** — agent viết script Python gọi tool qua RPC, gom pipeline nhiều bước thành một lượt không tốn context; script chạy trong container, từng tool call vẫn xuyên Policy Gate.

### Tools & Sandbox
- **[v0.1] Tool runtime** — registry tool có JSON schema + validation; lỗi trả về dạng agent-tự-sửa-được. Tool đầu: `exec`, `read_file`, `write_file`, `web_fetch` (qua egress whitelist).
- **[v0.1] Đăng ký project** — khai báo các project trong config (`projects: {tên: đường_dẫn}`); file/exec chỉ được phép trong workspace + project root đã đăng ký, mount vào sandbox theo từng project.
- **[v0.1] Docker sandbox** — mọi lệnh exec chạy trong container hardened (drop ALL capabilities, no-new-privileges, resource limits, timeout, mặc định không network); abstraction backend giữ sẵn đường thêm Singularity nếu khách cấm Docker daemon. *(vendor từ Hermes, MIT)*

### Security & Guardrails
- **[v0.1] Policy Gate** — hardline deny-list (không override được) → allowlist per-deployment → approval 2 mức (manual/smart, không có chế độ YOLO) → mặc định DENY; fail-closed có test riêng. Approval trong run không giám sát (cron) có timeout — hết hạn thì job fail sạch, không treo, không auto-approve.
- **[v0.1] Secret store** — mọi credential (provider key, VPN, DB connection string, SSH key, search/image API key) sau một interface duy nhất; secret không bao giờ vào context của model, span, log hay checkpoint — có test scan tự động cho bất biến này.
- **[v0.1] Result Filters** — redact secret/PII và quét prompt-injection trên mọi tool result trước khi vào context.
- **[v0.3] Policy engine policy-as-config** — rule YAML (điều kiện trên tool/args/session/phân loại dữ liệu → allow/deny/approve/redact), mount read-only; Gate v0.1 chuyển sang đọc engine này cùng interface.
- **[v0.3] Immutable core** — agent có thể *đề xuất* sửa style/capabilities qua review gate nhưng không bao giờ sửa được identity, deny-list gốc, policy file.

### Memory
- **[v0.1] Working memory file-based** — bộ file workspace theo chuẩn de-facto (AGENTS.md, SOUL.md, MEMORY.md, TOOLS.md, daily notes) nạp vào context theo token budget; con người đọc được, git-diff được, không hidden state. *(format OpenClaw, MIT)*
- **[v0.1] Memory review gate** — agent không ghi thẳng MEMORY.md; tool `memory_propose` chỉ ghi staging (`memory/pending/`), người vận hành duyệt bằng `yett memory list|approve|reject` (khác chủ đích so với cả 3 repo tham chiếu).
- **[v0.2] Cross-session memory** — SQLite FTS5 + trigram index (tốt cho tìm substring/tiếng Việt), tool `session_search` trả message gốc từ các phiên cũ. *(vendor schema từ Hermes, MIT)*

### Skills
- **[v0.2] Skills engine chuẩn agentskills.io** — skill = thư mục chứa SKILL.md (frontmatter + hướng dẫn), tương thích hệ sinh thái skill có sẵn; 3 tier precedence (workspace → managed → bundled).
- **[v0.2] Progressive disclosure** — chỉ nạp name+description của mọi skill vào context, body chỉ nạp khi dùng — không vỡ context.
- **[v0.2] Skill do agent tự tạo, có gate** — agent draft skill sau task phức tạp thành công và vá skill khi dùng thấy lỗi, nhưng mọi skill agent viết nằm ở staging đến khi được duyệt (scan code + provenance).

### Vận hành từ xa & dữ liệu (use-case local — dogfood v0.2)

Use-case đầu tiên: trợ lý DevOps cá nhân chạy local — quản lý project, báo cáo tiến độ, code, deploy UAT, điều tra lỗi, kiểm tra dữ liệu (spec: `docs/harness-local-use-case.md`).

- **[v0.2] SSH remote ops** — chỉ đến host khai báo trong profile config; lệnh phân lớp: read-only log/status tự động cho phép, deploy script khai báo trước phải approval từng lần, còn lại mặc định từ chối. **Hardline: tuyệt đối không lệnh xóa file OS qua SSH/VPN** (rm, find -delete, xargs rm, mọi biến thể né tránh) — không có đường approval; xóa hợp lệ duy nhất là bên trong deploy script do người dùng tự viết.
- **[v0.2] VPN** — bọc `openfortivpn` / `openvpn` (argv cố định từ profile allowlist, không shell, không flag từ model); tự bật trước khi SSH nếu `host.vpn_required`; credentials qua secret store → file tạm 0600 (cleanup bắt buộc), không vào context/span/log/error. Operator: `yett vpn connect|disconnect|status <profile>`.
- **[v0.2] Query DB an toàn** — connection profile trong secret store (model chỉ thấy tên profile); phòng thủ 4 lớp: DB user read-only → session read-only → SQL classifier trong Policy Gate (chỉ SELECT/SHOW/EXPLAIN/DESCRIBE; parse fail = từ chối) → approval tường minh từng câu cho write. **Hardline: không ALTER/DROP/TRUNCATE/DELETE/UPDATE nếu không được phép tường minh.**
- **[v0.2] Báo cáo tiến độ project** — skill tổng hợp từ workspace các project + git log, chạy tay hoặc theo cron.

### Chế độ trợ lý (use-case local)
- **[v0.2] Tìm kiếm & research** — tool `web_search` (API key trong secret store) + skill research: tìm → đọc nguồn → tổng hợp có trích dẫn vào workspace; domain fetch vẫn qua egress whitelist do người dùng kiểm soát.
- **[v0.2] Viết content** — skill + template trong workspace, không cần tool mới.
- **[v0.3] Subagents (delegation 1 cấp)** — định nghĩa subagent bằng file (`workspace/agents/*.md`: prompt + toolset con + budget); subagent bundled: `researcher`, `writer`, `illustrator`. Ràng buộc cứng: toolset con ⊆ toolset cha, cùng Policy Gate (không leo thang quyền, hardline áp nguyên vẹn), không delegate lồng nhau, trace lồng cây dưới span cha. Teams/độ sâu >1 vẫn nằm ở defer.
- **[v0.3] Tạo ảnh** — tool `image_gen` qua provider API hoặc backend local không-egress, ảnh lưu vào workspace.

### Hooks & Scheduler
- **[v0.2] Hooks lifecycle** — PreToolUse/PostToolUse với quyền **mutate/deny** (là điểm enforce policy thật, không chỉ observe), cùng các event session/bootstrap/startup; handler là Python entry point; hook lỗi không giết agent.
- **[v0.2] Cron + heartbeat** — lịch 3 syntax (`at`/`every`/`cron`), persist qua restart, chống overlap run; heartbeat đánh thức agent định kỳ theo checklist, kiêm tín hiệu liveness để phát hiện agent treo.

### Observability & Compliance
- **[v0.1] Tracing first-class** — mọi model/tool/hook call có span + correlation ID (5 loại span), lưu SQLite local, replay được một turn; CLI `traces list/get/follow/export`. Có từ commit đầu, không bolt-on.
- **[v0.1] Cost ledger** — mọi request đến provider có phí (LLM, và từ v0.2/v0.3 cả web_search, image_gen) ghi lại provider/model/đơn vị dùng/cost tính từ bảng giá config; xem tổng chi bằng `harness usage --by provider/model/day`; cảnh báo khi chạm ngưỡng budget tháng. Cost là ước tính từ bảng giá — đối soát định kỳ với billing của provider.
- **[v0.3] Analytics on-prem** — usage/cost/success-rate per session đọc từ chính span store (kiểu `/usage`, `/insights`), không thu thập thêm dữ liệu.
- **[v0.3] Compliance layer VN/gov** — audit log append-only cho mọi quyết định Gate/approval/ghi memory, phân loại dữ liệu, retention policy theo yêu cầu khách. *(tự thiết kế 100% — không có nguồn tham chiếu)*

### Giao diện web (app)
- **Web UI local** (`yett serve --open`) — khung chat trong trình duyệt (localhost), tái dùng toàn bộ core.
- **Duyệt approval trên giao diện** — khi agent cần chạy lệnh nhạy cảm (deploy, tool cần duyệt), UI hiện nút **Duyệt / Từ chối** kèm nguyên văn lệnh; turn chờ quyết định (timeout → từ chối sạch). Không cần vào terminal.
- **Tab Traces / Cost** — xem chi phí theo provider + danh sách trace gần đây ngay trên UI.
- **Tab Cấu hình** — xem và sửa `harness.yaml` ngay trong trình duyệt, validate trước khi lưu (config hỏng không ghi đè). Không phải mở file thủ công.
- **`yett doctor`** — kiểm tra máy có đủ công cụ chưa (Python/Docker/git/asyncssh/openfortivpn...), thiếu thì in lệnh cài đúng theo OS (winget/brew/apt).
- Đóng gói `yett.exe` (double-click chạy, không cần Python) — xem [`packaging/BUILD_EXE.md`](packaging/BUILD_EXE.md).

### Kênh giao tiếp
- **[v0.1] CLI/TUI** — kênh duy nhất của MVP.
- **[v0.3] Telegram** — long-poll Bot API, wired `yett serve` (fake/offline verified).
- **[v0.3] Zalo Official Bot API** — `channels.zalo` (poll mặc định hoặc webhook HTTPS + secret); gating/pairing như Telegram; **không** Zalo Personal. Docs tham chiếu: [getUpdates](https://docs.zaloplatforms.com/docs/BOT/apis/getUpdates), [setWebhook](https://docs.zaloplatforms.com/docs/BOT/apis/setWebhook), [sendMessage](https://docs.zaloplatforms.com/docs/BOT/apis/sendMessage). **Chủ đích không làm:** Zalo Personal (lib unofficial) và WeChat (plugin đóng, không portable).
  - Kiểm chứng hiện tại là contract/integration **offline** theo tài liệu công khai; chưa gọi live API vì không có credential/test bot. Bảng `getUpdates` ghi `timeout` là String nhưng sample gửi number (adapter theo bảng/String). Tài liệu cũng chưa chốt semantics idempotency/retry của `sendMessage`, body 429/`Retry-After`, lịch retry webhook, retention replay, hay cách đếm emoji trong giới hạn 2000. Adapter vì vậy chỉ retry outbound khi nhận 429 tường minh, chunk bảo thủ theo UTF-16, và dedupe `(chat_id, message_id)` trong cửa sổ RAM của một process (không tuyên bố exactly-once qua restart).
  - Pairing là bearer code chỉ cho chat `PRIVATE`, có rate limit và chỉ tồn tại tới khi process restart. Nên giữ bot token, pairing code và webhook secret trong secret backend qua các field `*_secret`; field inline chỉ để tương thích config cũ và luôn bị redact khỏi web config.

### Defer có chủ đích (chưa làm, có điều kiện mở lại)
- Semantic memory / knowledge graph — chỉ khi FTS5 đo được là không đủ.
- Multi-agent teams / delegation lồng sâu — chỉ khi có yêu cầu khách cụ thể (delegation 1 cấp đã vào v0.3).
- Self-evolution — chỉ sau khi immutable core chạy ổn định.
- Multi-tenant/RBAC — không làm; mô hình là mỗi khách một instance.

## Tài liệu

Đọc theo thứ tự:

| Tài liệu | Nội dung |
|---|---|
| [`docs/harness-reference-architecture.md`](docs/harness-reference-architecture.md) | Kiến trúc tham chiếu 9 khối + tổng kết 3 repo OSS + ràng buộc pháp lý |
| [`docs/harness-deep-comparison.md`](docs/harness-deep-comparison.md) | Verify từng claim về 3 repo (verdict + nguồn) + đánh giá vendorability |
| [`docs/harness-master-plan.md`](docs/harness-master-plan.md) | Kế hoạch tổng quan: làm gì / học từ đâu / vì sao; lộ trình 4 phase + exit criteria; rủi ro |
| [`docs/harness-architecture-design.md`](docs/harness-architecture-design.md) | Sơ đồ kiến trúc (component + sequence) + mô tả từng thành phần + bố trí dữ liệu |
| [`docs/harness-execution-plan.md`](docs/harness-execution-plan.md) | Kế hoạch thực thi chi tiết: work package/task + dependency + DoD + hệ thống red gate RG-0→RG-4 + ma trận phủ A→Z |
| [`docs/harness-local-use-case.md`](docs/harness-local-use-case.md) | Use-case đầu tiên (trợ lý DevOps cá nhân): 5 kịch bản, tool remote-ops/DB, hardline rules |
| [`docs/harness-detailed-spec-p0-p1.md`](docs/harness-detailed-spec-p0-p1.md) | Spec implementation-ready P0+P1: layout package, interface Python, schema SQLite, config, pseudocode, test đánh số |
| [`docs/harness-detailed-spec-p2.md`](docs/harness-detailed-spec-p2.md) | Spec chi tiết P2: SSH/VPN/DB tools, SQL classifier, skills, hooks, cron, RPC, eval suite |
| [`docs/harness-detailed-spec-p3.md`](docs/harness-detailed-spec-p3.md) | Spec chi tiết P3: policy engine, compliance, channels, subagent, packaging |

## Quyết định đã chốt

Tên sản phẩm **yett** (CLI `yett`, package `yett`, config `~/.yett/`) · Python ≥3.11 · SQLite (v0.1–0.2) · Docker sandbox · single-tenant · chuẩn skills agentskills.io · clean-room bắt buộc với GoClaw (chỉ đọc docs, không đọc code Go) · platform: **chạy được trên Windows, macOS, Linux** — 4 cách cài (Docker Desktop / WSL2 / macOS native / Windows PowerShell), xem [`INSTALL.md`](INSTALL.md). Khuyến nghị host trong **Docker Desktop** (yett chạy trong container Linux → mọi tính năng đúng thiết kế). cmdguard chặn lệnh xóa cả POSIX lẫn Windows · tiến độ đo bằng gate, không ràng buộc thời gian/số dev.

Chất lượng hành vi agent được giữ bằng **eval suite golden tasks** (v0.2): bộ kịch bản chuẩn chấm tự động, bắt buộc chạy khi sửa system prompt/skill/model — hành vi trôi là thấy ngay trong CI.

## License lưu ý

Code vendor từ Hermes Agent và format/spec từ OpenClaw đều MIT — giữ nguyên copyright notice trong thư mục `vendor/`. **Không copy bất kỳ code nào từ GoClaw** (CC BY-NC 4.0) — chỉ học pattern từ tài liệu công khai qua quy trình clean-room mô tả trong master plan.
