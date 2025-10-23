from __future__ import annotations

import pytest

from spisdil_moder_bot.models import LayerType
from spisdil_moder_bot.rules.registry import RuleRegistry
from tests.factories import make_rule


@pytest.mark.asyncio
async def test_rule_registry_returns_global_and_chat_specific() -> None:
    registry = RuleRegistry()
    global_rule = make_rule(rule_id="global", layer=LayerType.REGEX)
    chat_rule = make_rule(rule_id="chat", layer=LayerType.REGEX, chat_id=123)
    await registry.seed([global_rule, chat_rule])

    rules_global = await registry.get_rules_for_layer(LayerType.REGEX)
    assert {rule.rule_id for rule in rules_global} == {"global"}

    rules_chat = await registry.get_rules_for_layer(LayerType.REGEX, chat_id=123)
    assert {rule.rule_id for rule in rules_chat} == {"global", "chat"}
