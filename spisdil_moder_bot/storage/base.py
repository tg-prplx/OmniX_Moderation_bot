from __future__ import annotations

import abc
from typing import Iterable, Protocol

from ..models import ModerationResult, ModerationRule


class RuleRepository(abc.ABC):
    @abc.abstractmethod
    async def list_rules(self) -> list[ModerationRule]:
        ...

    @abc.abstractmethod
    async def upsert_rule(self, rule: ModerationRule) -> None:
        ...

    @abc.abstractmethod
    async def delete_rule(self, rule_id: str) -> None:
        ...


class IncidentRepository(abc.ABC):
    @abc.abstractmethod
    async def record_incident(self, result: ModerationResult) -> None:
        ...

    @abc.abstractmethod
    async def record_batch_results(self, results: Iterable[ModerationResult]) -> None:
        ...


class StorageGateway(RuleRepository, IncidentRepository, abc.ABC):
    """Combined repository interface for convenience."""

    @abc.abstractmethod
    async def connect(self) -> None:
        ...

    @abc.abstractmethod
    async def disconnect(self) -> None:
        ...
