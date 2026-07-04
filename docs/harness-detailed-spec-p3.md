# Detailed Spec — Phase 3 (implementation-ready)

> **Trạng thái:** Draft để review · 04/07/2026
> **Phạm vi:** hạ Phase 3 xuống mức code — policy engine (policy-as-config), immutable core, compliance layer, channels (Telegram → Zalo Bot), subagent delegation 1 cấp, image_gen, analytics, packaging deploy-per-tenant. Tiếp nối spec P0-P1 và P2.
> **Quy ước:** package `yett` (placeholder), **[Inference]** = chốt khi implement.

---

## 1. Bổ sung layout
```
src/yett/
├── policy/
│   ├── engine.py            # WP3.1 đọc policy YAML → Decision (thay deny-list hard-coded)
│   ├── schema.py            # WP3.1 pydantic cho rule
│   ├── immutable.py         # WP3.1 immutable core: identity + hardline read-only
│   └── loader.py            # WP3.1 load + validate; hỏng → từ chối khởi động
├── compliance/
│   ├── audit.py             # WP3.2 append-only audit log + hash chain
│   ├── classify.py          # WP3.2 phân loại dữ liệu
│   └── retention.py         # WP3.2 retention job
├── channels/
│   ├── base.py              # WP3.3 Protocol ChannelAdapter
│   ├── telegram.py          # WP3.3 Telegram Bot API
│   ├── zalo_bot.py          # WP3.3 Zalo Bot API
│   └── gating.py            # WP3.3 pairing + allowlist chat id
├── subagent/
│   ├── delegate.py          # WP3.6 tool delegate (1 cấp)
│   ├── definition.py        # WP3.6 load workspace/agents/*.md
│   └── bundled/             # researcher, writer, illustrator
├── tools/assist/
│   └── image_gen.py         # WP3.6 tool sinh ảnh
└── analytics/
    └── insights.py          # WP3.4 /usage /insights từ span store
```

---

## 2. Policy Engine (WP3.1)

### 2.1 Rule schema (policy/schema.py)
```python
class Match(BaseModel):
    tool: str | None = None            # "ssh_exec", "db_query", "*"
    args: dict[str, str] = {}          # điều kiện regex trên args
    session: str | None = None
    data_class: str | None = None      # "pii","secret","internal"
    time_window: str | None = None     # "workhours"

class Rule(BaseModel):
    id: str
    match: Match
    effect: Literal["allow","deny","approve","redact"]
    priority: int = 0                  # cao thắng; hardline luôn > mọi rule config

class PolicyFile(BaseModel):
    rules: list[Rule]
```

### 2.2 Engine thay Gate hard-coded (policy/engine.py)
```python
class PolicyEngine:
    def evaluate(self, tool, args, ctx) -> Decision:
        # 1) immutable hardline (SSH_DELETE, SQL_WRITE, secret access...) — KHÔNG rule config nào đảo được
        if hit := immutable.check(tool, args): return hit   # deny
        # 2) rule config theo priority
        for r in sorted(self.rules, key=lambda r: -r.priority):
            if r.match.matches(tool, args, ctx):
                return Decision(r.effect_verdict(), r.id, r.id)
        # 3) DEFAULT DENY
        return Decision("deny", "no rule matched — default deny", "DEFAULT_DENY")
```
**Quan trọng:** interface `evaluate(tool, args, ctx) -> Decision` **giống hệt P1.4.1** → Gate call-site không đổi; chỉ hoán backend. Toàn bộ test WP1.4 phải pass nguyên trạng (T-RG3-2).

### 2.3 Immutable core (policy/immutable.py)
```python
# identity (tên, purpose), hardline deny-list, policy file:
#   - mount READ-ONLY trong runtime (docker :ro / chmod)
#   - agent & hook KHÔNG có tool nào ghi được vào các path này
#   - subagent kế thừa, không nới
# check(): trả deny cho mọi hành vi chạm write vào vùng immutable
```
**Test (T-RG3-3):** red-team — agent/hook/subagent cố sửa policy/identity qua mọi tool (write_file, ssh, exec, delegate) → deny + audit; hardline vẫn chặn dù rule config cố allow.

---

## 3. Compliance (WP3.2)

### 3.1 Audit append-only + hash chain (compliance/audit.py)
```python
# audit/audit-YYYY-MM.jsonl ; mỗi dòng:
class AuditEntry(BaseModel):
    ts: float; actor: str            # "user"|"agent"|"subagent:<name>"
    kind: str                        # "gate","approval","memory_write","skill_activate","db_query"
    detail: dict                     # đã redact secret
    prev_hash: str; hash: str        # hash(prev_hash + entry) → chống sửa
# Không API xóa/sửa; verify_chain() phát hiện dòng bị thay đổi.
```
### 3.2 Phân loại + retention
```python
# classify.py: gắn data_class cho nội dung (pattern PII/secret/internal) → policy dùng
# retention.py: job xóa dữ liệu quá hạn theo config; mọi lần xóa ghi audit
class RetentionCfg(BaseModel):
    traces_days: int = 90
    audit_days: int = 3650           # gov thường giữ lâu [Inference]
    sessions_days: int = 365
```
**Test (T-RG3-4):** đối chiếu 1 ngày dogfood — mọi tool call/approval/memory write có dòng audit; `verify_chain()` xanh; sửa 1 dòng → verify fail. Retention xóa đúng lịch + có audit.

---

