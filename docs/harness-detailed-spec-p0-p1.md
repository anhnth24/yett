# Detailed Spec — Phase 0 & Phase 1 (implementation-ready)

> **Trạng thái:** Draft để review · 04/07/2026
> **Phạm vi:** hạ execution plan xuống mức code được — layout package, interface Python, schema SQLite, config mẫu, pseudocode, và test đánh số map thẳng vào tiêu chí gate. **Chỉ Phase 0 + Phase 1** (phần code trước). Phase 2+ có spec tương tự khi tới lượt (quy tắc: spec chi tiết viết ngay trước khi implement, không viết trước cả năm).
> **Đầu vào:** `harness-execution-plan.md` (task ID, DoD, gate) + `harness-architecture-design.md` (thành phần, bất biến).
> **Quy ước:** tên placeholder `hx` cho package/CLI (thay khi chốt tên ở P0.5.2). Ký hiệu **[Inference]** = lựa chọn kỹ thuật đề xuất, chốt khi implement. Interface là contract — chữ ký giữ ổn định; thân hàm minh họa.

---

## 0. Nguyên tắc code xuyên suốt

- **Python ≥3.11**, type hints bắt buộc, `mypy --strict` trong CI.
- **Protocol/ABC cho mọi ranh giới** (provider, sandbox, tool, secret store) — để thay backend không đụng core.
- **Không I/O ẩn:** mọi truy cập mạng/đĩa/secret đi qua interface tường minh (dễ test + dễ chứng minh no-egress).
- **Lỗi phân 2 loại:** `UserFacingError` (agent đọc được, sửa được) vs `SystemError` (bug, fail-closed). Gate/secret lỗi → luôn fail-closed.
- **Async:** core loop async (`asyncio`); tool call có thể chạy trong thread/subprocess. **[Inference]**
- **Không dùng `Date.now()`/random ẩn trong logic quyết định** để test deterministic (RG1-6).

---

## 1. Layout package (P0.3.1)

```
<repo>/
├── pyproject.toml            # pin exact deps; entry point hx = hx.cli:main
├── src/hx/
│   ├── __init__.py
│   ├── cli.py                # [P1] entrypoint: chat, traces, usage, memory, project
│   ├── config/
│   │   ├── loader.py         # đọc + validate harness.yaml, pricing.yaml (pydantic)
│   │   └── models.py         # pydantic models cho toàn bộ config
│   ├── core/
│   │   ├── loop.py           # [P1] agent loop: ContextStage/Think/Prune/Tool/Observe/Checkpoint/Finalize
│   │   ├── session.py        # [P1] Session Manager: session_key, queue, lifecycle
│   │   ├── context.py        # [P1] ghép context deterministic + token budget
│   │   ├── checkpoint.py     # [P1] persist/resume trạng thái vòng lặp + cancel
│   │   └── cancel.py         # [P1] cancel token, kill sạch
│   ├── provider/
│   │   ├── base.py           # [P1] Protocol Provider + types (Message, ToolCall, Usage, ChatResult)
│   │   ├── anthropic.py      # [P1] adapter Anthropic
│   │   ├── openai_compat.py  # [P1] adapter OpenAI-compatible
│   │   └── failover.py       # [P1] router 9 reasons; context-overflow → compaction
│   ├── security/
│   │   ├── gate.py           # [P1] Policy Gate: evaluate() -> Decision
│   │   ├── denylist.py       # [P1] hardline deny-list (v0.1 hard-coded)
│   │   ├── allowlist.py      # [P1] allowlist per-deployment từ config
│   │   ├── approval.py       # [P1] approval flow (CLI v0.1)
│   │   └── filters.py        # [P1] Result Filters: redact secret/PII, injection scan
│   ├── secrets/
│   │   ├── base.py           # [P1] Protocol SecretStore: get(name)
│   │   └── backends.py       # [P1] keyring / age-file backend
│   ├── tools/
│   │   ├── registry.py       # [P1] đăng ký tool, schema, validate
│   │   ├── base.py           # [P1] Protocol Tool
│   │   ├── builtin/
│   │   │   ├── exec.py       # [P1] -> sandbox
│   │   │   ├── files.py      # [P1] read_file/write_file (scope check)
│   │   │   └── web_fetch.py  # [P1] qua egress whitelist
│   │   └── projects.py       # [P1] đăng ký project, resolve path scope
│   ├── sandbox/
│   │   ├── base.py           # vendored ABC (P0.2.1)
│   │   ├── docker.py         # vendored
│   │   ├── local.py          # vendored (dev only)
│   │   └── wiring.py         # [P1] Gate→Registry→Sandbox→Filters
│   ├── memory/
│   │   ├── workspace.py      # [P1] loader file workspace + token budget
│   │   ├── review_gate.py    # [P1] staging memory/pending
│   │   └── store.py          # [P2] SQLite FTS5 (vendored schema, để sẵn)
│   ├── obs/
│   │   ├── tracer.py         # [P1] span model, correlation id
│   │   ├── spanstore.py      # [P1] buffer→flush SQLite
│   │   ├── cost.py           # [P1] cost ledger từ pricing.yaml
│   │   └── cli.py            # [P1] traces list/get/follow, usage
│   └── errors.py             # UserFacingError, SystemError, DenyError...
├── vendor/
│   ├── hermes_environments/  # P0.2.1 + LICENSE + MANIFEST.md (SHA nguồn)
│   └── hermes_state/         # P0.2.2 (schema, để sẵn cho P2)
├── tests/
│   ├── unit/  integration/  arch/  fixtures/
│   └── conftest.py
├── .import-linter            # cấm import lậu (AG-3) + core→sandbox thẳng
└── .github/workflows/ci.yml  # lint, mypy, test, import-linter, build container
```

