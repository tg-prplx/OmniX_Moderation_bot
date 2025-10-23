from __future__ import annotations

import asyncio
from typing import Optional
from uuid import uuid4

import structlog

from ..adapters.openai import RuleSynthesisClient, RuleSynthesisRequest
from ..models import ActionType, LayerType, ModerationRule, RuleSource, RuleType, ViolationPriority
from ..storage.base import StorageGateway
from .registry import RuleRegistry

logger = structlog.get_logger(__name__)

# Valid Omni Moderation API categories (OpenAI omni-moderation-latest, 2025)
OMNI_VALID_CATEGORIES = {
    "hate",
    "hate/threatening",
    "harassment",
    "harassment/threatening",
    "self-harm",
    "self-harm/intent",
    "self-harm/instructions",
    "sexual",
    "sexual/minors",
    "violence",
    "violence/graphic",
    "illicit",
    "illicit/violent",
}


class RuleService:
    def __init__(
        self,
        registry: RuleRegistry,
        storage: StorageGateway,
        synthesizer: RuleSynthesisClient,
    ) -> None:
        self._registry = registry
        self._storage = storage
        self._synthesizer = synthesizer
        self._lock = asyncio.Lock()

    async def bootstrap(self) -> None:
        rules = await self._storage.list_rules()
        await self._registry.seed(rules)
        logger.info("rules_bootstrapped", count=len(rules))

    async def add_rule(
        self,
        description: str,
        desired_action: ActionType,
        source: RuleSource,
        *,
        chat_id: Optional[int] = None,
        action_duration_seconds: Optional[int] = None,
        layer: Optional[LayerType] = None,
        rule_type: Optional[RuleType] = None,
        pattern: Optional[str] = None,
        category: Optional[str] = None,
    ) -> ModerationRule:
        async with self._lock:
            logger.info(
                "rule_add_requested",
                source=source,
                action=desired_action.value,
                chat_id=chat_id,
                duration=action_duration_seconds,
                layer_override=layer.value if layer else None,
                rule_type_override=rule_type.value if rule_type else None,
            )
            classification = None
            if layer is None or rule_type is None or pattern is None or category is None:
                classification = await self._synthesizer.classify_rule(
                    RuleSynthesisRequest(
                        rule_text=description,
                        source=source,
                        desired_action=desired_action.value,
                    )
                )
                logger.debug(
                    "rule_classification_response",
                    layer=classification.layer,
                    rule_type=classification.rule_type,
                    category=classification.category,
                    has_regex=bool(classification.regex),
                    priority=classification.priority,
                )

            # Resolve layer and type
            resolved_layer = layer or self._resolve_layer(classification.layer if classification else "chatgpt")
            resolved_type = rule_type or self._resolve_type(classification.rule_type if classification else "contextual")
            resolved_pattern = pattern if pattern is not None else (classification.regex if classification else None)
            resolved_category = category if category is not None else (classification.category if classification else None)

            # Validate and clean fields based on layer type
            if resolved_layer in (LayerType.OMNI, LayerType.CHATGPT):
                # Omni and ChatGPT layers use only category, not pattern
                if resolved_pattern is not None:
                    logger.warning(
                        "rule_validation_pattern_ignored",
                        layer=resolved_layer.value,
                        pattern_removed=resolved_pattern[:50] if resolved_pattern else None,
                        reason=f"{resolved_layer.value} layer does not use regex patterns",
                    )
                    resolved_pattern = None

                # Validate Omni categories against official API list
                if resolved_layer == LayerType.OMNI:
                    if not resolved_category or resolved_category not in OMNI_VALID_CATEGORIES:
                        logger.warning(
                            "rule_validation_invalid_omni_category",
                            category=resolved_category,
                            valid_categories=sorted(OMNI_VALID_CATEGORIES),
                            reason="Category not supported by Omni Moderation API, falling back to chatgpt layer",
                        )
                        resolved_layer = LayerType.CHATGPT
                        resolved_type = RuleType.CONTEXTUAL

            elif resolved_layer == LayerType.REGEX:
                # Regex layer requires pattern
                if not resolved_pattern:
                    logger.warning(
                        "rule_validation_missing_pattern",
                        layer=resolved_layer.value,
                        reason="regex layer requires pattern, falling back to chatgpt layer",
                    )
                    resolved_layer = LayerType.CHATGPT
                    resolved_type = RuleType.CONTEXTUAL

            rule = ModerationRule(
                rule_id=str(uuid4()),
                description=description,
                action=desired_action,
                source=source,
                layer=resolved_layer,
                rule_type=resolved_type,
                chat_id=chat_id,
                pattern=resolved_pattern,
                category=resolved_category,
                priority=self._resolve_priority(classification.priority if classification else 10),
                action_duration_seconds=action_duration_seconds,
                metadata={
                    "auto_generated": True,
                    **(
                        {"action_duration_seconds": action_duration_seconds}
                        if action_duration_seconds is not None
                        else {}
                    ),
                },
            )
            await self._storage.upsert_rule(rule)
            await self._registry.add_rule(rule)
            logger.info(
                "rule_added",
                rule_id=rule.rule_id,
                layer=rule.layer.value,
                rule_type=rule.rule_type.value,
                category=rule.category,
                has_pattern=bool(rule.pattern),
                priority=rule.priority.value,
                chat_id=chat_id,
            )
            return rule

    async def remove_rule(self, rule_id: str) -> None:
        await self._storage.delete_rule(rule_id)
        await self._registry.remove_rule(rule_id)
        logger.info("rule_removed", rule_id=rule_id)

    async def list_rules(self, chat_id: Optional[int] = None) -> list[ModerationRule]:
        rules = await self._storage.list_rules()
        if chat_id is None:
            return rules
        return [rule for rule in rules if rule.chat_id in (None, chat_id)]

    def _resolve_layer(self, value: str) -> LayerType:
        try:
            return LayerType(value)
        except ValueError:
            logger.warning("unknown_layer_from_classifier", layer=value)
            if value == "omni":
                return LayerType.OMNI
            if value == "regex":
                return LayerType.REGEX
            return LayerType.CHATGPT

    def _resolve_type(self, value: str) -> RuleType:
        try:
            return RuleType(value)
        except ValueError:
            logger.warning("unknown_rule_type_from_classifier", rule_type=value)
            return RuleType.SEMANTIC

    def _resolve_priority(self, value: int) -> ViolationPriority:
        bounded = max(0, min(100, value))
        buckets = [
            (ViolationPriority.THREATS, 90),
            (ViolationPriority.NSFW, 70),
            (ViolationPriority.HATE, 60),
            (ViolationPriority.SPAM, 40),
            (ViolationPriority.OTHER, 0),
        ]
        for priority, threshold in buckets:
            if bounded >= threshold:
                return priority
        return ViolationPriority.OTHER
