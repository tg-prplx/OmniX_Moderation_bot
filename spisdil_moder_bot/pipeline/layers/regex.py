from __future__ import annotations

import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Optional

import structlog

from ...models import LayerType, MessageEnvelope, ModerationVerdict
from ...models import ModerationRule, ViolationPriority
from ...utils.concurrency import run_blocking
from ..layers.base import ModerationLayer, WarmupCapable
from ...rules.registry import RuleRegistry

logger = structlog.get_logger(__name__)


class RegexLayer(ModerationLayer, WarmupCapable):
    layer_type = LayerType.REGEX

    def __init__(self, rules: RuleRegistry, *, max_workers: int = 4) -> None:
        super().__init__(priority=10)
        self._rules = rules
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="regex-layer")
        self._compiled: dict[str, re.Pattern[str]] = {}
        self._lock = asyncio.Lock()

    async def warmup(self) -> None:
        rules = await self._rules.get_rules_for_layer(LayerType.REGEX)
        for rule in rules:
            if rule.pattern:
                await run_blocking(self._compile_rule, rule)
        logger.info("regex_layer_warmup_completed", rules=len(rules))

    def _compile_rule(self, rule: ModerationRule) -> None:
        if rule.pattern and rule.rule_id not in self._compiled:
            self._compiled[rule.rule_id] = re.compile(rule.pattern, re.IGNORECASE | re.MULTILINE)

    async def evaluate(self, message: MessageEnvelope) -> ModerationVerdict | None:
        text = message.content_text()
        if not text:
            logger.debug("regex_skip_no_text", message_id=message.context.message_id)
            return None

        rules = await self._rules.get_rules_for_layer(
            LayerType.REGEX,
            chat_id=message.context.chat_id,
        )
        if not rules:
            logger.debug("regex_skip_no_rules")
            return None

        for rule in rules:
            if not rule.pattern:
                continue
            await run_blocking(self._compile_rule, rule)

        loop = asyncio.get_running_loop()
        match_rule = partial(self._match_rules, text=text)
        match = await loop.run_in_executor(self._executor, match_rule, rules)
        if match is None:
            logger.debug("regex_no_match", message_id=message.context.message_id)
            return None
        rule, matched_text = match
        logger.info(
            "regex_match",
            rule_id=rule.rule_id,
            message_id=message.context.message_id,
            user_id=message.context.user_id,
        )
        return ModerationVerdict(
            layer=self.layer_type,
            rule_code=rule.rule_id,
            priority=rule.priority,
            action=rule.action,
            reason=rule.description,
            violated=True,
            details={
                "matched": matched_text,
                "pattern": rule.pattern,
                **(
                    {"action_duration_seconds": rule.action_duration_seconds}
                    if rule.action_duration_seconds is not None
                    else {}
                ),
            },
        )

    def _match_rules(
        self, rules: list[ModerationRule], *, text: str
    ) -> Optional[tuple[ModerationRule, str]]:
        for rule in rules:
            pattern = self._compiled.get(rule.rule_id)
            if not pattern:
                continue
            match = pattern.search(text)
            if match:
                return rule, match.group(0)
        return None

    async def shutdown(self) -> None:
        await run_blocking(self._executor.shutdown, wait=False, cancel_futures=True)
