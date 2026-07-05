"""Integration: duyệt approval qua web UI (poll /api/pending → /api/approve → turn tiếp tục)."""

from __future__ import annotations

import itertools
import json
import threading
import time
import urllib.request
from pathlib import Path

from yett.app import App
from yett.approvals import ApprovalCenter
from yett.config.models import BudgetCfg, HarnessCfg, ProviderCfg, SandboxCfg, SecurityCfg, ToolRule
from yett.provider.fake import FakeProvider, text_result, tool_result
from yett.secrets.backends import InMemorySecretStore
from yett.web.server import serve


def _clock():
    c = itertools.count(1)
    return lambda: float(next(c))


def _post(url: str, obj: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(obj).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def _run_with_decision(tmp_path: Path, port: int, approve: bool) -> dict:
    (tmp_path / "ws").mkdir()
    sec = SecurityCfg(allowlist=[
        ToolRule(tool="exec", arg_patterns={"cmd": "^echo"}, effect="need_approval")
    ])
    cfg = HarnessCfg(
        provider=ProviderCfg(name="fake", model="fake-1"), workspace_root=tmp_path / "ws",
        sandbox=SandboxCfg(backend="local"), security=sec, budget=BudgetCfg(max_loop_iterations=4),
    )
    provider = FakeProvider([
        tool_result("c1", "exec", {"cmd": "echo hi"}),
        text_result("Kết thúc turn."),
    ])
    app = App(provider=provider, cfg=cfg, state_dir=tmp_path / "st",
              secrets=InMemorySecretStore(), clock=_clock())
    center = ApprovalCenter(default_timeout=10.0)
    httpd = serve(app, port=port, center=center)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.3)
    base = f"http://127.0.0.1:{port}"
    result: dict = {}

    def do_chat():
        result["r"] = _post(f"{base}/api/chat", {"message": "chạy echo"})

    th = threading.Thread(target=do_chat)
    th.start()

    decided = False
    for _ in range(80):
        pend = json.loads(urllib.request.urlopen(f"{base}/api/pending", timeout=5).read())["pending"]
        if pend:
            assert pend[0]["tool"] == "exec"
            _post(f"{base}/api/approve", {"id": pend[0]["id"], "approved": approve})
            decided = True
            break
        time.sleep(0.05)
    th.join(timeout=10)
    httpd.shutdown()
    app.close()
    assert decided, "không thấy pending approval"
    return result["r"]


def test_web_approval_approve(tmp_path: Path) -> None:
    r = _run_with_decision(tmp_path, 8811, approve=True)
    assert r["status"] == "done"
    # tool đã chạy (approve) → span exec không phải denied; ở đây kiểm turn hoàn tất
    assert "Kết thúc" in r["text"]


def test_web_approval_reject(tmp_path: Path) -> None:
    r = _run_with_decision(tmp_path, 8812, approve=False)
    assert r["status"] == "done"  # turn vẫn hoàn tất, nhưng tool bị từ chối
