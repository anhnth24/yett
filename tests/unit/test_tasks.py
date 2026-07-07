"""Test TaskStore + task tools + briefing (lớp trợ lý cá nhân)."""

from __future__ import annotations

import asyncio
import itertools
from pathlib import Path

import pytest

from yett.memory.tasks import TaskStore
from yett.tools.builtin.tasks import TaskAddTool, TaskListTool, TaskUpdateTool


def _clock():
    c = itertools.count(1)
    return lambda: float(next(c))


class _Ctx:
    session_key = "t"


def _store(tmp_path: Path) -> TaskStore:
    return TaskStore(tmp_path / "tasks.db", clock=_clock())


def test_add_and_get(tmp_path: Path) -> None:
    s = _store(tmp_path)
    t = s.add("deploy UAT", project="go-learn", priority="high", due="2026-07-08")
    assert t.id == 1 and t.status == "todo" and t.priority == "high"
    assert s.get(1).title == "deploy UAT"
    s.close()


def test_list_ordering_open_before_done_and_priority(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.add("low task", priority="low")
    hi = s.add("urgent", priority="high")
    done = s.add("finished")
    s.update(done.id, status="done")
    order = [t.id for t in s.list_tasks()]
    assert order[0] == hi.id           # high priority lên đầu
    assert order[-1] == done.id        # done xuống cuối
    s.close()


def test_update_done_sets_done_ts(tmp_path: Path) -> None:
    s = _store(tmp_path)
    t = s.add("x")
    assert t.done_ts is None
    upd = s.update(t.id, status="done")
    assert upd.status == "done" and upd.done_ts is not None
    s.close()


def test_invalid_status_priority_rejected(tmp_path: Path) -> None:
    s = _store(tmp_path)
    t = s.add("x")
    with pytest.raises(ValueError):
        s.update(t.id, status="bogus")
    with pytest.raises(ValueError):
        s.add("y", priority="urgent")
    s.close()


def test_due_on_or_before(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.add("late", due="2026-07-01")
    s.add("future", due="2026-12-31")
    s.add("no due")
    due = s.due_on_or_before("2026-07-07")
    assert [t.title for t in due] == ["late"]
    s.close()


def test_delete(tmp_path: Path) -> None:
    s = _store(tmp_path)
    t = s.add("x")
    assert s.delete(t.id) is True
    assert s.get(t.id) is None
    assert s.delete(999) is False
    s.close()


# ---- tools ----
def test_task_add_tool(tmp_path: Path) -> None:
    s = _store(tmp_path)
    tool = TaskAddTool(s)
    tool.validate({"title": "viết báo cáo"})
    res = asyncio.run(tool.run({"title": "viết báo cáo", "priority": "high"}, _Ctx()))
    assert res.ok and "viết báo cáo" in res.content and "#1" in res.content
    assert s.list_tasks()[0].priority == "high"
    s.close()


def test_task_add_tool_empty_title(tmp_path: Path) -> None:
    tool = TaskAddTool(_store(tmp_path))
    from yett.errors import UserFacingError
    with pytest.raises(UserFacingError):
        tool.validate({"title": "  "})


def test_task_list_tool_filter(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.add("a", project="p1")
    s.add("b", project="p2")
    tool = TaskListTool(s)
    res = asyncio.run(tool.run({"project": "p1"}, _Ctx()))
    assert "a" in res.content and "b" not in res.content
    s.close()


def test_task_update_tool_done(tmp_path: Path) -> None:
    s = _store(tmp_path)
    t = s.add("x")
    tool = TaskUpdateTool(s)
    res = asyncio.run(tool.run({"id": t.id, "status": "done"}, _Ctx()))
    assert res.ok and "[done]" in res.content
    s.close()


def test_task_update_tool_missing(tmp_path: Path) -> None:
    tool = TaskUpdateTool(_store(tmp_path))
    res = asyncio.run(tool.run({"id": 999}, _Ctx()))
    assert res.is_error and "999" in res.content
