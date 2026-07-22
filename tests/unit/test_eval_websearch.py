"""Test eval runner (AG-8) + web_search (WP2.8)."""

from __future__ import annotations

import pytest

from yett.errors import UserFacingError
from yett.eval.runner import GoldenTask, TaskRun, grade, run_suite
from yett.obs.cost import compute_call_cost
from yett.config.models import PriceRow
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
        return [{"title": "T", "url": "https://example.com/x", "snippet": "s"}]

    secrets = InMemorySecretStore({"search_key": "SECRETKEY"})
    tool = WebSearchTool(
        fake_search, secrets, "search_key", allowlist=["example.com"], cost_usd=0.01
    )
    res = await tool.run({"query": "cách làm X"}, _Ctx())
    assert captured["key"] == "SECRETKEY"
    assert "SECRETKEY" not in res.content  # key không lộ ra kết quả
    assert "T" in res.content
    assert res.span_attrs.get("cost_usd") == 0.01
    assert "SECRETKEY" not in str(res.span_attrs)


async def test_web_search_filters_egress() -> None:
    async def fake_search(query, key):
        return [
            {"title": "ok", "url": "https://docs.example.com/a", "snippet": "a"},
            {"title": "no", "url": "https://other.test/b", "snippet": "b"},
            {"title": "scheme", "url": "javascript://example.com/x", "snippet": "c"},
            {"title": "creds", "url": "https://user:pass@example.com/x", "snippet": "d"},
        ]

    tool = WebSearchTool(
        fake_search,
        InMemorySecretStore({"k": "KEY"}),
        "k",
        allowlist=["example.com"],
    )
    res = await tool.run({"query": "q"}, _Ctx())
    assert "docs.example.com" in res.content
    assert "other.test" not in res.content
    assert "javascript:" not in res.content
    assert "user:pass" not in res.content
    assert res.span_attrs.get("results_blocked") == 3


@pytest.mark.parametrize("limit", [0, -1, 11, "5", True, None])
async def test_web_search_rejects_invalid_limit_before_backend(limit) -> None:
    called = False

    async def fake_search(query, key):
        nonlocal called
        called = True
        return []

    tool = WebSearchTool(
        fake_search, InMemorySecretStore({"k": "KEY"}), "k", allowlist=["example.com"]
    )
    with pytest.raises(UserFacingError, match="limit"):
        tool.validate({"query": "q", "limit": limit})
    assert called is False


async def test_web_search_redacts_exact_key_reflected_by_backend() -> None:
    key = "provider-key-format-not-covered-by-generic-redactor"

    async def fake_search(query, api_key):
        return [
            {
                "title": f"reflected {api_key}",
                "url": f"https://example.com/?debug={api_key}",
                "snippet": f"token={api_key}",
            }
        ]

    tool = WebSearchTool(
        fake_search, InMemorySecretStore({"k": key}), "k", allowlist=["example.com"]
    )
    result = await tool.run({"query": "q"}, _Ctx())
    assert key not in result.content
    assert result.content.count("[REDACTED]") == 3


async def test_web_search_backend_errors_do_not_leak_key_or_crash_turn() -> None:
    key = "provider-key-format-not-covered-by-generic-redactor"

    async def unsafe_search(query, api_key):
        raise RuntimeError(f"request headers contained {api_key}")

    tool = WebSearchTool(
        unsafe_search, InMemorySecretStore({"k": key}), "k", allowlist=["example.com"]
    )
    with pytest.raises(UserFacingError, match="backend lỗi") as exc:
        await tool.run({"query": "q"}, _Ctx())
    assert key not in str(exc.value)


async def test_web_search_missing_secret_raises_user_facing() -> None:
    async def fake_search(query, key):
        raise AssertionError("không gọi search khi thiếu key")

    tool = WebSearchTool(
        fake_search, InMemorySecretStore(), "missing", allowlist=["example.com"]
    )
    with pytest.raises(UserFacingError, match="chưa đặt"):
        await tool.run({"query": "q"}, _Ctx())


def test_compute_call_cost_per_call() -> None:
    pricing = {"brave": {"web_search": PriceRow(per_call=0.005)}}
    assert compute_call_cost("brave", "web_search", pricing) == 0.005
    assert compute_call_cost("brave", "missing", pricing) == 0.0
    assert compute_call_cost("nope", "web_search", {}) == 0.0
