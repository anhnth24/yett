"""Test failover router: retry, fallback, context-overflow không failover (RG1-7)."""

from __future__ import annotations

import pytest

from yett.provider.base import FailReason
from yett.provider.failover import ContextOverflow, FailoverRouter, ProviderError
from yett.provider.fake import FakeProvider, text_result


class _FlakyProvider:
    """Fail N lần với reason cho trước rồi trả kết quả."""

    def __init__(self, reason: FailReason, fail_times: int, name: str = "flaky") -> None:
        self._reason = reason
        self._fail = fail_times
        self._name = name
        self.attempts = 0

    def name(self) -> str:
        return self._name

    def default_model(self) -> str:
        return "m"

    async def chat(self, messages, tools, *, stream=False):
        self.attempts += 1
        if self.attempts <= self._fail:
            raise ProviderError(self._reason)
        return text_result("ok")


@pytest.mark.asyncio
async def test_retry_then_success() -> None:
    p = _FlakyProvider(FailReason.RATE_LIMIT, fail_times=2)
    router = FailoverRouter(p, max_retries=5)
    res = await router.chat([], [])
    assert res.text == "ok"
    assert p.attempts == 3


@pytest.mark.asyncio
async def test_hard_error_switches_to_fallback() -> None:
    primary = _FlakyProvider(FailReason.AUTH, fail_times=99, name="primary")
    fallback = FakeProvider([text_result("from-fallback")])
    router = FailoverRouter(primary, fallback, max_retries=3)
    res = await router.chat([], [])
    assert res.text == "from-fallback"


@pytest.mark.asyncio
async def test_context_overflow_does_not_failover() -> None:
    primary = _FlakyProvider(FailReason.CONTEXT_OVERFLOW, fail_times=99)
    fallback = FakeProvider([text_result("should-not-be-used")])
    router = FailoverRouter(primary, fallback, max_retries=3)
    with pytest.raises(ContextOverflow):
        await router.chat([], [])


@pytest.mark.asyncio
async def test_retry_exhausted_without_fallback_raises() -> None:
    p = _FlakyProvider(FailReason.TIMEOUT, fail_times=99)
    router = FailoverRouter(p, max_retries=2)
    with pytest.raises(ProviderError):
        await router.chat([], [])
