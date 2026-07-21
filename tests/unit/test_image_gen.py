"""Unit tests for image_gen tool: path safety, MIME, secret redaction, cost attrs."""

from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest

from yett.config.models import PriceRow
from yett.errors import UserFacingError
from yett.obs.cost import compute_call_cost
from yett.secrets.backends import InMemorySecretStore
from yett.tools.assist.image_backend import GeneratedImage, decode_b64_image
from yett.tools.assist.image_gen import ImageGenTool
from yett.tools.projects import ProjectScope

# 1×1 PNG
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQ"
    "AAAABJRU5ErkJggg=="
)
_PNG_B64 = base64.b64encode(_PNG).decode()
SECRET = "IMAGE_SECRET_VALUE_ZZZ_DO_NOT_LEAK"


class _Ctx:
    session_key = "t"


def _scope(ws: Path) -> ProjectScope:
    return ProjectScope(ws, {})


async def _png_backend(prompt: str, key: str, *, size: str = "1024x1024") -> GeneratedImage:
    assert key == SECRET
    assert size in {"256x256", "512x512", "1024x1024", "1792x1024", "1024x1792"}
    return GeneratedImage(data=_PNG, mime="image/png", revised_prompt=f"rev:{prompt[:20]}")


async def test_image_gen_writes_png_and_records_cost(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    tool = ImageGenTool(
        _png_backend,
        InMemorySecretStore({"image_key": SECRET}),
        "image_key",
        scope=_scope(ws),
        cost_usd=0.04,
        cost_provider="openai_compat",
        cost_model="dall-e-3",
    )
    res = await tool.run({"prompt": "a cat", "path": "images/cat.png"}, _Ctx())
    out = ws / "images" / "cat.png"
    assert out.is_file()
    assert out.read_bytes() == _PNG
    assert SECRET not in res.content
    assert res.span_attrs["cost_usd"] == 0.04
    assert res.span_attrs["provider"] == "openai_compat"
    assert res.span_attrs["model"] == "dall-e-3"
    assert res.span_attrs["images"] == 1
    assert res.span_attrs["bytes"] == len(_PNG)
    assert SECRET not in str(res.span_attrs)


@pytest.mark.parametrize(
    "bad_path",
    [
        "../escape.png",
        "../../etc/passwd.png",
        "/tmp/out.png",
        "images/../../outside.png",
        "images/foo.txt",
        "images/has space.png",
        "images/foo;rm.png",
        "",
    ],
)
async def test_image_gen_rejects_unsafe_paths(tmp_path: Path, bad_path: str) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    called = False

    async def boom(prompt: str, key: str, *, size: str = "1024x1024") -> GeneratedImage:
        nonlocal called
        called = True
        return GeneratedImage(data=_PNG, mime="image/png")

    tool = ImageGenTool(
        boom, InMemorySecretStore({"k": SECRET}), "k", scope=_scope(ws)
    )
    with pytest.raises(UserFacingError):
        tool.validate({"prompt": "x", "path": bad_path})
    assert called is False


async def test_image_gen_rejects_path_traversal_via_symlink_root(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = ws / "link"
    link.symlink_to(outside)

    async def backend(prompt: str, key: str, *, size: str = "1024x1024") -> GeneratedImage:
        return GeneratedImage(data=_PNG, mime="image/png")

    tool = ImageGenTool(
        backend, InMemorySecretStore({"k": SECRET}), "k", scope=_scope(ws)
    )
    with pytest.raises(UserFacingError, match="ngoài workspace|bị từ chối"):
        await tool.run({"prompt": "x", "path": "link/evil.png"}, _Ctx())
    assert not (outside / "evil.png").exists()


async def test_image_gen_refuses_overwrite_symlink(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / "images").mkdir(parents=True)
    target = ws / "images" / "keep.png"
    target.write_bytes(b"KEEP")
    link = ws / "images" / "out.png"
    link.symlink_to("keep.png")  # symlink trong workspace — không follow để ghi đè target

    tool = ImageGenTool(
        _png_backend, InMemorySecretStore({"k": SECRET}), "k", scope=_scope(ws)
    )
    with pytest.raises(UserFacingError, match="symlink"):
        await tool.run({"prompt": "x", "path": "images/out.png"}, _Ctx())
    assert target.read_bytes() == b"KEEP"
    assert link.is_symlink()


async def test_image_gen_rejects_mime_extension_mismatch(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()

    async def backend(prompt: str, key: str, *, size: str = "1024x1024") -> GeneratedImage:
        return GeneratedImage(data=_PNG, mime="image/png")

    tool = ImageGenTool(
        backend, InMemorySecretStore({"k": SECRET}), "k", scope=_scope(ws)
    )
    with pytest.raises(UserFacingError, match="đuôi file không khớp"):
        await tool.run({"prompt": "x", "path": "images/out.jpg"}, _Ctx())


async def test_image_gen_redacts_key_reflected_in_revised_prompt(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()

    async def backend(prompt: str, key: str, *, size: str = "1024x1024") -> GeneratedImage:
        return GeneratedImage(
            data=_PNG, mime="image/png", revised_prompt=f"used key={key}"
        )

    tool = ImageGenTool(
        backend, InMemorySecretStore({"k": SECRET}), "k", scope=_scope(ws)
    )
    res = await tool.run({"prompt": "x", "path": "a.png"}, _Ctx())
    assert SECRET not in res.content
    assert "[REDACTED]" in res.content


async def test_image_gen_backend_errors_do_not_leak_key(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()

    async def unsafe(prompt: str, key: str, *, size: str = "1024x1024") -> GeneratedImage:
        raise RuntimeError(f"headers had {key}")

    tool = ImageGenTool(
        unsafe, InMemorySecretStore({"k": SECRET}), "k", scope=_scope(ws)
    )
    with pytest.raises(UserFacingError, match="backend lỗi") as exc:
        await tool.run({"prompt": "x", "path": "a.png"}, _Ctx())
    assert SECRET not in str(exc.value)


async def test_image_gen_missing_secret_raises_user_facing(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()

    async def boom(prompt: str, key: str, *, size: str = "1024x1024") -> GeneratedImage:
        raise AssertionError("must not call")

    tool = ImageGenTool(boom, InMemorySecretStore(), "missing", scope=_scope(ws))
    with pytest.raises(UserFacingError, match="chưa đặt"):
        await tool.run({"prompt": "x", "path": "a.png"}, _Ctx())


@pytest.mark.parametrize("size", [0, "huge", True, "2048x2048"])
async def test_image_gen_rejects_invalid_size(tmp_path: Path, size) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    tool = ImageGenTool(
        _png_backend, InMemorySecretStore({"k": SECRET}), "k", scope=_scope(ws)
    )
    with pytest.raises(UserFacingError, match="size"):
        tool.validate({"prompt": "q", "path": "a.png", "size": size})


def test_decode_b64_rejects_malformed_and_oversize() -> None:
    with pytest.raises(UserFacingError, match="base64"):
        decode_b64_image("@@@not-b64@@@")
    with pytest.raises(UserFacingError, match="không phải ảnh"):
        decode_b64_image(base64.b64encode(b"not-an-image").decode())
    with pytest.raises(UserFacingError, match="vượt"):
        decode_b64_image(base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 100).decode(), max_bytes=20)
    img = decode_b64_image(_PNG_B64)
    assert img.mime == "image/png"
    assert img.data == _PNG


def test_compute_call_cost_image_gen() -> None:
    pricing = {"openai_compat": {"dall-e-3": PriceRow(per_call=0.04)}}
    assert compute_call_cost("openai_compat", "dall-e-3", pricing) == 0.04
    assert compute_call_cost("openai_compat", "missing", pricing) == 0.0


async def test_atomic_write_is_regular_file_mode_600(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    tool = ImageGenTool(
        _png_backend, InMemorySecretStore({"k": SECRET}), "k", scope=_scope(ws)
    )
    await tool.run({"prompt": "x", "path": "out.png"}, _Ctx())
    mode = os.stat(ws / "out.png").st_mode & 0o777
    assert mode == 0o600
