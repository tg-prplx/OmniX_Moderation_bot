from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Optional

import aiosqlite
import structlog

from ..models import (
    ActionType,
    LayerType,
    ModerationResult,
    ModerationRule,
    RuleType,
    ViolationPriority,
)
from .base import StorageGateway

logger = structlog.get_logger(__name__)


CREATE_RULES = """
CREATE TABLE IF NOT EXISTS moderation_rules (
    rule_id TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    action TEXT NOT NULL,
    source TEXT NOT NULL,
    layer TEXT NOT NULL,
    rule_type TEXT NOT NULL,
    chat_id INTEGER,
    pattern TEXT,
    category TEXT,
    priority INTEGER NOT NULL,
    action_duration_seconds INTEGER,
    metadata_json TEXT NOT NULL
)
"""


CREATE_INCIDENTS = """
CREATE TABLE IF NOT EXISTS moderation_incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id TEXT,
    layer TEXT NOT NULL,
    action TEXT NOT NULL,
    priority INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    occurred_at TEXT NOT NULL,
    reason TEXT,
    payload_json TEXT NOT NULL
)
"""


class SQLiteStorage(StorageGateway):
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute(CREATE_RULES)
        await self._conn.execute(CREATE_INCIDENTS)
        await self._ensure_schema()
        await self._conn.commit()
        logger.info("sqlite_connected", path=str(self._path))

    async def disconnect(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def list_rules(self) -> list[ModerationRule]:
        assert self._conn
        cursor = await self._conn.execute("SELECT * FROM moderation_rules")
        rows = await cursor.fetchall()
        await cursor.close()
        rules = []
        for row in rows:
            metadata = json.loads(row["metadata_json"])
            rule = ModerationRule(
                rule_id=row["rule_id"],
                description=row["description"],
                action=ActionType(row["action"]),
                source=row["source"],
                layer=LayerType(row["layer"]),
                rule_type=RuleType(row["rule_type"]),
                chat_id=row["chat_id"],
                pattern=row["pattern"],
                category=row["category"],
                priority=ViolationPriority(row["priority"]),
                action_duration_seconds=row["action_duration_seconds"],
                metadata=metadata,
            )
            rules.append(rule)
        return rules

    async def upsert_rule(self, rule: ModerationRule) -> None:
        assert self._conn
        await self._conn.execute(
            """
            INSERT INTO moderation_rules (
                rule_id, description, action, source, layer, rule_type,
                chat_id,
                pattern, category, priority, action_duration_seconds, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(rule_id) DO UPDATE SET
                description=excluded.description,
                action=excluded.action,
                source=excluded.source,
                layer=excluded.layer,
                rule_type=excluded.rule_type,
                chat_id=excluded.chat_id,
                pattern=excluded.pattern,
                category=excluded.category,
                priority=excluded.priority,
                action_duration_seconds=excluded.action_duration_seconds,
                metadata_json=excluded.metadata_json
            """,
            (
                rule.rule_id,
                rule.description,
                rule.action.value,
                rule.source,
                rule.layer.value,
                rule.rule_type.value,
                rule.chat_id,
                rule.pattern,
                rule.category,
                int(rule.priority),
                rule.action_duration_seconds,
                json.dumps(rule.metadata),
            ),
        )
        await self._conn.commit()
        logger.info("sqlite_upsert_rule", rule_id=rule.rule_id, layer=rule.layer.value)

    async def delete_rule(self, rule_id: str) -> None:
        assert self._conn
        await self._conn.execute("DELETE FROM moderation_rules WHERE rule_id = ?", (rule_id,))
        await self._conn.commit()
        logger.info("sqlite_delete_rule", rule_id=rule_id)

    async def record_incident(self, result: ModerationResult) -> None:
        await self.record_batch_results([result])

    async def record_batch_results(self, results: Iterable[ModerationResult]) -> None:
        assert self._conn
        entries = []
        for result in results:
            if not result.verdict:
                continue
            ctx = result.message.context
            verdict = result.verdict
            entries.append(
                (
                    verdict.rule_code,
                    verdict.layer.value,
                    verdict.action.value,
                    int(verdict.priority),
                    ctx.chat_id,
                    ctx.user_id,
                    ctx.message_id,
                    ctx.timestamp.isoformat(),
                    verdict.reason,
                    json.dumps(verdict.details),
                )
            )
        if not entries:
            return
        await self._conn.executemany(
            """
            INSERT INTO moderation_incidents (
                rule_id, layer, action, priority, chat_id, user_id,
                message_id, occurred_at, reason, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            entries,
        )
        await self._conn.commit()
        logger.info("sqlite_record_incidents", count=len(entries))

    async def _ensure_schema(self) -> None:
        assert self._conn
        cursor = await self._conn.execute("PRAGMA table_info(moderation_rules)")
        columns = {row[1] for row in await cursor.fetchall()}
        await cursor.close()
        if "action_duration_seconds" not in columns:
            await self._conn.execute(
                "ALTER TABLE moderation_rules ADD COLUMN action_duration_seconds INTEGER"
            )
