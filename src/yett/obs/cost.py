"""Cost ledger (spec P0-P1 §3.6). Tính cost từ pricing.yaml; aggregate theo provider/model/ngày.

Cost là ƯỚC TÍNH từ bảng giá — cần đối soát định kỳ với billing provider.
"""

from __future__ import annotations

from datetime import datetime, timezone

from yett.config.models import PriceRow
from yett.provider.base import Usage


def compute_cost(provider: str, model: str, usage: Usage, pricing: dict) -> float:
    """USD cho một lần gọi LLM. Không có giá → 0.0 (không đoán)."""
    row = _lookup(provider, model, pricing)
    if row is None:
        return 0.0
    return round(
        usage.input_tokens / 1_000_000 * row.input_per_mtok
        + usage.output_tokens / 1_000_000 * row.output_per_mtok
        + usage.cache_read_tokens / 1_000_000 * row.cache_read_per_mtok,
        6,
    )


def compute_call_cost(provider: str, model: str, calls: int, pricing: dict) -> float:
    """USD cho tool tính theo lần gọi (image_gen, web_search)."""
    row = _lookup(provider, model, pricing)
    if row is None:
        return 0.0
    return round(calls * row.per_call, 6)


def _lookup(provider: str, model: str, pricing: dict) -> PriceRow | None:
    prov = pricing.get(provider)
    if not prov:
        return None
    row: PriceRow | None = prov.get(model) or prov.get("*")
    return row


def aggregate(spans: list[dict], by: str = "provider") -> dict[str, dict]:
    """Gom span có cost → tổng theo provider|model|day. Chỉ span có attrs.cost_usd."""
    out: dict[str, dict] = {}
    for s in spans:
        cost = s["attrs"].get("cost_usd")
        if cost is None:
            continue
        key = _key(s, by)
        agg = out.setdefault(key, {"cost_usd": 0.0, "calls": 0, "input_tokens": 0, "output_tokens": 0})
        agg["cost_usd"] = round(agg["cost_usd"] + cost, 6)
        agg["calls"] += 1
        agg["input_tokens"] += s["attrs"].get("input_tokens", 0)
        agg["output_tokens"] += s["attrs"].get("output_tokens", 0)
    return out


def _key(span: dict, by: str) -> str:
    a = span["attrs"]
    if by == "provider":
        return str(a.get("provider", "?"))
    if by == "model":
        return f"{a.get('provider','?')}/{a.get('model','?')}"
    if by == "day":
        ts = span.get("start_ts") or 0
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    return "all"
