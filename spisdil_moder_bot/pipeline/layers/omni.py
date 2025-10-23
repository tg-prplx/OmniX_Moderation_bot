from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional

import structlog

from ...adapters.openai import OmniModerationClient, OpenAIAdapterError
from ...models import (
    ActionType,
    LayerType,
    MessageEnvelope,
    ModerationRule,
    ModerationVerdict,
    ViolationPriority,
)
from ...rules.registry import RuleRegistry
from ..layers.base import ModerationLayer

logger = structlog.get_logger(__name__)


class OmniModerationLayer(ModerationLayer):
    layer_type = LayerType.OMNI

    def __init__(
        self,
        client: OmniModerationClient,
        rules: RuleRegistry,
        *,
        concurrency_limit: int = 5,
    ) -> None:
        super().__init__(priority=20)
        self._client = client
        self._rules = rules
        self._semaphore = asyncio.Semaphore(concurrency_limit)

    async def evaluate(self, message: MessageEnvelope) -> ModerationVerdict | None:
        text = message.content_text()
        image_urls = message.images or message.metadata.get("image_urls", [])
        message_id = message.context.message_id

        if text:
            result = await self._invoke(lambda: self._client.classify(text), message_id=message_id)
            verdict = await self._build_verdict(
                result,
                message=message,
                source="text",
                extra_details={"text_excerpt": text[:120]},
            )
            if verdict:
                return verdict

        for image_url in image_urls:
            logger.debug("omni_image_check", message_id=message_id, image_url=image_url)
            result = await self._invoke(
                lambda url=image_url: self._client.classify_image(url),
                message_id=message_id,
            )
            verdict = await self._build_verdict(
                result,
                message=message,
                source="image",
                extra_details={"image_reference": image_url},
            )
            if verdict:
                return verdict

        if not text and not image_urls:
            logger.debug("omni_skip_no_text", message_id=message_id)
        else:
            logger.debug("omni_not_flagged", message_id=message_id)
        return None

    async def _build_verdict(
        self,
        result: Optional[OmniModerationResult],
        *,
        message: MessageEnvelope,
        source: str,
        extra_details: dict,
    ) -> Optional[ModerationVerdict]:
        if result is None or not result.flagged:
            return None

        categories = {cat for cat, flagged in result.categories.items() if flagged}
        rule = await self._select_rule(categories, chat_id=message.context.chat_id)
        details = {
            "categories": result.categories,
            "scores": result.category_scores,
            "source": source,
            **extra_details,
        }
        if not rule:
            logger.info("omni_flagged_no_matching_rule", categories=list(categories))
            return None
        logger.info(
            "omni_flagged",
            rule_id=rule.rule_id,
            category=rule.category,
            message_id=message.context.message_id,
            source=source,
        )
        return ModerationVerdict(
            layer=self.layer_type,
            rule_code=rule.rule_id,
            priority=rule.priority,
            action=rule.action,
            reason=rule.description,
            violated=True,
            details={
                **details,
                "matched_category": rule.category,
                **(
                    {"action_duration_seconds": rule.action_duration_seconds}
                    if rule.action_duration_seconds is not None
                    else {}
                ),
            },
        )

    async def _invoke(
        self,
        func: Callable[[], Awaitable[OmniModerationResult]],
        *,
        message_id: int,
    ) -> Optional[OmniModerationResult]:
        async with self._semaphore:
            try:
                logger.debug("omni_request", message_id=message_id)
                return await func()
            except OpenAIAdapterError as exc:
                logger.error("omni_api_error", error=str(exc), message_id=message_id)
                return None

    async def _select_rule(self, categories: set[str], *, chat_id: Optional[int]) -> Optional[ModerationRule]:
        rules = await self._rules.get_rules_for_layer(LayerType.OMNI, chat_id=chat_id)
        best: Optional = None
        for rule in rules:
            if rule.category and rule.category in categories:
                if best is None or rule.priority > best.priority:
                    best = rule
        return best
