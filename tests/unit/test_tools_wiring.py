"""Test wiring bất biến Gate→...→Filters + project scope + exec sandbox (RG1-1b, RG1-3, P1.3.6)."""

from __future__ import annotations


from yett.config.models import SecurityCfg, ToolRule
from yett.sandbox.local import LocalSandbox
from yett.security.basic_gate import BasicGate
from yett.tools.builtin.exec import ExecTool
from yett.tools.builtin.files import ReadFileTool, WriteFileTool
from yett.tools.builtin.web_fetch import WebFetchTool
from yett.tools.projects import ProjectScope
from yett.tools.registry import Registry
from yett.tools.wiring import execute_tool


class _Ctx:
    session_key = "t"


def _registry(scope: ProjectScope) -> Registry:
    r = Registry()
    r.register(ExecTool(LocalSandbox()))
    r.register(ReadFileTool(scope))
    r.register(WriteFileTool(scope))
    r.register(WebFetchTool(["example.com"], _fake_fetch))
    return r


async def _fake_fetch(url: str) -> str:
    return f"CONTENT of {url}"


async def test_denied_tool_returns_error_not_exception(tmp_path) -> None:
    scope = ProjectScope(tmp_path, {})
    gate = BasicGate(SecurityCfg())  # allowlist rỗng → default deny
    res = await execute_tool("exec", {"cmd": "echo hi"}, _Ctx(), gate=gate, registry=_registry(scope))
    assert res.is_error and "DENIED" in res.content


async def test_hardline_never_reaches_sandbox(tmp_path) -> None:
    audit: list[dict] = []
    scope = ProjectScope(tmp_path, {})
    rules = [ToolRule(tool="exec", arg_patterns={"cmd": ".*"}, effect="allow")]
    gate = BasicGate(SecurityCfg(allowlist=rules))
    res = await execute_tool(
        "exec", {"cmd": "rm -rf /"}, _Ctx(), gate=gate, registry=_registry(scope),
        auditor=lambda **k: audit.append(k),
    )
    assert res.is_error and "DENIED" in res.content
    # audit ghi verdict=deny với rule hardline
    assert audit[0]["verdict"] == "deny" and audit[0]["rule_id"] == "DENY_EXEC"


async def test_exec_runs_when_allowed(tmp_path) -> None:
    scope = ProjectScope(tmp_path, {})
    # [allowlist-anchor] pattern giờ phải khớp TOÀN BỘ cmd (fullmatch, không còn prefix ngầm
    # định qua search()) — xem security/allowlist.py.
    rules = [ToolRule(tool="exec", arg_patterns={"cmd": r"^echo hello$"}, effect="allow")]
    gate = BasicGate(SecurityCfg(allowlist=rules))
    res = await execute_tool("exec", {"cmd": "echo hello"}, _Ctx(), gate=gate, registry=_registry(scope))
    assert not res.is_error and "hello" in res.content


async def test_file_scope_blocks_traversal(tmp_path) -> None:
    scope = ProjectScope(tmp_path, {})
    rules = [ToolRule(tool="read_file", arg_patterns={}, effect="allow")]
    gate = BasicGate(SecurityCfg(allowlist=rules))
    res = await execute_tool(
        "read_file", {"path": "/etc/hostname"}, _Ctx(), gate=gate, registry=_registry(scope)
    )
    assert res.is_error  # ngoài scope → validate/run raise → error


async def test_write_then_read_in_scope(tmp_path) -> None:
    scope = ProjectScope(tmp_path, {})
    rules = [
        ToolRule(tool="write_file", arg_patterns={}, effect="allow"),
        ToolRule(tool="read_file", arg_patterns={}, effect="allow"),
    ]
    gate = BasicGate(SecurityCfg(allowlist=rules))
    reg = _registry(scope)
    f = str(tmp_path / "note.txt")
    await execute_tool("write_file", {"path": f, "content": "xin chào"}, _Ctx(), gate=gate, registry=reg)
    res = await execute_tool("read_file", {"path": f}, _Ctx(), gate=gate, registry=reg)
    assert "xin chào" in res.content


async def test_web_fetch_egress_allowlist(tmp_path) -> None:
    scope = ProjectScope(tmp_path, {})
    rules = [ToolRule(tool="web_fetch", arg_patterns={}, effect="allow")]
    gate = BasicGate(SecurityCfg(allowlist=rules))
    reg = _registry(scope)
    ok = await execute_tool("web_fetch", {"url": "https://example.com/x"}, _Ctx(), gate=gate, registry=reg)
    assert not ok.is_error and "CONTENT" in ok.content
    bad = await execute_tool("web_fetch", {"url": "https://evil.com/x"}, _Ctx(), gate=gate, registry=reg)
    assert bad.is_error and "DENIED" in bad.content
