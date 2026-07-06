# yett — Đánh giá repo & ý kiến cải thiện

> Ngày: 2026-07-05 · Nhánh: `claude/harness-reference-architecture-0wwuek` · Phạm vi: toàn bộ `src/yett` (~5.2k LOC), test, config, CI.
> Phương pháp: 6 agent đọc song song từng subsystem + 5 agent adversarial thử phá các hardline, mỗi finding được 1 agent độc lập verify (refute-first). Các finding P0 được tác giả xác minh lại trực tiếp trên nguồn.

## TL;DR — ý kiến tổng

Repo cực bài bản về **thiết kế và tài liệu**: fail-closed được wire thật thành một choke point duy nhất (`tools/wiring.py::execute_tool`), classifier dùng parser (sqlglot AST, shlex) thay vì match chuỗi ngây thơ, import-linter giữ bất biến kiến trúc, tracing/audit hash-chain có sẵn từ đầu. Đây là nền tảng tốt.

**Nhưng** khoảng cách giữa *"được quảng cáo"* và *"thực sự chạy được"* là vấn đề lớn nhất. Bảng trạng thái README toàn ✅ — nhưng **test xanh vì mọi thứ chạy qua fake/offline, và CI chỉ chạy Linux**. Đường đi tới provider thật có **ba** khiếm khuyết chí mạng mà không test nào chạm tới: (a) system prompt/capabilities không bao giờ được gửi cho LLM, (b) assistant tool_use turn không vào context (tool_result mồ côi), (c) docker sandbox không được wire (exec chạy trên host). Test suite đang chứng minh *fake hoạt động*, không phải *sản phẩm hoạt động*. Vòng review sâu (Codex) đã reproduce thêm và bổ sung vào danh sách dưới đây.

## Bất biến được quảng cáo nhưng đã verify là hỏng/thiếu

