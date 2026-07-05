"""Provider factory (spec P0-P1 §3). Build provider từ config + secret store.

Đây là điểm DUY NHẤT core biết tên adapter cụ thể (import-linter cho phép factory).
"""

from __future__ import annotations

from yett.config.models import ProviderCfg
from yett.provider.base import Provider


def build_provider(cfg: ProviderCfg, secrets) -> Provider:
    key = secrets.get(cfg.api_key_secret) if cfg.api_key_secret else ""
    if cfg.name in ("openai_compat", "minimax", "glm", "openai", "openrouter", "deepseek", "groq"):
        from yett.provider.openai_compat import OpenAICompatProvider

        if not cfg.base_url:
            raise ValueError(f"provider '{cfg.name}' cần base_url")
        return OpenAICompatProvider(
            model=cfg.model, api_key=key, base_url=cfg.base_url, provider_name=cfg.name
        )
    if cfg.name == "anthropic":
        # Anthropic dùng Messages API riêng — nhiều endpoint (kể cả MiniMax/GLM) đã OpenAI-compatible
        # nên adapter native để sau; hiện khuyến nghị openai_compat.
        raise NotImplementedError(
            "adapter Anthropic native chưa nối; MiniMax/GLM dùng name=openai_compat với base_url tương ứng"
        )
    raise ValueError(f"provider không hỗ trợ: {cfg.name}")
