from __future__ import annotations

import asyncio
import logging
from typing import Optional

import structlog

from ..adapters.openai import GPTClient, OmniModerationClient, RuleSynthesisClient
from ..batching.batcher import MessageBatcher
from ..config import BotSettings
from ..logging.events import setup_logging
from ..models import ActionType, LayerType, MessageEnvelope, RuleSource, RuleType
from ..pipeline.layers.chatgpt import ChatGPTLayer
from ..pipeline.layers.omni import OmniModerationLayer
from ..pipeline.layers.regex import RegexLayer
from ..pipeline.pipeline import ModerationPipeline
from ..punishments.aggregator import PunishmentAggregator
from ..rules.registry import RuleRegistry
from ..rules.service import RuleService
from ..scheduler.scheduler import ModerationScheduler, DecisionCallback
from ..storage.sqlite import SQLiteStorage

logger = structlog.get_logger(__name__)


class ModerationCoordinator:
    def __init__(
        self,
        settings: BotSettings,
        *,
        decision_callback: Optional[DecisionCallback] = None,
    ) -> None:
        # Setup logging with settings
        log_level = getattr(logging, settings.logging.level.upper(), logging.INFO)
        setup_logging(level=log_level, use_json=settings.logging.use_json)
        self._settings = settings
        self._batcher = MessageBatcher(
            max_batch_size=settings.batch.max_batch_size,
            max_delay=settings.batch.max_delay_seconds,
        )
        self._storage = SQLiteStorage(settings.storage.sqlite_path)
        self._registry = RuleRegistry()
        self._synth_client = RuleSynthesisClient(
            api_key=settings.openai.api_key,
            base_url=settings.openai.base_url,
            timeout=settings.openai.timeout_seconds,
        )
        self._rule_service = RuleService(self._registry, self._storage, self._synth_client)
        self._omni_client = OmniModerationClient(
            api_key=settings.openai.api_key,
            base_url=settings.openai.base_url,
            timeout=settings.openai.timeout_seconds,
        )
        self._gpt_client = GPTClient(
            api_key=settings.openai.api_key,
            base_url=settings.openai.base_url,
            timeout=settings.openai.timeout_seconds,
        )
        self._pipeline = ModerationPipeline(
            layers=(
                RegexLayer(self._registry, max_workers=settings.layers.regex_workers),
                OmniModerationLayer(
                    self._omni_client, self._registry, concurrency_limit=settings.layers.omni_concurrency
                ),
                ChatGPTLayer(
                    self._gpt_client, self._registry, concurrency_limit=settings.layers.chatgpt_concurrency
                ),
            )
        )
        self._scheduler = ModerationScheduler(
            batcher=self._batcher,
            pipeline=self._pipeline,
            storage=self._storage,
            aggregator=PunishmentAggregator(),
            max_concurrent_batches=settings.scheduler.concurrent_batches,
            decision_callback=decision_callback,
        )
        self._ready = asyncio.Event()

    async def start(self) -> None:
        await self._storage.connect()
        await self._rule_service.bootstrap()
        await self._batcher.start()
        await self._scheduler.start()
        self._ready.set()
        logger.info("moderation_coordinator_started")

    async def shutdown(self) -> None:
        await self._scheduler.stop()
        await self._batcher.stop()
        await self._storage.disconnect()
        await self._omni_client.close()
        await self._gpt_client.close()
        await self._synth_client.close()
        logger.info("moderation_coordinator_stopped")

    async def ingest(self, message: MessageEnvelope) -> None:
        await self._ready.wait()
        logger.debug(
            "coordinator_ingest",
            chat_id=message.context.chat_id,
            message_id=message.context.message_id,
        )
        await self._batcher.submit(message)

    async def add_rule(
        self,
        description: str,
        action: ActionType,
        source: RuleSource = "admin",
        *,
        chat_id: Optional[int] = None,
        action_duration_seconds: Optional[int] = None,
        layer: Optional[LayerType] = None,
        rule_type: Optional[RuleType] = None,
        pattern: Optional[str] = None,
        category: Optional[str] = None,
    ):
        logger.info("coordinator_add_rule", source=source, action=action.value, chat_id=chat_id)
        return await self._rule_service.add_rule(
            description,
            action,
            source,
            chat_id=chat_id,
            action_duration_seconds=action_duration_seconds,
            layer=layer,
            rule_type=rule_type,
            pattern=pattern,
            category=category,
        )

    async def remove_rule(self, rule_id: str) -> None:
        logger.info("coordinator_remove_rule", rule_id=rule_id)
        await self._rule_service.remove_rule(rule_id)

    async def list_rules(self, chat_id: Optional[int] = None):
        return await self._rule_service.list_rules(chat_id)

    def pause_layer(self, layer: str, duration: float) -> None:
        from ..models import LayerType

        try:
            layer_enum = LayerType(layer)
        except ValueError:
            logger.warning("pause_layer_unknown", layer=layer)
            return
        self._scheduler.pause_layer(layer_enum, duration)

    def resume_layer(self, layer: str) -> None:
        from ..models import LayerType

        try:
            layer_enum = LayerType(layer)
        except ValueError:
            logger.warning("resume_layer_unknown", layer=layer)
            return
        self._scheduler.resume_layer(layer_enum)
