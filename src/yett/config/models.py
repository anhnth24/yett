"""Config models (spec P0-P1 §2). Pydantic; validate lúc load, lỗi → từ chối khởi động."""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, model_validator


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


class ZaloCfg(BaseModel):
    """Kênh Zalo Official Bot API (spec P3 §4) — không phải Zalo Personal.

    Token là secret (không plaintext). Fail-closed: enabled + mode=webhook đòi hỏi
    HTTPS webhook_url + secret 8–256 ký tự (validate lúc load config).
    """

    enabled: bool = False
    token_secret: str = "zalo_bot_token"  # TÊN secret, giá trị ở secret store/env
    allowed_chat_ids: list[str] = Field(default_factory=list)
    pairing_code: str = ""  # legacy inline value; prefer pairing_code_secret
    pairing_code_secret: str = ""
    briefing_hour: int | None = Field(default=None, ge=0, le=23)
    poll_timeout_sec: int = Field(default=30, ge=1, le=120)
    mode: Literal["poll", "webhook"] = "poll"
    webhook_url: str = ""
    webhook_secret: str = ""  # legacy inline value; prefer webhook_secret_secret
    webhook_secret_secret: str = ""
    webhook_path: str = "/api/channels/zalo/webhook"
    http_timeout_sec: float = Field(default=60.0, gt=0, le=300)
    max_retries: int = Field(default=2, ge=0, le=8)

    @model_validator(mode="after")
    def _fail_closed_webhook(self) -> "ZaloCfg":
        if not self.enabled:
            return self
        if not self.token_secret.strip():
            raise ValueError("channels.zalo: token_secret không được rỗng khi enabled")
        normalized_ids: list[str] = []
        for chat_id in self.allowed_chat_ids:
            normalized = chat_id.strip()
            if (
                not normalized
                or len(normalized) > 256
                or any(ord(char) < 0x20 or ord(char) == 0x7F for char in normalized)
            ):
                raise ValueError("channels.zalo: allowed_chat_ids chứa ID không hợp lệ")
            normalized_ids.append(normalized)
        self.allowed_chat_ids = list(dict.fromkeys(normalized_ids))
        if self.pairing_code:
            if self.pairing_code != self.pairing_code.strip():
                raise ValueError("channels.zalo: pairing_code không được có whitespace ở hai đầu")
            if (
                not 8 <= len(self.pairing_code) <= 256
                or any(ord(char) < 0x20 or ord(char) == 0x7F for char in self.pairing_code)
            ):
                raise ValueError("channels.zalo: pairing_code phải dài 8–256 ký tự")
        if self.pairing_code_secret and not self.pairing_code_secret.strip():
            raise ValueError("channels.zalo: pairing_code_secret không hợp lệ")
        if self.mode == "webhook":
            parsed = urlparse(self.webhook_url)
            if parsed.scheme != "https" or not parsed.hostname:
                raise ValueError(
                    "channels.zalo: mode=webhook đòi hỏi webhook_url HTTPS có hostname"
                )
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError(
                    "channels.zalo: webhook_url không được chứa credential/query/fragment"
                )
            if (
                not self.webhook_path.startswith("/")
                or self.webhook_path == "/"
                or "?" in self.webhook_path
                or "#" in self.webhook_path
            ):
                raise ValueError("channels.zalo: webhook_path phải là absolute path riêng")
            n = len(self.webhook_secret or "")
            if self.webhook_secret and (
                n < 8
                or n > 256
                or self.webhook_secret != self.webhook_secret.strip()
                or any(
                    ord(char) < 0x20 or ord(char) == 0x7F
                    for char in self.webhook_secret
                )
            ):
                raise ValueError(
                    "channels.zalo: mode=webhook đòi hỏi webhook_secret 8–256 ký tự (fail-closed)"
                )
            if not self.webhook_secret and not self.webhook_secret_secret.strip():
                raise ValueError(
                    "channels.zalo: mode=webhook cần webhook_secret_secret hoặc "
                    "webhook_secret inline"
                )
        return self


class ChannelsCfg(BaseModel):
    telegram: TelegramCfg = Field(default_factory=TelegramCfg)
    zalo: ZaloCfg = Field(default_factory=ZaloCfg)


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
