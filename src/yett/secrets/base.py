"""Secret store (spec P0-P1 §3.2).

Bất biến: giá trị secret KHÔNG bao giờ đi vào Message, span, log, hay checkpoint.
Consumer (provider client, db driver, vpn) chỉ cầm TÊN secret; giá trị được lấy
tại điểm dùng cuối và không lưu lại. Test RG1-9 scan mọi artifact để bảo đảm điều này.
"""

from __future__ import annotations

from typing import Protocol


class SecretStore(Protocol):
    def get(self, name: str) -> str:
        """Trả giá trị secret theo tên. Raise SecretNotFound nếu thiếu (fail-closed)."""
        ...
