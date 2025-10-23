from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Iterable, Optional

import structlog

from ..models import LayerType, ModerationRule

logger = structlog.get_logger(__name__)


class RuleRegistry:
    """In-memory cache for active moderation rules grouped by layer."""

    def __init__(self) -> None:
        self._rules: dict[LayerType, dict[Optional[int], list[ModerationRule]]] = defaultdict(
            lambda: defaultdict(list)
        )
        self._lock = asyncio.Lock()

    async def seed(self, rules: Iterable[ModerationRule]) -> None:
        async with self._lock:
            self._rules.clear()
            for rule in rules:
                self._rules[rule.layer][rule.chat_id].append(rule)
        logger.info(
            "rule_registry_seeded",
            totals={
                layer.value: sum(len(rules) for rules in by_chat.values())
                for layer, by_chat in self._rules.items()
            },
        )

    async def add_rule(self, rule: ModerationRule) -> None:
        async with self._lock:
            self._rules[rule.layer][rule.chat_id].append(rule)
        logger.info(
            "rule_registry_added",
            rule_id=rule.rule_id,
            layer=rule.layer.value,
            chat_id=rule.chat_id,
        )

    async def remove_rule(self, rule_id: str) -> None:
        async with self._lock:
            for layer, by_chat in self._rules.items():
                for chat_id, rules in list(by_chat.items()):
                    filtered = [rule for rule in rules if rule.rule_id != rule_id]
                    if filtered:
                        by_chat[chat_id] = filtered
                    else:
                        by_chat.pop(chat_id, None)
        logger.info("rule_registry_removed", rule_id=rule_id)

    async def get_rules_for_layer(self, layer: LayerType, chat_id: Optional[int] = None) -> list[ModerationRule]:
        async with self._lock:
            layer_rules = self._rules.get(layer, {})
            combined: list[ModerationRule] = list(layer_rules.get(None, []))
            if chat_id is not None:
                combined.extend(layer_rules.get(chat_id, []))
            return combined
