"""Integration: image_gen được App wire + invoke qua chat path (offline backend).

Chứng minh: registry đăng ký khi có ImageCfg+secrets; App.chat gọi được tool qua
FakeProvider; ảnh ghi trong workspace; key thiếu/rỗng fail an toàn; path unsafe bị
chặn; secret không vào context/span; policy Gate deny khi không có allowlist rule;
cost attribution vào ledger.
"""

from __future__ import annotations

import base64
import itertools
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from yett.app import App, build_app
from yett.config.models import (
    BudgetCfg,
    EgressCfg,
    HarnessCfg,
    ImageCfg,
    PriceRow,
    ProviderCfg,
    SandboxCfg,
    SecurityCfg,
    ToolRule,
)
from yett.obs.cost import aggregate
from yett.provider.fake import FakeProvider, text_result, tool_result
from yett.secrets.backends import InMemorySecretStore
from yett.tools.assist.image_backend import GeneratedImage

SECRET_VALUE = "IMAGE_SECRET_VALUE_ZZZ_DO_NOT_LEAK"
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQ"
    "AAAABJRU5ErkJggg=="
)


def _clock():
    c = itertools.count(1)
    return lambda: float(next(c))


def _cfg(tmp_path: Path, **kw) -> HarnessCfg:
    defaults: dict = dict(
        provider=ProviderCfg(name="fake", model="fake-1"),
        workspace_root=tmp_path / "ws",
        sandbox=SandboxCfg(backend="local"),
        budget=BudgetCfg(max_loop_iterations=6),
        image=ImageCfg(api_key_secret="image_key", model="dall-e-3"),
        egress=EgressCfg(allowlist=["api.openai.com"]),
        security=SecurityCfg(allowlist=[ToolRule(tool="image_gen", effect="allow")]),
    )
    defaults.update(kw)
    return HarnessCfg(**defaults)


async def _fake_image(prompt: str, key: str, *, size: str = "1024x1024") -> GeneratedImage:
    assert key == SECRET_VALUE
    return GeneratedImage(data=_PNG, mime="image/png", revised_prompt=f"ok:{prompt[:16]}")


