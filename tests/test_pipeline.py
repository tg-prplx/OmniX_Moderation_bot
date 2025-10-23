from __future__ import annotations

import asyncio

import pytest

from spisdil_moder_bot.models import (
    ActionType,
    LayerType,
    MessageEnvelope,
    ModerationVerdict,
    ViolationPriority,
)
from spisdil_moder_bot.pipeline.layers.base import ModerationLayer
from spisdil_moder_bot.pipeline.pipeline import ModerationPipeline
from tests.factories import make_envelope


class TriggerLayer(ModerationLayer):
    layer_type = LayerType.REGEX

    def __init__(self, verdict: ModerationVerdict | None, *, priority: int = 10) -> None:
        super().__init__(priority=priority)
        self._verdict = verdict
        self.calls = 0

    async def evaluate(self, message: MessageEnvelope) -> ModerationVerdict | None:
        self.calls += 1
        return self._verdict


class SpyLayer(ModerationLayer):
    layer_type = LayerType.OMNI

    def __init__(self, *, priority: int = 20) -> None:
        super().__init__(priority=priority)
        self.calls = 0

    async def evaluate(self, message: MessageEnvelope) -> ModerationVerdict | None:
        self.calls += 1
        return None


class FinalLayer(ModerationLayer):
    layer_type = LayerType.CHATGPT

    def __init__(self, *, priority: int = 30) -> None:
        super().__init__(priority=priority)
        self.calls = 0

    async def evaluate(self, message: MessageEnvelope) -> ModerationVerdict | None:
        self.calls += 1
        return None


def violation_verdict(layer: LayerType) -> ModerationVerdict:
    return ModerationVerdict(
        layer=layer,
        rule_code="rule",
        priority=ViolationPriority.THREATS,
        action=ActionType.BAN,
        reason="hit",
        violated=True,
    )


@pytest.mark.asyncio
async def test_pipeline_short_circuits_on_first_violation() -> None:
    layer = TriggerLayer(violation_verdict(LayerType.REGEX))
    spy = SpyLayer()
    final = FinalLayer()
    pipeline = ModerationPipeline([spy, layer, final])  # order irrelevant; pipeline sorts by priority
    result = await pipeline.process_message(make_envelope("boom"))

    assert layer.calls == 1
    assert spy.calls == 0
    assert final.calls == 0
    assert result.verdict and result.verdict.rule_code == "rule"
    assert result.evaluated_layers == [LayerType.REGEX]


@pytest.mark.asyncio
async def test_pipeline_evaluates_all_layers_when_no_violation() -> None:
    layer = TriggerLayer(None)
    spy = SpyLayer()
    final = FinalLayer()
    pipeline = ModerationPipeline([layer, spy, final])

    result = await pipeline.process_message(make_envelope("clean"))

    assert result.verdict is None
    assert layer.calls == 1
    assert spy.calls == 1
    assert final.calls == 1
    assert result.evaluated_layers == [LayerType.REGEX, LayerType.OMNI, LayerType.CHATGPT]


@pytest.mark.asyncio
async def test_pipeline_skips_disabled_layers() -> None:
    layer = TriggerLayer(None)
    spy = SpyLayer()
    final = FinalLayer()
    pipeline = ModerationPipeline([layer, spy, final])

    result = await pipeline.process_message(
        make_envelope("skip omni"), disabled_layers={LayerType.OMNI}
    )

    assert spy.calls == 0
    assert final.calls == 1
    assert result.evaluated_layers == [LayerType.REGEX, LayerType.CHATGPT]
