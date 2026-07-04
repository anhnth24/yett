# Spec kỹ thuật Phase 0 + Phase 1 — Implementation-ready

> **Trạng thái:** Draft để review · 04/07/2026
> **Vai trò:** hạ `harness-execution-plan.md` xuống mức code được ngay: layout package, interface Python, schema DB, config mẫu, test đánh số. Phạm vi: Phase 0 + Phase 1 (v0.1). Phase 2/3 có spec riêng khi khởi động.
> **Quy ước:** tên package tạm là `harness` (đổi khi P0.5.2 chốt tên — chỉ cần sed). Signature dưới đây là **hợp đồng thiết kế** — đổi phải cập nhật spec này trước, code sau. Chỗ đánh **[Inference]** là lựa chọn đề xuất, chốt khi review spec này.

---

## 1. Layout package

```
harness/
├── pyproject.toml              # pin exact; extras: [dev], [anthropic], [openai]
├── .github/workflows/ci.yml    # lint → typecheck → unit → integration → AG guards
├── .importlinter               # contract: core không import sandbox/provider trực tiếp
├── vendor/
│   ├── MANIFEST.md             # file gốc, commit SHA, ngày intake, license
│   ├── hermes_environments/    # base.py local.py docker.py ssh.py file_sync.py + LICENSE
│   └── hermes_state/           # schema.sql + fts5_compat.py + LICENSE
├── src/harness/
│   ├── config/                 # load + validate (pydantic) harness.yaml, pricing.yaml
│   ├── secrets/                # SecretStore (P1.4.5)
│   ├── tracing/                # Tracer, span model, store, cost ledger (WP1.6)
│   ├── security/               # PolicyGate, rules, ResultFilters (WP1.4)
│   ├── providers/              # Protocol + anthropic/ + openai_compat/ + failover (WP1.1)
│   ├── tools/                  # registry, builtin: exec/read_file/write_file/web_fetch (WP1.3)
│   ├── sandbox/                # adapter bọc vendor.hermes_environments (WP1.3)
│   ├── memory/                 # workspace loader + review gate (WP1.5)
│   ├── core/                   # stages, loop, checkpoint, cancel (WP1.2)
│   ├── session/                # SessionManager, queue (WP1.7)
│   └── cli/                    # chat TUI, traces, usage, approve, memory, config
└── tests/
    ├── unit/  integration/  arch/   # arch = import-linter + wiring tests
    └── golden/                      # (P2.9 — để sẵn thư mục)
```

**Luật import (`.importlinter`, chạy trong CI = một phần AG-1):**
- `core` KHÔNG import `sandbox`, `providers.anthropic`, `providers.openai_compat` — chỉ import interface (`tools.registry`, `providers.base`).
- `tools.builtin.*` KHÔNG import `sandbox` trực tiếp — đi qua `tools.registry` → `security.gate` → `sandbox`.
- Không module nào ngoài `memory.review_gate` được mở `workspace/MEMORY.md` ở mode ghi (kèm test grep AST).
- Không module nào ngoài `secrets` được đọc `secrets/`.

---

## 2. Config mẫu đầy đủ (`config/harness.yaml`)