def test_app_registers_image_gen_when_configured(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    secrets = InMemorySecretStore({"image_key": SECRET_VALUE})
    app = App(
        provider=FakeProvider([text_result("ok")]),
        cfg=_cfg(tmp_path),
        state_dir=tmp_path / "st",
        secrets=secrets,
        image_fn=_fake_image,
        clock=_clock(),
    )
    assert app.registry.has("image_gen")
    assert "image_gen" in app.capabilities_summary()
    assert SECRET_VALUE not in app.capabilities_summary()
    app.close()


def test_app_skips_image_gen_without_secrets_or_image_cfg(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    app = App(
        provider=FakeProvider([text_result("ok")]),
        cfg=_cfg(tmp_path, image=None),
        state_dir=tmp_path / "st",
        secrets=InMemorySecretStore({"image_key": SECRET_VALUE}),
        image_fn=_fake_image,
        clock=_clock(),
    )
    assert not app.registry.has("image_gen")
    app.close()

    app2 = App(
        provider=FakeProvider([text_result("ok")]),
        cfg=_cfg(tmp_path),
        state_dir=tmp_path / "st2",
        secrets=None,
        image_fn=_fake_image,
        clock=_clock(),
    )
    assert not app2.registry.has("image_gen")
    app2.close()


async def test_chat_invokes_image_gen_offline_and_attributes_cost(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    pricing = {"openai_compat": {"dall-e-3": PriceRow(per_call=0.04)}}
    secrets = InMemorySecretStore({"image_key": SECRET_VALUE})
    provider = FakeProvider(
        [
            tool_result(
                "c1",
                "image_gen",
                {"prompt": "logo xanh", "path": "images/logo.png", "size": "1024x1024"},
            ),
            text_result("Đã tạo ảnh."),
        ]
    )
    app = App(
        provider=provider,
        cfg=_cfg(tmp_path),
        state_dir=tmp_path / "st",
        secrets=secrets,
        image_fn=_fake_image,
        pricing=pricing,
        clock=_clock(),
    )
    res = await app.chat("vẽ logo")
    assert res.status == "done"
    assert "c1" in res.completed_tool_calls
    saved = (tmp_path / "ws" / "images" / "logo.png")
    assert saved.is_file()
    assert saved.read_bytes() == _PNG

    spans = app.spanstore.get_trace(res.trace_id)
    tool_spans = [s for s in spans if s["kind"] == "TOOL_CALL" and s["name"] == "image_gen"]
    assert len(tool_spans) == 1
    attrs = tool_spans[0]["attrs"]
    assert attrs.get("cost_usd") == 0.04
    assert attrs.get("provider") == "openai_compat"
    assert attrs.get("model") == "dall-e-3"
    assert attrs.get("images") == 1
    assert aggregate(spans, by="provider")["openai_compat"] == {
        "cost_usd": 0.04,
        "calls": 1,
        "input_tokens": 0,
        "output_tokens": 0,
    }
    assert SECRET_VALUE not in json.dumps(spans)
    hist = app.sessions.history("main", token_budget=50_000)
    hist_blob = " ".join(getattr(m, "content", "") or "" for m in hist)
    assert SECRET_VALUE not in hist_blob
    assert SECRET_VALUE not in res.text
    app.close()


async def test_missing_secret_fails_safely_via_chat(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    provider = FakeProvider(
        [
            tool_result("c1", "image_gen", {"prompt": "x", "path": "a.png"}),
            text_result("Thiếu key."),
        ]
    )
    app = App(
        provider=provider,
        cfg=_cfg(tmp_path),
        state_dir=tmp_path / "st",
        secrets=InMemorySecretStore(),
        image_fn=_fake_image,
        clock=_clock(),
    )
    res = await app.chat("vẽ")
    assert res.status == "done"
    spans = app.spanstore.get_trace(res.trace_id)
    tool_spans = [s for s in spans if s["name"] == "image_gen"]
    assert tool_spans and tool_spans[0]["attrs"].get("is_error") is True
    assert SECRET_VALUE not in json.dumps(spans)
    app.close()


async def test_policy_denies_image_gen_without_allowlist_rule(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    called = {"n": 0}

    async def tracking(prompt: str, key: str, *, size: str = "1024x1024") -> GeneratedImage:
        called["n"] += 1
        return GeneratedImage(data=_PNG, mime="image/png")

    provider = FakeProvider(
        [
            tool_result("c1", "image_gen", {"prompt": "x", "path": "a.png"}),
            text_result("Bị chặn."),
        ]
    )
    app = App(
        provider=provider,
        cfg=_cfg(tmp_path, security=SecurityCfg(allowlist=[])),
        state_dir=tmp_path / "st",
        secrets=InMemorySecretStore({"image_key": SECRET_VALUE}),
        image_fn=tracking,
        clock=_clock(),
    )
    res = await app.chat("vẽ")
    assert res.status == "done"
    assert called["n"] == 0
    assert SECRET_VALUE not in json.dumps(app.spanstore.get_trace(res.trace_id))
    app.close()


async def test_path_traversal_denied_via_chat_without_writing(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    called = {"n": 0}

    async def tracking(prompt: str, key: str, *, size: str = "1024x1024") -> GeneratedImage:
        called["n"] += 1
        return GeneratedImage(data=_PNG, mime="image/png")

    provider = FakeProvider(
        [
            tool_result(
                "c1",
                "image_gen",
                {"prompt": "x", "path": "../outside/evil.png"},
            ),
            text_result("Path lỗi."),
        ]
    )
    app = App(
        provider=provider,
        cfg=_cfg(tmp_path),
        state_dir=tmp_path / "st",
        secrets=InMemorySecretStore({"image_key": SECRET_VALUE}),
        image_fn=tracking,
        clock=_clock(),
    )
    res = await app.chat("vẽ")
    assert res.status == "done"
    assert called["n"] == 0  # validate chặn trước backend
    assert not (outside / "evil.png").exists()
    content = "\n".join(m.content or "" for m in provider.calls[1])
    assert "path" in content.lower() or "DENIED" in content or ".." in content
    app.close()


async def test_backend_exception_cannot_leak_secret_through_chat(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()

    async def unsafe(prompt: str, key: str, *, size: str = "1024x1024") -> GeneratedImage:
        raise RuntimeError(f"transport dumped key={key}")

    provider = FakeProvider(
        [
            tool_result("c1", "image_gen", {"prompt": "q", "path": "a.png"}),
            text_result("Image lỗi."),
        ]
    )
    app = App(
        provider=provider,
        cfg=_cfg(tmp_path),
        state_dir=tmp_path / "st",
        secrets=InMemorySecretStore({"image_key": SECRET_VALUE}),
        image_fn=unsafe,
        clock=_clock(),
    )
    result = await app.chat("vẽ")
    model_context = "\n".join(m.content or "" for m in provider.calls[1])
    assert SECRET_VALUE not in json.dumps(app.spanstore.get_trace(result.trace_id))
    assert SECRET_VALUE not in model_context
    assert "image_gen backend lỗi" in model_context
    app.close()


def test_build_app_wires_image_with_injected_provider_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg_path = tmp_path / "harness.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "provider": {
                    "name": "glm",
                    "model": "glm-5.2",
                    "api_key_secret": "llm_key",
                },
                "workspace_root": str(ws),
                "sandbox": {"backend": "local"},
                "image": {
                    "provider": "openai_compat",
                    "model": "dall-e-3",
                    "api_key_secret": "image_key",
                },
                "egress": {"allowlist": ["api.openai.com"]},
                "security": {"allowlist": [{"tool": "image_gen", "effect": "allow"}]},
                "secret_backend": "env",
            }
        ),
        encoding="utf-8",
    )
    secrets = InMemorySecretStore({"llm_key": "LLM_KEY_VAL", "image_key": SECRET_VALUE})
    from yett.provider import factory as factory_mod

    monkeypatch.setattr(
        factory_mod,
        "build_provider",
        lambda cfg, secrets: FakeProvider([text_result("ok")]),
    )
    app = build_app(cfg_path, secrets, state_dir=tmp_path / "st")
    assert app.registry.has("image_gen")
    assert SECRET_VALUE not in app.capabilities_summary()
    assert "LLM_KEY_VAL" not in app.capabilities_summary()
    app.close()


def test_config_rebuild_hot_swaps_image_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg_path = tmp_path / "harness.yaml"

    def write_config(model: str) -> None:
        cfg_path.write_text(
            yaml.safe_dump(
                {
                    "provider": {
                        "name": "glm",
                        "model": "glm-5.2",
                        "api_key_secret": "llm_key",
                    },
                    "workspace_root": str(ws),
                    "sandbox": {"backend": "local"},
                    "image": {
                        "api_key_secret": "image_key",
                        "model": model,
                    },
                    "egress": {"allowlist": ["api.openai.com"]},
                    "security": {
                        "allowlist": [{"tool": "image_gen", "effect": "allow"}]
                    },
                }
            ),
            encoding="utf-8",
        )

    from yett.provider import factory as factory_mod
    from yett.web.server import _hot_swap

    monkeypatch.setattr(
        factory_mod,
        "build_provider",
        lambda cfg, secrets: FakeProvider([text_result("ok")]),
    )
    secrets = InMemorySecretStore(
        {"llm_key": "LLM_KEY_VAL", "image_key": SECRET_VALUE}
    )
    write_config("dall-e-2")
    old = build_app(cfg_path, secrets, state_dir=tmp_path / "st")
    assert old.registry.get("image_gen")._cost_model == "dall-e-2"

    write_config("dall-e-3")
    server = SimpleNamespace(_app=old)
    _hot_swap(
        server,
        threading.Lock(),
        None,
        lambda: build_app(cfg_path, secrets, state_dir=tmp_path / "st"),
    )
    new = server._app
    assert new is not old
    assert new.registry.get("image_gen")._cost_model == "dall-e-3"
    new.close()


def test_console_secrets_lists_image_key(tmp_path: Path) -> None:
    from yett.web.consoledata import secrets_status

    (tmp_path / "ws").mkdir()
    app = App(
        provider=FakeProvider([text_result("ok")]),
        cfg=_cfg(tmp_path),
        state_dir=tmp_path / "st",
        secrets=InMemorySecretStore({"image_key": SECRET_VALUE}),
        image_fn=_fake_image,
        clock=_clock(),
    )
    rows = secrets_status(app)
    names = {r["name"]: r for r in rows}
    assert "image_key" in names
    assert names["image_key"]["isset"] is True
    assert SECRET_VALUE not in json.dumps(rows)
    app.close()
