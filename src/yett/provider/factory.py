"""Provider factory (spec P0-P1 §3). Build provider từ config + secret store.

Đây là điểm DUY NHẤT core biết tên adapter cụ thể (import-linter cho phép factory).
"""

from __future__ import annotations

from yett.config.models import ProviderCfg
from yett.provider.base import Provider
from yett.provider.registry import default_base_url

# Mọi provider dưới đây nói OpenAI-compatible → dùng chung một adapter.
_OAI_COMPAT = {
    "openai_compat", "openai", "gemini", "deepseek", "glm", "minimax",
    "grok", "qwen", "mistral", "openrouter", "groq", "anthropic_oai",
}


def build_provider(cfg: ProviderCfg, secrets) -> Provider:
    # api_key inline (tiện) ưu tiên nếu có; ngược lại lấy theo TÊN từ secret store.
    if cfg.api_key:
        key = cfg.api_key
    elif cfg.api_key_secret:
        key = secrets.get(cfg.api_key_secret)
    else:
        key = ""
    if cfg.name in _OAI_COMPAT:
        from yett.provider.openai_compat import OpenAICompatProvider

        base_url = cfg.base_url or default_base_url(cfg.name)
        if not base_url:
            raise ValueError(
                f"provider '{cfg.name}' cần base_url (không có mặc định) — khai trong config"
            )
        return OpenAICompatProvider(
            model=cfg.model, api_key=key, base_url=base_url, provider_name=cfg.name
        )
    if cfg.name == "anthropic":
        from yett.provider.anthropic import AnthropicProvider

        base_url = cfg.base_url or default_base_url("anthropic") or "https://api.anthropic.com/v1"
        return AnthropicProvider(
            model=cfg.model, api_key=key, base_url=base_url, provider_name=cfg.name
        )
    raise ValueError(f"provider không hỗ trợ: {cfg.name}")
