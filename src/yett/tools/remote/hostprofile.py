"""Host profile (spec P2 §2.1). SSH chỉ đến host khai báo trước; host lạ → deny."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from yett.errors import UserFacingError


class HostProfile(BaseModel):
    address: str
    auth: str  # "keyfile:<secret_name>" — key qua secret store
    vpn_required: str | None = None
    tier: Literal["uat", "restricted"] = "uat"
    log_paths: list[str] = Field(default_factory=list)
    deploy_script: str | None = None


class HostRegistry:
    def __init__(self, hosts: dict[str, HostProfile]) -> None:
        self._hosts = hosts

    def resolve(self, name: str) -> HostProfile:
        if name not in self._hosts:
            raise UserFacingError(f"host '{name}' chưa đăng ký — không cho SSH đại")
        return self._hosts[name]

    def has(self, name: str) -> bool:
        return name in self._hosts