**Import-linter contracts (AG-3 + bất biến kiến trúc):**
- `hx.core` KHÔNG được import `hx.sandbox.*` trực tiếp (phải qua `tools.wiring`).
- Mọi thứ ngoài `hx.sandbox` và `hx.memory.store` KHÔNG được import `vendor.*` trừ qua adapter khai báo trong manifest.
- `hx.core`/`hx.tools` KHÔNG import `hx.provider.anthropic`/`openai_compat` trực tiếp (chỉ qua `provider.base` + factory).

---

## 2. Config schema (config/models.py) — pydantic

```python
# harness.yaml — model hoá bằng pydantic, validate lúc load; lỗi → từ chối khởi động (fail-closed)
class ProviderCfg(BaseModel):
    name: str                      # "anthropic" | "openai_compat"
    model: str
    api_key_secret: str            # TÊN secret, không phải giá trị
    base_url: str | None = None
    max_retries: int = 4

class BudgetCfg(BaseModel):
    max_loop_iterations: int = 20
    context_token_budget: int = 150_000        # [Inference] theo model
    monthly_cost_alert_usd: float | None = None

class ProjectCfg(BaseModel):
    path: Path                      # /mnt/d/... trên WSL2
    # read-write mount vào sandbox khi làm việc trên project này

class EgressCfg(BaseModel):
    allowlist: list[str] = []       # domain được phép (web_fetch, provider host tự thêm)

class SandboxCfg(BaseModel):
    backend: Literal["docker", "local"] = "docker"
    network: Literal["none", "proxy"] = "none"
    timeout_sec: int = 120
    mem_limit: str = "2g"
    cpus: float = 2.0

class SecurityCfg(BaseModel):
    allowlist: list[ToolRule] = []   # rule cho phép per-tool
    approval_mode: Literal["manual", "smart"] = "manual"
    approval_timeout_sec: int = 300  # scheduled run: hết hạn → fail sạch

class HarnessCfg(BaseModel):
    provider: ProviderCfg
    fallback_provider: ProviderCfg | None = None
    budget: BudgetCfg = BudgetCfg()
    projects: dict[str, ProjectCfg] = {}
    egress: EgressCfg = EgressCfg()
    sandbox: SandboxCfg = SandboxCfg()
    security: SecurityCfg = SecurityCfg()
    secret_backend: Literal["keyring", "age"] = "keyring"
    timezone: str = "Asia/Ho_Chi_Minh"   # tường minh, không theo máy
    workspace_root: Path
```

`pricing.yaml`:
```yaml
anthropic:
  claude-fable-5: { input_per_mtok: 3.0, output_per_mtok: 15.0, cache_read_per_mtok: 0.3 }  # [Inference — số thật lấy từ bảng giá]
openai_compat:
  "*": { input_per_mtok: 0.0, output_per_mtok: 0.0 }   # tự điền theo endpoint
```

---

## 3. Interface cốt lõi (contract — chữ ký ổn định)

