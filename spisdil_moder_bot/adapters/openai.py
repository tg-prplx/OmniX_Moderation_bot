from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from typing import Any, Literal, Optional

import httpx
import structlog
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = structlog.get_logger(__name__)


class OpenAIAdapterError(Exception):
    pass


class OpenAIAdapter:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 15.0,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        self._owns_client = client is None
        self._lock = asyncio.Lock()

    async def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            client = self._client

        retry = AsyncRetrying(
            wait=wait_exponential(multiplier=0.5, min=0.5, max=5.0),
            stop=stop_after_attempt(5),
            retry=retry_if_exception_type((httpx.TimeoutException, httpx.ReadError)),
        )
        async for attempt in retry:
            with attempt:
                logger.debug(
                    "openai_request",
                    path=path,
                    attempt=attempt.retry_state.attempt_number,
                )
                response = await client.post(path, json=payload)
                if response.status_code >= 500:
                    response.raise_for_status()
                if response.status_code >= 400:
                    raise OpenAIAdapterError(f"API error: {response.status_code} {response.text}")
                data = response.json()
                logger.debug(
                    "openai_response",
                    path=path,
                    status=response.status_code,
                )
                return data
        raise OpenAIAdapterError("Retry exhausted")

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


@dataclass(slots=True)
class OmniModerationResult:
    flagged: bool
    categories: dict[str, bool]
    category_scores: dict[str, float]
class OmniModerationClient(OpenAIAdapter):
    async def classify(self, text: str, *, model: str = "omni-moderation-latest") -> OmniModerationResult:
        payload = {
            "model": model,
            "input": [
                {
                    "type": "text",
                    "text": text,
                }
            ],
        }
        logger.debug("omni_api_call", model=model, text_preview=text[:60])
        data = await self.post("/moderations", payload)
        result = data["results"][0]
        return OmniModerationResult(
            flagged=result["flagged"],
            categories=result.get("categories", {}),
            category_scores=result.get("category_scores", {}),
        )

    async def classify_image(
        self,
        image: str | bytes,
        *,
        model: str = "omni-moderation-latest",
    ) -> OmniModerationResult:
        if isinstance(image, bytes):
            encoded = base64.b64encode(image).decode("ascii")
            image_url = f"data:image/png;base64,{encoded}"
        elif isinstance(image, str):
            image_url = image if image.startswith("data:") else image
        else:
            raise TypeError("Unsupported image payload")
        payload = {
            "model": model,
            "input": [
                {
                    "type": "image_url",
                    "image_url": {"url": image_url},
                }
            ],
        }
        logger.debug("omni_api_image_call", model=model)
        data = await self.post("/moderations", payload)
        result = data["results"][0]
        return OmniModerationResult(
            flagged=result["flagged"],
            categories=result.get("categories", {}),
            category_scores=result.get("category_scores", {}),
        )
        logger.debug("omni_api_image_call", model=model)
        data = await self.post("/moderations", payload)
        result = data["results"][0]
        return OmniModerationResult(
            flagged=result["flagged"],
            categories=result.get("categories", {}),
            category_scores=result.get("category_scores", {}),
        )


@dataclass(slots=True)
class ChatCompletionRequest:
    model: str
    messages: list[dict[str, str]]
    temperature: Optional[float] = None
    max_completion_tokens: Optional[int] = 256
    response_format: Optional[dict[str, Any]] = None


@dataclass(slots=True)
class ChatCompletionResult:
    content: str
    finish_reason: Literal["stop", "length", "content_filter"]
    tokens: int
    prompt_tokens: int
    completion_tokens: int


class GPTClient(OpenAIAdapter):
    async def complete(self, request: ChatCompletionRequest) -> ChatCompletionResult:
        payload = {
            "model": request.model,
            "messages": request.messages,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.response_format is not None:
            payload["response_format"] = request.response_format
        if request.max_completion_tokens is not None:
            payload["max_completion_tokens"] = request.max_completion_tokens
        logger.debug("gpt_api_call", model=request.model, messages_count=len(request.messages))
        data = await self.post("/chat/completions", payload)
        choice = data["choices"][0]
        usage = data.get("usage", {})
        return ChatCompletionResult(
            content=choice["message"]["content"],
            finish_reason=choice["finish_reason"],
            tokens=usage.get("total_tokens", 0),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
        )


@dataclass(slots=True)
class RuleSynthesisRequest:
    rule_text: str
    source: str
    desired_action: str


@dataclass(slots=True)
class RuleSynthesisResult:
    rule_type: str
    layer: str
    category: str
    regex: str | None
    priority: int


class RuleSynthesisClient(OpenAIAdapter):
    async def classify_rule(
        self, request: RuleSynthesisRequest, *, model: str = "gpt-5-mini"
    ) -> RuleSynthesisResult:
        payload = {
            "model": model,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Moderation policy assistant. Classify rules into layers. Return ONLY JSON.\n\n"
                        "LAYERS:\n"
                        "1. 'regex' - Pattern matching (e.g., 'block word X', 'ban URLs')\n"
                        "   Fields: regex (pattern), rule_type='regex', priority\n\n"
                        "2. 'omni' - OpenAI Moderation API (AI content detection)\n"
                        "   Fields: category (EXACT match from list below), rule_type='semantic', priority\n"
                        "   VALID CATEGORIES:\n"
                        "   - hate, hate/threatening\n"
                        "   - harassment, harassment/threatening\n"
                        "   - self-harm, self-harm/intent, self-harm/instructions\n"
                        "   - sexual, sexual/minors\n"
                        "   - violence, violence/graphic\n"
                        "   - illicit, illicit/violent\n"
                        "   NO regex field for omni!\n\n"
                        "3. 'chatgpt' - Contextual analysis (custom categories)\n"
                        "   Fields: category (e.g., 'spam', 'advertising', 'trolling'), rule_type='contextual', priority\n"
                        "   NO regex field for chatgpt!\n\n"
                        "RULES:\n"
                        "- Use 'omni' ONLY if category matches list above EXACTLY\n"
                        "- Use 'chatgpt' for all other categories (spam, ads, etc.)\n"
                        "- Never include 'regex' field for omni/chatgpt\n\n"
                        "Return JSON: {rule_type, layer, category, regex (regex only!), priority (0-100)}"
                    ),
                },
                {
                    "role": "user",
                    "content": f"Rule: {request.rule_text}\nSource: {request.source}\nAction: {request.desired_action}",
                },
            ],
        }
        logger.debug("rule_synthesis_call", model=model, text_preview=request.rule_text[:60])
        data = await self.post("/chat/completions", payload)
        content = data["choices"][0]["message"]["content"]
        import json

        try:
            parsed = json.loads(content.strip("` \n"))
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("rule_synthesis_parse_failed", error=str(exc), content=content)
            raise OpenAIAdapterError("Failed to parse rule synthesis response") from exc
        return RuleSynthesisResult(
            rule_type=parsed.get("rule_type", "semantic"),
            layer=parsed.get("layer", "chatgpt"),
            category=parsed.get("category", "other"),
            regex=parsed.get("regex"),
            priority=int(parsed.get("priority", 10)),
        )
