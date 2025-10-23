from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator, Deque, Optional

import structlog

from ..models import MessageEnvelope


@dataclass(slots=True)
class MessageBatch:
    items: list[MessageEnvelope]
    created_at: datetime
    flush_reason: str


logger = structlog.get_logger(__name__)


class MessageBatcher:
    """Aggregate incoming messages into fixed-size or time-based batches."""

    def __init__(
        self,
        max_batch_size: int,
        max_delay: float,
        *,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        if max_batch_size <= 0:
            raise ValueError("max_batch_size must be positive")
        if max_delay <= 0:
            raise ValueError("max_delay must be positive")

        self._loop = loop or asyncio.get_event_loop()
        self._max_batch_size = max_batch_size
        self._max_delay = max_delay
        self._pending: Deque[MessageEnvelope] = deque()
        self._queue: asyncio.Queue[MessageBatch] = asyncio.Queue()
        self._flush_task: Optional[asyncio.Task[None]] = None
        self._lock = asyncio.Lock()
        self._stopped = asyncio.Event()
        self._drained = False

    async def start(self) -> None:
        self._stopped.clear()
        self._drained = False
        logger.info(
            "batcher_started",
            max_batch_size=self._max_batch_size,
            max_delay=self._max_delay,
        )

    async def stop(self) -> None:
        self._stopped.set()
        async with self._lock:
            await self._flush("stop")
        if self._flush_task:
            self._flush_task.cancel()
            self._flush_task = None
        self._drained = True
        logger.info("batcher_stopped")

    async def __aenter__(self) -> "MessageBatcher":
        await self.start()
        return self

    async def __aexit__(self, *_) -> None:
        await self.stop()

    async def submit(self, message: MessageEnvelope) -> None:
        async with self._lock:
            self._pending.append(message)
            logger.debug(
                "batcher_message_enqueued",
                queue_size=len(self._pending),
                chat_id=message.context.chat_id,
                user_id=message.context.user_id,
            )
            if len(self._pending) == 1:
                self._schedule_timer()
            if len(self._pending) >= self._max_batch_size:
                await self._flush("size")

    def _schedule_timer(self) -> None:
        if self._flush_task and not self._flush_task.done():
            return
        self._flush_task = self._loop.create_task(self._delayed_flush())

    async def _delayed_flush(self) -> None:
        try:
            await asyncio.sleep(self._max_delay)
            async with self._lock:
                await self._flush("timer")
        except asyncio.CancelledError:
            pass

    async def _flush(self, reason: str) -> None:
        if not self._pending:
            return
        batch = MessageBatch(
            items=list(self._pending),
            created_at=datetime.now(timezone.utc),
            flush_reason=reason,
        )
        self._pending.clear()
        if self._flush_task:
            self._flush_task.cancel()
            self._flush_task = None
        await self._queue.put(batch)
        logger.info("batcher_flush", reason=reason, batch_size=len(batch.items))

    async def get(self) -> MessageBatch:
        if self._drained and self._queue.empty():
            raise RuntimeError("Batcher has been stopped and drained.")
        batch = await self._queue.get()
        logger.debug("batcher_batch_dequeued", size=len(batch.items), reason=batch.flush_reason)
        return batch

    def __aiter__(self) -> AsyncIterator[MessageBatch]:
        return self._batch_iterator()

    async def _batch_iterator(self) -> AsyncIterator[MessageBatch]:
        while True:
            if self._stopped.is_set() and self._queue.empty():
                break
            yield await self.get()