```yaml
model:
  default: anthropic/claude-sonnet-5        # provider/model
  fallback: openai_compat/gpt-5             # dùng khi failover (trừ context-overflow)

providers:
  anthropic:
    api_key: secret:anthropic_key           # LUÔN là tham chiếu tên secret
    prompt_cache: true
  openai_compat:
    base_url: https://api.example.com/v1
    api_key: secret:openai_key

budget:
  context_tokens: 100000                    # trần context mỗi turn
  bootstrap_files_tokens: 12000             # trần cho workspace files
  max_iterations: 20                        # trần vòng lặp
  monthly_cost_usd: 100                     # ngưỡng cảnh báo (80%/100%)

workspace:
  root: ~/.harness/workspace

projects:                                    # P1.3.6 — root được phép ngoài workspace
  goweb:  /mnt/d/projects/goweb
  report: /mnt/e/work/report-tool

sandbox:
  backend: docker                            # docker | local (local bị chặn nếu profile=prod)
  profile: dev                               # dev | prod
  image: harness-exec:latest
  network: none
  limits: {cpu: "2", memory: 2g, timeout_s: 300, stdout_kb: 512}

security:
  approval: manual                           # manual | smart  (không có yolo)
  approval_timeout_s: 900                    # cho run không giám sát (P2.4.3)
  egress_whitelist:                          # cho web_fetch
    - https://docs.python.org
  # hardline deny-list KHÔNG nằm ở đây — hard-code trong security/rules.py (immutable)

tracing:
  flush_interval_s: 5
  buffer_spans: 500
  otlp: {enabled: false}

secrets:
  backend: keyring                           # keyring | agefile  [Inference]
```

`config/pricing.yaml`: `{provider: {model: {in_per_mtok, out_per_mtok, cache_read_per_mtok, cache_write_per_mtok}}}` — thiếu giá của model đang dùng → **process từ chối khởi động** (fail-closed cả với tiền).

Config hỏng/thiếu trường bắt buộc → in lỗi rõ ràng, exit khác 0. Không có default ngầm cho các trường security.

---

## 3. Interface hợp đồng (Python)

### 3.1 Provider (`providers/base.py`) — từ spec clean-room P0.1.2

```python
class StopReason(Enum): END_TURN; TOOL_USE; MAX_TOKENS; ERROR

@dataclass(frozen=True)
class Usage:
    input_tokens: int; output_tokens: int
    cache_read_tokens: int = 0; cache_write_tokens: int = 0

@dataclass(frozen=True)
class ToolCall:
    id: str; name: str; arguments: dict          # arguments đã parse JSON

@dataclass(frozen=True)
class ChatResponse:
    text: str | None
    tool_calls: tuple[ToolCall, ...]
    stop_reason: StopReason
    usage: Usage
    raw_model: str                                # model thực tế provider trả về

class Provider(Protocol):
    def name(self) -> str: ...
    def default_model(self) -> str: ...
    def chat(self, req: ChatRequest) -> ChatResponse: ...
    def chat_stream(self, req: ChatRequest) -> Iterator[StreamEvent]: ...
    # ChatRequest: model, messages, tools(schema), max_tokens, system

class FailoverReason(Enum):        # 9 lý do chuẩn — mapping lỗi provider về đây
    AUTH; RATE_LIMIT; OVERLOADED; TIMEOUT; NETWORK
    CONTEXT_OVERFLOW               # → KHÔNG failover: raise NeedsCompaction
    BAD_REQUEST; CONTENT_FILTER; UNKNOWN
```

Contract test (`tests/unit/providers/test_contract.py`) chạy trên MỌI adapter qua fixture parametrize: response mapping, usage đúng, tool_call parse, stream ghép lại == non-stream, từng FailoverReason giả lập được.

### 3.2 Policy Gate (`security/gate.py`) — giữ nguyên interface đến v0.3

```python
@dataclass(frozen=True)
class Verdict:
    decision: Literal["allow", "deny", "need_approval"]
    reason: str                    # agent đọc được: "bị chặn bởi rule X, thử Y"
    rule_id: str                   # cho audit

class PolicyGate:
    def evaluate(self, call: ToolCallCtx) -> Verdict: ...
    # ToolCallCtx: tool_name, arguments, session_key, source ("agent"|"subagent"|"rpc")

def gated(gate: PolicyGate, call: ToolCallCtx) -> Verdict:
    """Wrapper fail-closed DUY NHẤT được phép gọi evaluate.
    Mọi exception/timeout(2s) từ evaluate → Verdict("deny", "policy gate error — fail closed", "FAILCLOSED")."""
```

