"""Complexity router (học từ knowledge-agent-template §router).

Phân loại độ khó câu hỏi → chọn số vòng lặp (step budget) phù hợp. Câu dễ dùng ít
vòng → ít LLM call → rẻ hơn; câu khó mới được nhiều vòng. Heuristic thuần: KHÔNG tốn
thêm LLM call (khác bản gốc dùng model rẻ), deterministic, chạy offline được — hợp
triết lý on-prem/cost-aware của yett.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from yett.config.models import RouterCfg

Tier = Literal["trivial", "simple", "moderate", "complex"]

# Dấu hiệu việc nhiều bước (deploy, sửa lỗi, phân tích, so sánh, điều tra…).
_MULTI_STEP = re.compile(
    r"\b(deploy|fix|sửa|refactor|phân tích|analyze|debug|điều tra|so sánh|compare|"
    r"migrate|chuyển đổi|tối ưu|optimize|review|kiểm tra|investigate|trace|vì sao|"
    r"why|root cause|nguyên nhân|toàn bộ|tất cả|end.to.end)\b",
    re.IGNORECASE,
)
# Nối nhiều yêu cầu trong một câu.
_CONJ = re.compile(r"\b(và|rồi|sau đó|then|and then|,\s*sau|;)\b", re.IGNORECASE)
# Chào hỏi/xác nhận ngắn → trivial.
_GREETING = re.compile(
    r"^\s*(hi|hello|chào|xin chào|ok|okay|cảm ơn|thanks|thank you|yes|no|ừ|vâng|được)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Route:
    tier: Tier
    max_iterations: int


class ComplexityRouter:
    def __init__(self, cfg: RouterCfg) -> None:
        self._cfg = cfg

    def classify(self, message: str) -> Route:
        msg = message.strip()
        steps = {
            "trivial": self._cfg.trivial_steps,
            "simple": self._cfg.simple_steps,
            "moderate": self._cfg.moderate_steps,
            "complex": self._cfg.complex_steps,
        }
        tier = self._tier(msg)
        return Route(tier=tier, max_iterations=steps[tier])

    def _tier(self, msg: str) -> Tier:
        words = len(msg.split())
        has_code = "```" in msg or msg.count("\n") >= 4
        multi = bool(_MULTI_STEP.search(msg))
        conj = len(_CONJ.findall(msg))

        # Chào hỏi/xác nhận rất ngắn, không phải câu hỏi thật.
        if words <= 6 and _GREETING.match(msg) and "?" not in msg:
            return "trivial"
        # Nhiều việc nối nhau hoặc kèm code lớn hoặc câu rất dài → phức tạp.
        if has_code or conj >= 2 or (multi and words > 30) or words > 80:
            return "complex"
        # Có từ khoá việc-nhiều-bước hoặc câu vừa → moderate.
        if multi or conj >= 1 or words > 25:
            return "moderate"
        # Câu hỏi ngắn gọn thông thường.
        if words > 8 or "?" in msg:
            return "simple"
        return "trivial"
