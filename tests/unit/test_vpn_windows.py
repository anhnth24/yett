"""Native-Windows contract for the POSIX/WSL2 VPN subprocess backend."""

from __future__ import annotations

import os

import pytest

from yett.config.models import SecurityCfg, VpnProfileCfg
from yett.errors import UserFacingError
from yett.security.basic_gate import BasicGate
from yett.tools.remote.vpn import SubprocessArgvCommander


class _Ctx:
    session_key = "windows-test"


def test_vpn_config_and_gate_remain_portable() -> None:
    profile = VpnProfileCfg(
        kind="openfortivpn",
        host="vpn.example.com",
        cred_secret="vpn_password",
        username="operator",
    )
    assert profile.kind == "openfortivpn"

    gate = BasicGate(SecurityCfg(), vpn_profiles={"office"})
    assert gate.evaluate("vpn", {"action": "status", "profile": "office"}, _Ctx()).verdict == "allow"
    assert (
        gate.evaluate("vpn", {"action": "connect", "profile": "office"}, _Ctx()).verdict
        == "need_approval"
    )
    assert gate.evaluate("vpn", {"action": "status", "profile": "other"}, _Ctx()).verdict == "deny"


@pytest.mark.skipif(os.name != "nt", reason="native Windows contract")
async def test_native_windows_vpn_fails_closed_before_spawn() -> None:
    with pytest.raises(UserFacingError, match="Windows native"):
        await SubprocessArgvCommander().start(["openvpn"])
