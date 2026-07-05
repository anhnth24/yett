"""Test ApprovalCenter: request chờ resolve, timeout → deny, luồng duyệt/từ chối."""

from __future__ import annotations

import asyncio
import threading


from yett.approvals import ApprovalCenter


async def test_approve_flow() -> None:
    center = ApprovalCenter(default_timeout=5.0)

    async def user_approves_after_delay():
        # đợi request xuất hiện rồi duyệt
        for _ in range(50):
            pend = center.list_pending()
            if pend:
                center.resolve(pend[0]["id"], True)
                return
            await asyncio.sleep(0.02)

    task = asyncio.create_task(user_approves_after_delay())
    ok = await center.request("ssh_exec", {"cmd": "/opt/deploy/run.sh", "host": "uat"})
    await task
    assert ok is True
    assert center.list_pending() == []  # dọn sau khi xong


async def test_reject_flow() -> None:
    center = ApprovalCenter(default_timeout=5.0)

    async def user_rejects():
        for _ in range(50):
            pend = center.list_pending()
            if pend:
                center.resolve(pend[0]["id"], False)
                return
            await asyncio.sleep(0.02)

    task = asyncio.create_task(user_rejects())
    ok = await center.request("exec", {"cmd": "rm -rf x"})
    await task
    assert ok is False


async def test_timeout_denies() -> None:
    center = ApprovalCenter(default_timeout=0.1)
    ok = await center.request("exec", {"cmd": "deploy"})
    assert ok is False  # hết hạn → deny sạch


def test_resolve_unknown_id() -> None:
    center = ApprovalCenter()
    assert center.resolve("nope", True) is False


def test_pending_summary_truncates() -> None:
    center = ApprovalCenter(default_timeout=10.0)
    big = "x" * 500

    def bg():
        import time
        for _ in range(50):
            if center.list_pending():
                p = center.list_pending()[0]
                assert len(p["args"]["cmd"]) <= 301  # rút gọn
                center.resolve(p["id"], False)
                return
            time.sleep(0.02)

    t = threading.Thread(target=bg)
    t.start()
    asyncio.run(center.request("exec", {"cmd": big}))
    t.join()
