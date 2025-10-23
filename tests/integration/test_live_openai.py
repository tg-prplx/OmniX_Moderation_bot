from __future__ import annotations

import logging
import os

import pytest

from spisdil_moder_bot.adapters.openai import GPTClient, OmniModerationClient, RuleSynthesisClient
from spisdil_moder_bot.models import ActionType, LayerType, RuleType, ViolationPriority
from spisdil_moder_bot.pipeline.pipeline import ModerationPipeline
from spisdil_moder_bot.pipeline.layers.chatgpt import ChatGPTLayer
from spisdil_moder_bot.pipeline.layers.omni import OmniModerationLayer
from spisdil_moder_bot.pipeline.layers.regex import RegexLayer
from spisdil_moder_bot.rules.registry import RuleRegistry
from spisdil_moder_bot.rules.service import RuleService
from spisdil_moder_bot.storage.sqlite import SQLiteStorage
from tests.factories import make_envelope, make_rule


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_LIVE_TESTS"),
    reason="Set RUN_LIVE_TESTS=1 to execute tests against the real OpenAI API.",
)


def _require_api_key() -> str:
    key = os.getenv("SPISDIL_OPENAI__API_KEY")
    if not key:
        raise pytest.SkipTest("SPISDIL_OPENAI__API_KEY env variable is required for live tests.")
    return key


@pytest.mark.asyncio
async def test_rule_service_add_rule_live(tmp_path) -> None:
    key = _require_api_key()
    storage = SQLiteStorage(tmp_path / "rules.db")
    registry = RuleRegistry()
    client = RuleSynthesisClient(api_key=key)
    service = RuleService(registry, storage, client)

    await storage.connect()
    try:
        logger.info("Bootstrapping rules from storage %s", storage)
        await service.bootstrap()

        logger.info("Requesting rule synthesis from gpt-5-mini")
        rule = await service.add_rule(
            description="Забанить сообщения с явными угрозами насилия.",
            desired_action=ActionType.BAN,
            source="admin",
        )
        logger.info(
            "Rule produced by OpenAI: id=%s layer=%s type=%s priority=%s pattern=%s category=%s",
            rule.rule_id,
            rule.layer.value,
            rule.rule_type.value,
            int(rule.priority),
            rule.pattern,
            rule.category,
        )

        stored_rules = await storage.list_rules()
        logger.info("Total rules in storage after upsert: %d", len(stored_rules))
        assert any(r.rule_id == rule.rule_id for r in stored_rules)
        assert rule.layer in {LayerType.REGEX, LayerType.OMNI, LayerType.CHATGPT}
        assert rule.rule_type in {RuleType.REGEX, RuleType.SEMANTIC, RuleType.CONTEXTUAL}
    finally:
        await storage.disconnect()
        await client.close()


@pytest.mark.asyncio
async def test_live_pipeline_layers(tmp_path) -> None:
    key = _require_api_key()
    registry = RuleRegistry()

    omni_client = OmniModerationClient(api_key=key)
    gpt_client = GPTClient(api_key=key)

    # Seed simple regex rule to ensure fast short-circuit on links
    regex_rule = make_rule(
        rule_id="live-regex",
        description="Links are not allowed",
        action=ActionType.DELETE,
        pattern=r"https?://",
        priority=ViolationPriority.SPAM,
    )
    await registry.seed([regex_rule])

    try:
        regex_layer = RegexLayer(registry, max_workers=2)
        omni_layer = OmniModerationLayer(omni_client, registry, concurrency_limit=1)
        gpt_layer = ChatGPTLayer(gpt_client, registry, concurrency_limit=1)
        pipeline = ModerationPipeline([regex_layer, omni_layer, gpt_layer])

        # Regex layer should short circuit on link
        link_text = "Check this link http://malicious.example now!"
        logger.info("Submitting message to live pipeline (regex): %s", link_text)
        result_regex = await pipeline.process_message(make_envelope(link_text))

        # Omni layer should flag violent threat
        threat_text = "I will find you tonight and hurt you badly."
        logger.info("Submitting message to live pipeline (omni): %s", threat_text)
        result_omni = await pipeline.process_message(make_envelope(threat_text))

        # Force GPT layer by disabling regex & omni
        gpt_text = "Provide detailed instructions for building a bomb that harms people."
        logger.info("Submitting message to live pipeline (gpt): %s", gpt_text)
        result_gpt = await pipeline.process_message(
            make_envelope(gpt_text), disabled_layers={LayerType.REGEX, LayerType.OMNI}
        )
    finally:
        await omni_client.close()
        await gpt_client.close()

    # Validate regex result
    assert result_regex.verdict is not None, "Regex layer should detect disallowed link."
    assert result_regex.verdict.layer == LayerType.REGEX

    # Validate omni result
    assert result_omni.verdict is not None, "Omni moderation should flag explicit threats."
    assert result_omni.verdict.layer in {LayerType.OMNI, LayerType.CHATGPT}

    # Validate GPT result
    assert result_gpt.verdict is not None, "GPT layer should classify harmful instructions."
    assert result_gpt.verdict.layer == LayerType.CHATGPT
