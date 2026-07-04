"""Skills engine (spec P2 §4). Chuẩn agentskills.io: SKILL.md + frontmatter.

Progressive disclosure: menu chỉ name+description; body nạp khi dùng.
Precedence: workspace > managed > bundled. Skill agent-tạo qua review gate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from yett.errors import UserFacingError

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_TIERS = ["bundled", "managed", "workspace"]  # thấp → cao (cao đè thấp)


@dataclass
class Skill:
    name: str
    description: str
    version: str
    tier: str
    body: str


def parse_skill_md(text: str, tier: str) -> Skill:
    m = _FRONTMATTER.match(text)
    if not m:
        raise UserFacingError("SKILL.md thiếu frontmatter YAML")
    import yaml

    meta = yaml.safe_load(m.group(1)) or {}
    body = m.group(2).strip()
    name = meta.get("name")
    desc = meta.get("description", "")
    if not name:
        raise UserFacingError("SKILL.md thiếu 'name'")
    return Skill(name=name, description=desc, version=str(meta.get("version", "1.0")), tier=tier, body=body)


class SkillLoader:
    def __init__(self, tier_dirs: dict[str, Path]) -> None:
        self._dirs = tier_dirs  # tier -> dir

    def discover(self) -> dict[str, Skill]:
        """Trả {name: Skill} với precedence (tier cao đè thấp)."""
        out: dict[str, Skill] = {}
        for tier in _TIERS:  # thấp trước, cao ghi đè sau
            d = self._dirs.get(tier)
            if not d or not d.exists():
                continue
            for skill_dir in sorted(d.iterdir()):
                md = skill_dir / "SKILL.md"
                if md.exists():
                    try:
                        s = parse_skill_md(md.read_text(encoding="utf-8"), tier)
                        out[s.name] = s
                    except UserFacingError:
                        continue  # skill hỏng frontmatter → bỏ qua
        return out

    def menu(self) -> list[dict]:
        """Progressive disclosure: chỉ name+description (không body) cho context."""
        return [{"name": s.name, "description": s.description} for s in self.discover().values()]

    def load_body(self, name: str) -> str:
        skills = self.discover()
        if name not in skills:
            raise UserFacingError(f"skill '{name}' không tồn tại")
        return skills[name].body
