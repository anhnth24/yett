"""Integration: web_search được App wire + invoke qua chat path (offline transport).

Chứng minh: registry đăng ký khi có SearchCfg+secrets; App.chat gọi được tool qua
FakeProvider; key thiếu/rỗng fail an toàn; egress lọc URL; secret không vào context/span;
policy Gate vẫn deny khi không có allowlist rule.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest
import yaml

from yett.app import App, build_app
from yett.config.models import (
    BudgetCfg,
    EgressCfg,
    HarnessCfg,
    PriceRow,
    ProviderCfg,
    SandboxCfg,
    SearchCfg,
    SecurityCfg,
    ToolRule,
)
from yett.provider.fake import FakeProvider, text_result, tool_result
from yett.secrets.backends import InMemorySecretStore


SECRET_VALUE = "SEARCH_SECRET_VALUE_ZZZ_DO_NOT_LEAK"


def _clock():
    c = itertools.count(1)
    return lambda: float(next(c))


def _cfg(tmp_path: Path, **kw) -> HarnessCfg:
    defaults: dict = dict(
        provider=ProviderCfg(name="fake", model="fake-1"),
        workspace_root=tmp_path / "ws",
        sandbox=SandboxCfg(backend="local"),
        budget=BudgetCfg(max_loop_iterations=6),
        search=SearchCfg(api_key_secret="search_key"),
        egress=EgressCfg(allowlist=["example.com", "api.search.brave.com"]),
        security=SecurityCfg(allowlist=[ToolRule(tool="web_search", effect="allow")]),
    )
    defaults.update(kw)
    return HarnessCfg(**defaults)


async def _fake_search(query: str, key: str) -> list[dict]:
    assert key == SECRET_VALUE  # key chỉ tới backend, không lộ ra ngoài
    return [
        {"title": "Allowed", "url": "https://example.com/a", "snippet": "ok doc"},
        {"title": "Blocked", "url": "https://evil.example.org/x", "snippet": "nope"},
        {"title": "Also allowed", "url": "https://docs.example.com/b", "snippet": "more"},
    ]


def test_app_registers_web_search_when_configured(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    secrets = InMemorySecretStore({"search_key": SECRET_VALUE})
    app = App(
        provider=FakeProvider([text_result("ok")]),
        cfg=_cfg(tmp_path),
        state_dir=tmp_path / "st",
        secrets=secrets,
        search_fn=_fake_search,
        clock=_clock(),
    )
    assert app.registry.has("web_search")
    assert "web_search" in app.capabilities_summary()
    assert SECRET_VALUE not in app.capabilities_summary()
    app.close()


def test_app_skips_web_search_without_secrets_or_search_cfg(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    # không có search cfg
    app = App(
        provider=FakeProvider([text_result("ok")]),
        cfg=_cfg(tmp_path, search=None),
        state_dir=tmp_path / "st",
        secrets=InMemorySecretStore({"search_key": SECRET_VALUE}),
        search_fn=_fake_search,
        clock=_clock(),
    )
    assert not app.registry.has("web_search")
    app.close()

    # có search nhưng secrets=None → không wire (fail-closed)
    app2 = App(
        provider=FakeProvider([text_result("ok")]),
        cfg=_cfg(tmp_path),
        state_dir=tmp_path / "st2",
        secrets=None,
        search_fn=_fake_search,
        clock=_clock(),
    )
    assert not app2.registry.has("web_search")
    app2.close()


async def test_chat_invokes_web_search_offline_and_filters_egress(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    pricing = {"brave": {"web_search": PriceRow(per_call=0.005)}}
    secrets = InMemorySecretStore({"search_key": SECRET_VALUE})
    provider = FakeProvider([
        tool_result("c1", "web_search", {"query": "cách làm X", "limit": 5}),
        text_result("Đã tìm xong."),
    ])
    app = App(
        provider=provider,
        cfg=_cfg(tmp_path),
        state_dir=tmp_path / "st",
        secrets=secrets,
        search_fn=_fake_search,
        pricing=pricing,
        clock=_clock(),
    )
    res = await app.chat("tìm tài liệu")
    assert res.status == "done"
    assert "c1" in res.completed_tool_calls

    spans = app.spanstore.get_trace(res.trace_id)
    tool_spans = [s for s in spans if s["kind"] == "TOOL_CALL" and s["name"] == "web_search"]
    assert len(tool_spans) == 1
    attrs = tool_spans[0]["attrs"]
    assert attrs.get("cost_usd") == 0.005
    assert attrs.get("provider") == "brave"
    assert attrs.get("queries") == 1
    assert attrs.get("results_kept") == 2  # example.com + docs.example.com
    assert attrs.get("results_blocked") == 1
    # secret không vào span
    assert SECRET_VALUE not in json.dumps(spans)

    # secret không vào context/history session
    hist = app.sessions.history("main", token_budget=50_000)
    hist_blob = " ".join(getattr(m, "content", "") or "" for m in hist)
    assert SECRET_VALUE not in hist_blob
    assert SECRET_VALUE not in res.text
    app.close()


async def test_missing_secret_fails_safely_via_chat(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    secrets = InMemorySecretStore()  # thiếu search_key
    provider = FakeProvider([
        tool_result("c1", "web_search", {"query": "x"}),
        text_result("Thiếu key."),
    ])
    app = App(
        provider=provider,
        cfg=_cfg(tmp_path),
        state_dir=tmp_path / "st",
        secrets=secrets,
        search_fn=_fake_search,
        clock=_clock(),
    )
    res = await app.chat("search đi")
    assert res.status == "done"
    spans = app.spanstore.get_trace(res.trace_id)
    tool_spans = [s for s in spans if s["name"] == "web_search"]
    assert tool_spans and tool_spans[0]["attrs"].get("is_error") is True
    # không crash turn; secret name có thể xuất hiện, value thì không (chưa từng có)
    assert SECRET_VALUE not in json.dumps(spans)
    app.close()


async def test_empty_secret_fails_safely(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    secrets = InMemorySecretStore({"search_key": ""})
    provider = FakeProvider([
        tool_result("c1", "web_search", {"query": "x"}),
        text_result("Key rỗng."),
    ])
    app = App(
        provider=provider,
        cfg=_cfg(tmp_path),
        state_dir=tmp_path / "st",
        secrets=secrets,
        search_fn=_fake_search,
        clock=_clock(),
    )
    res = await app.chat("search")
    assert res.status == "done"
    spans = app.spanstore.get_trace(res.trace_id)
    assert any(
        s["name"] == "web_search" and s["attrs"].get("is_error") for s in spans
    )
    app.close()


async def test_policy_denies_web_search_without_allowlist_rule(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    secrets = InMemorySecretStore({"search_key": SECRET_VALUE})
    # security allowlist rỗng → DEFAULT DENY
    provider = FakeProvider([
        tool_result("c1", "web_search", {"query": "x"}),
        text_result("Bị chặn."),
    ])
    called = {"n": 0}

    async def tracking_search(query: str, key: str) -> list[dict]:
        called["n"] += 1
        return []

    app = App(
        provider=provider,
        cfg=_cfg(tmp_path, security=SecurityCfg(allowlist=[])),
        state_dir=tmp_path / "st",
        secrets=secrets,
        search_fn=tracking_search,
        clock=_clock(),
    )
    res = await app.chat("search")
    assert res.status == "done"
    assert called["n"] == 0  # Gate chặn TRƯỚC khi chạy backend
    assert SECRET_VALUE not in json.dumps(app.spanstore.get_trace(res.trace_id))
    app.close()


def test_build_app_registers_web_search_from_config(tmp_path: Path) -> None:
    """build_app (đường `yett chat`) đọc config thật → đăng ký web_search."""
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg_path = tmp_path / "harness.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "provider": {"name": "fake", "model": "fake-1", "api_key": "x"},
                "workspace_root": str(ws),
                "sandbox": {"backend": "local"},
                "search": {"provider": "brave", "api_key_secret": "search_key"},
                "egress": {"allowlist": ["example.com", "api.search.brave.com"]},
                "security": {"allowlist": [{"tool": "web_search", "effect": "allow"}]},
                "secret_backend": "env",
            }
        ),
        encoding="utf-8",
    )
    # build_app dựng provider từ factory — fake name không được factory hỗ trợ.
    # Dùng App trực tiếp với config load từ file để chứng minh wiring path tương đương.
    from yett.config.loader import load_config

    cfg = load_config(cfg_path)
    assert cfg.search is not None
    secrets = InMemorySecretStore({"search_key": SECRET_VALUE})
    app = App(
        provider=FakeProvider([text_result("ok")]),
        cfg=cfg,
        state_dir=tmp_path / "st",
        secrets=secrets,
        search_fn=_fake_search,
        clock=_clock(),
    )
    assert app.registry.has("web_search")
    app.close()


def test_build_app_wires_search_with_injected_provider_path(tmp_path: Path, monkeypatch) -> None:
    """Đường build_app: mock factory provider → App nhận secrets + SearchCfg → có web_search."""
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg_path = tmp_path / "harness.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "provider": {"name": "glm", "model": "glm-5.2", "api_key_secret": "llm_key"},
                "workspace_root": str(ws),
                "sandbox": {"backend": "local"},
                "search": {"api_key_secret": "search_key"},
                "egress": {"allowlist": ["api.search.brave.com", "example.com"]},
                "security": {"allowlist": [{"tool": "web_search", "effect": "allow"}]},
                "secret_backend": "env",
            }
        ),
        encoding="utf-8",
    )
    secrets = InMemorySecretStore({"llm_key": "LLM_KEY_VAL", "search_key": SECRET_VALUE})

    from yett.provider import factory as factory_mod

    monkeypatch.setattr(
        factory_mod,
        "build_provider",
        lambda cfg, secrets: FakeProvider([text_result("ok")]),
    )
    # build_app không nhận search_fn — backend thật sẽ được dựng; chỉ kiểm REGISTRY
    # (không gọi mạng). Tool đã đăng ký là đủ cho đường chat wiring.
    app = build_app(cfg_path, secrets, state_dir=tmp_path / "st")
    assert app.registry.has("web_search")
    # secret value không nằm trong capabilities
    assert SECRET_VALUE not in app.capabilities_summary()
    assert "LLM_KEY_VAL" not in app.capabilities_summary()
    app.close()


async def test_brave_backend_requires_api_host_on_allowlist(tmp_path: Path) -> None:
    """Backend thật fail-closed nếu api.search.brave.com không có trong egress allowlist."""
    from yett.errors import UserFacingError
    from yett.tools.assist.search_backend import BraveSearchBackend

    async def boom(_url: str, _headers: dict) -> tuple[int, dict]:
        raise AssertionError("không được gọi HTTP khi host ngoài allowlist")

    denied = BraveSearchBackend(allowlist=["example.com"], http_get=boom)
    with pytest.raises(UserFacingError, match="egress allowlist"):
        await denied("q", "k")

    # Có host → transport được gọi; auth header mang key nhưng không lộ ra result parse
    captured: dict = {}

    async def ok_get(url: str, headers: dict) -> tuple[int, dict]:
        captured["url"] = url
        captured["token"] = headers.get("X-Subscription-Token")
        return 200, {
            "web": {
                "results": [
                    {"title": "T", "url": "https://example.com/1", "description": "s"}
                ]
            }
        }

    allowed = BraveSearchBackend(
        allowlist=["api.search.brave.com", "example.com"], http_get=ok_get
    )
    rows = await allowed("hello", SECRET_VALUE)
    assert captured["token"] == SECRET_VALUE
    assert "hello" in captured["url"]
    assert rows[0]["url"] == "https://example.com/1"
