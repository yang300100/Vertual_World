"""事实写入：把世界客观事实与人物主观记忆落到 SQLite。

只依赖 `domain`、`repository` 等同位/更底层的模块，不含任何业务规则，
因此可以被领域规则层、流程层、门禁层自由依赖。

**已知例外**：`get_actor` 抛出的 `ActionRuleError` 定义在高层模块 `actions`，
构成一处反向依赖；待该异常下沉到公共底层模块后即可消除。

**为什么这里没有 `observe_event`**：`record_event` 原本在末尾调用
`SocietyService.observe_event`（`society` 模块）。把那次调用一并搬进来，
会让本模块依赖 `society`，而 `society` 又依赖 `actions`（`goal_tick`），
于是形成 `facts -> society -> actions -> facts` 的新环——正是本任务要拆的东西。
因此在本次搬迁中，事件落库后的「观察者推导」这一尾部动作留在
`ActionService._record_event` 的薄委托里，由门禁层衔接，行为完全不变。
下一步（Task 3）会把 `observe_event` 下沉到本模块，届时该尾部动作再归位。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from uuid import uuid4

from world_engine.actions import ActionRuleError
from world_engine.domain import ActionType
from world_engine.repository import to_iso, utc_now

__all__ = [
    "actor_exists",
    "change_relationship",
    "get_actor",
    "importance",
    "record_event",
    "record_memory",
    "update_character",
]


def update_character(
    connection: sqlite3.Connection, character_id: str, **changes: object
) -> None:
    if not changes:
        return
    changes["updated_at"] = to_iso(utc_now())
    assignments = ", ".join(f"{column} = ?" for column in changes)
    values = list(changes.values()) + [character_id]
    connection.execute(
        f"UPDATE characters SET {assignments} WHERE id = ?",  # noqa: S608
        values,
    )


def change_relationship(
    connection: sqlite3.Connection,
    world_id: str,
    source_id: str,
    target_id: str,
    affinity_delta: int,
    trust_delta: int,
) -> None:
    now = to_iso(utc_now())
    connection.execute(
        """
        INSERT INTO relationships(
            world_id, source_character_id, target_character_id,
            affinity, trust, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(world_id, source_character_id, target_character_id)
        DO UPDATE SET
            affinity = MAX(-100, MIN(100, affinity + excluded.affinity)),
            trust = MAX(-100, MIN(100, trust + excluded.trust)),
            updated_at = excluded.updated_at
        """,
        (world_id, source_id, target_id, affinity_delta, trust_delta, now),
    )


def record_event(
    connection: sqlite3.Connection,
    *,
    world_id: str,
    tick_id: str,
    occurred_at: datetime,
    event_type: str,
    actor_id: str | None,
    target_id: str | None,
    location_id: str | None,
    summary: str,
    payload: dict[str, object],
    importance: str | None = None,
) -> str:
    event_id = str(uuid4())
    connection.execute(
        """
        INSERT INTO world_events(
            id, world_id, tick_id, occurred_at, event_type, actor_id, target_id,
            location_id, summary, importance, payload_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            world_id,
            tick_id,
            to_iso(occurred_at),
            event_type,
            actor_id,
            target_id,
            location_id,
            summary,
            importance
            or (
                "major"
                if event_type == "world.major_death"
                else "routine"
            ),
            json.dumps(payload, ensure_ascii=False),
            to_iso(utc_now()),
        ),
    )
    return event_id


def record_memory(
    connection: sqlite3.Connection,
    *,
    world_id: str,
    character_id: str,
    event_id: str,
    memory_type: str,
    summary: str,
    importance: int,
) -> None:
    connection.execute(
        """
        INSERT INTO character_memories(
            id, world_id, character_id, event_id, memory_type,
            summary, importance, confidence, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1.0, ?)
        """,
        (
            str(uuid4()),
            world_id,
            character_id,
            event_id,
            memory_type,
            summary,
            importance,
            to_iso(utc_now()),
        ),
    )


def importance(action: ActionType) -> int:
    return {
        ActionType.ACTIVITY: 4,
        ActionType.SOCIALIZE: 6,
        ActionType.ATTACK: 8,
        ActionType.USE: 5,
        ActionType.GATHER: 4,
        ActionType.TRAVEL: 5,
        ActionType.WORK: 4,
        ActionType.EAT: 3,
        ActionType.REST: 2,
        ActionType.IDLE: 1,
    }[action]


def actor_exists(connection: sqlite3.Connection, actor_id: str) -> bool:
    return (
        connection.execute("SELECT 1 FROM characters WHERE id = ?", (actor_id,)).fetchone()
        is not None
    )


def get_actor(connection: sqlite3.Connection, world_id: str, actor_id: str) -> sqlite3.Row:
    actor = connection.execute(
        "SELECT * FROM characters WHERE id = ? AND world_id = ?",
        (actor_id, world_id),
    ).fetchone()
    if actor is None:
        raise ActionRuleError("行动者不存在于当前世界")
    return actor
