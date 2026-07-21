"""Setup wizard (`yett setup`). Từng bước: provider → model → key → project → ghi config.

Tách logic thuần (catalog, build_config, các bước) khỏi I/O (prompt) để test được.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml

from yett.provider.registry import PROVIDER_BASE_URLS


@dataclass
class ProviderChoice:
    key: str            # provider.name trong config
    label: str
    models: list[str]   # gợi ý model (đầu tiên = mặc định)
    note: str = ""


# Catalog hiển thị trong wizard. Model + giá tham khảo (chi tiết ở pricing.yaml).
CATALOG: list[ProviderChoice] = [
    ProviderChoice("glm", "GLM / Z.ai (Zhipu)", ["glm-5.2", "glm-4.6", "glm-4.5-air"],
                   "glm-5.2 ~$1.4/$4.4 · glm-4.6 rẻ hơn"),
    ProviderChoice("minimax", "MiniMax", ["MiniMax-M3", "MiniMax-M2"], "M3 ~$0.3/$1.2 (chưa verify)"),
    ProviderChoice("deepseek", "DeepSeek", ["deepseek-chat", "deepseek-reasoner"],
                   "chat ~$0.27/$1.1 · rẻ"),
    ProviderChoice("gemini", "Google Gemini", ["gemini-2.5-flash", "gemini-2.5-pro"],
                   "flash ~$0.3/$2.5"),
    ProviderChoice("openai", "OpenAI", ["gpt-5.4-mini", "gpt-5.5"], "mini ~$0.75/$4.5"),
    ProviderChoice("anthropic", "Anthropic (Messages API native)",
                   ["claude-haiku-4-5", "claude-sonnet-5", "claude-opus-4-8"], "haiku ~$1/$5"),
    ProviderChoice("anthropic_oai", "Anthropic (OpenAI-compat)",
                   ["claude-haiku-4-5", "claude-sonnet-5", "claude-opus-4-8"], "haiku ~$1/$5"),
    ProviderChoice("grok", "xAI Grok", ["grok-4.3"], "~$1.25/$2.5"),
    ProviderChoice("qwen", "Alibaba Qwen", ["qwen-plus", "qwen-max"], "plus ~$0.4/$1.2 (chưa verify)"),
    ProviderChoice("mistral", "Mistral", ["mistral-large-latest", "mistral-medium-latest"],
                   "large ~$0.5/$1.5"),
]


@dataclass
class Answers:
    provider: str = "glm"
    model: str = "glm-5.2"
    api_key: str = ""
    base_url: str | None = None
    projects: dict[str, str] = field(default_factory=dict)
    workspace_root: str = "./workspace"
    timezone: str = "Asia/Ho_Chi_Minh"
    monthly_budget_usd: float | None = None


def find_provider(key: str) -> ProviderChoice | None:
    return next((p for p in CATALOG if p.key == key), None)


def build_config(ans: Answers) -> dict:
    """Dựng dict config từ câu trả lời (secret KHÔNG nằm ở đây — chỉ tên)."""
    provider: dict = {
        "name": ans.provider,
        "model": ans.model,
        "api_key_secret": "llm_key",
    }
    if ans.base_url:
        provider["base_url"] = ans.base_url
    elif ans.provider not in PROVIDER_BASE_URLS:
        provider["base_url"] = ans.base_url or ""

    egress = _egress_for(ans.provider)
    cfg: dict = {
        "provider": provider,
        "budget": {
            "max_loop_iterations": 20,
            "context_token_budget": 120000,
            "monthly_cost_alert_usd": ans.monthly_budget_usd,
        },
        "projects": {name: {"path": path} for name, path in ans.projects.items()},
        "egress": {"allowlist": egress},
        "sandbox": {"backend": "docker", "network": "none", "timeout_sec": 120},
        "security": {
            "approval_mode": "manual",
            "approval_timeout_sec": 300,
            "allowlist": [
                {"tool": "read_file", "effect": "allow"},
                {"tool": "write_file", "effect": "allow"},
                {"tool": "exec", "arg_patterns": {"cmd": "^(git|ls|cat|grep|python|pytest|npm|go) "},
                 "effect": "allow"},
            ],
        },
        "secret_backend": "file",
        "timezone": ans.timezone,
        "workspace_root": ans.workspace_root,
    }
    return cfg


def _egress_for(provider: str) -> list[str]:
    host = PROVIDER_BASE_URLS.get(provider, "")
    from urllib.parse import urlparse

    domains = []
    if host:
        d = urlparse(host).hostname
        if d:
            domains.append(d)
    return domains


# ---------- phần I/O (inject được để test) ----------
Prompt = Callable[[str], str]
Emit = Callable[[str], None]


def run_wizard(
    *,
    prompt: Prompt,
    emit: Emit,
    config_path: Path,
    secret_setter: Callable[[str, str], None],
    existing_config: bool = False,
) -> Answers:
    """Chạy wizard tương tác. Trả Answers, ghi config + secret. I/O inject để test."""
    emit("=== yett setup ===")
    if existing_config:
        emit(f"⚠️  {config_path} đã tồn tại — sẽ ghi đè nếu tiếp tục.")

    # Bước 1: provider
    emit("\n[1/5] Chọn nhà cung cấp LLM:")
    for i, p in enumerate(CATALOG, 1):
        emit(f"  {i}. {p.label}  ({p.note})")
    choice = prompt("Số thứ tự (mặc định 1 = GLM): ").strip() or "1"
    idx = _to_int(choice, 1, len(CATALOG)) - 1
    prov = CATALOG[idx]
    ans = Answers(provider=prov.key)

    # Bước 2: model
    emit(f"\n[2/5] Model cho {prov.label}:")
    for i, m in enumerate(prov.models, 1):
        emit(f"  {i}. {m}")
    mchoice = prompt(f"Số thứ tự (mặc định 1 = {prov.models[0]}): ").strip() or "1"
    ans.model = prov.models[_to_int(mchoice, 1, len(prov.models)) - 1]

    # base_url nếu provider không có mặc định
    if prov.key not in PROVIDER_BASE_URLS:
        ans.base_url = prompt("Nhập base_url (endpoint OpenAI-compatible): ").strip()

    # Bước 3: API key
    emit("\n[3/5] API key (lưu vào secrets/llm_key, quyền 600 — KHÔNG vào config/git):")
    ans.api_key = prompt("Dán API key: ").strip()

    # Bước 4: project
    emit("\n[4/5] Thêm project (đường dẫn WSL2, vd /mnt/d/work/duan). Enter trống để dừng:")
    while True:
        path = prompt("  Đường dẫn project (Enter để bỏ qua): ").strip()
        if not path:
            break
        name = prompt("  Tên gọi ngắn cho project: ").strip() or Path(path).name
        ans.projects[name] = path

    # Bước 5: budget cảnh báo
    emit("\n[5/5] Ngưỡng cảnh báo chi phí/tháng (USD, Enter để bỏ qua):")
    b = prompt("  Ngưỡng USD: ").strip()
    ans.monthly_budget_usd = float(b) if b else None

    # Ghi
    if ans.api_key:
        secret_setter("llm_key", ans.api_key)
        emit("✓ Đã lưu API key vào secrets/llm_key (600)")
    cfg = build_config(ans)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    emit(f"✓ Đã ghi {config_path}")
    emit("\nXong! Chạy thử:  yett chat \"xin chào\"")
    return ans


def _to_int(s: str, lo: int, hi: int) -> int:
    try:
        v = int(s)
    except ValueError:
        return lo
    return v if lo <= v <= hi else lo
