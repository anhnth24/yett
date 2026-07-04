"""Subagent definition (spec P3 §5.1). File-based; toolset PHẢI ⊆ toolset cha."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

from yett.errors import UserFacingError

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


class SubagentDef(BaseModel):
    name: str
    description: str
    system_prompt: str
    toolset: list[str] = Field(default_factory=list)
    max_iterations: int = 10
    token_budget: int = 50_000


def parse_subagent_md(text: str) -> SubagentDef:
    m = _FRONTMATTER.match(text)
    if not m:
        raise UserFacingError("subagent md thiếu frontmatter")
    import yaml

    meta = yaml.safe_load(m.group(1)) or {}
    return SubagentDef(
        name=meta["name"],
        description=meta.get("description", ""),
        system_prompt=m.group(2).strip(),
        toolset=meta.get("toolset", []),
        max_iterations=meta.get("max_iterations", 10),
        token_budget=meta.get("token_budget", 50_000),
    )


def load_subagent(agents_dir: Path, name: str, *, parent_toolset: set[str]) -> SubagentDef:
    """Load + kiểm toolset ⊆ cha. Vượt → từ chối load (không leo thang quyền)."""
    p = agents_dir / f"{name}.md"
    if not p.exists():
        raise UserFacingError(f"subagent '{name}' không tồn tại")
    sub = parse_subagent_md(p.read_text(encoding="utf-8"))
    extra = set(sub.toolset) - parent_toolset
    if extra:
        raise UserFacingError(
            f"subagent '{name}' khai toolset vượt cha: {sorted(extra)} — từ chối (không leo thang quyền)"
        )
    return sub
