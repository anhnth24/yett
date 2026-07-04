"""VPN tool (spec P2 §2.3). Bọc CLI OpenVPN/Fortinet. Credentials từ secret store,

KHÔNG vào context/span/log. CLI runner injectable để test offline. Trên WSL2 đánh giá
openfortivpn cho Fortinet (P0.5.1).
"""

from __future__ import annotations

from typing import Protocol

from yett.errors import UserFacingError
from yett.tools.base import ToolCtx, ToolResult


class VpnRunner(Protocol):
    async def connect(self, profile: str, *, creds: dict[str, str]) -> bool: ...
    async def disconnect(self, profile: str) -> bool: ...
    async def status(self, profile: str) -> bool: ...  # True = đang kết nối


class VpnManager:
    """Quản lý trạng thái VPN + đảm bảo bật trước khi SSH (ensure)."""

    def __init__(self, runner: VpnRunner, secrets, profiles: dict[str, dict]) -> None:
        self._runner = runner
        self._secrets = secrets
        self._profiles = profiles  # name -> {cred_secret: <secret name>, ...}
        self._connected: set[str] = set()

    async def ensure(self, profile: str) -> None:
        if profile in self._connected or await self._runner.status(profile):
            self._connected.add(profile)
            return
        creds = self._creds(profile)
        ok = await self._runner.connect(profile, creds=creds)
        if not ok:
            raise UserFacingError(f"không kết nối được VPN '{profile}'")
        self._connected.add(profile)

    def _creds(self, profile: str) -> dict[str, str]:
        cfg = self._profiles.get(profile, {})
        secret_name = cfg.get("cred_secret")
        if not secret_name:
            return {}
        # Lấy tại điểm dùng; giá trị không rời khỏi đây.
        return {"password": self._secrets.get(secret_name)}


class VpnTool:
    """Bật/tắt/xem trạng thái VPN."""

    name = "vpn"
    schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["connect", "disconnect", "status"]},
            "profile": {"type": "string"},
        },
        "required": ["action", "profile"],
    }

    def __init__(self, manager: VpnManager) -> None:
        self._m = manager

    def validate(self, args: dict) -> None:
        if args.get("action") not in ("connect", "disconnect", "status"):
            raise UserFacingError("action phải là connect|disconnect|status")
        if not args.get("profile"):
            raise UserFacingError("thiếu 'profile'")

    async def run(self, args: dict, ctx: ToolCtx) -> ToolResult:
        action, profile = args["action"], args["profile"]
        if action == "connect":
            await self._m.ensure(profile)
            return ToolResult.success(f"VPN '{profile}' đã kết nối")
        if action == "disconnect":
            await self._m._runner.disconnect(profile)
            self._m._connected.discard(profile)
            return ToolResult.success(f"VPN '{profile}' đã ngắt")
        connected = await self._m._runner.status(profile)
        return ToolResult.success(f"VPN '{profile}': {'connected' if connected else 'disconnected'}")
