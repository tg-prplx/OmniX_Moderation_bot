from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, IntEnum
from typing import Literal, Optional


class LayerType(str, Enum):
    REGEX = "regex"
    OMNI = "omni"
    CHATGPT = "chatgpt"


class ViolationPriority(IntEnum):
    THREATS = 100
    NSFW = 80
    HATE = 70
    SPAM = 50
    OTHER = 10


class ActionType(str, Enum):
    DELETE = "delete"
    WARN = "warn"
    MUTE = "mute"
    BAN = "ban"
    NONE = "none"


class RuleType(str, Enum):
    REGEX = "regex"
    SEMANTIC = "semantic"
    CONTEXTUAL = "contextual"


@dataclass(slots=True)
class ChatContext:
    chat_id: int
    user_id: int
    message_id: int
    timestamp: datetime
    username: Optional[str] = None
    language_code: Optional[str] = None


@dataclass(slots=True)
class MessageEnvelope:
    context: ChatContext
    text: Optional[str] = None
    caption: Optional[str] = None
    media_type: Optional[str] = None
    images: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def content_text(self) -> str:
        return self.text or self.caption or ""


@dataclass(slots=True)
class ModerationVerdict:
    layer: LayerType
    rule_code: str
    priority: ViolationPriority
    action: ActionType
    reason: str
    violated: bool
    details: dict = field(default_factory=dict)

    def short_circuit(self) -> bool:
        return self.violated and self.action != ActionType.NONE


@dataclass(slots=True)
class ModerationResult:
    message: MessageEnvelope
    verdict: Optional[ModerationVerdict]
    evaluated_layers: list[LayerType] = field(default_factory=list)


RuleSource = Literal["admin", "auto"]


@dataclass(slots=True)
class ModerationRule:
    rule_id: str
    description: str
    action: ActionType
    source: RuleSource
    layer: LayerType
    rule_type: RuleType
    chat_id: Optional[int] = None
    pattern: Optional[str] = None
    category: Optional[str] = None
    priority: ViolationPriority = ViolationPriority.OTHER
    action_duration_seconds: Optional[int] = None
    metadata: dict = field(default_factory=dict)


__all__ = [
    "ActionType",
    "ChatContext",
    "LayerType",
    "MessageEnvelope",
    "ModerationResult",
    "ModerationRule",
    "ModerationVerdict",
    "RuleSource",
    "RuleType",
    "ViolationPriority",
]
