from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from uuid import uuid4

from world_engine.domain import ActionOutcome, ActionProposal, ActionType
from world_engine.repository import to_iso, utc_now


class ActionRuleError(ValueError):
    pass


def _clamp(value: int, minimum: int = 0, maximum: int = 100) -> int:
    return max(minimum, min(maximum, value))


class ActionService:
    """所有人物动作的最终规则门禁和执行入口。"""

    def execute(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        tick_id: str,
        occurred_at: datetime,
        proposal: ActionProposal,
    ) -> ActionOutcome:
        try:
            actor = self._get_actor(connection, world_id, proposal.actor_id)
            summary, target_id = self._apply(connection, world_id, actor, proposal)
            event_id = self._record_event(
                connection,
                world_id=world_id,
                tick_id=tick_id,
                occurred_at=occurred_at,
                event_type=f"action.{proposal.action.value}",
                actor_id=actor["id"],
                target_id=target_id,
                location_id=actor["location_id"],
                summary=summary,
                payload={"reason": proposal.reason, "metadata": proposal.metadata},
            )
            self._record_memory(
                connection,
                world_id=world_id,
                character_id=actor["id"],
                event_id=event_id,
                memory_type="experienced",
                summary=summary,
                importance=self._importance(proposal.action),
            )
            if target_id:
                self._record_memory(
                    connection,
                    world_id=world_id,
                    character_id=target_id,
                    event_id=event_id,
                    memory_type="experienced",
                    summary=summary,
                    importance=self._importance(proposal.action),
                )
            return ActionOutcome(
                accepted=True,
                actor_id=proposal.actor_id,
                action=proposal.action,
                summary=summary,
                event_id=event_id,
            )
        except ActionRuleError as exc:
            summary = f"{proposal.actor_id}的{proposal.action.value}行动未能成立：{exc}"
            event_id = self._record_event(
                connection,
                world_id=world_id,
                tick_id=tick_id,
                occurred_at=occurred_at,
                event_type="action.rejected",
                actor_id=(
                    proposal.actor_id
                    if self._actor_exists(connection, proposal.actor_id)
                    else None
                ),
                target_id=None,
                location_id=None,
                summary=summary,
                payload={"action": proposal.action.value, "reason": str(exc)},
            )
            return ActionOutcome(
                accepted=False,
                actor_id=proposal.actor_id,
                action=proposal.action,
                summary=summary,
                event_id=event_id,
                rejection_reason=str(exc),
            )

    def _apply(
        self,
        connection: sqlite3.Connection,
        world_id: str,
        actor: sqlite3.Row,
        proposal: ActionProposal,
    ) -> tuple[str, str | None]:
        if proposal.action is ActionType.REST:
            return self._rest(connection, actor), None
        if proposal.action is ActionType.EAT:
            return self._eat(connection, actor), None
        if proposal.action is ActionType.WORK:
            return self._work(connection, actor), None
        if proposal.action is ActionType.TRAVEL:
            return self._travel(connection, world_id, actor, proposal), None
        if proposal.action is ActionType.SOCIALIZE:
            return self._socialize(connection, world_id, actor, proposal)
        if proposal.action is ActionType.IDLE:
            return self._idle(connection, actor), None
        raise ActionRuleError("未知行动类型")

    def _rest(self, connection: sqlite3.Connection, actor: sqlite3.Row) -> str:
        energy = _clamp(actor["energy"] + 28)
        satiety = _clamp(actor["satiety"] - 4)
        self._update_character(connection, actor["id"], energy=energy, satiety=satiety)
        return f"{actor['name']}停下来休息，精力恢复到{energy}。"

    def _eat(self, connection: sqlite3.Connection, actor: sqlite3.Row) -> str:
        if actor["money"] < 3:
            raise ActionRuleError("没有足够的钱购买食物")
        location = connection.execute(
            "SELECT name, resources_json FROM locations WHERE id = ?",
            (actor["location_id"],),
        ).fetchone()
        resources = json.loads(location["resources_json"])
        if int(resources.get("food", 0)) <= 0:
            raise ActionRuleError("当前位置没有可获得的食物")
        resources["food"] = int(resources["food"]) - 1
        connection.execute(
            "UPDATE locations SET resources_json = ? WHERE id = ?",
            (json.dumps(resources, ensure_ascii=False), actor["location_id"]),
        )
        satiety = _clamp(actor["satiety"] + 42)
        money = actor["money"] - 3
        self._update_character(connection, actor["id"], satiety=satiety, money=money)
        return f"{actor['name']}在{location['name']}获得食物，饱食度升到{satiety}。"

    def _work(self, connection: sqlite3.Connection, actor: sqlite3.Row) -> str:
        location = connection.execute(
            "SELECT name, kind FROM locations WHERE id = ?",
            (actor["location_id"],),
        ).fetchone()
        if location["kind"] != "workplace":
            raise ActionRuleError("当前位置不是可以工作的场所")
        if actor["energy"] < 20:
            raise ActionRuleError("精力不足以完成工作")
        energy = _clamp(actor["energy"] - 16)
        satiety = _clamp(actor["satiety"] - 8)
        money = actor["money"] + 9
        self._update_character(
            connection, actor["id"], energy=energy, satiety=satiety, money=money
        )
        return f"{actor['name']}在{location['name']}完成工作，获得9枚货币。"

    def _travel(
        self,
        connection: sqlite3.Connection,
        world_id: str,
        actor: sqlite3.Row,
        proposal: ActionProposal,
    ) -> str:
        if not proposal.destination_id:
            raise ActionRuleError("没有指定目的地")
        destination = connection.execute(
            "SELECT id, name FROM locations WHERE id = ? AND world_id = ?",
            (proposal.destination_id, world_id),
        ).fetchone()
        if destination is None:
            raise ActionRuleError("目的地不存在于当前世界")
        if destination["id"] == actor["location_id"]:
            raise ActionRuleError("人物已经在目的地")
        if actor["energy"] < 8:
            raise ActionRuleError("精力不足以旅行")
        energy = _clamp(actor["energy"] - 7)
        satiety = _clamp(actor["satiety"] - 3)
        self._update_character(
            connection,
            actor["id"],
            energy=energy,
            satiety=satiety,
            location_id=destination["id"],
        )
        return f"{actor['name']}动身前往{destination['name']}。"

    def _socialize(
        self,
        connection: sqlite3.Connection,
        world_id: str,
        actor: sqlite3.Row,
        proposal: ActionProposal,
    ) -> tuple[str, str]:
        if not proposal.target_id:
            raise ActionRuleError("没有指定交流对象")
        target = connection.execute(
            "SELECT * FROM characters WHERE id = ? AND world_id = ?",
            (proposal.target_id, world_id),
        ).fetchone()
        if target is None:
            raise ActionRuleError("交流对象不存在于当前世界")
        if target["id"] == actor["id"]:
            raise ActionRuleError("不能把自己作为交流对象")
        if target["location_id"] != actor["location_id"]:
            raise ActionRuleError("双方不在同一地点")
        self._change_relationship(connection, world_id, actor["id"], target["id"], 3, 2)
        self._change_relationship(connection, world_id, target["id"], actor["id"], 2, 1)
        self._update_character(
            connection,
            actor["id"],
            energy=_clamp(actor["energy"] - 4),
            satiety=_clamp(actor["satiety"] - 2),
        )
        return f"{actor['name']}与{target['name']}进行了一次交流。", target["id"]

    def _idle(self, connection: sqlite3.Connection, actor: sqlite3.Row) -> str:
        energy = _clamp(actor["energy"] + 2)
        satiety = _clamp(actor["satiety"] - 2)
        self._update_character(connection, actor["id"], energy=energy, satiety=satiety)
        return f"{actor['name']}观察周围，没有采取重大行动。"

    @staticmethod
    def _get_actor(
        connection: sqlite3.Connection, world_id: str, actor_id: str
    ) -> sqlite3.Row:
        actor = connection.execute(
            "SELECT * FROM characters WHERE id = ? AND world_id = ?",
            (actor_id, world_id),
        ).fetchone()
        if actor is None:
            raise ActionRuleError("行动者不存在于当前世界")
        return actor

    @staticmethod
    def _actor_exists(connection: sqlite3.Connection, actor_id: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM characters WHERE id = ?", (actor_id,)
        ).fetchone() is not None

    @staticmethod
    def _update_character(
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

    @staticmethod
    def _change_relationship(
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

    @staticmethod
    def _record_event(
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
    ) -> str:
        event_id = str(uuid4())
        connection.execute(
            """
            INSERT INTO world_events(
                id, world_id, tick_id, occurred_at, event_type, actor_id, target_id,
                location_id, summary, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                json.dumps(payload, ensure_ascii=False),
                to_iso(utc_now()),
            ),
        )
        return event_id

    @staticmethod
    def _record_memory(
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

    @staticmethod
    def _importance(action: ActionType) -> int:
        return {
            ActionType.SOCIALIZE: 6,
            ActionType.TRAVEL: 5,
            ActionType.WORK: 4,
            ActionType.EAT: 3,
            ActionType.REST: 2,
            ActionType.IDLE: 1,
        }[action]
