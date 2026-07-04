"""Analytics (spec P3 §6). /usage /insights đọc THẲNG span store; budget alert."""

from __future__ import annotations

from yett.obs import cost


def usage_report(spans: list[dict], *, by: str = "provider") -> dict[str, dict]:
    """Tổng chi theo provider|model|day từ span store (không thu thập thêm)."""
    return cost.aggregate(spans, by=by)


def total_cost(spans: list[dict]) -> float:
    return round(sum(v["cost_usd"] for v in cost.aggregate(spans, by="all").values()), 6)


def check_budget(spans: list[dict], monthly_limit: float | None) -> dict:
    """Trả trạng thái budget: {spent, limit, pct, alert}. alert khi >=80%/100%."""
    spent = total_cost(spans)
    if not monthly_limit:
        return {"spent": spent, "limit": None, "pct": None, "alert": None}
    pct = round(spent / monthly_limit * 100, 1)
    alert = None
    if pct >= 100:
        alert = "over"
    elif pct >= 80:
        alert = "warn"
    return {"spent": spent, "limit": monthly_limit, "pct": pct, "alert": alert}
