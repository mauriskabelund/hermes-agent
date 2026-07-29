import asyncio
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_coalesces_burst_updates_into_one_later_delivery():
    from gateway.progress_delivery import CoalescedProgressDelivery

    delivered = []

    async def deliver(value):
        delivered.append(value)
        return SimpleNamespace(success=True, retryable=False, retry_after=None)

    progress = CoalescedProgressDelivery(deliver, min_interval=0.03)
    await progress.push("one")
    await progress.push("two")
    await progress.push("three")
    await asyncio.sleep(0.05)
    await progress.close()

    assert delivered == ["one", "three"]


@pytest.mark.asyncio
async def test_retryable_failure_honors_retry_after_without_fallback_spam():
    from gateway.progress_delivery import CoalescedProgressDelivery

    delivered = []
    outcomes = [
        SimpleNamespace(success=False, retryable=True, retry_after=0.04),
        SimpleNamespace(success=True, retryable=False, retry_after=None),
    ]

    async def deliver(value):
        delivered.append(value)
        return outcomes.pop(0)

    progress = CoalescedProgressDelivery(deliver, min_interval=0.01)
    await progress.push("working")
    await progress.push("newest")
    await asyncio.sleep(0.02)
    assert delivered == ["working"]
    await asyncio.sleep(0.05)
    await progress.close()

    assert delivered == ["working", "newest"]


@pytest.mark.asyncio
async def test_close_cancels_pending_nonessential_update():
    from gateway.progress_delivery import CoalescedProgressDelivery

    delivered = []

    async def deliver(value):
        delivered.append(value)
        return SimpleNamespace(success=True, retryable=False, retry_after=None)

    progress = CoalescedProgressDelivery(deliver, min_interval=60)
    await progress.push("first")
    await progress.push("stale")
    await progress.close()
    await asyncio.sleep(0)

    assert delivered == ["first"]