from __future__ import annotations

import pytest

from spisdil_moder_bot.models import (
    ActionType,
    LayerType,
    ModerationResult,
    ModerationVerdict,
    ViolationPriority,
)
from spisdil_moder_bot.punishments.aggregator import PunishmentAggregator
from tests.factories import make_envelope


def verdict(layer: LayerType, priority: ViolationPriority) -> ModerationVerdict:
    return ModerationVerdict(
        layer=layer,
        rule_code=f"{layer.value}-rule",
        priority=priority,
        action=ActionType.MUTE,
        reason="violation",
        violated=True,
    )


def result_with_verdict(verdict_obj: ModerationVerdict) -> ModerationResult:
    return ModerationResult(message=make_envelope("text"), verdict=verdict_obj)


def test_aggregator_prefers_higher_layer_priority() -> None:
    aggregator = PunishmentAggregator()
    regex = result_with_verdict(verdict(LayerType.REGEX, ViolationPriority.SPAM))
    gpt = result_with_verdict(verdict(LayerType.CHATGPT, ViolationPriority.OTHER))

    decision = aggregator.decide([regex, gpt])

    assert decision is not None
    assert decision.verdict.layer == LayerType.CHATGPT
    assert len(decision.conflicting) == 1


def test_aggregator_returns_none_for_clean_batch() -> None:
    aggregator = PunishmentAggregator()
    clean = ModerationResult(message=make_envelope("clean"), verdict=None)

    decision = aggregator.decide([clean])

    assert decision is None
