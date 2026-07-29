"""Rate-limit-safe delivery for replaceable, nonessential progress updates."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Any, Optional


class CoalescedProgressDelivery:
    """Deliver the newest progress state without flooding a chat platform."""

    def __init__(
        self,
        deliver: Callable[[str], Awaitable[Any]],
        *,
        min_interval: float = 2.0,
        default_retry_after: float = 10.0,
    ) -> None:
        if min_interval < 0:
            raise ValueError("min_interval must be non-negative")
        self._deliver = deliver
        self._min_interval = float(min_interval)
        self._default_retry_after = max(float(default_retry_after), self._min_interval)
        self._latest: Optional[str] = None
        self._last_attempt_at: Optional[float] = None
        self._next_allowed_at = 0.0
        self._task: Optional[asyncio.Task[None]] = None
        self._closed = False
        self._lock = asyncio.Lock()

    async def push(self, value: str) -> None:
        if self._closed:
            return
        self._latest = value
        async with self._lock:
            if self._closed or self._task is not None:
                return
            now = time.monotonic()
            if self._last_attempt_at is None or now >= self._next_allowed_at:
                await self._attempt_locked()
            else:
                self._schedule_locked(self._next_allowed_at - now)

    def _schedule_locked(self, delay: float) -> None:
        if self._closed or self._task is not None or self._latest is None:
            return
        self._task = asyncio.create_task(self._deliver_later(max(0.0, delay)))

    async def _deliver_later(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            async with self._lock:
                self._task = None
                if not self._closed and self._latest is not None:
                    await self._attempt_locked()
        except asyncio.CancelledError:
            raise

    async def _attempt_locked(self) -> None:
        value = self._latest
        if value is None:
            return
        self._latest = None
        self._last_attempt_at = time.monotonic()
        outcome = await self._deliver(value)
        now = time.monotonic()
        self._next_allowed_at = now + self._min_interval
        if bool(getattr(outcome, "success", False)):
            if self._latest is not None:
                self._schedule_locked(self._min_interval)
            return
        if bool(getattr(outcome, "retryable", False)):
            if self._latest is None:
                self._latest = value
            requested = getattr(outcome, "retry_after", None)
            retry_after = (
                max(float(requested), self._min_interval)
                if requested is not None
                else self._default_retry_after
            )
            self._next_allowed_at = now + retry_after
            self._schedule_locked(retry_after)

    async def close(self) -> None:
        """Discard pending progress so it cannot delay the terminal response."""
        self._closed = True
        self._latest = None
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task