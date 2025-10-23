from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from spisdil_moder_bot.models import (
    ActionType,
    ChatContext,
    LayerType,
    MessageEnvelope,
    ModerationRule,
    RuleSource,
    RuleType,
    ViolationPriority,
)


def make_envelope(
    text: str = "hello world",
    *,
    chat_id: int = 100,
    user_id: int = 10,
    message_id: int = 1,
    timestamp: Optional[datetime] = None,
    caption: Optional[str] = None,
    media_type: Optional[str] = None,
    images: Optional[list[str]] = None,
) -> MessageEnvelope:
    ctx = ChatContext(
        chat_id=chat_id,
        user_id=user_id,
        message_id=message_id,
        timestamp=timestamp or datetime.now(timezone.utc),
        username="tester",
    )
    return MessageEnvelope(
        context=ctx,
        text=text,
        caption=caption,
        media_type=media_type,
        images=images or [],
    )


def make_rule(
    *,
    rule_id: str = "rule-1",
    description: str = "test rule",
    action: ActionType = ActionType.WARN,
    source: RuleSource = "admin",
    layer: LayerType = LayerType.REGEX,
    rule_type: RuleType = RuleType.REGEX,
    chat_id: Optional[int] = None,
    pattern: Optional[str] = None,
    category: Optional[str] = None,
    priority: ViolationPriority = ViolationPriority.OTHER,
    action_duration_seconds: Optional[int] = None,
) -> ModerationRule:
    return ModerationRule(
        rule_id=rule_id,
        description=description,
        action=action,
        source=source,
        layer=layer,
        rule_type=rule_type,
        chat_id=chat_id,
        pattern=pattern,
        category=category,
        priority=priority,
        action_duration_seconds=action_duration_seconds,
        metadata={},
    )