### 3.1 Provider (provider/base.py)

```python
@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: dict

@dataclass(frozen=True)
class ChatResult:
    text: str | None
    tool_calls: list[ToolCall]
    usage: Usage
    stop_reason: Literal["end_turn", "tool_use", "max_tokens", "refusal"]
    raw_model: str

class Provider(Protocol):
    def name(self) -> str: ...
    def default_model(self) -> str: ...
    async def chat(self, messages: list[Message], tools: list[ToolSchema],
                   *, stream: bool = False) -> ChatResult: ...

# Failover reasons (failover/failover.py) — 9 canonical
class FailReason(Enum):
    RATE_LIMIT, TIMEOUT, SERVER_5XX, AUTH, BAD_REQUEST, \
    CONTENT_FILTER, OVERLOADED, NETWORK, CONTEXT_OVERFLOW = range(9)
# CONTEXT_OVERFLOW → KHÔNG failover; signal cho loop chạy compaction rồi retry.
```

### 3.2 Secret store (secrets/base.py)

```python
class SecretStore(Protocol):
    def get(self, name: str) -> str: ...   # raise SecretNotFound nếu thiếu (fail-closed)
# Bất biến: giá trị secret KHÔNG đi vào Message, span, log, checkpoint.
# Consumer (provider client, db driver, vpn) nhận giá trị tại điểm dùng cuối, không lưu lại.
```

### 3.3 Policy Gate (security/gate.py)

```python
@dataclass(frozen=True)
class Decision:
    verdict: Literal["allow", "deny", "need_approval"]
    reason: str                 # agent đọc được khi deny
    rule_id: str | None = None

class PolicyGate:
    def evaluate(self, tool: str, args: dict, ctx: SessionCtx) -> Decision:
        # thứ tự: hardline deny-list → allowlist → approval tier → DEFAULT DENY
        # exception BẤT KỲ trong hàm này → caller nhận deny (fail-closed), KHÔNG raise lên loop
        ...
```
Wrapper bắt buộc (thể hiện fail-closed):
```python
def safe_evaluate(gate, tool, args, ctx) -> Decision:
    try:
        return gate.evaluate(tool, args, ctx)
    except Exception as e:
        log.error("gate_failure", tool=tool, err=str(e))
        return Decision("deny", "policy gate error — denied by fail-closed", "GATE_ERROR")
```

### 3.4 Tool (tools/base.py) + Registry

```python
class Tool(Protocol):
    name: str
    schema: dict                       # JSON schema cho args
    def validate(self, args: dict) -> None: ...   # raise UserFacingError nếu sai
    async def run(self, args: dict, ctx: ToolCtx) -> ToolResult: ...

class Registry:
    def register(self, tool: Tool) -> None: ...
    def schemas(self) -> list[ToolSchema]: ...     # cấp cho Provider
    def get(self, name: str) -> Tool: ...
    # subagent: schemas() lọc theo toolset con (P3.6) — v0.1 trả tất cả
```

### 3.5 Sandbox (dùng vendored qua adapter)

```python
class Sandbox(Protocol):
    async def run(self, cmd: list[str], *, cwd: str, timeout: int,
                  env: dict) -> ExecResult: ...   # stdout, stderr, exit_code, timed_out
    async def cleanup(self) -> None: ...
# Adapter bọc vendored DockerEnvironment; áp SandboxCfg (network=none, limits).
```

### 3.6 Tracer (obs/tracer.py)

```python
class SpanKind(Enum): AGENT, LLM_CALL, TOOL_CALL, EMBEDDING, EVENT = range(5)

@dataclass
class Span:
    id: str; parent_id: str | None; trace_id: str
    kind: SpanKind; name: str
    start: float; end: float | None
    attrs: dict                      # usage, cost, verdict, tool, ...
    # KHÔNG chứa secret (đi qua redact trước khi ghi)

class Tracer:
    def start_span(self, kind, name, **attrs) -> Span: ...
    def end_span(self, span, **attrs) -> None: ...
    # correlation: trace_id theo turn; parent_id lồng cây (subagent P3 dùng lại)
```

---

## 4. Agent loop — pseudocode (core/loop.py, spec P0.1.4)

