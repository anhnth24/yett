"""Briefing chủ động (lớp trợ lý): tổng hợp 'hôm nay có gì' từ việc + hoạt động gần đây.

Dùng cho: card 'Chào buổi sáng' trên web, job cron gửi briefing, và để agent trả lời
'tôi đang làm dở gì'. Thuần đọc state THẬT (TaskStore + traces) — không bịa.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from yett.app import App


def today_iso(tz: str) -> str:
    try:
        return datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001 — tz lạ thì rơi về UTC
        return datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%d")


def briefing_data(app: "App") -> dict[str, Any]:
    """Dữ liệu briefing có cấu trúc (cho web + text)."""
    today = today_iso(app.cfg.timezone)
    tasks = app.tasks
    opent = tasks.open_tasks()
    due = tasks.due_on_or_before(today)
    overdue = [t for t in due if t.due and t.due < today]
    doing = [t for t in opent if t.status == "doing"]
    return {
        "date": today,
        "open_count": len(opent),
        "doing": [t.as_dict() for t in doing],
        "due_today_or_earlier": [t.as_dict() for t in due],
        "overdue": [t.as_dict() for t in overdue],
        "top": [t.as_dict() for t in opent[:5]],
    }


def daily_briefing(app: "App") -> str:
    """Briefing dạng văn bản, thân thiện kiểu trợ lý."""
    d = briefing_data(app)
    if d["open_count"] == 0:
        return f"Chào anh! Hôm nay ({d['date']}) chưa có việc nào trong danh sách. Cần thêm gì không?"
    lines = [f"Chào anh! Tóm tắt hôm nay ({d['date']}):",
             f"- Đang có {d['open_count']} việc chưa xong."]
    if d["overdue"]:
        lines.append(f"- ⚠️ {len(d['overdue'])} việc QUÁ HẠN: "
                     + "; ".join(f"#{t['id']} {t['title']} (due {t['due']})" for t in d["overdue"]))
    due_today = [t for t in d["due_today_or_earlier"] if t not in d["overdue"]]
    if due_today:
        lines.append("- Đến hạn hôm nay: "
                     + "; ".join(f"#{t['id']} {t['title']}" for t in due_today))
    if d["doing"]:
        lines.append("- Đang làm dở: "
                     + "; ".join(f"#{t['id']} {t['title']}" for t in d["doing"]))
    if not d["overdue"] and not due_today:
        top = d["top"]
        if top:
            lines.append("- Ưu tiên tiếp theo: "
                         + "; ".join(f"#{t['id']} {t['title']}" for t in top[:3]))
    return "\n".join(lines)
