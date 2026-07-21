"""Platform-independent backend helpers + Windows MemoryReviewGate coverage."""

from __future__ import annotations

import json
import os
import stat
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

import yett.memory.review_gate as review_mod
from yett.app import App
from yett.config.models import BudgetCfg, HarnessCfg, ProviderCfg, SandboxCfg, SecurityCfg
from yett.memory.review_gate import (
    MemoryReviewError,
    MemoryReviewGate,
    _child_under_root,
    _is_reparse_lstat,
    _is_reparse_path,
    _is_safe_entry_name,
)
from yett.provider.fake import FakeProvider, text_result
from yett.secrets.backends import InMemorySecretStore

_WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="Windows path backend only")


@pytest.mark.parametrize(
    ("name", "ok"),
    [
        ("MEMORY.md", True),
        ("review-audit.jsonl", True),
        (".yett-memory-review.lock", True),
        ("a" * 32 + ".md", True),
        ("", False),
        (".", False),
        ("..", False),
        ("../escape", False),
        ("a/b", False),
        ("a\\b", False),
        ("a\x00b", False),
        ("audit:stream", False),
        ("CON", False),
        ("nul.txt", False),
        ("COM1.log", False),
        ("LPT³", False),
        ("trailing.", False),
        ("trailing ", False),
        (" leading", False),
        ("question?.md", False),
        ("a" * 256, False),
        ("\ud800", False),
    ],
)
def test_is_safe_entry_name(name: str, ok: bool) -> None:
    assert _is_safe_entry_name(name) is ok


