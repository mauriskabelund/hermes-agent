"""Rate-limit-safe delivery for replaceable, nonessential progress updates."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Optional


logger = logging.getLogger(__name__)


class CoalescedProgressDelivery:
    """Deliver the newest progress state without blocking answer consumption."""

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

    async def push(self, value: str) -> None:
        """Queue the newest state; platform I/O always runs in a background task."""
        if self._closed:
            return
        self._latest = value
        if self._task is None:
            now = time.monotonic()
            delay = 0.0 if self._last_attempt_at is None else max(
                0.0, self._next_allowed_at - now
            )
            self._schedule(delay)

    def _schedule(self, delay: float) -> None:
        if self._closed or self._task is not None or self._latest is None:
            return
        self._task = asyncio.create_task(self._deliver_later(max(0.0, delay)))

    async def _deliver_later(self, delay: float) -> None:
        current = asyncio.current_task()
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            if self._closed:
                return
            value = self._latest
            if value is None:
                return
            self._latest = None
            self._last_attempt_at = time.monotonic()
            try:
                outcome = await self._deliver(value)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Progress is explicitly nonessential. A platform failure must not
                # escape into the answer SSE loop or become an unobserved task.
                logger.warning("Nonessential progress delivery failed", exc_info=True)
                self._next_allowed_at = time.monotonic() + self._default_retry_after
                return

            now = time.monotonic()
            self._next_allowed_at = now + self._min_interval
            if bool(getattr(outcome, "success", False)):
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
        finally:
            if self._task is current:
                self._task = None
            if not self._closed and self._latest is not None:
                self._schedule(max(0.0, self._next_allowed_at - time.monotonic()))

    async def close(self) -> None:
        """Discard and cancel progress so it cannot delay the terminal response."""
        self._closed = True
        self._latest = None
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
