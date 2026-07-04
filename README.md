# Agent Harness (tên làm việc — chưa chốt)

> **Trạng thái:** Giai đoạn thiết kế (Phase 0 chưa bắt đầu). Repo hiện chứa bộ tài liệu kiến trúc/kế hoạch; code sẽ được phát triển theo lộ trình trong `docs/harness-master-plan.md`.

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
- **[v0.1] Agent loop bounded** — vòng lặp Think → Prune → Tool → Observe → Checkpoint có trần số vòng; **Prune** tỉa context mỗi vòng theo token budget; **Checkpoint** persist trạng thái để kill giữa chừng vẫn resume được.
- **[v0.1] Provider layer** — một interface thống nhất (chat/stream/tool-call/usage), 2 adapter đầu: Anthropic + OpenAI-compatible; đổi model/provider bằng config không sửa core; retry/backoff, failover theo lý do lỗi chuẩn hóa, prompt caching; token + cost ghi vào trace từng call.
- **[v0.2] RPC code execution** — agent viết script Python gọi tool qua RPC, gom pipeline nhiều bước thành một lượt không tốn context; script chạy trong container, từng tool call vẫn xuyên Policy Gate.

### Tools & Sandbox
- **[v0.1] Tool runtime** — registry tool có JSON schema + validation; lỗi trả về dạng agent-tự-sửa-được. Tool đầu: `exec`, `read_file`, `write_file`, `web_fetch` (qua egress whitelist).
- **[v0.1] Docker sandbox** — mọi lệnh exec chạy trong container hardened (drop ALL capabilities, no-new-privileges, resource limits, timeout, mặc định không network); abstraction backend giữ sẵn đường thêm Singularity nếu khách cấm Docker daemon. *(vendor từ Hermes, MIT)*

### Security & Guardrails
- **[v0.1] Policy Gate** — hardline deny-list (không override được) → allowlist per-deployment → approval 2 mức (manual/smart, không có chế độ YOLO) → mặc định DENY; fail-closed có test riêng.
- **[v0.1] Result Filters** — redact secret/PII và quét prompt-injection trên mọi tool result trước khi vào context.
- **[v0.3] Policy engine policy-as-config** — rule YAML (điều kiện trên tool/args/session/phân loại dữ liệu → allow/deny/approve/redact), mount read-only; Gate v0.1 chuyển sang đọc engine này cùng interface.
- **[v0.3] Immutable core** — agent có thể *đề xuất* sửa style/capabilities qua review gate nhưng không bao giờ sửa được identity, deny-list gốc, policy file.

### Memory
- **[v0.1] Working memory file-based** — bộ file workspace theo chuẩn de-facto (AGENTS.md, SOUL.md, MEMORY.md, TOOLS.md, daily notes) nạp vào context theo token budget; con người đọc được, git-diff được, không hidden state. *(format OpenClaw, MIT)*
- **[v0.1] Memory review gate** — agent không ghi thẳng MEMORY.md; đề xuất vào staging, được duyệt mới merge (khác chủ đích so với cả 3 repo tham chiếu).
- **[v0.2] Cross-session memory** — SQLite FTS5 + trigram index (tốt cho tìm substring/tiếng Việt), tool `session_search` trả message gốc từ các phiên cũ. *(vendor schema từ Hermes, MIT)*

### Skills
- **[v0.2] Skills engine chuẩn agentskills.io** — skill = thư mục chứa SKILL.md (frontmatter + hướng dẫn), tương thích hệ sinh thái skill có sẵn; 3 tier precedence (workspace → managed → bundled).
- **[v0.2] Progressive disclosure** — chỉ nạp name+description của mọi skill vào context, body chỉ nạp khi dùng — không vỡ context.
- **[v0.2] Skill do agent tự tạo, có gate** — agent draft skill sau task phức tạp thành công và vá skill khi dùng thấy lỗi, nhưng mọi skill agent viết nằm ở staging đến khi được duyệt (scan code + provenance).

### Vận hành từ xa & dữ liệu (use-case local — dogfood v0.2)

Use-case đầu tiên: trợ lý DevOps cá nhân chạy local — quản lý project, báo cáo tiến độ, code, deploy UAT, điều tra lỗi, kiểm tra dữ liệu (spec: `docs/harness-local-use-case.md`).

- **[v0.2] SSH remote ops** — chỉ đến host khai báo trong profile config; lệnh phân lớp: read-only log/status tự động cho phép, deploy script khai báo trước phải approval từng lần, còn lại mặc định từ chối. **Hardline: tuyệt đối không lệnh xóa file OS qua SSH/VPN** (rm, find -delete, xargs rm, mọi biến thể né tránh) — không có đường approval; xóa hợp lệ duy nhất là bên trong deploy script do người dùng tự viết.
- **[v0.2] VPN** — bọc OpenVPN/Fortinet client (đánh giá openfortivpn), tự bật trước khi SSH nếu host yêu cầu; credentials nằm trong secret store, không bao giờ vào context của model.
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

### Kênh giao tiếp
- **[v0.1] CLI/TUI** — kênh duy nhất của MVP.
- **[v0.3] Telegram → Zalo Bot API** — theo thứ tự độ khó đã verify. **Chủ đích không làm:** Zalo Personal (phụ thuộc thư viện unofficial reverse-engineered, rủi ro ToS) và WeChat (plugin đóng của bên thứ ba, không portable).

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

## Quyết định đã chốt

Python ≥3.11 · SQLite (v0.1–0.2) · Docker sandbox · single-tenant · chuẩn skills agentskills.io · clean-room bắt buộc với GoClaw (chỉ đọc docs, không đọc code Go).

## License lưu ý

Code vendor từ Hermes Agent và format/spec từ OpenClaw đều MIT — giữ nguyên copyright notice trong thư mục `vendor/`. **Không copy bất kỳ code nào từ GoClaw** (CC BY-NC 4.0) — chỉ học pattern từ tài liệu công khai qua quy trình clean-room mô tả trong master plan.
