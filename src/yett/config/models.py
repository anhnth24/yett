"""Config models (spec P0-P1 §2). Pydantic; validate lúc load, lỗi → từ chối khởi động."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_VPN_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_VPN_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,252}[A-Za-z0-9])?$")


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


class VpnProfileCfg(BaseModel):
    """Profile VPN khai báo trước (spec P2 §2.3). Model chỉ được chọn TÊN profile — không
    truyền flag/argv tùy ý. Credentials là TÊN secret; giá trị lấy tại điểm dùng cuối.

    kind=openfortivpn: host (+ port) + cred_secret (password); username có thể plaintext
    hoặc username_secret.
    kind=openvpn: config_file tuyệt đối tới .ovpn; nếu cần user/pass thì cred_secret
    (+ username / username_secret) → file auth tạm, KHÔNG đưa password lên argv.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["openfortivpn", "openvpn"] = "openfortivpn"
    cred_secret: str = ""  # TÊN secret password; rỗng = không auth-user-pass (vd cert-only)
    username: str = ""
    username_secret: str = ""
    host: str = ""  # openfortivpn
    port: int = Field(default=443, ge=1, le=65535)
    config_file: str = ""  # openvpn — absolute path
    connect_timeout_sec: int = Field(default=60, ge=1, le=600)

    @field_validator("host")
    @classmethod
    def _host_safe(cls, v: str) -> str:
        v = (v or "").strip()
        if v and _VPN_HOST_RE.fullmatch(v) is None:
            raise ValueError(
                "vpn host chỉ được hostname/IPv4 (chữ, số, '.', '-'); không khoảng trắng/metachar"
            )
        return v

    @field_validator("config_file")
    @classmethod
    def _config_file_abs(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            return ""
        p = Path(v)
        if not p.is_absolute():
            raise ValueError("vpn config_file phải là đường dẫn tuyệt đối")
        # Chặn traversal lexical trước khi runner mở file (O_NOFOLLOW).
        if ".." in p.parts:
            raise ValueError("vpn config_file không được chứa '..'")
        return v

    @model_validator(mode="after")
    def _kind_fields(self) -> VpnProfileCfg:
        if self.kind == "openfortivpn":
            if not self.host:
                raise ValueError("openfortivpn cần 'host'")
            if not self.cred_secret:
                raise ValueError("openfortivpn cần 'cred_secret' (TÊN secret password)")
        elif self.kind == "openvpn":
            if not self.config_file:
                raise ValueError("openvpn cần 'config_file'")
        if self.username and self.username_secret:
            raise ValueError("chỉ chọn một trong 'username' hoặc 'username_secret'")
        return self


class RemoteCfg(BaseModel):
    hosts: dict[str, HostCfg] = Field(default_factory=dict)
    vpn_profiles: dict[str, VpnProfileCfg] = Field(default_factory=dict)

    @field_validator("vpn_profiles")
    @classmethod
    def _profile_names(cls, v: dict[str, VpnProfileCfg]) -> dict[str, VpnProfileCfg]:
        for name in v:
            if _VPN_PROFILE_NAME_RE.fullmatch(name) is None:
                raise ValueError(
                    f"tên vpn profile '{name}' không hợp lệ "
                    "(chỉ [A-Za-z0-9_-], bắt đầu bằng chữ/số, ≤64 ký tự)"
                )
        return v


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
