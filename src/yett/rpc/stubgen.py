"""Sinh stub yett_tools.py từ registry (spec P2 WP2.5). Script gọi tool qua stub này."""

from __future__ import annotations

from yett.provider.base import ToolSchema


def generate_stub(schemas: list[ToolSchema]) -> str:
    """Sinh module Python: mỗi tool → một hàm gọi RPC. Chỉ tool được phép (subagent toolset)."""
    lines = [
        '"""Auto-generated tool stubs. Gọi tool qua RPC (mỗi call xuyên Policy Gate)."""',
        "import json, os, socket",
        "",
        "def _rpc(tool, args):",
        "    # bản production: gửi qua Unix socket HX_RPC_SOCKET; bản test: injected _CALL",
        "    return _CALL(tool, args)  # noqa: F821",
        "",
    ]
    for s in schemas:
        params = list(s.parameters.get("properties", {}).keys())
        sig = ", ".join(params)
        arg_dict = ", ".join(f'"{p}": {p}' for p in params)
        lines += [
            f"def {s.name}({sig}):",
            f'    """{s.description}"""',
            f"    return _rpc({s.name!r}, {{{arg_dict}}})",
            "",
        ]
    return "\n".join(lines)
