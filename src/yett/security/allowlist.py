"""Allowlist per-deployment (spec P0-P1 §3.5). Match tool + regex trên args."""

from __future__ import annotations

import re

from yett.config.models import ToolRule
from yett.security.gate import Decision


def match_allowlist(tool: str, args: dict, rules: list[ToolRule]) -> Decision | None:
    """Trả Decision (allow/need_approval) nếu có rule khớp; None nếu không rule nào khớp."""
    for rule in rules:
        if rule.tool not in (tool, "*"):
            continue
        if _args_match(args, rule.arg_patterns):
            if rule.effect == "allow":
                return Decision("allow", f"allowlist: {rule.tool}", f"ALLOW_{rule.tool}")
            return Decision("need_approval", f"allowlist: {rule.tool}", f"ALLOW_{rule.tool}")
    return None


def _args_match(args: dict, patterns: dict[str, str]) -> bool:
    for key, pat in patterns.items():
        val = str(args.get(key, ""))
        if not re.search(pat, val):
            return False
    return True
