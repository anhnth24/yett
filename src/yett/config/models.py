"""Config models (spec P0-P1 §2). Pydantic; validate lúc load, lỗi → từ chối khởi động."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class ProviderCfg(BaseModel):
    name: str  # "anthropic" | "openai_compat" | "fake"
    model: str
    # Cách 1 (khuyến nghị): api_key_secret = TÊN secret, key nằm ở secret store/env.
    api_key_secret: str = ""
    # Cách 2 (tiện, local): api_key = dán thẳng key vào đây. config/harness.yaml đã gitignored.
    api_key: str = ""
    base_url: str | None = None
    max_retries: int = Field(default=4, ge=0)


class BudgetCfg(BaseModel):
    max_loop_iterations: int = 20
    context_token_budget: int = 150_000
    monthly_cost_alert_usd: float | None = None


class RouterCfg(BaseModel):
    """Complexity router: phân loại độ khó câu hỏi → giới hạn số vòng lặp (tiết kiệm chi phí).

    Câu dễ không tiêu hết budget; câu khó mới được nhiều vòng. Heuristic (không tốn thêm
    LLM call), deterministic, chạy offline được."""

    enabled: bool = True
    # số vòng lặp tối đa theo mức độ (bị chặn trên bởi budget.max_loop_iterations)
    trivial_steps: int = 2
    simple_steps: int = 4
    moderate_steps: int = 8
    complex_steps: int = 20


class ProjectCfg(BaseModel):
    path: Path
    # Map tài nguyên cho project (tên tham chiếu remote.hosts / databases). Dùng để inject
    # bối cảnh khi chat theo project → LLM biết project này dùng host/db nào. Rỗng = không ràng.
    hosts: list[str] = Field(default_factory=list)
    databases: list[str] = Field(default_factory=list)


class EgressCfg(BaseModel):
    allowlist: list[str] = Field(default_factory=list)


class SandboxCfg(BaseModel):
    backend: Literal["docker", "local"] = "docker"
    network: Literal["none", "proxy"] = "none"
    timeout_sec: int = 120
    mem_limit: str = "2g"
    cpus: float = 2.0


class ToolRule(BaseModel):
    """Allowlist rule per-tool (v0.1). arg_patterns: regex phải khớp mọi arg nêu."""

    tool: str
    arg_patterns: dict[str, str] = Field(default_factory=dict)
    effect: Literal["allow", "need_approval"] = "allow"


class SecurityCfg(BaseModel):
    allowlist: list[ToolRule] = Field(default_factory=list)
    approval_mode: Literal["manual", "smart"] = "manual"
    approval_timeout_sec: int = 300


class DbProfileCfg(BaseModel):
    driver: Literal["postgres", "mysql", "sqlserver", "sqlite"]
    dsn_secret: str  # connection string là secret; model chỉ thấy tên profile
    readonly: bool = True


class SearchCfg(BaseModel):
    """Cấu hình web_search. API key chỉ là TÊN secret — giá trị nằm ở secret store."""

    api_key_secret: str
    provider: Literal["brave"] = "brave"
    base_url: str | None = None  # mặc định theo provider; override khi self-host/proxy


class ImageCfg(BaseModel):
    """Cấu hình image_gen. API key chỉ là TÊN secret — giá trị nằm ở secret store."""

    api_key_secret: str
    provider: Literal["openai_compat"] = "openai_compat"
    model: str = "dall-e-3"
    # Mặc định OpenAI Images API; override khi dùng endpoint OpenAI-compatible khác.
    base_url: str | None = None
    # b64_json = không tải URL ngoài; url = tải ảnh qua egress allowlist (fail-closed).
    response_format: Literal["b64_json", "url"] = "b64_json"


class HostCfg(BaseModel):
    """Server SSH khai báo trước (spec P2 §2.1). Host lạ → deny (không SSH đại)."""

    address: str
    auth: str = ""  # "keyfile:<secret_name>" — key qua secret store
    port: int = 22
    user: str = ""
    vpn_required: str | None = None
    tier: Literal["uat", "restricted"] = "uat"
    log_paths: list[str] = Field(default_factory=list)
    deploy_script: str | None = None


class RemoteCfg(BaseModel):
    hosts: dict[str, HostCfg] = Field(default_factory=dict)
    vpn_profiles: dict[str, dict] = Field(default_factory=dict)  # name -> {cred_secret: <tên>}


class TelegramCfg(BaseModel):
    """Kênh Telegram (spec P3 §4). Token là secret (không để plaintext); chỉ chat_id đã ghép
    mới sai khiến được agent. Long-polling → không cần mở port vào máy."""

    enabled: bool = False
    token_secret: str = "tg_bot_token"     # TÊN secret, giá trị ở secret store/env
    allowed_chat_ids: list[str] = Field(default_factory=list)  # ghép sẵn (bỏ pairing)
    pairing_code: str = ""                  # gửi mã này cho bot để tự ghép; rỗng = tắt pairing
    briefing_hour: int | None = None        # giờ (theo timezone) gửi briefing tự động; None = tắt
    poll_timeout_sec: int = 25              # long-poll getUpdates


class ChannelsCfg(BaseModel):
    telegram: TelegramCfg = Field(default_factory=TelegramCfg)


class HarnessCfg(BaseModel):
    provider: ProviderCfg
    fallback_provider: ProviderCfg | None = None
    budget: BudgetCfg = Field(default_factory=BudgetCfg)
    router: RouterCfg = Field(default_factory=RouterCfg)
    projects: dict[str, ProjectCfg] = Field(default_factory=dict)
    databases: dict[str, DbProfileCfg] = Field(default_factory=dict)
    remote: RemoteCfg = Field(default_factory=RemoteCfg)
    channels: ChannelsCfg = Field(default_factory=ChannelsCfg)
    search: SearchCfg | None = None
    image: ImageCfg | None = None
    egress: EgressCfg = Field(default_factory=EgressCfg)
    sandbox: SandboxCfg = Field(default_factory=SandboxCfg)
    security: SecurityCfg = Field(default_factory=SecurityCfg)
    skills_enabled: bool = True
    subagents_enabled: bool = True
    secret_backend: Literal["keyring", "age", "env", "file"] = "env"
    timezone: str = "Asia/Ho_Chi_Minh"
    workspace_root: Path


class PriceRow(BaseModel):
    input_per_mtok: float = 0.0
    output_per_mtok: float = 0.0
    cache_read_per_mtok: float = 0.0
    per_call: float = 0.0  # cho image_gen/web_search (tính theo lần gọi)
