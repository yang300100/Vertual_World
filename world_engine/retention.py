"""数据保留策略：约束世界长期运行时事件衍生表的增长。

`world_events` 是事实来源，永不删除。但由它派生的表会随运行线性膨胀：

- `memory_jobs`：数据库触发器为每个事件建一条整理任务，完成后不再被读取；
- `character_event_knowledge` / `event_observers`：每个事件按参与者扇出，
  直接决定 NPC 记得什么，因此**默认不清理**，只有显式配置保留窗口才归档。

前者安全且收益明确，默认开启；后者涉及玩法语义，默认关闭。
归档表刻意不带索引，只为冷数据留档，不占用主表的查询路径。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from world_engine.repository import to_iso, utc_now

ARCHIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS character_event_knowledge_archive (
    character_id TEXT NOT NULL, event_id TEXT NOT NULL, world_id TEXT NOT NULL,
    source_kind TEXT NOT NULL, confidence REAL NOT NULL, hops INTEGER NOT NULL,
    learned_world_time TEXT NOT NULL, archived_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_observers_archive (
    event_id TEXT NOT NULL, character_id TEXT NOT NULL, channel TEXT NOT NULL,
    archived_at TEXT NOT NULL
);
"""

DEFAULT_MEMORY_JOB_KEEP_RECENT = 500


@dataclass(frozen=True, slots=True)
class RetentionOutcome:
    """一次保留策略执行的统计结果。"""

    memory_jobs_deleted: int = 0
    knowledge_archived: int = 0
    observers_archived: int = 0

    @property
    def touched_anything(self) -> bool:
        return bool(
            self.memory_jobs_deleted or self.knowledge_archived or self.observers_archived
        )


def initialize(connection: sqlite3.Connection) -> None:
    """建立归档表，可在启动时安全重复调用。"""
    connection.executescript(ARCHIVE_SCHEMA)


def prune_completed_memory_jobs(
    connection: sqlite3.Connection,
    world_id: str,
    *,
    keep_recent: int = DEFAULT_MEMORY_JOB_KEEP_RECENT,
) -> int:
    """删除已完成的记忆整理任务，只保留最近若干条用于界面回看。

    触发器为每个事件生成一条任务，`world_events` 有多少事件这里就有多少行，
    而 `status='done'` 的任务不会再被处理逻辑选中，属纯冗余。
    """
    deleted = connection.execute(
        """
        DELETE FROM memory_jobs
        WHERE world_id = ? AND status = 'done' AND id NOT IN (
            SELECT id FROM memory_jobs
            WHERE world_id = ? AND status = 'done'
            ORDER BY created_at DESC, id DESC
            LIMIT ?
        )
        """,
        (world_id, world_id, max(0, keep_recent)),
    ).rowcount
    return int(deleted or 0)


def knowledge_cutoff(current_world_time: datetime, retention_days: int) -> str:
    """计算归档截止世界时间：早于该时刻的目击记录会被归档。"""
    return to_iso(current_world_time - timedelta(days=max(0, retention_days)))


def archive_old_knowledge(
    connection: sqlite3.Connection,
    world_id: str,
    *,
    before_world_time: str,
) -> tuple[int, int]:
    """把截止时间之前的目击与知识记录移入归档表，返回 (知识数, 目击数)。

    注意：这会真正改变 NPC 的记忆范围——被归档的事件不再参与知识检索。
    因此只有显式配置了保留天数才会被调用。
    """
    archived_at = to_iso(utc_now())
    knowledge = connection.execute(
        """
        INSERT INTO character_event_knowledge_archive
        SELECT character_id, event_id, world_id, source_kind, confidence, hops,
               learned_world_time, ?
        FROM character_event_knowledge
        WHERE world_id = ? AND learned_world_time < ?
        """,
        (archived_at, world_id, before_world_time),
    ).rowcount
    connection.execute(
        """
        DELETE FROM character_event_knowledge
        WHERE world_id = ? AND learned_world_time < ?
        """,
        (world_id, before_world_time),
    )
    observers = connection.execute(
        """
        INSERT INTO event_observers_archive
        SELECT o.event_id, o.character_id, o.channel, ?
        FROM event_observers o JOIN world_events e ON e.id = o.event_id
        WHERE e.world_id = ? AND e.occurred_at < ?
        """,
        (archived_at, world_id, before_world_time),
    ).rowcount
    connection.execute(
        """
        DELETE FROM event_observers
        WHERE event_id IN (
            SELECT id FROM world_events WHERE world_id = ? AND occurred_at < ?
        )
        """,
        (world_id, before_world_time),
    )
    return int(knowledge or 0), int(observers or 0)


def apply_retention(
    connection: sqlite3.Connection,
    world_id: str,
    *,
    current_world_time: datetime,
    memory_job_keep_recent: int = DEFAULT_MEMORY_JOB_KEEP_RECENT,
    knowledge_retention_days: int = 0,
) -> RetentionOutcome:
    """按配置执行一轮保留策略。知识归档仅在保留天数大于 0 时启用。"""
    deleted = prune_completed_memory_jobs(
        connection, world_id, keep_recent=memory_job_keep_recent
    )
    archived_knowledge = 0
    archived_observers = 0
    if knowledge_retention_days > 0:
        archived_knowledge, archived_observers = archive_old_knowledge(
            connection,
            world_id,
            before_world_time=knowledge_cutoff(current_world_time, knowledge_retention_days),
        )
    return RetentionOutcome(
        memory_jobs_deleted=deleted,
        knowledge_archived=archived_knowledge,
        observers_archived=archived_observers,
    )