def test_child_under_root_rejects_escape(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    assert _child_under_root(root, "memory", "pending") == root / "memory" / "pending"
    with pytest.raises(MemoryReviewError, match="không an toàn"):
        _child_under_root(root, "..", "escape")
    with pytest.raises(MemoryReviewError, match="không an toàn"):
        _child_under_root(root, "a/b")


def test_is_reparse_lstat_detects_symlink_mode() -> None:
    mode = stat.S_IFLNK | 0o777
    st = SimpleNamespace(st_mode=mode, st_file_attributes=0)
    assert _is_reparse_lstat(st) is True  # type: ignore[arg-type]


def test_is_reparse_lstat_detects_reparse_attribute() -> None:
    mode = stat.S_IFDIR | 0o700
    st = SimpleNamespace(st_mode=mode, st_file_attributes=0x400)
    assert _is_reparse_lstat(st) is True  # type: ignore[arg-type]
    st_clear = SimpleNamespace(st_mode=mode, st_file_attributes=0)
    assert _is_reparse_lstat(st_clear) is False  # type: ignore[arg-type]


def test_is_reparse_path_real_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    assert _is_reparse_path(link) is True
    assert _is_reparse_path(target) is False


def test_windows_named_proposal_creation_is_exclusive(tmp_path: Path) -> None:
    name = "a" * 32 + ".md"
    MemoryReviewGate._windows_create_named_proposal(tmp_path, name, "original")
    with pytest.raises(FileExistsError):
        MemoryReviewGate._windows_create_named_proposal(tmp_path, name, "replacement")
    assert (tmp_path / name).read_text(encoding="utf-8") == "original"


def test_windows_atomic_replace_closes_source_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MoveFileExW rejects the default CRT handle while it is still open."""
    real_open = review_mod.os.open
    real_replace = review_mod.os.replace
    temp_fd: int | None = None

    def tracking_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal temp_fd
        if dir_fd is None:
            fd = real_open(path, flags, mode)
        else:
            fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if str(path).endswith(".tmp"):
            temp_fd = fd
        return fd

    def asserting_replace(source, destination, *args, **kwargs):
        assert temp_fd is not None
        with pytest.raises(OSError):
            os.fstat(temp_fd)
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(review_mod.os, "open", tracking_open)
    monkeypatch.setattr(review_mod.os, "replace", asserting_replace)

    snapshot = MemoryReviewGate._windows_atomic_write_at(
        tmp_path, "MEMORY.md", "atomic", expected=None
    )
    assert snapshot[3] == review_mod.hashlib.sha256(b"atomic").hexdigest()
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == "atomic"


def test_windows_read_rejects_reparse_swap_between_check_and_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    controlled = tmp_path / "controlled"
    controlled.write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.write_text("outside secret", encoding="utf-8")
    real_open = review_mod.os.open
    swapped = False

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if Path(path) == controlled and not swapped:
            controlled.unlink()
            controlled.symlink_to(outside)
            swapped = True
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    # Model Windows, where O_NOFOLLOW is unavailable and a CRT open follows the
    # reparse point introduced after the initial lstat.
    monkeypatch.setattr(review_mod.os, "O_NOFOLLOW", 0)
    monkeypatch.setattr(review_mod.os, "open", racing_open)
    with pytest.raises(MemoryReviewError, match="TOCTOU"):
        MemoryReviewGate._windows_read_regular_snapshot_at(tmp_path, controlled.name)


def test_windows_lock_and_unlock_same_first_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fd = os.open(tmp_path / "lock", os.O_RDWR | os.O_CREAT, 0o600)
    calls: list[tuple[int, int, int]] = []
    fake_msvcrt = SimpleNamespace(LK_LOCK=1, LK_UNLCK=2)

    def locking(locked_fd: int, mode: int, count: int) -> None:
        calls.append((mode, count, os.lseek(locked_fd, 0, os.SEEK_CUR)))

    fake_msvcrt.locking = locking
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    monkeypatch.setattr(review_mod.os, "name", "nt")
    try:
        MemoryReviewGate._lock_file(fd)
        os.lseek(fd, 1, os.SEEK_SET)
        MemoryReviewGate._unlock_file(fd)
        assert calls == [(fake_msvcrt.LK_LOCK, 1, 0), (fake_msvcrt.LK_UNLCK, 1, 0)]
        os.lseek(fd, 0, os.SEEK_SET)
        assert os.read(fd, 1) == b"\0"
    finally:
        os.close(fd)


@_WINDOWS_ONLY
def test_windows_review_gate_propose_list_approve_reject(tmp_path: Path) -> None:
    gate = MemoryReviewGate(tmp_path)
    approved = gate.propose("windows approve me")
    rejected = gate.propose("windows reject me")
    pending = dict(gate.list_pending())
    assert approved in pending and rejected in pending
    assert gate.approve(approved)
    assert "windows approve me" in (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    assert gate.reject(rejected)
    assert dict(gate.list_pending()) == {}
    assert "windows reject me" not in (tmp_path / "MEMORY.md").read_text(encoding="utf-8")


@_WINDOWS_ONLY
def test_windows_review_gate_concurrent_approvals(tmp_path: Path) -> None:
    gate = MemoryReviewGate(tmp_path)
    proposals = [gate.propose(f"win-item-{i}") for i in range(12)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda pid: MemoryReviewGate(tmp_path).approve(pid), proposals))
    assert results == [True] * len(proposals)
    memory = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    for pid in proposals:
        assert memory.count(f"yett-memory-proposal:{pid}") == 1
    assert gate.list_pending() == []


@_WINDOWS_ONLY
def test_windows_review_gate_rejects_pending_reparse(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "memory").mkdir()
    link = tmp_path / "memory" / "pending"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        # Junctions often work without Developer Mode / elevation.
        import subprocess

        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            pytest.skip(f"cannot create symlink/junction: {completed.stderr}")
    with pytest.raises(MemoryReviewError, match="an toàn"):
        MemoryReviewGate(tmp_path).propose("must not escape")
    assert list(outside.iterdir()) == []


@_WINDOWS_ONLY
def test_windows_review_gate_rejects_staged_file_symlink(tmp_path: Path) -> None:
    gate = MemoryReviewGate(tmp_path)
    gate.ensure_storage()
    victim = tmp_path / "victim"
    victim.write_text("outside secret", encoding="utf-8")
    pid = "b" * 32
    link = gate.pending_dir / f"{pid}.md"
    try:
        link.symlink_to(victim)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    with pytest.raises(MemoryReviewError, match="không an toàn"):
        gate.approve(pid)
    assert victim.read_text(encoding="utf-8") == "outside secret"
    assert not (tmp_path / "MEMORY.md").exists()


@_WINDOWS_ONLY
def test_windows_review_gate_audit_corruption_fail_closed(tmp_path: Path) -> None:
    gate = MemoryReviewGate(tmp_path, clock=lambda: 3.0)
    pid = gate.propose("before corruption")
    assert gate.approve(pid)
    audit_path = tmp_path / "memory" / "review-audit.jsonl"
    rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert [row["action"] for row in rows] == ["proposed", "approved"]
    audit_path.write_text(audit_path.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
    with pytest.raises(MemoryReviewError, match="hash-chain"):
        gate.propose("must fail closed")
    with pytest.raises(MemoryReviewError, match="hash-chain"):
        gate.list_pending()


@_WINDOWS_ONLY
def test_windows_app_initializes_memory_review_storage(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = HarnessCfg(
        provider=ProviderCfg(name="fake", model="fake-1"),
        workspace_root=ws,
        sandbox=SandboxCfg(backend="local"),
        budget=BudgetCfg(max_loop_iterations=2),
        security=SecurityCfg(allowlist=[]),
    )
    app = App(
        provider=FakeProvider([text_result("ok")]),
        cfg=cfg,
        state_dir=tmp_path / "st",
        secrets=InMemorySecretStore(),
    )
    try:
        assert isinstance(app.memory_gate, MemoryReviewGate)
        assert app.memory_gate.pending_dir.is_dir()
        assert not _is_reparse_path(app.memory_gate.pending_dir)
        pid = app.memory_gate.propose("from app init path")
        assert app.memory_gate.list_pending()[0][0] == pid
    finally:
        app.close()
