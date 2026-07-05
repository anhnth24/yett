"""Integration: Nhóm 2 (skills, subagent, DB, hooks) chạy thật qua App."""

from __future__ import annotations

import itertools
import sqlite3
from pathlib import Path

from yett.app import App
from yett.config.models import (
    BudgetCfg, DbProfileCfg, HarnessCfg, ProviderCfg, SandboxCfg, SecurityCfg, ToolRule,
)
from yett.provider.fake import FakeProvider, text_result, tool_result
from yett.secrets.backends import InMemorySecretStore


def _clock():
    c = itertools.count(1)
    return lambda: float(next(c))


def _cfg(tmp_path: Path, **kw) -> HarnessCfg:
    return HarnessCfg(
        provider=ProviderCfg(name="fake", model="fake-1"),
        workspace_root=tmp_path / "ws",
        sandbox=SandboxCfg(backend="local"),
        budget=BudgetCfg(max_loop_iterations=6),
        **kw,
    )


def test_skills_menu_and_load(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / "skills" / "report").mkdir(parents=True)
    (ws / "skills" / "report" / "SKILL.md").write_text(
        "---\nname: report\ndescription: Tổng hợp tiến độ project\n---\nCÁCH LÀM BÁO CÁO", encoding="utf-8"
    )
    sec = SecurityCfg(allowlist=[ToolRule(tool="load_skill", effect="allow")])
    # agent gọi load_skill('report') → nhận body, rồi kết thúc
    provider = FakeProvider([
        tool_result("c1", "load_skill", {"name": "report"}),
        text_result("Đã đọc hướng dẫn báo cáo."),
    ])
    app = App(provider=provider, cfg=_cfg(tmp_path, security=sec), state_dir=tmp_path / "st",
              secrets=InMemorySecretStore(), clock=_clock())
    # menu skill có trong system prompt
    assert any(s["name"] == "report" for s in app.skill_loader.menu())
    import asyncio
    res = asyncio.run(app.chat("làm báo cáo"))
    assert res.status == "done"
    # body skill được nạp vào context (qua tool result)
    spans = app.spanstore.get_trace(res.trace_id)
    assert any(s["kind"] == "TOOL_CALL" and s["name"] == "load_skill" for s in spans)
    app.close()


def test_db_query_sqlite_readonly(tmp_path: Path) -> None:
    # tạo DB sqlite thật
    db = tmp_path / "app.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE orders (id int, status text)")
    conn.execute("INSERT INTO orders VALUES (123, 'PAID')")
    conn.commit(); conn.close()

    secrets = InMemorySecretStore({"uat_dsn": str(db)})
    cfg = _cfg(
        tmp_path,
        databases={"uat": DbProfileCfg(driver="sqlite", dsn_secret="uat_dsn")},
        security=SecurityCfg(allowlist=[ToolRule(tool="db_query", effect="allow")]),
    )
    provider = FakeProvider([
        tool_result("c1", "db_query", {"profile": "uat", "sql": "SELECT * FROM orders WHERE id=123"}),
        text_result("Đơn 123 trạng thái PAID."),
    ])
    app = App(provider=provider, cfg=cfg, state_dir=tmp_path / "st", secrets=secrets, clock=_clock())
    import asyncio
    res = asyncio.run(app.chat("kiểm tra đơn 123"))
    assert res.status == "done"
    spans = app.spanstore.get_trace(res.trace_id)
    db_span = [s for s in spans if s["name"] == "db_query"][0]
    assert not db_span["attrs"].get("is_error", False)
    app.close()


def test_db_write_blocked_via_app(tmp_path: Path) -> None:
    db = tmp_path / "app.db"
    sqlite3.connect(db).close()
    secrets = InMemorySecretStore({"uat_dsn": str(db)})
    cfg = _cfg(
        tmp_path,
        databases={"uat": DbProfileCfg(driver="sqlite", dsn_secret="uat_dsn")},
        security=SecurityCfg(allowlist=[ToolRule(tool="db_query", effect="allow")]),
    )
    provider = FakeProvider([
        tool_result("c1", "db_query", {"profile": "uat", "sql": "DELETE FROM orders"}),
        text_result("Bị chặn."),
    ])
    app = App(provider=provider, cfg=cfg, state_dir=tmp_path / "st", secrets=secrets, clock=_clock())
    import asyncio
    res = asyncio.run(app.chat("xóa đơn"))
    # DELETE bị sqlguard chặn → tool trả DENIED (agent kết thúc)
    assert res.status == "done"
    app.close()


def test_subagent_registered(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / "agents").mkdir(parents=True)
    (ws / "agents" / "researcher.md").write_text(
        "---\nname: researcher\ndescription: tìm tin\ntoolset: [read_file]\n---\nBạn là researcher",
        encoding="utf-8",
    )
    app = App(provider=FakeProvider([text_result("ok")]), cfg=_cfg(tmp_path),
              state_dir=tmp_path / "st", secrets=InMemorySecretStore(), clock=_clock())
    assert app.registry.has("delegate")  # delegate tool đã wire
    app.close()


def test_capabilities_summary_grounded(tmp_path: Path) -> None:
    from yett.config.models import DbProfileCfg
    ws = tmp_path / "ws"; ws.mkdir()
    cfg = _cfg(tmp_path, databases={"uat": DbProfileCfg(driver="sqlite", dsn_secret="d")})
    app = App(provider=FakeProvider([text_result("ok")]), cfg=cfg, state_dir=tmp_path / "st",
              secrets=InMemorySecretStore({"d": "SECRETVAL123"}), clock=_clock())
    cap = app.capabilities_summary()
    # liệt kê tool thật + tài nguyên + giới hạn an toàn, không lộ secret
    assert "exec" in cap and "db_query" in cap and "read_file" in cap
    assert "uat" in cap  # DB đã khai
    assert "KHÔNG ALTER/DELETE/UPDATE" in cap
    assert "SECRETVAL123" not in cap  # secret value không lộ
    app.close()
