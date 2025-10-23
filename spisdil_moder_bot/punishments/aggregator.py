from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

import structlog

from ..models import ActionType, LayerType, ModerationResult, ModerationVerdict, ViolationPriority

logger = structlog.get_logger(__name__)


LAYER_RANK = {
    LayerType.REGEX: 1,
    LayerType.OMNI: 2,
    LayerType.CHATGPT: 3,
}


@dataclass(slots=True)
class PunishmentDecision:
    verdict: ModerationVerdict
    conflicting: list[ModerationVerdict]

    @property
    def action(self) -> ActionType:
        return self.verdict.action


class PunishmentAggregator:
    def decide(self, results: Iterable[ModerationResult]) -> Optional[PunishmentDecision]:
        best: Optional[ModerationVerdict] = None
        conflicts: list[ModerationVerdict] = []

        for result in results:
            verdict = result.verdict
            if not verdict or not verdict.violated:
                continue
            if best is None or self._is_better(verdict, best):
                if best is not None:
                    conflicts.append(best)
                best = verdict
            else:
                conflicts.append(verdict)

        if best is None:
            return None

        logger.info(
            "punishment_decision",
            action=best.action.value,
            rule=best.rule_code,
            layer=best.layer.value,
            priority=int(best.priority),
            conflicts=len(conflicts),
        )
        return PunishmentDecision(verdict=best, conflicting=conflicts)

    def _is_better(self, candidate: ModerationVerdict, current: ModerationVerdict) -> bool:
        candidate_rank = (
            LAYER_RANK.get(candidate.layer, 0),
            int(candidate.priority),
        )
        current_rank = (
            LAYER_RANK.get(current.layer, 0),
            int(current.priority),
        )
        return candidate_rank > current_rank
