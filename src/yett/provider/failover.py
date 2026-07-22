"""Failover router (spec P0-P1 §3.1, §3.4).

Phân loại lỗi provider → 9 canonical reason. CONTEXT_OVERFLOW KHÔNG failover
(báo loop chạy compaction). Retry/backoff cho lỗi tạm thời; chuyển fallback khi hết retry.
"""

from __future__ import annotations

import asyncio

from yett.provider.base import ChatResult, FailReason, Message, Provider, ToolSchema

# Lỗi tạm thời → nên retry; lỗi cứng → chuyển fallback ngay; overflow → không failover.
_RETRYABLE = {FailReason.RATE_LIMIT, FailReason.TIMEOUT, FailReason.SERVER_5XX,
              FailReason.OVERLOADED, FailReason.NETWORK}
_HARD = {FailReason.AUTH, FailReason.BAD_REQUEST, FailReason.CONTENT_FILTER}


class ProviderError(Exception):
    def __init__(self, reason: FailReason, msg: str = "") -> None:
        super().__init__(msg or reason.name)
        self.reason = reason


class ContextOverflow(Exception):
    """Signal riêng: loop phải compaction rồi thử lại, KHÔNG failover."""


class FailoverRouter:
    def __init__(
        self,
        primary: Provider,
        fallback: Provider | None = None,
        *,
        max_retries: int = 4,
        backoff_base: float = 1.0,
        sleep=asyncio.sleep,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if backoff_base < 0:
            raise ValueError("backoff_base must be non-negative")
        self._primary = primary
        self._fallback = fallback
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._sleep = sleep

    async def chat(self, messages: list[Message], tools: list[ToolSchema]) -> ChatResult:
        try:
            return await self._try(self._primary, messages, tools)
        except ContextOverflow:
            raise  # loop xử lý compaction
        except ProviderError as e:
            if e.reason in _HARD and self._fallback is not None:
                return await self._try(self._fallback, messages, tools)
            if self._fallback is not None:
                return await self._try(self._fallback, messages, tools)
            raise

    async def _try(self, provider: Provider, messages, tools) -> ChatResult:
        last: Exception | None = None
        # max_retries means retries *after* the initial request.
        attempts = self._max_retries + 1
        for attempt in range(attempts):
            try:
                return await provider.chat(messages, tools)
            except ProviderError as e:
                if e.reason == FailReason.CONTEXT_OVERFLOW:
                    raise ContextOverflow() from e
                last = e
                if e.reason not in _RETRYABLE:
                    raise
                # Do not delay after the final failed attempt before failover/raise.
                if self._backoff_base and attempt + 1 < attempts:
                    await self._sleep(self._backoff_base * (2**attempt))
        assert last is not None
        raise last