```python
async def run_turn(msg: InboundMessage, sess: Session) -> str:
    trace = tracer.start_span(AGENT, "turn", session=sess.key)
    cancel = CancelToken()
    try:
        ctx = await context_stage(sess, msg)          # deterministic, token budget
        for i in range(cfg.budget.max_loop_iterations):
            cancel.check()                            # ranh giới stage: hủy sạch
            # THINK
            with span(LLM_CALL):
                res = await provider_call(ctx.messages, registry.schemas())
            record_cost(res.usage)                    # cost ledger
            if res.stop_reason == "end_turn":
                break
            # PRUNE (nếu vượt budget)
            if ctx.tokens > cfg.budget.context_token_budget:
                ctx = prune(ctx)                      # thay tool result cũ bằng tham chiếu
            # TOOL (mỗi tool_call)
            for tc in res.tool_calls:
                cancel.check()
                result = await execute_tool(tc, sess) # Gate→Hook→Registry→Sandbox→Filters
                ctx.add_tool_result(tc.id, result)    # OBSERVE
            checkpoint.save(sess, ctx, i)             # CHECKPOINT: resume được
        else:
            emit_event("max_iterations_reached")
        await finalize_stage(sess, ctx)               # memory→staging, summary→FTS(P2)
        return ctx.final_text()
    except Cancelled:
        checkpoint.mark_canceled(sess)                # resume KHÔNG lặp side-effect
        return "Đã hủy."
    finally:
        tracer.end_span(trace)

async def execute_tool(tc: ToolCall, sess) -> ToolResult:      # wiring bất biến
    dec = safe_evaluate(gate, tc.name, tc.args, sess.ctx)
    emit_event("gate", tool=tc.name, verdict=dec.verdict, rule=dec.rule_id)  # audit
    if dec.verdict == "deny":
        return ToolResult.error(dec.reason)           # agent đọc được, tự sửa
    if dec.verdict == "need_approval":
        if not await approval.request(tc, sess):      # timeout → False (scheduled)
            return ToolResult.error("approval denied/timeout")
    tool = registry.get(tc.name)
    try: tool.validate(tc.args)
    except UserFacingError as e: return ToolResult.error(str(e))
    with span(TOOL_CALL, tool=tc.name):
        raw = await tool.run(tc.args, sess.tool_ctx)  # tool exec → sandbox nếu cần
    return filters.apply(raw)                         # redact + injection scan
```

---

## 5. Schema SQLite

### 5.1 traces.db (obs/spanstore.py, P1.6)
```sql
CREATE TABLE spans (
  id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, parent_id TEXT,
  kind TEXT NOT NULL, name TEXT NOT NULL,
  start_ts REAL NOT NULL, end_ts REAL,
  session_key TEXT, attrs_json TEXT NOT NULL   -- usage, cost, verdict... (đã redact)
);
CREATE INDEX idx_spans_trace ON spans(trace_id);
CREATE INDEX idx_spans_session ON spans(session_key, start_ts);
CREATE INDEX idx_spans_kind ON spans(kind, start_ts);   -- cho usage query theo provider/ngày
CREATE TABLE schema_version (v INTEGER);
```
`harness usage --by provider --month YYYY-MM` = query span kind=LLM_CALL (+EMBEDDING/tool image/search), group theo `attrs_json->>'provider'`, sum `attrs_json->>'cost_usd'`. Token/cost chỉ lấy từ span có usage → không đếm đôi.

### 5.2 scheduler.db (P1: chỉ checkpoint; cron ở P2)
```sql
CREATE TABLE checkpoints (
  session_key TEXT NOT NULL, turn_id TEXT NOT NULL,
  iteration INTEGER, state_json TEXT NOT NULL,   -- ctx đủ để resume
  status TEXT NOT NULL,                           -- running | done | canceled
  updated_ts REAL, PRIMARY KEY (session_key, turn_id)
);
```

### 5.3 sessions.db (P2 — schema vendored, để sẵn migration v0.1)

---

## 6. Test map — mỗi tiêu chí gate ↔ test cụ thể

Đánh số `T-<gate>-<n>`; đây là DoD kiểm chứng được, viết cùng lúc với code.

