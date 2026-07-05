"""CLI entrypoint (spec P0-P1 §1). Lệnh: yett.

Lệnh:
  yett --version
  yett demo            chạy một turn mẫu offline (FakeProvider) — minh hoạ end-to-end
  yett traces list|get chạy trên state dir
  yett usage           tổng chi theo provider
Các lệnh nối provider thật (chat) cần config + secret; khung sẵn ở đây.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from yett import __version__


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="yett", description="agent harness (fail-closed, on-prem)")
    p.add_argument("--version", action="version", version=f"yett {__version__}")
    sub = p.add_subparsers(dest="command")

    setup = sub.add_parser("setup", help="wizard cài đặt từng bước (provider, key, project)")
    setup.add_argument("--config", default="config/harness.yaml")

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
    return p


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
    args = build_parser().parse_args(argv)
    if not args.command:
        build_parser().print_help()
        return 0
    if args.command == "setup":
        return _cmd_setup(args)
    if args.command == "demo":
        return _cmd_demo()
    if args.command == "traces":
        return _cmd_traces(args)
    if args.command == "usage":
        return _cmd_usage(args)
    if args.command == "chat":
        return _cmd_chat(args)
    return 0


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


if __name__ == "__main__":
    raise SystemExit(main())
