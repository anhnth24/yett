"""Integration: các endpoint console web mới (meta/secrets/subagents/projects/jobs/trace/cancel).

Offline, FakeProvider, sandbox=local. Dựng App có: project map host/db, 1 subagent def,
1 secret đã đặt + 2 secret thiếu — để kiểm consoledata trả dữ liệu THẬT."""

from __future__ import annotations

import itertools
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

from yett.app import App
from yett.config.models import (
    BudgetCfg,
    DbProfileCfg,
    HarnessCfg,
    HostCfg,
    ProjectCfg,
    ProviderCfg,
    RemoteCfg,
    SandboxCfg,
    SearchCfg,
)
from yett.provider.fake import FakeProvider, text_result
from yett.secrets.backends import InMemorySecretStore
from yett.web.server import serve


def _clock():
    c = itertools.count(1)
    return lambda: float(next(c))


def _app(tmp_path: Path) -> App:
    ws = tmp_path / "ws"
    (ws / "agents").mkdir(parents=True, exist_ok=True)
    (ws / "agents" / "researcher.md").write_text(
        "---\nname: researcher\ndescription: Tìm & tổng hợp\ntoolset: [read_file]\n---\nBạn là researcher.",
        encoding="utf-8",
    )
    cfg = HarnessCfg(
        provider=ProviderCfg(name="fake", model="fake-1"),
        workspace_root=ws,
        sandbox=SandboxCfg(backend="local"),
        budget=BudgetCfg(max_loop_iterations=3),
        projects={"go-learn": ProjectCfg(path=ws, hosts=["uat-01"], databases=["uat"])},
        databases={"uat": DbProfileCfg(driver="postgres", dsn_secret="uat_dsn")},
        remote=RemoteCfg(hosts={"uat-01": HostCfg(address="10.0.0.5", auth="keyfile:ssh_uat01")}),
        search=SearchCfg(api_key_secret="search_key"),
    )
    secrets = InMemorySecretStore()
    secrets.set("search_key", "SUPER_SECRET_VALUE_ZZZ")  # đã đặt; uat_dsn + ssh_uat01 CỐ Ý thiếu
    return App(provider=FakeProvider([text_result("ok")]), cfg=cfg,
               state_dir=tmp_path / "st", secrets=secrets, clock=_clock())


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, json.loads(r.read())


def _post(url, obj):
    req = urllib.request.Request(url, data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_console_endpoints(tmp_path: Path) -> None:
    app = _app(tmp_path)
    httpd = serve(app, host="127.0.0.1", port=0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        # meta: dữ liệu thật từ config
        _, m = _get(base + "/api/meta")
        assert m["provider"] == "fake" and m["counts"]["projects"] == 1
        assert m["counts"]["hosts"] == 1 and "read_file" in m["tools"]

        # secrets: chỉ TÊN + đã-đặt/thiếu, KHÔNG có value
        _, s = _get(base + "/api/secrets")
        by = {x["name"]: x["isset"] for x in s["secrets"]}
        assert by.get("search_key") is True
        assert by.get("uat_dsn") is False and by.get("ssh_uat01") is False
        assert "SUPER_SECRET_VALUE_ZZZ" not in json.dumps(s)  # value KHÔNG lọt ra

        # subagents: đọc từ ws/agents/*.md
        _, sa = _get(base + "/api/subagents")
        assert any(x["name"] == "researcher" and "read_file" in x["toolset"]
                   for x in sa["subagents"])

        # projects: map host/db
        _, p = _get(base + "/api/projects")
        go = next(x for x in p["projects"] if x["name"] == "go-learn")
        assert go["hosts"] == ["uat-01"] and go["databases"] == ["uat"]

        # trace không tồn tại → spans rỗng (không lỗi)
        _, tr = _get(base + "/api/trace?id=khongco")
        assert tr["spans"] == []

        # cancel khi không có turn → ok False (không lỗi)
        _, c = _post(base + "/api/cancel", {"session": "main"})
        assert c["ok"] is False

        # jobs: tạo (every) → list → run → delete; spec sai → 400
        assert _post(base + "/api/jobs",
                     {"id": "j1", "spec": "every:3600", "prompt": "p"})[1]["ok"] is True
        assert _post(base + "/api/jobs", {"id": "b", "spec": "rác", "prompt": "p"})[0] == 400
        _, jl = _get(base + "/api/jobs")
        assert any(j["id"] == "j1" for j in jl["jobs"])
        assert _post(base + "/api/jobs/run", {"id": "j1"})[1]["ok"] is True
        assert _post(base + "/api/jobs/delete", {"id": "j1"})[1]["ok"] is True
    finally:
        httpd.shutdown()
        app.close()


def test_chat_project_context_injected(tmp_path: Path) -> None:
    """Chat kèm project → app.chat inject bối cảnh project (turn vẫn done qua FakeProvider)."""
    app = _app(tmp_path)
    httpd = serve(app, host="127.0.0.1", port=0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        code, j = _post(f"http://127.0.0.1:{port}/api/chat",
                        {"message": "báo cáo", "session": "main", "project": "go-learn"})
        assert code == 200 and j["status"] == "done"
    finally:
        httpd.shutdown()
        app.close()
