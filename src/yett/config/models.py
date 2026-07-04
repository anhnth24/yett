"""Config models (spec P0-P1 §2). Pydantic; validate lúc load, lỗi → từ chối khởi động."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class ProviderCfg(BaseModel):
    name: str  # "anthropic" | "openai_compat" | "fake"
    model: str
    api_key_secret: str = ""  # TÊN secret, không phải giá trị
    base_url: str | None = None
    max_retries: int = 4


class BudgetCfg(BaseModel):
    max_loop_iterations: int = 20
    context_token_budget: int = 150_000
    monthly_cost_alert_usd: float | None = None


class ProjectCfg(BaseModel):
    path: Path


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


class HarnessCfg(BaseModel):
    provider: ProviderCfg
    fallback_provider: ProviderCfg | None = None
    budget: BudgetCfg = Field(default_factory=BudgetCfg)
    projects: dict[str, ProjectCfg] = Field(default_factory=dict)
    egress: EgressCfg = Field(default_factory=EgressCfg)
    sandbox: SandboxCfg = Field(default_factory=SandboxCfg)
    security: SecurityCfg = Field(default_factory=SecurityCfg)
    secret_backend: Literal["keyring", "age", "env"] = "env"
    timezone: str = "Asia/Ho_Chi_Minh"
    workspace_root: Path


class PriceRow(BaseModel):
    input_per_mtok: float = 0.0
    output_per_mtok: float = 0.0
    cache_read_per_mtok: float = 0.0
    per_call: float = 0.0  # cho image_gen/web_search (tính theo lần gọi)