| Test | Nội dung | Gate |
|---|---|---|
| T-RG1-1a | Integration: một turn (LLM + exec + read_file + web_fetch) dưới network monitor → chỉ có kết nối tới provider host + egress allowlist | RG1-1, AG-2 |
| T-RG1-1b | web_fetch tới domain ngoài allowlist → Gate deny, không có gói tin ra | RG1-1 |
| T-RG1-2a | `gate.evaluate` raise → `safe_evaluate` trả deny (unit) | RG1-2, AG-1 |
| T-RG1-2b | Config policy hỏng → process exit ≠0 lúc khởi động, không chạy | RG1-2 |
| T-RG1-2c | Demo script: monkeypatch gate raise → tool call bị từ chối, loop không crash | RG1-2 |
| T-RG1-3 | Lệnh trong hardline deny-list → span `gate` verdict=deny, KHÔNG có span tool_call/exec | RG1-3 |
| T-RG1-4 | `kill -9` giữa vòng lặp (sau tool A, trước tool B) → resume: tool A không chạy lại, tiếp tục từ B | RG1-4 |
| T-RG1-5a | Sau demo, `traces get <id>` dựng lại đủ cây span của turn | RG1-5 |
| T-RG1-5b | cost_usd trong span = usage × pricing.yaml, khớp con số provider trả (±0) | RG1-5 |
| T-RG1-6 | Chạy `context_stage` 2 lần cùng input → byte-identical (snapshot) | RG1-6 |
| T-RG1-7 | Đổi `provider.name` anthropic↔openai_compat trong config → demo pass cả 2 | RG1-7 |
| T-RG1-8 | import-linter: không path nào ghi thẳng MEMORY.md ngoài review_gate | RG1-8 |
| T-RG1-9 | Scan artifact demo (context dump + spans + logs + checkpoint) → 0 lần xuất hiện giá trị secret test | RG1-9 |
| T-RG1-10a | Cancel khi đang chờ approval → dừng sạch, không chạy tool | RG1-10 |
| T-RG1-10b | Cancel khi container đang chạy → container bị kill, checkpoint=canceled | RG1-10 |
| T-arch-1 | import-linter: `hx.core` không import `hx.sandbox.*` | wiring |
| T-arch-2 | path-traversal: read/write ngoài workspace+project root → deny | RG1 (P1.3.6) |

---

## 7. Thứ tự implement Phase 1 (chi tiết hoá dependency graph)

Tuần theo execution plan §4, nhưng ở mức file:

1. **Nền (song song):** `errors.py`, `config/*`, `obs/tracer.py`+`spanstore.py` (WP1.6 trước), `secrets/*` (P1.4.5).
2. **Security lõi:** `security/gate.py`+`denylist.py`+`allowlist.py`+`filters.py`, `safe_evaluate` (WP1.4). Viết T-RG1-2* ngay.
3. **Provider:** `provider/base.py`→`anthropic.py`→`openai_compat.py`→`failover.py` (WP1.1). Contract test chạy trên cả 2.
4. **Sandbox+Tools:** adapter `sandbox/*` (vendored), `tools/registry.py`+`builtin/*`+`projects.py`, `tools/wiring.py` (WP1.3). T-arch-1/2.
5. **Core loop:** `context.py`→`loop.py`→`checkpoint.py`→`cancel.py` (WP1.2). T-RG1-4/6/10.
6. **Memory:** `memory/workspace.py`+`review_gate.py` (WP1.5).
7. **Tích hợp:** `core/session.py`+`cli.py` (WP1.7). T-RG1-1/5/7 + kịch bản demo.

Mỗi bước: code + test đánh số + demo 15' (DoD = demo được, không phải merge được).

---

## 8. Việc tôi làm được ngay trong repo này (không cần chờ)

- **P0.3.1** skeleton: tạo `pyproject.toml`, cây `src/hx/` với các module + Protocol rỗng (chữ ký ở §3) + docstring, `.import-linter`, CI stub.
- **P0.2.1/P0.2.2** vendor intake: cấu trúc `vendor/` + `MANIFEST.md` template (điền SHA khi có mạng tới GitHub) + LICENSE.
- Test skeleton: `tests/` với conftest + 1 test arch chạy được (import-linter).

Cần bạn trước khi đặt tên thật: **P0.5.2 chốt tên sản phẩm** (giờ dùng `hx`). Còn lại tôi bootstrap được ngay khi bạn ra lệnh.

---

*Spec này là tầng chi tiết nhất cho P0–P1. Khi bắt đầu Phase 2, viết `harness-detailed-spec-p2.md` tương tự (remote ops, DB classifier, skills, hooks, RPC) — không viết trước để tránh spec trôi.*
