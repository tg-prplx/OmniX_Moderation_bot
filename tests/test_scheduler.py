from __future__ import annotations

import asyncio
from collections import deque
from typing import Iterable, Optional

import pytest

from spisdil_moder_bot.batching.batcher import MessageBatcher
from spisdil_moder_bot.models import (
    ActionType,
    LayerType,
    ModerationResult,
    ModerationVerdict,
    ViolationPriority,
)
from spisdil_moder_bot.pipeline.layers.base import ModerationLayer
from spisdil_moder_bot.pipeline.pipeline import ModerationPipeline
from spisdil_moder_bot.scheduler.scheduler import ModerationScheduler
from spisdil_moder_bot.punishments.aggregator import PunishmentAggregator
from spisdil_moder_bot.storage.base import StorageGateway
from tests.factories import make_envelope


class InMemoryStorage(StorageGateway):
    def __init__(self) -> None:
        self.rules: list = []
        self.incidents: deque[ModerationResult] = deque()

    async def connect(self) -> None:  # pragma: no cover - noop
        return None

    async def disconnect(self) -> None:  # pragma: no cover - noop
        return None

    async def list_rules(self):
        return list(self.rules)

    async def upsert_rule(self, rule):
        self.rules.append(rule)

    async def delete_rule(self, rule_id: str):
        self.rules = [rule for rule in self.rules if getattr(rule, "rule_id", None) != rule_id]

    async def record_incident(self, result: ModerationResult) -> None:
        self.incidents.append(result)

    async def record_batch_results(self, results: Iterable[ModerationResult]) -> None:
        for result in results:
            if result.verdict:
                self.incidents.append(result)


class AlwaysViolatingLayer(ModerationLayer):
    layer_type = LayerType.REGEX

    def __init__(self) -> None:
        super().__init__(priority=10)

    async def evaluate(self, message):
        return ModerationVerdict(
            layer=self.layer_type,
            rule_code="always",
            priority=ViolationPriority.SPAM,
            action=ActionType.WARN,
            reason="auto",
            violated=True,
        )


@pytest.mark.asyncio
async def test_scheduler_processes_batches_and_invokes_decision_callback() -> None:
    batcher = MessageBatcher(max_batch_size=1, max_delay=0.01)
    pipeline = ModerationPipeline([AlwaysViolatingLayer()])
    storage = InMemoryStorage()

    decisions: list[tuple[ModerationVerdict, ModerationResult]] = []
    decision_event = asyncio.Event()

    async def decision_callback(decision, result):
        decisions.append((decision.verdict, result))
        decision_event.set()

    scheduler = ModerationScheduler(
        batcher=batcher,
        pipeline=pipeline,
        storage=storage,
        aggregator=PunishmentAggregator(),
        max_concurrent_batches=1,
        decision_callback=decision_callback,
    )

    await batcher.start()
    await scheduler.start()
    try:
        await batcher.submit(make_envelope("violation"))
        await asyncio.wait_for(decision_event.wait(), timeout=1)

        assert len(decisions) == 1
        verdict, result = decisions[0]
        assert verdict.rule_code == "always"
        assert storage.incidents  # incident recorded
    finally:
        await scheduler.stop()
        await batcher.stop()