**Thang độ tin cậy:** **A** = tác giả tự đọc nguồn/chạy lệnh trực tiếp trong phiên (bằng chứng ở [Phụ lục](#phụ-lục-bằng-chứng--độ-tin-cậy)); **B** = agent adversarial verify qua truy vết code-path (tác giả đọc guard nhưng chưa tự chạy exploit đúng payload). Không finding nào là suy đoán thuần.

| Cam kết (README) | Thực tế | Bằng chứng | Tin cậy |
|---|---|---|---|
| No-egress: sandbox không network mặc định | **HỎNG** — DockerSandbox không bao giờ được khởi tạo; app âm thầm fallback về LocalSandbox chạy trên host, không cô lập | `app.py:47`, `app.py:79`, `sandbox/local.py:39` | **A** (đọc nguồn trực tiếp) |
| Agent loop bounded chạy được | **HỎNG với provider thật** — assistant tool_use turn không được thêm vào context; provider thật reject tool_result mồ côi. Chỉ FakeProvider chịu được | `core/loop.py:131,160`, `core/context.py:28-31` | **A** (đọc nguồn trực tiếp) |
| Agent loop có system prompt/safety | **HỎNG với provider thật** — `Context.system` giữ riêng, không bao giờ vào `messages`; `chat()` chỉ nhận `messages` → capabilities/safety/skill menu/Sources-First KHÔNG tới LLM | `core/context.py:21,58-64`, `core/loop.py`, `provider/base.py:70`, `provider/openai_compat.py:53` | **A** (đọc nguồn trực tiếp) |
| HARDLINE: không lệnh xóa OS qua SSH | **HỎNG** — `ls & rm -rf /data` được classify READONLY → ALLOW (toán tử `&` không nằm trong regex tách lệnh) | `security/cmdguard.py:70,142` | **A** (đọc regex trực tiếp; repro empiric bởi Codex) |
| Secrets không bao giờ vào context | **PARTIAL** — redact private key chỉ xóa dòng header PEM, để lọt toàn bộ body key | `security/filters.py:19` | **B** (truy vết code-path) |
| DB read-only (parse fail = từ chối) | **HỎNG trên MySQL** — `/*!...*/` bị strip trước khi parse, executor chạy raw SQL | `tools/db/sqlguard.py:30` | **B** (truy vết code-path) |

## Ưu tiên sửa

### P0 — sửa trước khi dùng thật (bất biến lõi bị phá)

1. **DockerSandbox không được wire** (`app.py:48,81`). `sandbox = LocalSandbox() if backend=="local" else None` → với default `backend: docker`, sandbox=None → `sb = sandbox or LocalSandbox()`. DockerSandbox là dead code. Exec chạy host, egress allowlist chỉ bọc `web_fetch` nên `python -c "urllib...169.254.169.254"` hoặc `git clone http://...` ra mạng tự do.
   → **Fix:** khởi tạo DockerSandbox khi `backend=="docker"`; nếu Docker không sẵn sàng thì **fail-closed** (từ chối exec), tuyệt đối không âm thầm hạ cấp về host. `local.py:4` docstring "bị Policy Gate chặn trong production" hiện là sai — không chỗ nào check `backend` để chặn.

2. **Loop bỏ assistant tool_use turn** (`loop.py:160`). Vòng lặp `for tc in res.tool_calls: ... ctx.add_tool_result(tc.id, ...)` nhưng không có bước ghi lại message assistant chứa các tool_use trước đó. Anthropic/OpenAI yêu cầu thứ tự: assistant(tool_use) → tool_result. Thiếu bước này → lỗi "tool_result without tool_use".
   → **Fix:** thêm `ctx.add_assistant_tool_calls(res.tool_calls)` (hoặc tương đương) trước khi add tool results; khi resume, khôi phục cả tool results vào context, không chỉ danh sách `completed`.

3. **System prompt/capabilities không bao giờ tới LLM** (`context.py:21,58-64` + `loop.py` + `provider`). `Context` giữ `system` là field riêng, `assemble_context` chỉ append history + user vào `messages` — **không** thêm system Message. Loop gọi `chat(ctx.messages, tools)`; `Provider.chat` (`base.py:70`) không có tham số system; `openai_compat.chat` build `body["messages"]` chỉ từ `messages` (`openai_compat.py:53`). ⇒ `capabilities_summary()`, workspace memory, skill menu, **safety instructions**, và cả prompt "Sources-First" (commit `2cc89a8`) đều không bao giờ tới model thật — agent chạy với zero system prompt. Vô hình vì FakeProvider bỏ qua cấu trúc messages. **Sửa TRƯỚC router/anti-loop.**
   → **Fix:** prepend một `Message(role="system", ...)` trong `assemble_context` (dataclass Message đã hỗ trợ role "system"), hoặc thêm tham số `system` cho `chat()`; thêm test assert provider nhận được system prompt.

4. **cmdguard bypass qua `&`** (`cmdguard.py:70`). Regex tách `(?:&&|\|\||;|\||\n)` thiếu lone `&`. `ls & rm -rf /data`, `echo hi & rm -rf /data` → READONLY → ALLOW qua SSH, không có approval.
   → **Fix:** thêm `&` (và `\n`) vào cả hai regex tách trong `_contains_delete` và `_is_readonly`; test 45-case hiện có không phủ toán tử background.

5. **Filter để lọt body private key** (`filters.py:19`). Pattern `-----BEGIN [A-Z ]*PRIVATE KEY-----` không DOTALL, chỉ khớp header → `redact()` thay 1 dòng, body base64 + footer END vào thẳng context khi model gọi `cat ~/.ssh/id_rsa`.
   → **Fix:** khớp cả block header→footer (DOTALL) và redact toàn khối; thêm test key thật (fixture).

### P1 — hardline & secret còn hở

| # | File | Vấn đề | Fix ngắn |
|---|---|---|---|
| 5 | `sqlguard.py:30` | `_normalize` strip `/*!...*/` MySQL exec-comment trước parse → write lọt trên driver mysql | Không strip exec-comment; hoặc classify trên SQL raw theo dialect |
| 6 | `cmdguard.py:86` | Redirect không-space `echo x>/etc/hostname` + traversal `>/tmp/../etc/passwd` né guard overwrite | Regex nhận fd-prefix `1>`, canonicalize target, chỉ miễn `/dev/null` + tmp thật |
| 7 | `filters.py:16,18,39` | Miss `sk-proj-/sk-svcacct-/sk-admin-`, miss ODBC DSN `Pwd=/Password=`, không redact secret trong dict/list lồng | Cập nhật pattern key mới; redact đệ quy trong `redact_attrs` |
| 8 | `immutable.py:39` | Bảo vệ file policy/identity dùng `str(p) in target` (substring path) → `sed -i policy.yaml` (path tương đối) lọt | So khớp canonical bằng `Path.resolve()` + `is_relative_to`; path không resolve được = protected |
| 9 | `remote/ssh_exec.py:96` | `log_read` allowlist qua `fnmatch` bypass → đọc file remote tùy ý | Canonicalize + containment thay vì glob |
| 10 | `tools/wiring.py:37` | Subagent tool-subset chỉ enforce ở schema hiển thị cho LLM, không enforce lúc execute | Check `tc.name ∈ allowed_tools` trong `execute_tool` |
| 11 | `tools/wiring.py:59` | PreToolUse hook mutate args sau gate, args mới không được re-gate → denylist bị vượt | Re-run `safe_evaluate`/denylist trên `mutated_args` trước validate/run |
| 12 | `remote/ssh_backend.py:23` | `known_hosts=None` → tắt xác minh host key (MITM) | Dùng known_hosts thật / pin key trong host profile |
| 13 | `secrets/file_store.py:29` | `os.chmod(0o600)` là no-op trên Windows + `except OSError: pass` nuốt lỗi; docstring hứa 600. Windows là platform quảng cáo chính | Set ACL owner-only trên Windows (icacls/pywin32); không set được thì **cảnh báo to**, không im lặng |
| 14 | `config/harness.yaml:22` | Một chuỗi đúng định dạng API key MiniMax (`sk-cp-…`) nằm plaintext trong working tree — tác giả **đọc thấy trực tiếp** giá trị này qua `sed`, và nó đã bị echo vào output tool (lộ ngoài file cục bộ). File **có** trong `.gitignore` (chưa bị commit — tốt). `/api/config` GET trả raw config kèm key, không auth. **Chưa test key còn active hay không** (không gọi mạng — cố ý). | **Rotate như phòng ngừa** cho credential đã lộ (không phụ thuộc việc nó còn sống); chuyển sang secret store; feature inline `api_key` mâu thuẫn trực tiếp với bất biến secret-store. *(Tin cậy: **A** cho "tồn tại + đã lộ"; không khẳng định "đang active".)* |
| 15 | `tools/builtin/exec.py:36` | `cwd=args.get("cwd")` truyền thẳng vào sandbox, **không** qua `ProjectScope.resolve_in_scope()` như read_file/list/grep → cwd tùy ý (`exec {"cmd":"...","cwd":"D:\\"}`). Với LocalSandbox-on-host (P0-1) = lệnh allowlisted chạy ở thư mục bất kỳ trên host. **→ nâng P0 nếu LocalSandbox là runtime thật** (xem câu hỏi mở). Tin cậy **A** | Scope-check `cwd` qua ProjectScope trước khi chạy; cwd ngoài scope = từ chối |
| 16 | `tools/wiring.py:70,77-78` | PostToolUse mutate **sau** Filters: `filter_apply` chạy dòng 70, rồi return `post.mutated_result` thẳng (77-78) không filter lại → hook đưa secret/prompt-injection vào context sau filter. `hooks/runner.py:3` document đúng thứ tự là PostToolUse→Filters. Gương của #11. Tin cậy **A** | Chạy PostToolUse **trước** filter, hoặc filter lại `mutated_result` trước return |
| 17 | `secrets/resolve.py:15-18` | `secret_backend=keyring/age` (khai báo hợp lệ ở `config/models.py`) **âm thầm** fallback về `FileSecretStore` (plaintext). Comment tự thừa nhận "adapter native để sau". User tưởng dùng backend an toàn hơn nhưng thực tế đọc/ghi plaintext. Tin cậy **A** | Backend chưa implement phải **fail startup**, không downgrade; hoặc bỏ keyring/age khỏi model tới khi có adapter |

### P2 — đúng đắn/độ bền

- `web/server.py:37` không validate Host/Origin → DNS-rebinding cho service localhost; `server.py:52` `/api/config` GET lộ secret không auth; `cli.py:33` `--host` nhận mọi bind address, zero auth.
- `web_fetch.py:40` egress allowlist chỉ check URL đầu, không check redirect → SSRF.
- `compliance/audit.py:45` hash-chain không tamper-evident với attacker có quyền file (không HMAC/khóa) — "append-only" chỉ chống sửa ngây thơ; `audit.py:26` đọc cả file mỗi lần append (O(n²), không lock).
- `memory/review_gate.py` **không được wire** → bất biến "agent không ghi thẳng MEMORY.md" chưa được enforce.
- `tools/base.py:39` JSON schema khai báo cho provider nhưng `validate()` chỉ check presence ad-hoc, không validate theo schema.
- `codenav.py:117` GrepTool compile regex do agent cung cấp, chạy đồng bộ → ReDoS/block event loop.
- `analytics/insights.py:17` `check_budget` so limit tháng với cost all-time (sai) — mà cũng không có caller.
- `retention.py:18` xóa im lặng khi `auditor is None`, trái contract "mọi delete được audit".
- **SSH port trong config bị ignore**: `config/models.py:88` có `port: int = 22` nhưng `app.py:163-166` build `HostProfile` không truyền port, và `remote/hostprofile.py:12-18` không có field `port` → asyncssh luôn nối cổng 22, config `port` vô tác dụng. Fix: thêm `port` vào `HostProfile` + truyền `asyncssh.connect(..., port=host.port)`. *(Cùng chỗ: `user` bị gộp vào address dạng `user@addr` — nên kiểm tra asyncssh có parse không, hoặc truyền `username=` tách bạch.)*
- **`web_search` khai báo nhưng không app-wired**: `search=SearchCfg(...)` hợp lệ trong config nhưng `_wire_search` (`app.py:96`) là no-op, `registry.has("web_search")==False`. README nên ghi "tool class implemented, chưa app-wired" thay vì ✅ (trùng mục over-engineering P3).

### P3 — over-engineering (góc nhìn ponytail, repo mới v0.0.1)

Nhiều code **định nghĩa nhưng không caller** — trong sản phẩm bảo mật, dead code trông như coverage nhưng không phải, là nợ:
- `obs/cost.py::compute_call_cost`, `analytics/insights.py::check_budget`, `compliance/retention.py::purge_old_messages` — không caller trong `src/`.
- `core/session.py::Session._lock`/`lock()` — không bao giờ gọi (mà lại là gốc của issue serialization).
- `provider/base.py`: `FailReason` 9 giá trị nhưng adapter thật chỉ emit vài; `Usage.cache_write_tokens` không bao giờ set; `SpanKind.EMBEDDING` không bao giờ emit.
- `tools/db/db_query.py`: `DbProfile.readonly` không bao giờ đọc.
- `sandbox/docker.py:27`: ternary `network if network!='none' else 'none'` là no-op; và cả class chưa wire (xem P0-1).
- `skills/review_gate.py` + `_DANGER` blocklist, `web/page.py` config-editor tab: tính năng chưa wire hoàn chỉnh / rủi ro (sửa policy qua browser).
- `context.py::prune` gọi `self.tokens()` (full re-scan) mỗi message → O(n²).

→ **Khuyến nghị:** với mỗi mục, hoặc wire vào đường thực thi, hoặc xóa. Nếu giữ làm placeholder cho phase sau thì đánh dấu rõ, đừng để bảng README tick ✅.

## Điểm mạnh (công bằng)

- Fail-closed **thật**: `safe_evaluate` (`gate.py:34-44`) bắt mọi exception → deny; `execute_tool` là đường thực thi duy nhất, dùng chung cho loop và RPC broker; default-deny kết thúc cả hai backend gate.
- `ProjectScope.resolve_in_scope` dùng parent-containment trên resolved path (không phải string-prefix) — chống traversal đúng cách.
- sqlguard fail-closed đúng: parse fail → DENY, multi-statement → DENY, walk AST gồm CTE/subquery (vấn đề chỉ ở lớp normalize comment).
- RPC broker chặn recursive execute_code/delegate, cap calls/output, strip env secret — vá đúng các lỗi Hermes đã biết.
- Cron overlap chống bằng UPDATE điều kiện atomic (rowcount==1), không read-then-write.
- Tracing cố ý không ghi `tc.args`/nội dung message vào span; timestamp inject từ ngoài (test deterministic).
- import-linter enforce bất biến kiến trúc (core không import thẳng sandbox/adapter); mypy strict sạch 103 file.

## Vấn đề meta (ý kiến mạnh nhất)

Sửa lỗ hổng là cần, nhưng gốc rễ là **quy trình verify**. Đề xuất:
1. **CI thêm leg Windows + macOS.** Bug secret-perm (P1-13) và mọi giả định POSIX sẽ hiện ngay. Hiện CI chỉ `ubuntu-latest`.
2. **Ít nhất 1 contract test provider thật** (record/replay HTTP như VCR) để bắt lỗi loop P0-2 — lỗi này vô hình với FakeProvider.
3. **1 integration test docker-sandbox** gated theo daemon, assert exec **không** ra mạng và **không** chạy trên host.
4. **Sửa bảng trạng thái README** phân biệt rõ "logic test qua fake" vs "verified end-to-end". Số test đang lệch (README ghi 161 và 183; thực tế ~213, 1 fail trên Windows).
5. **`ruff` không chạy được ở env local** (thiếu trong system Python313) — claim "ruff xanh" chỉ verify được trong CI Linux; nên document rõ hoặc pin trong dev env dùng chung.

## Review commit mới `2cc89a8` (complexity router + anti-loop + batch search + Sources-First)

Bốn thay đổi, thuần Python, không thêm dependency — tinh thần đúng (KISS/DRY). Nhưng xây **trên** loop vẫn hỏng.

**Tốt:**
- `ComplexityRouter` (`core/complexity.py`): heuristic deterministic, offline, không tốn LLM call, config-gated (`RouterCfg`). Hợp triết lý cost-aware/on-prem.
- `SearchTool` (`codenav.py:174+`): tái dùng GrepTool/ReadFileTool/ListDirTool qua ProjectScope (không reimplement), cap 10 op/lần, bắt `UserFacingError` per-section nên 1 op lỗi không giết cả batch. DRY đúng.
- Anti-loop `reserve_final_steps`/`force_text_after_repeats`: pattern rẻ, hợp lý — ép trả lời khi cạn budget hoặc kẹt lặp tool.
- Prompt Sources-First trùng reality-filter.

**Vấn đề:**
1. **P0-2 vẫn chưa sửa** (ưu tiên sai). Commit chạm thẳng `loop.py` nhưng vẫn chỉ có `add_assistant(text)` (`loop.py:131`, text-only) + `add_tool_result` (`loop.py:160`); `context.py` không có method ghi assistant *tool_use* turn. Anti-loop/router build lên trên loop chưa chạy được với provider thật. Nên fix P0-2 **trước**, rồi mới thêm feature.
2. **Test vẫn fake-only.** `test_core_loop.py` mới: "provider luôn trả tool_call" — vẫn FakeProvider, nên logic anti-loop mới (ép `tools=[]`, cap iteration) chưa từng chạy với chuỗi message tool_use của provider thật. P0-2 vẫn vô hình.
3. **SearchTool khuếch đại ReDoS.** GrepTool compile regex do agent cấp và scan đồng bộ (P2, `codenav.py`); giờ 1 lần `search` gom tới 10 grep → 1 call có thể block event loop ~10×. → thêm timeout/giới hạn hoặc chạy trong executor; `_section` cũng chỉ bắt `UserFacingError` (exception khác vẫn crash turn — giống `wiring.py:66`).
4. **Router `enabled: True` mặc định siết mọi turn.** Câu khó bị phân nhầm "simple" (4 vòng) + `reserve_final_steps=1` → chỉ ~3 vòng tool trước khi ép trả lời. Với điều tra DevOps ("vì sao service chết") có thể cụt. Tunable + bị budget chặn trên, nhưng nên khởi đầu bảo thủ hoặc document rõ ngưỡng.
5. Nhỏ: `search` chưa chắc có trong allowlist mẫu → default-deny sẽ chặn (fail-closed, an toàn nhưng tool im lặng không dùng được cho tới khi allowlist hóa).

**Verdict commit:** hướng đi và cách viết tốt, nhưng thứ tự ưu tiên nên đảo — sửa loop (P0-2) + thêm 1 test provider thật trước, rồi mới router/anti-loop; nếu không, đang tối ưu chi phí cho một loop chưa hoạt động thật.

## Phụ lục: bằng chứng & độ tin cậy

Bằng chứng cho các finding **Tier A** (tự chạy/đọc trực tiếp trong phiên 2026-07-05). Finding **Tier B** đến từ agent adversarial verify qua truy vết code-path; ai muốn reproduce thì chạy payload trong cột "Repro" của báo cáo gốc.

**Baseline (test + typecheck):**
```
$ mypy src/yett   → Success: no issues found in 104 source files   (104 sau commit 2cc89a8; +complexity.py)
$ pytest -q       → baseline earlier run: 1 failed, 212 passed
FAILED tests/unit/test_setup_wizard.py::test_file_secret_store — assert 438 == 384
  (438 = 0o666 thực tế; 384 = 0o600 kỳ vọng → chmod 0o600 no-op trên Windows)
$ ruff            → No module named ruff   (không chạy được ở env local Python313; claim "ruff xanh" chỉ verify trong CI)
```
> **Lưu ý test baseline:** lần chạy `pytest` đầy đủ trong phiên này KHÔNG tin cậy trên Windows (lỗi quyền temp/cache, process không thoát trong timeout). Con số 212/1 là từ một lần chạy trước; **không claim "test xanh" trong phiên này**. mypy thì chạy sạch lại được (104 file).

**P0-1 DockerSandbox không wire** (`src/yett/app.py`, số dòng hiện tại sau commit `2cc89a8`):
```
48:  sandbox = LocalSandbox() if cfg.sandbox.backend == "local" else None  # docker=None
81:  sb = sandbox or LocalSandbox()                                        # None → LocalSandbox
```
`config/harness.example.yaml` đặt `sandbox.backend: docker` (default) → nhánh `else None` → fallback host. `DockerSandbox` không được `register`/instantiate ở bất kỳ đâu trong `src/`.

**P0-2 Loop bỏ assistant tool_use turn** (`src/yett/core/loop.py` + `context.py`):
```
loop.py:131  ctx.add_assistant(text)          # chỉ text
loop.py:160  ctx.add_tool_result(tc.id, ...)  # tool result
context.py:  chỉ có add_assistant(text) + add_tool_result(call_id, content)
             — KHÔNG có method ghi assistant message chứa tool_use blocks
```
Commit `2cc89a8` (mới nhất) chạm `loop.py` nhưng không thêm bước này → bug còn nguyên.

**P0-3 System prompt không tới provider** (`context.py` + `provider`):
```
context.py:21     class Context: system: str          # system giữ RIÊNG, ngoài messages
context.py:58-64  assemble_context(): chỉ append history + user vào messages; KHÔNG add system Message
base.py:70        Provider.chat(messages, tools, *, stream)   # KHÔNG có tham số system
openai_compat.py:53  body["messages"] = [_to_openai_msg(m) for m in messages]  # chỉ từ messages
```
Message dataclass CÓ hỗ trợ role="system" (base.py:31) nhưng không ai tạo nó → LLM nhận zero system prompt.

**P1-15/16/17 & P2-SSH port** (đọc nguồn trực tiếp):
```
exec.py:36        res = await self._sandbox.run(argv, cwd=args.get("cwd"), ...)   # cwd không scope-check
wiring.py:70      result = filter_apply(raw, ...)          # filter TRƯỚC
wiring.py:77-78   return ToolResult(..., content=post.mutated_result, ...)  # PostToolUse mutate, KHÔNG filter lại
resolve.py:15-18  # keyring/age → "hiện dùng file store" → FileSecretStore (plaintext), im lặng
hostprofile.py:12-18  class HostProfile(...)   # KHÔNG có field port
app.py:163-166    HostProfile(address=addr, auth=..., ...)  # không truyền port (models.py:88 có port=22)
```

**CI chỉ Linux** (`.github/workflows/ci.yml`): `jobs.test.runs-on: ubuntu-latest` (dòng 10), không có matrix Windows/macOS → không leg nào bắt được test fail Windows ở trên.

**P1-14 Key trong working tree** (`config/harness.yaml:22`):
```
$ git ls-files config/          → chỉ harness.example.yaml, pricing.yaml (harness.yaml KHÔNG tracked)
$ grep harness.yaml .gitignore  → /config/harness.yaml  (đã ignore)
$ sed -n '22p' config/harness.yaml → api_key: sk-cp-uknqTaO1**********(redacted)**********
```
Kết luận: chưa bị commit; nhưng giá trị thật nằm plaintext trên đĩa và đã lộ qua output tool trong phiên này.

## Đánh giá phản biện của Codex (adversarial-review)

Codex chấm "needs-attention / no-ship" với 2 finding **high**. Lưu ý bối cảnh: Codex chỉ đọc **chính file report này** (chạy `Get-Content` trên nó), **không** đọc codebase — nên verdict của nó là về *độ tự-chứng-minh của tài liệu*, không phải về việc bug có thật hay không.

- **Finding #1 (thiếu bằng chứng reproduce trong artifact) — ĐÚNG, đã tiếp thu.** Report tự nhận "verified" nhưng không mang theo lệnh/output/excerpt. Đã thêm cột **Tin cậy** + **Phụ lục bằng chứng** ở trên. Không hạ finding xuống "giả thuyết" vì Codex nêu quan ngại trừu tượng, không đưa bằng chứng mới rằng finding nào sai (nguyên tắc: chỉ đảo quyết định đã verify khi có bằng chứng mới).
- **Finding #2 (khuyến nghị rotate key quá mức) — SAI về dữ kiện, đã làm rõ.** Codex gọi đây là "unverified allegation"; thực tế tác giả **đọc trực tiếp** giá trị key qua `sed`. Việc **không** nhúng key đầy đủ vào report là *đúng* thực hành (chính Codex cũng khuyên "secured channel"), không phải lỗ hổng. Đã tách rõ "tồn tại + đã lộ" (Tier A, chắc chắn) khỏi "đang active" (chưa test). Rotate vẫn là khuyến nghị đúng cho credential đã lộ, bất kể còn sống hay không — đây là chuẩn xử lý sự cố, không phải phản ứng thái quá.

## Câu hỏi chưa giải quyết

- DockerSandbox chưa wire là **có chủ đích hoãn** (fallback tạm cho v0.0.1) hay bug? README nói docker là khuyến nghị host — nếu chủ đích thì cần fail-closed rõ ràng, không im lặng hạ cấp.
- Loop P0-2/P0-3 (assistant tool_use + system prompt) — đã từng chạy với provider thật lần nào chưa, hay toàn bộ dogfood qua `yett demo` (FakeProvider)? Cả ba khiếm khuyết real-provider chỉ vô hình vì FakeProvider bỏ qua cấu trúc messages.
- **LocalSandbox là runtime thật hay chỉ dev/test?** Quyết định này chốt độ nghiêm trọng của #15 (exec.cwd) và P0-1: nếu chỉ dev/test → `build_app` phải enforce rõ (từ chối khi backend=docker mà Docker thiếu); nếu là runtime thật được support → bắt buộc scope-check `cwd` + cảnh báo security posture (không có network isolation).
- Feature inline `api_key` trong config (commit b81e673) có mâu thuẫn chủ đích với secret-store không, hay chấp nhận trade-off "local convenience"?
- `secret_backend` khai báo `keyring`/`age` — có ý định implement adapter thật (thì #17 là bug tạm), hay nên gỡ khỏi schema tới khi có (tránh cảm giác an toàn giả)?
