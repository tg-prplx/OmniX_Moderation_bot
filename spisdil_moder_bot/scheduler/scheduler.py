from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Optional

import structlog

from ..batching.batcher import MessageBatch, MessageBatcher
from ..models import LayerType, ModerationResult
from ..pipeline.pipeline import ModerationPipeline
from ..punishments.aggregator import PunishmentAggregator, PunishmentDecision
from ..storage.base import StorageGateway

logger = structlog.get_logger(__name__)

DecisionCallback = Callable[[PunishmentDecision, ModerationResult], Awaitable[None]]


class ModerationScheduler:
    def __init__(
        self,
        batcher: MessageBatcher,
        pipeline: ModerationPipeline,
        storage: StorageGateway,
        *,
        aggregator: Optional[PunishmentAggregator] = None,
        max_concurrent_batches: int = 3,
        decision_callback: Optional[DecisionCallback] = None,
    ) -> None:
        self._batcher = batcher
        self._pipeline = pipeline
        self._storage = storage
        self._aggregator = aggregator or PunishmentAggregator()
        self._decision_callback = decision_callback
        self._semaphore = asyncio.Semaphore(max_concurrent_batches)
        self._tasks: set[asyncio.Task[None]] = set()
        self._running = False
        self._disabled_until: dict[LayerType, float] = {}
        self._main_task: Optional[asyncio.Task[None]] = None

    async def start(self) -> None:
        if self._running:
            return
        await self._pipeline.warmup()
        self._running = True
        self._main_task = asyncio.create_task(self._run())
        logger.info("scheduler_started")

    async def stop(self) -> None:
        self._running = False
        if self._main_task:
            self._main_task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        logger.info("scheduler_stopped")

    async def _run(self) -> None:
        while self._running:
            batch = await self._batcher.get()
            logger.info(
                "scheduler_batch_received",
                size=len(batch.items),
                reason=batch.flush_reason,
            )
            await self._semaphore.acquire()
            task = asyncio.create_task(self._process_batch(batch))
            self._tasks.add(task)
            task.add_done_callback(lambda t: self._on_task_done(t))

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        self._semaphore.release()
        if task.exception():
            logger.error("batch_task_failed", error=str(task.exception()))

    async def _process_batch(self, batch: MessageBatch) -> None:
        disabled = self._current_disabled_layers()
        try:
            logger.debug(
                "scheduler_process_batch",
                size=len(batch.items),
                disabled_layers={layer.value for layer in disabled},
            )
            results = await self._pipeline.process_batch(batch, disabled_layers=disabled)
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("batch_processing_failed", error=str(exc))
            return

        await self._storage.record_batch_results(results)
        for result in results:
            decision = self._aggregator.decide([result])
            if not decision:
                continue
            logger.info(
                "scheduler_decision",
                action=decision.verdict.action.value,
                rule=decision.verdict.rule_code,
            )
            if self._decision_callback:
                try:
                    await self._decision_callback(decision, result)
                except Exception as exc:  # pylint: disable=broad-except
                    logger.error("decision_callback_failed", error=str(exc))

    def _current_disabled_layers(self) -> set[LayerType]:
        now = time.monotonic()
        disabled = {layer for layer, until in self._disabled_until.items() if until > now}
        for layer in list(self._disabled_until):
            if self._disabled_until[layer] <= now:
                self._disabled_until.pop(layer, None)
        return disabled

    def pause_layer(self, layer: LayerType, duration: float) -> None:
        until = time.monotonic() + duration
        self._disabled_until[layer] = max(until, self._disabled_until.get(layer, 0))
        logger.warning("layer_paused", layer=layer.value, duration=duration)

    def resume_layer(self, layer: LayerType) -> None:
        if layer in self._disabled_until:
            self._disabled_until.pop(layer, None)
            logger.info("layer_resumed", layer=layer.value)
