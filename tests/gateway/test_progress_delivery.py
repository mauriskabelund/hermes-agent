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
    await asyncio.sleep(0)
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
    await asyncio.sleep(0)
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
    await asyncio.sleep(0)
    await progress.push("stale")
    await progress.close()
    await asyncio.sleep(0)

    assert delivered == ["first"]


@pytest.mark.asyncio
async def test_delivery_exception_isolated_from_answer_path():
    from gateway.progress_delivery import CoalescedProgressDelivery

    async def deliver(_value):
        raise RuntimeError("nonessential progress failed")

    progress = CoalescedProgressDelivery(deliver, min_interval=0.01)
    await progress.push("working")
    await asyncio.sleep(0)
    await progress.close()


@pytest.mark.asyncio
async def test_blocked_platform_delivery_never_blocks_answer_side_pushes():
    from gateway.progress_delivery import CoalescedProgressDelivery

    started = asyncio.Event()
    blocked = asyncio.Event()

    async def deliver(_value):
        started.set()
        await blocked.wait()
        return SimpleNamespace(success=True, retryable=False, retry_after=None)

    progress = CoalescedProgressDelivery(deliver, min_interval=0.01)
    await asyncio.wait_for(progress.push("first"), timeout=0.05)
    await asyncio.wait_for(started.wait(), timeout=0.05)
    await asyncio.wait_for(progress.push("newest"), timeout=0.05)
    await progress.close()