## 4. Channels (WP3.3)
```python
class ChannelAdapter(Protocol):
    async def receive(self) -> AsyncIterator[InboundMessage]: ...
    async def send(self, session_key: str, msg: OutboundMessage) -> None: ...

# telegram.py: long-poll/webhook Bot API; token từ secret store
# gating.py: pairing code + allowlist chat_id → chỉ người được ghép mới sai khiến
# Đẩy approval request qua channel (P2.4.3 [P3]): tin nhắn hiện nguyên văn lệnh+host, nút duyệt
```
Channel chỉ là entry adapter TRƯỚC Session Manager — cùng core, cùng Policy Gate, cùng hardline dù lệnh từ CLI hay Telegram.
**Test:** chat_id ngoài allowlist → bỏ qua; approval qua Telegram → verdict áp đúng vào turn; hardline (xóa file/DB write) từ Telegram vẫn deny.

---

## 5. Subagent delegation 1 cấp (WP3.6)

### 5.1 Definition (subagent/definition.py)
```python
class SubagentDef(BaseModel):
    name: str; description: str
    system_prompt: str
    toolset: list[str]              # PHẢI ⊆ toolset cha; vượt → từ chối load
    max_iterations: int = 10
    token_budget: int = 50_000
# file: workspace/agents/<name>.md (frontmatter + body = system_prompt)
# subagent do agent tạo → review gate như skill
```

### 5.2 Tool delegate (subagent/delegate.py)
```python
class DelegateTool(Tool):
    name = "delegate"
    async def run(self, args, ctx):
        sub = load_def(args["agent"])
        assert set(sub.toolset) <= ctx.allowed_tools, "toolset vượt cha"
        if ctx.is_subagent: raise DenyError("delegate lồng nhau bị cấm (1 cấp)")  # chặn ở registry
        sub_ctx = ctx.child(toolset=sub.toolset, budget=sub.token_budget, is_subagent=True)
        span = tracer.start_span(AGENT, f"subagent:{sub.name}", parent=ctx.span)  # lồng cây
        result = await run_turn_with(sub, sub_ctx)     # CÙNG loop, CÙNG Gate/policy
        return ToolResult.ok(result)
```
**Bất biến:** subagent dùng **cùng PolicyEngine + cùng policy file** → không leo thang quyền; hardline (AG-6/AG-7) áp nguyên trong subagent; không delegate lồng nhau; span lồng dưới cha.
**Test (T-RG3-9):** red-team — subagent khai toolset vượt cha → từ chối load; subagent gọi delegate → deny; subagent thử hardline (xóa file/DB write) → deny; `traces get` thấy cả cây.

### 5.3 image_gen + bundled subagents
```python
# image_gen.py: provider API (key secret store) hoặc backend local không-egress [Inference: ComfyUI/SD]
#   ảnh lưu workspace; cost span vào ledger
# bundled: researcher (web_search+web_fetch+rw workspace), writer (rw workspace), illustrator (image_gen)
```

---

## 6. Analytics + Packaging (WP3.4)
```python
# insights.py: /usage (token/cost theo provider/model/ngày/session), /insights --days N
#   đọc THẲNG span store, không thu thập thêm; budget alert qua heartbeat
```
Packaging deploy-per-tenant:
```
deploy/
├── docker-compose.yml       # 1 stack = 1 tenant
├── config.template.yaml     # điền khi cài
├── backup.sh / restore.sh   # 3 db + workspace + secrets + config
└── RUNBOOK.md               # cài ≤1h, upgrade path
```
**Test (T-RG3-1):** người ngoài team cài sạch theo runbook ≤1h; T-RG3 upgrade v0.2→v0.3 giữ dữ liệu; backup→restore trên máy khác nguyên vẹn.

---

## 7. Thứ tự implement Phase 3
1. **Policy engine (WP3.1)** trước — mọi thứ khác dựa vào nó; giữ interface P1.4.1, chạy lại toàn bộ test WP1.4.
2. Immutable core (cùng WP3.1) + red-team T-RG3-3.
3. Compliance (WP3.2) — audit chain + retention.
4. Subagent (WP3.6) — cần policy engine vững trước (không leo thang quyền).
5. Channels (WP3.3) — Telegram trước, Zalo Bot sau; approval qua channel.
6. Analytics (WP3.4) + image_gen.
7. Packaging + hardening (WP3.4.2, WP3.5) — cuối, trước RG-3.

---

## 8. Bản đồ spec → gate (toàn Phase 3)
| Spec | Test | Gate |
|---|---|---|
| Policy engine giữ interface | test WP1.4 pass nguyên trạng | RG3-2 |
| Immutable core | red-team sửa policy/identity | RG3-3 |
| Audit chain | verify_chain + đối chiếu ngày dogfood | RG3-4 |
| Subagent không leo thang | red-team delegate/toolset/hardline | RG3-9 |
| Channels hardline giữ nguyên | lệnh nguy hiểm từ Telegram → deny | RG3-2 |
| Packaging | cài ≤1h bởi người ngoài team | RG3-1 |
| Compliance khách | checklist ký | RG3-7 |
| Legal | vendor manifest + không nhiễm GoClaw | RG3-8 |

---

*Ba spec P0-P1 / P2 / P3 phủ toàn bộ đường code đến RG-3. Phase 4 (pilot) là vận hành, không cần spec code — dùng RUNBOOK + checklist RG-4. Khi bắt đầu mỗi phase, đối chiếu spec với thực tế và cập nhật (spec là contract sống, không phải đá tảng).*