Thứ tự rule trong `evaluate`: `hardline` (hard-code trong `rules.py`, không đọc config) → `allowlist` (config) → `approval` → **default deny**. Rule = hàm thuần `(ToolCallCtx) -> Verdict | None`, có unit test riêng từng rule.

### 3.3 Tool (`tools/registry.py`)

```python
@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict                       # JSON Schema, validate trước handler
    handler: Callable[[dict, ToolContext], ToolResult]

@dataclass(frozen=True)
class ToolResult:
    ok: bool
    content: str                             # đã đi qua ResultFilters trước khi trả
    error_hint: str | None = None            # agent-sửa-được: "path ngoài root; các root hợp lệ: ..."

class Registry:
    def execute(self, call: ToolCall, ctx: ToolContext) -> ToolResult:
        # 1. gated() → deny/approval xử lý tại đây
        # 2. jsonschema.validate → lỗi thành ToolResult(ok=False, error_hint=...)
        # 3. handler chạy (exec → sandbox)
        # 4. ResultFilters.apply(result)
        # 5. span tool_call bọc toàn bộ 1-4
```

Builtin v0.1: `exec` (sandbox), `read_file`/`write_file` (chỉ trong workspace + project roots — resolve symlink trước khi check), `web_fetch` (Gate check URL vs egress_whitelist TRƯỚC khi mở kết nối).

### 3.4 Sandbox adapter (`sandbox/adapter.py`)

Bọc `vendor.hermes_environments`: map config → constructor args của `DockerEnvironment`; thay 4 lazy-import config-plumbing của Hermes bằng `harness.config`. Trả `ExecResult(exit_code, stdout, stderr, duration_ms, truncated: bool)`. `profile: prod` + `backend: local` → refuse khởi động.

### 3.5 Tracing (`tracing/`) — từ spec clean-room P0.1.3

```python
class SpanKind(Enum): AGENT; LLM_CALL; TOOL_CALL; EMBEDDING; EVENT

with tracer.span(SpanKind.TOOL_CALL, name="exec", parent=turn_span) as sp:
    sp.set(attrs={...}); ...
# span tự ghi duration, status, exception; đẩy vào buffer; flush 5s/500 spans
```

DDL `state/traces.db`:

```sql
CREATE TABLE spans (
  span_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, parent_id TEXT,
  kind TEXT NOT NULL, name TEXT NOT NULL,
  start_ns INTEGER NOT NULL, end_ns INTEGER, status TEXT,          -- ok|error|denied|canceled
  session_key TEXT, attrs_json TEXT,
  -- cost ledger (NULL nếu span không có phí):
  provider TEXT, model TEXT,
  units_json TEXT,                -- {"in":1200,"out":300,"cache_read":900} | {"queries":1} | {"images":2}
  cost_usd REAL
);
CREATE INDEX idx_spans_trace ON spans(trace_id);
CREATE INDEX idx_spans_time  ON spans(start_ns);
CREATE INDEX idx_spans_cost  ON spans(provider, start_ns) WHERE cost_usd IS NOT NULL;
```

Quy tắc: token/cost CHỈ ghi trên `LLM_CALL` (và provider phi-LLM sau này) — aggregation không bao giờ cộng span `AGENT` (tránh đếm đôi). `harness usage` = một câu SQL GROUP BY trên bảng này.

### 3.6 Secret store (`secrets/`)

```python
class SecretStore(Protocol):
    def get(self, name: str) -> Secret        # Secret.__repr__ = "Secret(<name>)" — không bao giờ lộ giá trị
    def set(self, name: str, value: str) -> None
```

`Secret` là wrapper chặn serialize (raise nếu vào json.dumps/str-format trong f-string log **[Inference — cơ chế: __format__ raise]**); giá trị chỉ lấy qua `.reveal()` tại điểm dùng cuối (provider client, env của VPN process). Test RG1-9 = grep giá trị secret test trong: context gửi provider (mock), spans db, log file, checkpoint db.

### 3.7 Core loop (`core/`) — từ spec clean-room P0.1.4

```python
def run