"""Danh mục provider OpenAI-compatible (spec P0-P1 §3.3).

base_url mặc định theo tên provider → config chỉ cần chọn name + model + key.
Giá trong config/pricing.yaml (đối soát billing thật). base_url [Unverified] — kiểm khi có key.
"""

from __future__ import annotations

# name -> base_url mặc định (OpenAI-compatible endpoint). Override bằng cfg.base_url nếu khác.
OPENAI_COMPAT_BASE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "anthropic_oai": "https://api.anthropic.com/v1",  # Anthropic có endpoint OpenAI-compat
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "deepseek": "https://api.deepseek.com",
    "glm": "https://api.z.ai/api/paas/v4",  # quốc tế; TQ: https://open.bigmodel.cn/api/paas/v4
    "minimax": "https://api.minimax.io/v1",
    "grok": "https://api.x.ai/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "mistral": "https://api.mistral.ai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "groq": "https://api.groq.com/openai/v1",
}


def default_base_url(name: str) -> str | None:
    return OPENAI_COMPAT_BASE_URLS.get(name)
