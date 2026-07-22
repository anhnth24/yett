"""CLI entrypoint (spec P0-P1 §1). Lệnh: yett.

Lệnh:
  yett --version
  yett demo            chạy một turn mẫu offline (FakeProvider) — minh hoạ end-to-end
  yett traces list|get chạy trên state dir
  yett usage           tổng chi theo provider
  yett memory list|approve|reject  duyệt đề xuất MEMORY.md (staging → merge)
  yett vpn connect <profile>  operator foreground VPN owner (Ctrl+C disconnects)
Các lệnh nối provider thật (chat) cần config + secret; khung sẵn ở đây.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path

from yett import __version__


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="yett", description="agent harness (fail-closed, on-prem)")
    p.add_argument("--version", action="version", version=f"yett {__version__}")
    sub = p.add_subparsers(dest="command")

    setup = sub.add_parser("setup", help="wizard cài đặt từng bước (provider, key, project)")
    setup.add_argument("--config", default="config/harness.yaml")

    serve = sub.add_parser("serve", help="mở giao diện chat web (localhost)")
    serve.add_argument("--config", default="config/harness.yaml")
    serve.add_argument("--pricing", default="config/pricing.yaml")
    serve.add_argument("--state", default="state")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--open", action="store_true", help="tự mở trình duyệt")

    sub.add_parser("doctor", help="kiểm tra máy có đủ công cụ chưa (thiếu thì chỉ cách cài)")
    sub.add_parser("demo", help="chạy một turn mẫu offline (FakeProvider)")

    chat = sub.add_parser("chat", help="hội thoại với agent (cần config + provider)")
    chat.add_argument("message")
    chat.add_argument("--config", default="config/harness.yaml")
    chat.add_argument("--pricing", default="config/pricing.yaml")
    chat.add_argument("--state", default="state")
    chat.add_argument("--session", default="main")

    traces = sub.add_parser("traces", help="xem trace")
    traces.add_argument("action", choices=["list", "get"], nargs="?", default="list")
    traces.add_argument("trace_id", nargs="?")
    traces.add_argument("--state", default="state")

    usage = sub.add_parser("usage", help="tổng chi phí")
    usage.add_argument("--by", choices=["provider", "model", "day"], default="provider")
    usage.add_argument("--state", default="state")

    memory = sub.add_parser(
        "memory",
        help="duyệt đề xuất MEMORY.md (list|approve|reject) — agent chỉ ghi staging",
    )
    mem_sub = memory.add_subparsers(dest="memory_action")
    mem_list = mem_sub.add_parser("list", help="liệt kê đề xuất đang chờ duyệt")
    mem_list.add_argument("--config", default="config/harness.yaml")
    mem_list.add_argument(
        "--workspace", default=None,
        help="workspace root (mặc định lấy từ config)",
    )
    mem_approve = mem_sub.add_parser("approve", help="duyệt đề xuất → append vào MEMORY.md")
    mem_approve.add_argument("id", help="id đề xuất (32 hex)")
    mem_approve.add_argument("--config", default="config/harness.yaml")
    mem_approve.add_argument("--workspace", default=None)
    mem_reject = mem_sub.add_parser("reject", help="từ chối đề xuất (xóa staging)")
    mem_reject.add_argument("id", help="id đề xuất (32 hex)")
    mem_reject.add_argument("--config", default="config/harness.yaml")
    mem_reject.add_argument("--workspace", default=None)

    vpn = sub.add_parser(
        "vpn",
        help="operator: foreground connect VPN profile (Ctrl+C disconnects owned process)",
    )
    vpn.add_argument("action", choices=["connect", "disconnect", "status"])
    vpn.add_argument("profile", help="tên profile trong remote.vpn_profiles")
    vpn.add_argument("--config", default="config/harness.yaml")
    return p


def non_loopback_warning(host: str) -> str | None:
    """Trả cảnh báo nếu `--host` bind ra ngoài loopback (127.0.0.1/localhost/::1), None
    nếu không. Server không có auth (chỉ Host/Origin check chống DNS-rebinding cho
    localhost) — bind ra LAN/0.0.0.0 phơi API cho cả mạng, cần tường lửa/reverse-proxy
    có xác thực đứng trước."""
    from yett.web.server import LOOPBACK_HOSTS

    if host in LOOPBACK_HOSTS:
        return None
    return (
        f"[yett] CẢNH BÁO: --host={host} không phải localhost — API sẽ lộ ra ngoài máy "
        "này KHÔNG có xác thực (chỉ kiểm Host/Origin, không phải auth). Chỉ dùng sau "
        "tường lửa/VPN hoặc reverse-proxy có xác thực đứng trước; khuyến nghị SSH tunnel "
        "(`ssh -L <port>:127.0.0.1:<port> host`) thay vì bind ra ngoài trực tiếp."
    )


def _cmd_serve(args) -> int:
    from yett.app import App, build_app
    from yett.config.loader import load_config
    from yett.errors import YettError
    from yett.secrets.resolve import build_secret_store
    from yett.web.server import serve_forever

    if not Path(args.config).exists():
        print(f"[yett] chưa có config {args.config}. Chạy 'yett setup' trước.", file=sys.stderr)
        return 1
    warning = non_loopback_warning(args.host)
    if warning:
        print(warning, file=sys.stderr)
    pricing = args.pricing if Path(args.pricing).exists() else None
    def rebuild() -> App:
        # Hot-reload: đọc lại config vừa lưu + dựng App mới (server swap dưới chat_lock).
        secrets2 = build_secret_store(load_config(args.config).secret_backend)
        return build_app(args.config, secrets2, state_dir=Path(args.state), pricing_path=pricing)

    try:
        app = rebuild()
    except YettError as e:
        print(f"[yett] lỗi khởi động: {e}", file=sys.stderr)
        return 1
    serve_forever(app, host=args.host, port=args.port, open_browser=args.open,
                  config_path=args.config, rebuild=rebuild)
    return 0


def _cmd_setup(args) -> int:
    from yett.secrets.file_store import FileSecretStore
    from yett.setup_wizard import run_wizard

    cfg_path = Path(args.config)
    store = FileSecretStore()
    run_wizard(
        prompt=lambda msg: input(msg),
        emit=lambda msg: print(msg),
        config_path=cfg_path,
        secret_setter=store.set,
        existing_config=cfg_path.exists(),
    )
    return 0


def _cmd_demo() -> int:
    import itertools

    from yett.app import App
    from yett.config.models import BudgetCfg, HarnessCfg, ProviderCfg, SandboxCfg
    from yett.provider.fake import FakeProvider, text_result

    tmp = Path(".yett-demo")
    (tmp / "ws").mkdir(parents=True, exist_ok=True)
    c = itertools.count(1)
    cfg = HarnessCfg(
        provider=ProviderCfg(name="fake", model="fake-1"),
        workspace_root=tmp / "ws",
        sandbox=SandboxCfg(backend="local"),
        budget=BudgetCfg(max_loop_iterations=3),
    )
    app = App(
        provider=FakeProvider([text_result("Chào anh! Em là yett, sẵn sàng làm việc.")]),
        cfg=cfg, state_dir=tmp / "state", clock=lambda: float(next(c)),
    )
    res = asyncio.run(app.chat("xin chào", session_key="main"))
    app.close()
    print(f"[yett demo] status={res.status} trace={res.trace_id}")
    print(f"[agent] {res.text}")
    return 0


def _cmd_traces(args) -> int:
    from yett.obs.spanstore import SpanStore

    db = Path(args.state) / "traces.db"
    if not db.exists():
        print(f"chưa có traces tại {db}", file=sys.stderr)
        return 1
    store = SpanStore(db)
    if args.action == "list":
        for t in store.list_traces():
            print(f"{t['trace_id']}  {t['name']}  status={t['attrs'].get('status','?')}")
    elif args.action == "get" and args.trace_id:
        for s in store.get_trace(args.trace_id):
            print(f"  [{s['kind']}] {s['name']}  {s['attrs']}")
    store.close()
    return 0


def _cmd_usage(args) -> int:
    from yett.obs import cost
    from yett.obs.spanstore import SpanStore

    db = Path(args.state) / "traces.db"
    if not db.exists():
        print(f"chưa có traces tại {db}", file=sys.stderr)
        return 1
    store = SpanStore(db)
    all_spans = []
    for t in store.list_traces(limit=1000):
        all_spans.extend(store.get_trace(t["trace_id"]))
    agg = cost.aggregate(all_spans, by=args.by)
    for key, v in sorted(agg.items()):
        print(f"{key}: ${v['cost_usd']:.4f}  ({v['calls']} calls)")
    store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    # UTF-8 cho stdout/stderr: tránh crash 'charmap'/cp1252 khi in tiếng Việt trên Windows
    # (console codepage hoặc khi redirect ra file). Không tác dụng phụ trên Linux/Docker (đã UTF-8).
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    if not args.command:
        build_parser().print_help()
        return 0
    if args.command == "doctor":
        from yett.doctor import run_doctor

        run_doctor()
        return 0
    if args.command == "setup":
        return _cmd_setup(args)
    if args.command == "serve":
        return _cmd_serve(args)
    if args.command == "demo":
        return _cmd_demo()
    if args.command == "traces":
        return _cmd_traces(args)
    if args.command == "usage":
        return _cmd_usage(args)
    if args.command == "chat":
        return _cmd_chat(args)
    if args.command == "memory":
        return _cmd_memory(args)
    if args.command == "vpn":
        return _cmd_vpn(args)
    return 0


def _resolve_workspace(config: str, workspace: str | None) -> Path | None:
    """Workspace cho memory CLI: --workspace thắng; không thì đọc config; thiếu → None."""
    if workspace:
        return Path(workspace)
    from yett.config.loader import load_config

    cfg_path = Path(config)
    if not cfg_path.exists():
        return None
    return Path(load_config(cfg_path).workspace_root)


def _cmd_memory(args) -> int:
    from yett.errors import YettError
    from yett.memory.review_gate import MemoryReviewGate, is_valid_proposal_id

    action = getattr(args, "memory_action", None)
    if not action:
        print("dùng: yett memory list|approve|reject", file=sys.stderr)
        return 1
    try:
        root = _resolve_workspace(args.config, args.workspace)
    except (YettError, OSError, ValueError) as exc:
        print(f"[yett] không đọc được workspace memory: {exc}", file=sys.stderr)
        return 1
    if root is None:
        print(
            f"[yett] chưa có config {args.config}. Truyền --workspace hoặc chạy 'yett setup'.",
            file=sys.stderr,
        )
        return 1
    gate = MemoryReviewGate(root)
    try:
        if action == "list":
            pending = gate.list_pending()
            if not pending:
                print("(không có đề xuất memory đang chờ)")
                return 0
            for pid, content in pending:
                preview = "".join(ch if ch.isprintable() else " " for ch in content)
                if len(preview) > 120:
                    preview = preview[:117] + "..."
                print(f"{pid}\t{preview}")
            return 0
        if action in {"approve", "reject"} and not is_valid_proposal_id(args.id):
            print("[yett] id đề xuất không hợp lệ (cần đúng 32 ký tự hex thường)", file=sys.stderr)
            return 1
        if action == "approve":
            if not gate.approve(args.id, actor="operator:cli"):
                print(f"[yett] không tìm thấy đề xuất id={args.id}", file=sys.stderr)
                return 1
            print(f"[yett] đã duyệt {args.id} → MEMORY.md")
            return 0
        if action == "reject":
            if not gate.reject(args.id, actor="operator:cli"):
                print(f"[yett] không tìm thấy đề xuất id={args.id}", file=sys.stderr)
                return 1
            print(f"[yett] đã từ chối {args.id}")
            return 0
    except (YettError, OSError, ValueError) as exc:
        print(f"[yett] memory {action} thất bại an toàn: {exc}", file=sys.stderr)
        return 1
    return 1


def _cmd_chat(args) -> int:
    from yett.app import build_app
    from yett.config.loader import load_config
    from yett.errors import YettError
    from yett.secrets.resolve import build_secret_store

    if not Path(args.config).exists():
        print(f"[yett] chưa có config {args.config}. Chạy 'yett setup' để tạo.", file=sys.stderr)
        return 1
    pricing = args.pricing if Path(args.pricing).exists() else None
    try:
        secrets = build_secret_store(load_config(args.config).secret_backend)
        app = build_app(args.config, secrets, state_dir=Path(args.state), pricing_path=pricing)
    except YettError as e:
        print(f"[yett] lỗi khởi động: {e}", file=sys.stderr)
        return 1
    try:
        res = asyncio.run(app.chat(args.message, session_key=args.session))
        print(f"[agent] {res.text}")
        print(f"[trace {res.trace_id} · {res.status} · {res.iterations} vòng]", file=sys.stderr)
    finally:
        app.close()
    return 0


def _cmd_vpn(args) -> int:
    """Operator CLI: foreground owner for a profile-allowlisted VPN.

    Không in secret; lỗi UserFacingError đã redact. Readiness chỉ dựa vào marker CLI + process
    stability, không claim route/DNS thật. status/disconnect từ process khác fail closed thay vì
    nhận nuôi PID không còn chứng minh được ownership.
    """
    from yett.config.loader import load_config
    from yett.errors import UserFacingError, YettError
    from yett.secrets.resolve import build_secret_store
    from yett.security.filters import redact
    from yett.tools.remote.vpn import SubprocessVpnRunner, VpnManager

    if not Path(args.config).exists():
        print(f"[yett] chưa có config {args.config}. Chạy 'yett setup' trước.", file=sys.stderr)
        return 1
    try:
        cfg = load_config(args.config)
        profiles = dict(cfg.remote.vpn_profiles)
        if not profiles:
            print("[yett] remote.vpn_profiles trống — không có profile để dùng.", file=sys.stderr)
            return 1
        if args.profile not in profiles:
            print(
                f"[yett] profile '{args.profile}' không nằm trong allowlist config.",
                file=sys.stderr,
            )
            return 1
        secrets = build_secret_store(cfg.secret_backend)
        mgr = VpnManager(SubprocessVpnRunner(profiles), secrets, profiles)
    except YettError as e:
        print(f"[yett] {redact(str(e))}", file=sys.stderr)
        return 1

    async def _run() -> int:
        try:
            if args.action == "connect":
                await mgr.ensure(args.profile)
                print(
                    f"[yett] VPN '{args.profile}' đã kết nối; tiến trình này đang sở hữu tunnel. "
                    "Giữ terminal mở, nhấn Ctrl+C để ngắt."
                )
                while await mgr.status(args.profile):
                    await asyncio.sleep(1)
                print(
                    f"[yett] VPN '{args.profile}' đã dừng ngoài dự kiến.",
                    file=sys.stderr,
                )
                return 1
            print(
                "[yett] status/disconnect không nhận nuôi PID từ lần chạy CLI khác. "
                "Kiểm tra/ngắt bằng Ctrl+C tại terminal `yett vpn connect` đang sở hữu tunnel.",
                file=sys.stderr,
            )
            return 1
        except asyncio.CancelledError:
            try:
                await asyncio.shield(mgr.disconnect(args.profile))
            except UserFacingError:
                pass
            raise
        except UserFacingError as e:
            print(f"[yett] {redact(str(e))}", file=sys.stderr)
            return 1
        finally:
            mgr.close()

    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def _stop(_signum, _frame) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _stop)
    try:
        try:
            return asyncio.run(_run())
        except KeyboardInterrupt:
            print(f"\n[yett] VPN '{args.profile}' đã ngắt")
            return 130
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    raise SystemExit(main())
