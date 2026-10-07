"""持续行动的真实结束结果沿开始事件返回目标。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from world_engine.domain import ActionProposal, ActionType
from world_engine.repository import to_iso


def bind_execution(
    connection: sqlite3.Connection, world_id: str, event_id: str,
    proposal: ActionProposal, before: dict[str, object], observed: dict[str, object],
) -> None:
    """Yuki 保存开始前的状态，结束时再核对整段行动实际改变了什么。"""
    goal_id = proposal.metadata.get("goal_id")
    if goal_id and not connection.execute(
        "SELECT 1 FROM npc_agency_goals WHERE id=? AND world_id=? AND character_id=?",
        (goal_id, world_id, proposal.actor_id),
    ).fetchone():
        goal_id = None
    for kind in ("movement", "activity"):
        execution = observed.get(kind)
        previous = before.get(kind)
        if not isinstance(execution, dict) or execution == previous:
            continue
        if isinstance(previous, dict) and execution.get("id") == previous.get("id"):
            continue
        connection.execute(
            "INSERT OR IGNORE INTO action_execution_links(execution_kind,execution_id,"
            "world_id,character_id,action_type,source_event_id,goal_id,initial_json) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (kind, execution["id"], world_id, proposal.actor_id, proposal.action.value,
             event_id, goal_id,
             json.dumps(before, ensure_ascii=False)),
        )


def finish_execution(
    connection: sqlite3.Connection, *, kind: str, execution_id: str,
    event_id: str, world_id: str, actor_id: str, at: datetime, status: str,
    source_event_id: str | None = None,
) -> None:
    from world_engine.actions import ActionService

    link = connection.execute(
        "SELECT * FROM action_execution_links WHERE execution_kind=? AND execution_id=?",
        (kind, execution_id),
    ).fetchone()
    if link and link["result_event_id"]:
        return
    event = connection.execute("SELECT summary FROM world_events WHERE id=?", (event_id,)).fetchone()
    action_type = ActionType(link["action_type"]) if link else (
        ActionType.TRAVEL if kind == "movement" else ActionType.ACTIVITY
    )
    origin = link["source_event_id"] if link else source_event_id
    goal_id = link["goal_id"] if link else None
    proposal = ActionProposal(
        actor_id=actor_id, action=action_type, reason=event["summary"],
        metadata={"goal_id": goal_id, "cause_event_ids": [origin] if origin else []},
    )
    observed = ActionService._observed_state(connection, actor_id)
    before = json.loads(link["initial_json"]) if link else observed
    ActionService._record_effect(
        connection, world_id, event_id, proposal, at, before, observed, None, None,
        f"action.{action_type.value}.{status}",
    )
    if not link:
        return
    connection.execute(
        "UPDATE action_execution_links SET result_event_id=?,result_status=? "
        "WHERE execution_kind=? AND execution_id=? AND result_event_id IS NULL",
        (event_id, status, kind, execution_id),
    )
    if goal_id:
        connection.execute(
            "UPDATE npc_agency_steps SET status=?,observed_json=?,reason=? "
            "WHERE goal_id=? AND event_id=?",
            ("completed" if status == "completed" else "rejected",
             json.dumps(observed, ensure_ascii=False), event["summary"], goal_id, origin),
        )
        retry = at if status == "completed" else at + timedelta(minutes=15)
        connection.execute(
            "UPDATE npc_agency_goals SET retry_world_time=?,updated_world_time=?,"
            "status=?,next_step_json=? WHERE id=? AND status IN ('active','blocked')",
            (to_iso(retry), to_iso(at), "active" if status == "completed" else "blocked",
             json.dumps({"result_event_id": event_id, "reason": event["summary"]},
                        ensure_ascii=False), goal_id),
        )
