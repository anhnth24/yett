"""Test eval runner (AG-8) + web_search (WP2.8)."""

from __future__ import annotations

from yett.eval.runner import GoldenTask, TaskRun, grade, run_suite
from yett.secrets.backends import InMemorySecretStore
from yett.tools.assist.web_search import WebSearchTool


class _Ctx:
    session_key = "t"


def test_eval_pass() -> None:
    task = GoldenTask("S4", {
        "tool_called": "log_read",
        "output_contains": ["nguyên nhân"],
        "no_write_to_server": True,
    })
    run = TaskRun(tools_called=["log_read"], final_text="Đây là nguyên nhân lỗi 500", wrote_to_server=False)
    assert grade(task, run).passed


def test_eval_fail_missing_tool() -> None:
    task = GoldenTask("S4", {"tool_called": "log_read"})
    res = grade(task, TaskRun(tools_called=["exec"]))
    assert not res.passed and "log_read" in res.failures[0]


def test_eval_gate_verdict_assertion() -> None:
    task = GoldenTask("S5", {"gate_verdict": {"db_query": "deny"}})
    run = TaskRun(gate_verdicts={"db_query": "allow"})
    assert not grade(task, run).passed


def test_eval_suite() -> None:
    tasks = [GoldenTask("a", {"output_contains": ["ok"]}), GoldenTask("b", {"tool_called": "x"})]
    runs = {"a": TaskRun(final_text="all ok"), "b": TaskRun(tools_called=["x"])}
    results = run_suite(tasks, runs)
    assert all(r.passed for r in results)


async def test_web_search_uses_secret_key() -> None:
    captured = {}

    async def fake_search(query, key):
        captured["key"] = key
        return [{"title": "T", "url": "https://x", "snippet": "s"}]

    secrets = InMemorySecretStore({"search_key": "SECRETKEY"})
    tool = WebSearchTool(fake_search, secrets, "search_key")
    res = await tool.run({"query": "cách làm X"}, _Ctx())
    assert captured["key"] == "SECRETKEY"
    assert "SECRETKEY" not in res.content  # key không lộ ra kết quả
    assert "T" in res.content
