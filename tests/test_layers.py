from __future__ import annotations

import json
from typing import Optional

import pytest

from spisdil_moder_bot.adapters.openai import (
    ChatCompletionResult,
    OmniModerationResult,
)
from spisdil_moder_bot.models import (
    ActionType,
    LayerType,
    RuleType,
    ViolationPriority,
)
from spisdil_moder_bot.pipeline.layers.chatgpt import ChatGPTLayer
from spisdil_moder_bot.pipeline.layers.omni import OmniModerationLayer
from spisdil_moder_bot.pipeline.layers.regex import RegexLayer
from spisdil_moder_bot.rules.registry import RuleRegistry
from tests.factories import make_envelope, make_rule


class FakeOmniClient:
    def __init__(
        self,
        result: OmniModerationResult,
        image_result: Optional[OmniModerationResult] = None,
    ) -> None:
        self._result = result
        self._image_result = image_result or result
        self.calls: list[tuple[str, str]] = []

    async def classify(self, text: str, *, model: str = "omni-moderation-latest") -> OmniModerationResult:
        self.calls.append(("text", text))
        return self._result

    async def classify_image(
        self,
        image_url: str,
        *,  # noqa: D417
        model: str = "omni-moderation-latest",
    ) -> OmniModerationResult:
        self.calls.append(("image", image_url))
        return self._image_result


class FakeGPTClient:
    def __init__(self, result: ChatCompletionResult) -> None:
        self._result = result
        self.calls = 0
        self.last_request = None

    async def complete(self, request) -> ChatCompletionResult:
        self.calls += 1
        self.last_request = request
        return self._result


@pytest.mark.asyncio
async def test_regex_layer_matches_pattern() -> None:
    registry = RuleRegistry()
    rule = make_rule(
        rule_id="regex-1",
        pattern=r"forbidden",
        action=ActionType.DELETE,
        priority=ViolationPriority.NSFW,
    )
    await registry.seed([rule])
    layer = RegexLayer(registry, max_workers=2)
    await layer.warmup()

    verdict = await layer.evaluate(make_envelope("This message has forbidden content"))

    assert verdict is not None
    assert verdict.rule_code == "regex-1"
    assert verdict.details["matched"] == "forbidden"
    assert verdict.action == ActionType.DELETE


@pytest.mark.asyncio
async def test_omni_layer_uses_categories_and_rules() -> None:
    registry = RuleRegistry()
    rule = make_rule(
        rule_id="omni-1",
        description="nsfw policy",
        action=ActionType.MUTE,
        layer=LayerType.OMNI,
        rule_type=RuleType.SEMANTIC,
        category="nsfw",
        priority=ViolationPriority.NSFW,
    )
    await registry.seed([rule])
    result = OmniModerationResult(
        flagged=True,
        categories={"nsfw": True},
        category_scores={"nsfw": 0.9},
    )
    client = FakeOmniClient(result)
    layer = OmniModerationLayer(client, registry, concurrency_limit=1)

    verdict = await layer.evaluate(make_envelope("flagged text"))

    assert client.calls == [("text", "flagged text")]
    assert verdict is not None
    assert verdict.rule_code == "omni-1"
    assert verdict.details["matched_category"] == "nsfw"


@pytest.mark.asyncio
async def test_omni_layer_handles_images() -> None:
    registry = RuleRegistry()
    rule = make_rule(
        rule_id="omni-img",
        description="nsfw image policy",
        action=ActionType.DELETE,
        layer=LayerType.OMNI,
        rule_type=RuleType.SEMANTIC,
        category="sexual",
        priority=ViolationPriority.NSFW,
    )
    await registry.seed([rule])

    image_result = OmniModerationResult(
        flagged=True,
        categories={"sexual": True},
        category_scores={"sexual": 0.99},
    )
    client = FakeOmniClient(
        result=OmniModerationResult(flagged=False, categories={}, category_scores={}),
        image_result=image_result,
    )
    layer = OmniModerationLayer(client, registry, concurrency_limit=1)

    verdict = await layer.evaluate(make_envelope(text="", images=["https://example.com/nsfw.jpg"]))

    assert ("image", "https://example.com/nsfw.jpg") in client.calls
    assert verdict is not None
    assert verdict.rule_code == "omni-img"
    assert verdict.details["source"] == "image"


@pytest.mark.asyncio
async def test_chatgpt_layer_maps_response_to_rule_aliases() -> None:
    registry = RuleRegistry()
    rule = make_rule(
        rule_id="gpt-1",
        description="hate speech policy",
        action=ActionType.BAN,
        layer=LayerType.CHATGPT,
        rule_type=RuleType.CONTEXTUAL,
        category="hate",
        priority=ViolationPriority.HATE,
    )
    # Add alias
    rule.metadata["aliases"] = ["harassment"]
    await registry.seed([rule])

    payload = {
        "violation": True,
        "category": "harassment",
        "severity": "hate",
        "action": "ban",
        "reason": "explicit harassment",
    }
    completion = ChatCompletionResult(
        content=json.dumps(payload),
        finish_reason="stop",
        tokens=42,
    )
    client = FakeGPTClient(completion)
    layer = ChatGPTLayer(client, registry, concurrency_limit=1)

    verdict = await layer.evaluate(make_envelope("contextual abuse"))

    assert client.calls == 1
    assert verdict is not None
    assert verdict.rule_code == "gpt-1"
    assert verdict.action == ActionType.BAN
    assert verdict.priority == ViolationPriority.HATE


@pytest.mark.asyncio
async def test_chatgpt_layer_handles_invalid_json() -> None:
    registry = RuleRegistry()
    await registry.seed([])
    completion = ChatCompletionResult(
        content="non-json response",
        finish_reason="stop",
        tokens=0,
    )
    client = FakeGPTClient(completion)
    layer = ChatGPTLayer(client, registry, concurrency_limit=1)

    verdict = await layer.evaluate(make_envelope("message"))

    assert verdict is None


@pytest.mark.asyncio
async def test_chatgpt_layer_includes_image_context() -> None:
    registry = RuleRegistry()
    await registry.seed([])
    payload = {
        "violation": True,
        "category": "violence",
        "severity": "threats",
        "action": "ban",
        "reason": "Image contains violent content",
    }
    completion = ChatCompletionResult(
        content=json.dumps(payload),
        finish_reason="stop",
        tokens=10,
    )
    client = FakeGPTClient(completion)
    layer = ChatGPTLayer(client, registry, concurrency_limit=1)

    verdict = await layer.evaluate(make_envelope(text="", images=["BASE64IMAGE"]))

    assert verdict is not None
    assert client.last_request is not None
    user_message = next(msg for msg in client.last_request.messages if msg["role"] == "user")
    assert "Images present: 1" in user_message["content"]


@pytest.mark.asyncio
async def test_omni_layer_ignores_categories_without_rules() -> None:
    registry = RuleRegistry()
    await registry.seed([])
    result = OmniModerationResult(
        flagged=True,
        categories={"harassment": True},
        category_scores={"harassment": 0.8},
    )
    client = FakeOmniClient(result)
    layer = OmniModerationLayer(client, registry, concurrency_limit=1)

    verdict = await layer.evaluate(make_envelope("harassing text"))

    assert verdict is None
