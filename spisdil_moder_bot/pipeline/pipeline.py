from __future__ import annotations

import asyncio
from typing import Iterable, Sequence

import structlog

from ..batching.batcher import MessageBatch
from ..models import LayerType, MessageEnvelope, ModerationResult
from .layers.base import ModerationLayer, WarmupCapable

logger = structlog.get_logger(__name__)


class ModerationPipeline:
    def __init__(self, layers: Iterable[ModerationLayer]) -> None:
        ordered = sorted(layers)
        self.layers: Sequence[ModerationLayer] = tuple(ordered)
        logger.info(
            "pipeline_initialized",
            layers=[layer.layer_type.value for layer in self.layers],
        )

    async def warmup(self) -> None:
        logger.info("pipeline_warmup_start")
        await asyncio.gather(
            *(layer.warmup() for layer in self.layers if isinstance(layer, WarmupCapable))
        )
        logger.info("pipeline_warmup_complete")

    async def process_message(
        self,
        message: MessageEnvelope,
        *,
        disabled_layers: set[LayerType] | None = None,
    ) -> ModerationResult:
        logger.debug(
            "pipeline_process_message_start",
            message_id=message.context.message_id,
            chat_id=message.context.chat_id,
        )
        evaluated: list[LayerType] = []
        for layer in self.layers:
            if disabled_layers and layer.layer_type in disabled_layers:
                logger.debug("layer_skipped", layer=layer.layer_type.value, reason="disabled")
                continue
            evaluated.append(layer.layer_type)
            verdict = await layer.evaluate(message)
            if verdict and verdict.short_circuit():
                logger.debug(
                    "short_circuit",
                    layer=layer.layer_type.value,
                    rule=verdict.rule_code,
                    action=verdict.action.value,
                )
                result = ModerationResult(message=message, verdict=verdict, evaluated_layers=evaluated)
                logger.info(
                    "pipeline_message_violation",
                    message_id=message.context.message_id,
                    layer=verdict.layer.value,
                    rule=verdict.rule_code,
                    action=verdict.action.value,
                )
                return result
        logger.debug(
            "pipeline_message_clean",
            message_id=message.context.message_id,
            evaluated=[layer.value for layer in evaluated],
        )
        return ModerationResult(message=message, verdict=None, evaluated_layers=evaluated)

    async def process_batch(
        self,
        batch: MessageBatch,
        *,
        disabled_layers: set[LayerType] | None = None,
    ) -> list[ModerationResult]:
        logger.info(
            "pipeline_process_batch_start",
            size=len(batch.items),
            reason=batch.flush_reason,
        )
        results = await asyncio.gather(
            *(self.process_message(item, disabled_layers=disabled_layers) for item in batch.items)
        )
        violations = sum(1 for result in results if result.verdict and result.verdict.violated)
        logger.info(
            "pipeline_process_batch_complete",
            size=len(batch.items),
            violations=violations,
        )
        return results
