"""Analytics (spec P3 §6). /usage /insights đọc THẲNG span store; budget alert."""

from __future__ import annotations

from yett.obs import cost


def usage_report(spans: list[dict], *, by: str = "provider") -> dict[str, dict]:
    """Tổng chi theo provider|model|day từ span store (không thu thập thêm)."""
    return cost.aggregate(spans, by=by)


def total_cost(spans: list[dict]) -> float:
    return round(float(sum(v["cost_usd"] for v in cost.aggregate(spans, by="all").values())), 6)
