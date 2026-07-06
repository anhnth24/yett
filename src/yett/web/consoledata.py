"""Dữ liệu THẬT cho các màn console web (Tổng quan / Agent / Project / Credential).

Hàm thuần lấy từ `App` đã lắp ráp + config — KHÔNG bịa số. Màn nào không có nguồn thật
(vd job theo lịch) thì trả rỗng để UI hiện trạng thái rỗng trung thực. Không hàm nào ở
đây trả VALUE của secret — chỉ tên + đã-đặt/thiếu (bất biến secret của yett)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from yett.errors import SecretNotFound

if TYPE_CHECKING:
    from yett.app import App


def overview(app: "App") -> dict[str, Any]:
    """KPI + trạng thái hệ thống, tất cả từ config/registry thật."""
    cfg = app.cfg
    return {
        "provider": cfg.provider.name,
        "model": cfg.provider.model,
        "provider_inline_key": bool(cfg.provider.api_key and not cfg.provider.api_key_secret),
        "budget": {
            "monthly_alert_usd": cfg.budget.monthly_cost_alert_usd,
            "max_loop_iterations": cfg.budget.max_loop_iterations,
            "context_token_budget": cfg.budget.context_token_budget,
        },
        "sandbox": {"backend": cfg.sandbox.backend, "network": cfg.sandbox.network},
        "approval_mode": cfg.security.approval_mode,
        "timezone": cfg.timezone,
        "skills_enabled": cfg.skills_enabled,
        "subagents_enabled": cfg.subagents_enabled,
        "tools": sorted(app.registry.names()),
        "counts": {
            "projects": len(cfg.projects),
            "databases": len(cfg.databases),
            "hosts": len(cfg.remote.hosts),
            "tools": len(app.registry.names()),
        },
    }


def secrets_status(app: "App") -> list[dict[str, Any]]:
    """Tên secret được config tham chiếu + đã-đặt/thiếu. KHÔNG trả value.

    Kiểm tra đã-đặt bằng cách gọi store.get rồi bắt SecretNotFound — value lấy ra chỉ để
    biết có tồn tại, không đưa vào response."""
    cfg = app.cfg
    refs: list[tuple[str, str]] = []

    def add(name: str, source: str) -> None:
        if name:
            refs.append((name, source))

    add(cfg.provider.api_key_secret, f"provider · {cfg.provider.name}")
    if cfg.fallback_provider:
        add(cfg.fallback_provider.api_key_secret, f"fallback · {cfg.fallback_provider.name}")
    for n, db in cfg.databases.items():
        add(db.dsn_secret, f"db_query · {n}")
    if cfg.search:
        add(cfg.search.api_key_secret, "web_search")
    for hn, h in cfg.remote.hosts.items():
        if h.auth.startswith("keyfile:"):
            add(h.auth.split(":", 1)[1], f"ssh_exec · {hn}")
    for vn, vp in cfg.remote.vpn_profiles.items():
        add(str(vp.get("cred_secret", "")), f"vpn · {vn}")

    store = app._secrets
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name, source in refs:
        if name in seen:
            continue
        seen.add(name)
        try:
            store.get(name)
            isset = True
        except SecretNotFound:
            isset = False
        except Exception:  # noqa: BLE001 — backend lỗi khác cũng coi như chưa sẵn sàng
            isset = False
        out.append({"name": name, "source": source, "isset": isset})
    return out


def subagents(app: "App") -> list[dict[str, Any]]:
    """Subagent def thật quét từ `{workspace_root}/agents/*.md`. Rỗng nếu chưa có."""
    from yett.subagent.definition import parse_subagent_md

    d = Path(app.cfg.workspace_root) / "agents"
    out: list[dict[str, Any]] = []
    if d.is_dir():
        for f in sorted(d.glob("*.md")):
            try:
                s = parse_subagent_md(f.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 — file def hỏng thì bỏ qua, không sập màn
                continue
            out.append({
                "name": s.name, "description": s.description, "toolset": s.toolset,
                "max_iterations": s.max_iterations, "token_budget": s.token_budget,
            })
    return out


def projects(app: "App") -> dict[str, Any]:
    """workspace_root + project đã đăng ký trong config (name → path + có tồn tại)."""
    cfg = app.cfg
    return {
        "workspace_root": str(cfg.workspace_root),
        "projects": [
            {"name": n, "path": str(p.path), "exists": Path(p.path).exists(),
             "hosts": p.hosts, "databases": p.databases}
            for n, p in cfg.projects.items()
        ],
    }
