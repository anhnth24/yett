"""Skill review gate (spec P2 P2.1.4). Skill agent-tạo → staging, scan, duyệt mới active.

Tham chiếu behavior skills_guard/provenance của Hermes: scan nội dung nguy hiểm + ghi
nguồn gốc. Skill pending KHÔNG xuất hiện trong menu cho tới khi được duyệt.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

# Dấu hiệu skill nguy hiểm (scan trước khi cho active).
_DANGER = re.compile(
    r"(?i)(rm\s+-rf|curl\s+[^\n]*\|\s*(sh|bash)|eval\s*\(|base64\s+-d|/etc/shadow|nc\s+-e)"
)


class SkillReviewGate:
    def __init__(self, workspace_skills: Path) -> None:
        self._pending = workspace_skills / "pending"

    def propose(self, name: str, skill_md: str, *, origin: str = "agent") -> dict:
        """Agent đề xuất skill → staging + scan + provenance. Trả {id, flags}."""
        self._pending.mkdir(parents=True, exist_ok=True)
        sid = uuid.uuid4().hex[:8]
        d = self._pending / sid
        d.mkdir()
        (d / "SKILL.md").write_text(skill_md, encoding="utf-8")
        flags = _scan(skill_md)
        (d / "origin.json").write_text(
            json.dumps({"origin": origin, "flags": flags, "name": name}, ensure_ascii=False),
            encoding="utf-8",
        )
        return {"id": sid, "flags": flags}

    def list_pending(self) -> list[dict]:
        if not self._pending.exists():
            return []
        out = []
        for d in sorted(self._pending.iterdir()):
            origin = d / "origin.json"
            if origin.exists():
                out.append({"id": d.name, **json.loads(origin.read_text(encoding="utf-8"))})
        return out

    def approve(self, sid: str, managed_dir: Path) -> bool:
        """Duyệt → chuyển sang managed tier (active). Trả False nếu không tồn tại."""
        src = self._pending / sid
        if not (src / "SKILL.md").exists():
            return False
        meta = json.loads((src / "origin.json").read_text(encoding="utf-8"))
        dest = managed_dir / meta["name"]
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "SKILL.md").write_text((src / "SKILL.md").read_text(encoding="utf-8"), encoding="utf-8")
        _rmtree(src)
        return True


def _scan(text: str) -> list[str]:
    return ["dangerous_pattern"] if _DANGER.search(text) else []


def _rmtree(p: Path) -> None:
    for c in p.iterdir():
        c.unlink()
    p.rmdir()
