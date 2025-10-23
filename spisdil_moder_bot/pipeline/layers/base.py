from __future__ import annotations

import abc
from typing import Protocol, runtime_checkable

from ...models import LayerType, MessageEnvelope, ModerationVerdict


class ModerationLayer(abc.ABC):
    """Base class for moderation layers."""

    layer_type: LayerType

    def __init__(self, priority: int) -> None:
        self.priority = priority

    @abc.abstractmethod
    async def evaluate(self, message: MessageEnvelope) -> ModerationVerdict | None:
        ...

    def __lt__(self, other: "ModerationLayer") -> bool:
        return self.priority < other.priority


@runtime_checkable
class WarmupCapable(Protocol):
    async def warmup(self) -> None:
        ...
