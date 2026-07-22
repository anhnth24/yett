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
    router = FailoverRouter(p, max_retries=2, backoff_base=0)
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
    assert res.provider_name == "fake"


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
    router = FailoverRouter(p, max_retries=2, backoff_base=0)
    with pytest.raises(ProviderError):
        await router.chat([], [])
    assert p.attempts == 3


async def test_retry_backoff_skips_sleep_after_final_failure() -> None:
    p = _FlakyProvider(FailReason.OVERLOADED, fail_times=99)
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    router = FailoverRouter(p, max_retries=2, backoff_base=0.5, sleep=record_sleep)
    with pytest.raises(ProviderError):
        await router.chat([], [])
    assert p.attempts == 3
    assert delays == [0.5, 1.0]


def test_retry_configuration_must_be_non_negative() -> None:
    with pytest.raises(ValueError):
        FailoverRouter(FakeProvider([]), max_retries=-1)


async def test_zero_retries_still_makes_initial_attempt() -> None:
    p = _FlakyProvider(FailReason.NETWORK, fail_times=99)
    with pytest.raises(ProviderError):
        await FailoverRouter(p, max_retries=0).chat([], [])
    assert p.attempts == 1
