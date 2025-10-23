from __future__ import annotations

import asyncio

import pytest

from spisdil_moder_bot.batching.batcher import MessageBatcher
from tests.factories import make_envelope


@pytest.mark.asyncio
async def test_batcher_flushes_on_size() -> None:
    batcher = MessageBatcher(max_batch_size=2, max_delay=10)
    await batcher.start()
    try:
        await batcher.submit(make_envelope("first"))
        await batcher.submit(make_envelope("second"))
        batch = await asyncio.wait_for(batcher.get(), timeout=1)
        assert len(batch.items) == 2
        assert batch.flush_reason == "size"
    finally:
        await batcher.stop()


@pytest.mark.asyncio
async def test_batcher_flushes_on_timer() -> None:
    batcher = MessageBatcher(max_batch_size=10, max_delay=0.1)
    await batcher.start()
    try:
        await batcher.submit(make_envelope("delayed"))
        batch = await asyncio.wait_for(batcher.get(), timeout=1)
        assert len(batch.items) == 1
        assert batch.flush_reason == "timer"
    finally:
        await batcher.stop()
