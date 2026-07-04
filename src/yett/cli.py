"""CLI entrypoint (spec P0-P1 §1). Lệnh: yett.

Skeleton: khung lệnh, chưa nối core. Các lệnh thật (chat, traces, usage, memory,
project) implement ở WP1.7.
"""

from __future__ import annotations

import argparse
import sys

from yett import __version__


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="yett", description="agent harness (fail-closed, on-prem)")
    p.add_argument("--version", action="version", version=f"yett {__version__}")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("chat", help="[P1] hội thoại với agent")
    traces = sub.add_parser("traces", help="[P1] xem trace")
    traces.add_argument("action", choices=["list", "get", "follow"], nargs="?", default="list")
    sub.add_parser("usage", help="[P1] tổng chi phí theo provider/model/ngày")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.command:
        build_parser().print_help()
        return 0
    # Skeleton: các lệnh chưa nối core loop (Phase 1).
    print(f"[yett] lệnh '{args.command}' chưa được implement (skeleton Phase 0).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
