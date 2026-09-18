"""世界事件写入的唯一入口。

历史上 `actions.py`、`combat.py`、`removal.py` 各写了一份 `_record_event`，
列集合与重要性语义并不一致，其中战斗路径还漏掉了知识传播登记。
所有事件都应经过这里，以保证三件事同时发生：

1. `world_events` 落库，列集合固定；
2. 因果链、编年史与目击者知识登记（`SocietyService.observe_event`）；
3. `importance` 语义统一（`major` / `routine`）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from uuid import uuid4

from world_engine.repository import to_iso, utc_now

IMPORTANCE_MAJOR = "major"
IMPORTANCE_ROUTINE = "routine"

# 只有这些事件类型会被标记为重大历史事件。
MAJOR_EVENT_TYPES = frozenset({"world.major_death"})


def importance_for(event_type: str) -> str:
    """事件类型到重要性的唯一映射。"""
    return IMPORTANCE_MAJOR if event_type in MAJOR_EVENT_TYPES else IMPORTANCE_ROUTINE


def record_event(
    connection: sqlite3.Connection,
    *,
    world_id: str,
    tick_id: str,
    occurred_at: datetime,
    event_type: str,
    summary: str,
    actor_id: str | None = None,
    target_id: str | None = None,
    location_id: str | None = None,
    payload: dict[str, object] | None = None,
    importance: str | None = None,
) -> str:
    """写入一条世界事件并登记其因果与目击者知识，返回事件 ID。"""
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
            importance or importance_for(event_type),
            json.dumps(payload or {}, ensure_ascii=False),
            to_iso(utc_now()),
        ),
    )
    # 延迟导入：society 依赖事件层，模块级导入会成环。
    from world_engine.society import SocietyService

    SocietyService.observe_event(connection, event_id)
    return event_id
