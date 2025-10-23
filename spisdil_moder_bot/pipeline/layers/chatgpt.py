from __future__ import annotations

import asyncio
import json
from typing import Optional

import structlog

from ...adapters.openai import ChatCompletionRequest, GPTClient, OpenAIAdapterError
from ...models import (
    ActionType,
    LayerType,
    MessageEnvelope,
    ModerationRule,
    ModerationVerdict,
    ViolationPriority,
)
from ...rules.registry import RuleRegistry
from ..layers.base import ModerationLayer

logger = structlog.get_logger(__name__)


class ChatGPTLayer(ModerationLayer):
    layer_type = LayerType.CHATGPT

    def __init__(
        self,
        client: GPTClient,
        rules: RuleRegistry,
        *,
        model: str = "gpt-5-nano",
        concurrency_limit: int = 2,
    ) -> None:
        super().__init__(priority=30)
        self._client = client
        self._rules = rules
        self._model = model
        self._semaphore = asyncio.Semaphore(concurrency_limit)
        self._system_prompt = (
            "Strict moderation. Output format: single JSON only.\n"
            "{\"violation\":bool,\"category\":str,\"severity\":str,\"action\":str,\"reason\":str}\n"
            "Allowed actions: warn, delete, mute, ban, none (lowercase).\n"
            "You will receive the list of active moderation rules (category, configured action, human description).\n"
            "Flag content only when it clearly violates one of those descriptions and return that exact category.\n"
            "If none apply, respond with violation=false and action='none'.\n"
            "No text before/after JSON. No explanations. No markdown. No reasoning."
        )

    async def evaluate(self, message: MessageEnvelope) -> ModerationVerdict | None:
        text = message.content_text()
        if not text and not message.images:
            logger.debug("chatgpt_skip_no_text", message_id=message.context.message_id)
            return None

        available_rules = await self._rules.get_rules_for_layer(LayerType.CHATGPT, chat_id=message.context.chat_id)
        user_payload = self._build_user_payload(
            message,
            available_rules=[rule for rule in available_rules if rule.category] or None,
        )
        user_content = [
            {
                "type": "text",
                "text": user_payload,
            }
        ]
        if message.images:
            for image in message.images[:4]:
                user_content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": image},
                    }
                )
        request = ChatCompletionRequest(
            model=self._model,
            messages=[
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": user_content},
            ],
            max_completion_tokens=2048,
            response_format={"type": "json_object"},
        )
        async with self._semaphore:
            try:
                logger.debug(
                    "chatgpt_request",
                    message_id=message.context.message_id,
                    model=self._model,
                    user_payload_length=len(user_payload),
                    system_prompt_length=len(self._system_prompt),
                )
                completion = await self._client.complete(request)
                logger.debug(
                    "chatgpt_response_received",
                    message_id=message.context.message_id,
                    finish_reason=completion.finish_reason,
                    total_tokens=completion.tokens,
                    prompt_tokens=completion.prompt_tokens,
                    completion_tokens=completion.completion_tokens,
                    content_length=len(completion.content) if completion.content else 0,
                    content_preview=completion.content[:150] if completion.content else "",
                )
            except OpenAIAdapterError as exc:
                logger.error("chatgpt_api_error", error=str(exc), message_id=message.context.message_id)
                return None

        if completion.finish_reason == "length":
            logger.warning(
                "chatgpt_response_truncated",
                message_id=message.context.message_id,
                total_tokens=completion.tokens,
                prompt_tokens=completion.prompt_tokens,
                completion_tokens=completion.completion_tokens,
                max_allowed=2048,
                reason="Response exceeded max_completion_tokens limit",
            )
            return None

        try:
            data = self._extract_json(completion.content)
            logger.debug(
                "chatgpt_json_parsed",
                message_id=message.context.message_id,
                violation=data.get("violation"),
                category=data.get("category"),
            )
        except json.JSONDecodeError as exc:
            logger.error(
                "chatgpt_invalid_json",
                error=str(exc),
                response=completion.content[:200] if completion.content else "<empty>",
                finish_reason=completion.finish_reason,
                message_id=message.context.message_id,
            )
            return None

        if not data.get("violation"):
            logger.debug("chatgpt_not_flagged", message_id=message.context.message_id)
            return None

        category = str(data.get("category", "other")).lower()
        severity = str(data.get("severity", category)).lower()
        gpt_suggested_action = self._action_from_payload(data.get("action", "warn"))
        rule = await self._resolve_rule(category, chat_id=message.context.chat_id)

        if not rule:
            logger.warning(
                "chatgpt_violation_no_rule",
                category=category,
                severity=severity,
                gpt_suggested_action=gpt_suggested_action.value,
                reason=data.get("reason"),
                message_id=message.context.message_id,
            )
            return None

        logger.info(
            "chatgpt_violation",
            rule_code=rule.rule_id,
            category=category,
            configured_action=rule.action.value,
            message_id=message.context.message_id,
        )
        return ModerationVerdict(
            layer=self.layer_type,
            rule_code=rule.rule_id,
            priority=rule.priority,
            action=rule.action,
            reason=data.get("reason") or rule.description,
            violated=True,
            details={
                "raw": data,
                "total_tokens": completion.tokens,
                "prompt_tokens": completion.prompt_tokens,
                "completion_tokens": completion.completion_tokens,
                "gpt_severity": severity,
                **(
                    {"action_duration_seconds": rule.action_duration_seconds}
                    if rule.action_duration_seconds is not None
                    else {}
                ),
            },
        )

    def _extract_json(self, content: str) -> dict:
        if not content:
            raise json.JSONDecodeError("Empty response from GPT", "", 0)
        stripped = content.strip()
        if not stripped:
            raise json.JSONDecodeError("Empty response after stripping", stripped, 0)
        try:
            return json.loads(stripped.strip("` \n"))
        except json.JSONDecodeError:
            pass
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start != -1 and end != -1 and end > start:
            snippet = stripped[start : end + 1]
            return json.loads(snippet)
        raise json.JSONDecodeError("No JSON object found in response", stripped, 0)

    async def _resolve_rule(self, category: str, *, chat_id: Optional[int]) -> Optional[ModerationRule]:
        rules = await self._rules.get_rules_for_layer(LayerType.CHATGPT, chat_id=chat_id)
        logger.debug(
            "chatgpt_available_rules",
            chat_id=chat_id,
            total_rules=len(rules),
            rules_categories=[r.category for r in rules if r.category],
            searching_for=category,
        )
        best: Optional[ModerationRule] = None
        for rule in rules:
            match = False
            if rule.category and rule.category.lower() == category:
                match = True
            if rule.metadata.get("aliases"):
                aliases = {alias.lower() for alias in rule.metadata["aliases"]}
                if category in aliases:
                    match = True
            if match:
                if best is None or rule.priority > best.priority:
                    best = rule
        if best:
            logger.debug("chatgpt_rule_matched", rule_id=best.rule_id, category=best.category, action=best.action.value)
        return best

    def _priority_from_severity(self, severity: str) -> ViolationPriority:
        mapping = {
            "threats": ViolationPriority.THREATS,
            "nsfw": ViolationPriority.NSFW,
            "hate": ViolationPriority.HATE,
            "spam": ViolationPriority.SPAM,
        }
        return mapping.get(severity, ViolationPriority.OTHER)

    def _action_from_payload(self, action: str) -> ActionType:
        if not action:
            return ActionType.WARN
        normalized = action.strip().lower()
        synonyms = {
            "delete_message": "delete",
            "remove_message": "delete",
            "remove": "delete",
            "kick": "ban",
            "ban_user": "ban",
            "no_action": "none",
            "none": "none",
        }
        normalized = synonyms.get(normalized, normalized)
        try:
            return ActionType(normalized)
        except ValueError:
            logger.warning("chatgpt_unknown_action", action=action)
            return ActionType.WARN

    def _build_user_payload(
        self,
        message: MessageEnvelope,
        *,
        available_rules: Optional[list[ModerationRule]] = None,
    ) -> str:
        context_parts = [
            f"chat_id: {message.context.chat_id}",
            f"user_id: {message.context.user_id}",
            f"message_id: {message.context.message_id}",
            f"timestamp: {message.context.timestamp.isoformat()}",
        ]
        if message.context.username:
            context_parts.append(f"username: @{message.context.username}")
        lines = [
            "Moderation context:",
            *context_parts,
        ]
        if available_rules:
            lines.extend(["", "Active moderation rules (category — action — description):"])
            sorted_rules = sorted(
                available_rules,
                key=lambda rule: (rule.category or "", rule.action.value),
            )
            for rule in sorted_rules:
                lines.append(
                    f"- {rule.category} — {rule.action.value} — {rule.description or 'no description'}"
                )
            categories = ", ".join(
                sorted({rule.category for rule in available_rules if rule.category}, key=str.lower)
            )
            lines.extend(
                [
                    "",
                    "Allowed categories (use one only if the message clearly violates the matching rule):",
                    categories,
                ]
            )
        lines.extend(["", "Message:", message.content_text() or "<empty>"])
        if message.images:
            lines.extend(
                [
                    "",
                    f"Images present: {len(message.images)} (content attached separately for analysis)",
                ]
            )
        return "\n".join(lines)
