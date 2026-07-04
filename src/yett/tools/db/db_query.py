"""db_query + db_config (spec P2 §3). Read-only 4 lớp; connection string là secret.

L2 (session read-only) + L3 (sqlguard) enforce ở đây; L1 (DB user) là vận hành;
L4 (approval write) là db_write riêng (không mở trong tool này).
"""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel

from yett.errors import UserFacingError
from yett.tools.base import ToolCtx, ToolResult
from yett.tools.db.sqlguard import classify_sql


class DbProfile(BaseModel):
    driver: Literal["postgres", "mysql", "sqlserver", "sqlite"]
    dsn_secret: str  # connection string là secret; model chỉ thấy tên profile
    readonly: bool = True


class DbExecutor(Protocol):
    async def query_readonly(self, dsn: str, driver: str, sql: str) -> list[dict]:
        """Chạy SQL ở session READ-ONLY (L2). dsn không được log."""
        ...


class DbQueryTool:
    """Chạy một câu SELECT read-only trên DB profile (không ALTER/DELETE/UPDATE)."""

    name = "db_query"
    schema = {
        "type": "object",
        "properties": {"profile": {"type": "string"}, "sql": {"type": "string"}},
        "required": ["profile", "sql"],
    }

    def __init__(self, profiles: dict[str, DbProfile], executor: DbExecutor, secrets) -> None:
        self._profiles = profiles
        self._exec = executor
        self._secrets = secrets

    def validate(self, args: dict) -> None:
        if not args.get("profile") or not args.get("sql"):
            raise UserFacingError("cần 'profile' và 'sql'")

    async def run(self, args: dict, ctx: ToolCtx) -> ToolResult:
        prof = self._profiles.get(args["profile"])
        if prof is None:
            return ToolResult.error(f"profile DB '{args['profile']}' chưa cấu hình")
        # L3 SQL classifier (Gate cũng chạy, đây là defense-in-depth)
        dec = classify_sql(args["sql"], prof.driver)
        if dec.verdict != "allow":
            return ToolResult.error(f"[DENIED] {dec.reason}")
        dsn = self._secrets.get(prof.dsn_secret)  # lấy tại điểm dùng, không log
        rows = await self._exec.query_readonly(dsn, prof.driver, args["sql"])
        return ToolResult.success(_format(rows))


def _format(rows: list[dict]) -> str:
    if not rows:
        return "(0 dòng)"
    head = list(rows[0].keys())
    lines = [" | ".join(head)]
    for r in rows[:100]:
        lines.append(" | ".join(str(r.get(h, "")) for h in head))
    if len(rows) > 100:
        lines.append(f"... (+{len(rows)-100} dòng)")
    return "\n".join(lines)
