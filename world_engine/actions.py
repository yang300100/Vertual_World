from __future__ import annotations

import json
import random
import sqlite3
from datetime import datetime
from uuid import uuid4

from world_engine.domain import ActionOutcome, ActionProposal, ActionType
from world_engine.geo import great_circle_distance_km
from world_engine.movement import MovementService
from world_engine.repository import to_iso, utc_now


class ActionRuleError(ValueError):
    pass


def _clamp(value: int, minimum: int = 0, maximum: int = 100) -> int:
    return max(minimum, min(maximum, value))


class ActionService:
    """所有人物动作的最终规则门禁和执行入口。"""

    def __init__(self) -> None:
        self.movement = MovementService()

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
            summary, target_id = self._apply(
                connection, world_id, actor, proposal, occurred_at
            )
            event_id = self._record_event(
                connection,
                world_id=world_id,
                tick_id=tick_id,
                occurred_at=occurred_at,
                event_type=f"action.{proposal.action.value}",
                actor_id=actor["id"],
                target_id=target_id,
                location_id=self._current_location_id(actor),
                summary=summary,
                payload={
                    "reason": proposal.reason,
                    "metadata": proposal.metadata,
                    "dialogue": proposal.dialogue,
                    "reply": proposal.reply,
                },
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
                    proposal.actor_id if self._actor_exists(connection, proposal.actor_id) else None
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
        occurred_at: datetime,
    ) -> tuple[str, str | None]:
        if proposal.action is ActionType.REST:
            return self._rest(connection, actor), None
        if proposal.action is ActionType.EAT:
            return self._eat(connection, actor), None
        if proposal.action is ActionType.WORK:
            return self._work(connection, actor), None
        if proposal.action is ActionType.TRAVEL:
            return self._travel(
                connection, world_id, actor, proposal, occurred_at
            ), None
        if proposal.action is ActionType.SOCIALIZE:
            return self._socialize(connection, world_id, actor, proposal)
        if proposal.action is ActionType.IDLE:
            return self._idle(connection, actor), None
        if proposal.action is ActionType.ATTACK:
            return self._attack(connection, world_id, actor, proposal)
        if proposal.action is ActionType.GATHER:
            return self._gather(connection, world_id, actor, proposal)
        if proposal.action is ActionType.USE:
            return self._use(connection, world_id, actor, proposal)
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
            "SELECT name, resources_json, longitude, latitude FROM locations WHERE id = ?",
            (self._require_current_location(actor),),
        ).fetchone()
        self._assert_near_location(actor, location)
        resources = json.loads(location["resources_json"])
        if int(resources.get("food", 0)) <= 0:
            raise ActionRuleError("当前位置没有可获得的食物")
        resources["food"] = int(resources["food"]) - 1
        connection.execute(
            "UPDATE locations SET resources_json = ? WHERE id = ?",
            (json.dumps(resources, ensure_ascii=False), self._require_current_location(actor)),
        )
        satiety = _clamp(actor["satiety"] + 42)
        money = actor["money"] - 3
        self._update_character(connection, actor["id"], satiety=satiety, money=money)
        return f"{actor['name']}在{location['name']}获得食物，饱食度升到{satiety}。"

    def _work(self, connection: sqlite3.Connection, actor: sqlite3.Row) -> str:
        location = connection.execute(
            "SELECT name, kind, longitude, latitude FROM locations WHERE id = ?",
            (self._require_current_location(actor),),
        ).fetchone()
        self._assert_near_location(actor, location)
        if location["kind"] != "workplace":
            raise ActionRuleError("当前位置不是可以工作的场所")
        if actor["energy"] < 20:
            raise ActionRuleError("精力不足以完成工作")
        energy = _clamp(actor["energy"] - 16)
        satiety = _clamp(actor["satiety"] - 8)
        money = actor["money"] + 9
        self._update_character(connection, actor["id"], energy=energy, satiety=satiety, money=money)
        return f"{actor['name']}在{location['name']}完成工作，获得9枚货币。"

    def _travel(
        self,
        connection: sqlite3.Connection,
        world_id: str,
        actor: sqlite3.Row,
        proposal: ActionProposal,
        occurred_at: datetime,
    ) -> str:
        if not proposal.destination_id:
            raise ActionRuleError("没有指定目的地")
        destination = connection.execute(
            """
            SELECT id, name, longitude, latitude
            FROM locations WHERE id = ? AND world_id = ?
            """,
            (proposal.destination_id, world_id),
        ).fetchone()
        if destination is None:
            raise ActionRuleError("目的地不存在于当前世界")
        if destination["id"] == self._current_location_id(actor):
            raise ActionRuleError("人物已经在目的地")
        if actor["energy"] < 8:
            raise ActionRuleError("精力不足以旅行")
        energy = _clamp(actor["energy"] - 1)
        satiety = _clamp(actor["satiety"] - 1)
        movement = self.movement.start(
            connection,
            world_id=world_id,
            character_id=actor["id"],
            destination_longitude=destination["longitude"],
            destination_latitude=destination["latitude"],
            world_time=occurred_at,
            record_log=False,
        )
        self._update_character(
            connection, actor["id"], energy=energy, satiety=satiety
        )
        return (
            f"{actor['name']}动身前往{destination['name']}，"
            f"将以{movement.speed_kmh:g}公里/小时持续移动。"
        )

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
        if great_circle_distance_km(
            actor["longitude"],
            actor["latitude"],
            target["longitude"],
            target["latitude"],
        ) > 5.0:
            raise ActionRuleError("双方距离过远，无法交流")
        self._change_relationship(connection, world_id, actor["id"], target["id"], 3, 2)
        self._change_relationship(connection, world_id, target["id"], actor["id"], 2, 1)
        self._update_character(
            connection,
            actor["id"],
            energy=_clamp(actor["energy"] - 4),
            satiety=_clamp(actor["satiety"] - 2),
        )
        dialogue = (
            f"「{actor['name']}」开口道：“{proposal.dialogue}”。" if proposal.dialogue else ""
        )
        reply = f"「{target['name']}」回应道：“{proposal.reply}”。" if proposal.reply else ""
        return (
            f"{actor['name']}与{target['name']}进行了一次交流。{dialogue}{reply}",
            target["id"],
        )

    def _idle(self, connection: sqlite3.Connection, actor: sqlite3.Row) -> str:
        energy = _clamp(actor["energy"] + 2)
        satiety = _clamp(actor["satiety"] - 2)
        self._update_character(connection, actor["id"], energy=energy, satiety=satiety)
        return f"{actor['name']}观察周围，没有采取重大行动。"

    def _attack(
        self,
        connection: sqlite3.Connection,
        world_id: str,
        actor: sqlite3.Row,
        proposal: ActionProposal,
    ) -> tuple[str, str]:
        if not proposal.target_id:
            raise ActionRuleError("攻击需要指定目标")
        target = connection.execute(
            "SELECT * FROM characters WHERE id = ? AND world_id = ?",
            (proposal.target_id, world_id),
        ).fetchone()
        if target is None:
            raise ActionRuleError("目标不存在于当前世界")
        if target["id"] == actor["id"]:
            raise ActionRuleError("不能攻击自己")
        if great_circle_distance_km(
            actor["longitude"],
            actor["latitude"],
            target["longitude"],
            target["latitude"],
        ) > 5.0:
            raise ActionRuleError("攻击目标距离过远")
        if int(target["health"]) <= 0:
            raise ActionRuleError("目标已无战力")

        atk_bonus = self._equipment_bonus(connection, actor, "attack")
        def_bonus = self._equipment_bonus(connection, target, "defense")
        dmg = max(1, random.randint(10, 20) + actor["energy"] // 10 + atk_bonus - def_bonus)
        target_health = max(0, int(target["health"]) - dmg)
        attacker_health = int(actor["health"])
        reply_text = ""
        if target_health > 0:
            t_atk = self._equipment_bonus(connection, target, "attack")
            a_def = self._equipment_bonus(connection, actor, "defense")
            ret = max(1, random.randint(6, 16) + int(target["energy"]) // 10 + t_atk - a_def)
            attacker_health = max(0, attacker_health - ret)
            reply_text = f"「{target['name']}」反手回击，造成 {ret} 点伤害。"
        self._update_character(connection, actor["id"], health=attacker_health)
        self._update_character(connection, target["id"], health=target_health)

        summary = f"{actor['name']}向{target['name']}发起攻击，造成 {dmg} 点伤害。{reply_text}"

        # 目标死亡：重要人物记入重大历史事件
        if target_health <= 0:
            event_type = "world.major_death" if self._is_important(target) else "action.target_down"
            self._record_event(
                connection,
                world_id=world_id,
                tick_id=str(uuid4()),
                occurred_at=utc_now(),
                event_type=event_type,
                actor_id=actor["id"],
                target_id=target["id"],
                location_id=self._current_location_id(actor),
                summary=(
                    f"{target['name']}被击倒。"
                    if event_type == "action.target_down"
                    else f"重要人物{target['name']}在冲突中身亡，此事将载入史册。"
                ),
                payload={"reason": proposal.reason},
            )
        # 公开攻击引来外界代价
        current_location_id = self._current_location_id(actor)
        location_row = (
            connection.execute(
                "SELECT kind FROM locations WHERE id = ?", (current_location_id,)
            ).fetchone()
            if current_location_id
            else None
        )
        location_kind = location_row["kind"] if location_row else None
        if location_kind in ("city", "public"):
            self._record_event(
                connection,
                world_id=world_id,
                tick_id=str(uuid4()),
                occurred_at=utc_now(),
                event_type="world.warden_intervention",
                actor_id=actor["id"],
                target_id=target["id"],
                location_id=current_location_id,
                summary=f"守卫与河务的注意被惊动：{actor['name']}的公开攻击引来了追究。",
                payload={"reason": proposal.reason},
            )
        return summary, target["id"]

    @staticmethod
    def _is_important(target: sqlite3.Row) -> bool:
        if int(target["is_core"]):
            return True
        identity = target["identity"] or ""
        return any(
            key in identity
            for key in (
                "女王",
                "代表",
                "召集人",
                "记录官",
                "首席",
                "行誓者",
                "祭官",
                "灯判",
                "守潮",
                "调度官",
                "井见",
                "海议长",
                "港守",
                "传声人",
            )
        )

    @staticmethod
    def _equipment_bonus(
        connection: sqlite3.Connection, character_row: sqlite3.Row, kind: str
    ) -> int:
        """读取人物装备位的武器(攻击)或护符(防御)加成；技能"剑术/蛮力"等加攻。"""
        if kind == "attack":
            weapon = connection.execute(
                """
                SELECT it.attack_bonus AS b
                FROM item_instances ii JOIN item_types it ON it.id = ii.item_type_id
                WHERE ii.container_id = ? AND ii.container_type = 'character_equipment'
                  AND it.category = 'weapon'
                """,
                (character_row["id"],),
            ).fetchone()
            bonus = weapon["b"] if weapon else 0
            if "skills_json" in character_row.keys() and any(
                key in ("剑术", "蛮力", "搏斗") for key in json.loads(character_row["skills_json"])
            ):
                bonus += 4
            return bonus
        charm = connection.execute(
            """
            SELECT it.defense_bonus AS b
            FROM item_instances ii JOIN item_types it ON it.id = ii.item_type_id
            WHERE ii.container_id = ? AND ii.container_type = 'character_equipment'
              AND it.category = 'charm'
            """,
            (character_row["id"],),
        ).fetchone()
        return charm["b"] if charm else 0

    def _use(
        self,
        connection: sqlite3.Connection,
        world_id: str,
        actor: sqlite3.Row,
        proposal: ActionProposal,
    ) -> tuple[str, str | None]:
        item_name = (proposal.metadata or {}).get("item")
        if not item_name:
            raise ActionRuleError("使用需要指定物品")
        instance = connection.execute(
            """
            SELECT ii.id, it.* FROM item_instances ii
            JOIN item_types it ON it.id = ii.item_type_id
            WHERE ii.container_id = ? AND ii.container_type = 'character_inventory' AND it.name = ?
            """,
            (actor["id"], item_name),
        ).fetchone()
        if instance is None:
            raise ActionRuleError("背包中没有该物品")
        if instance["category"] == "consumable" and instance["heal"] > 0:
            health = min(100, int(actor["health"]) + instance["heal"])
            self._update_character(connection, actor["id"], health=health)
            connection.execute("DELETE FROM item_instances WHERE id = ?", (instance["id"],))
            return f"{actor['name']}使用了{instance['name']}，生命恢复到{health}。", None
        if instance["category"] in ("weapon", "charm"):
            connection.execute(
                "UPDATE item_instances SET container_type = 'character_equipment', "
                "container_id = ? WHERE id = ?",
                (actor["id"], instance["id"]),
            )
            return f"{actor['name']}装备了{instance['name']}。", None
        return f"{actor['name']}使用了{instance['name']}。", None

    def _gather(
        self,
        connection: sqlite3.Connection,
        world_id: str,
        actor: sqlite3.Row,
        proposal: ActionProposal,
    ) -> tuple[str, str | None]:
        item_name = (proposal.metadata or {}).get("item")
        if not item_name:
            raise ActionRuleError("拾取需要指定物品")
        location = connection.execute(
            "SELECT longitude, latitude FROM locations WHERE id = ?",
            (self._require_current_location(actor),),
        ).fetchone()
        self._assert_near_location(actor, location)
        ground = connection.execute(
            """
            SELECT ii.id, it.name FROM item_instances ii
            JOIN item_types it ON it.id = ii.item_type_id
            WHERE ii.container_id = ? AND ii.container_type = 'location_ground' AND it.name = ?
            """,
            (self._require_current_location(actor), item_name),
        ).fetchone()
        if ground is None:
            raise ActionRuleError("此处地上没有这件物品")
        count = connection.execute(
            "SELECT COUNT(*) n FROM item_instances "
            "WHERE container_id = ? AND container_type = 'character_inventory'",
            (actor["id"],),
        ).fetchone()["n"]
        if count >= 2:
            raise ActionRuleError("背包已满，无法拾取")
        connection.execute(
            "UPDATE item_instances SET container_type = 'character_inventory', "
            "container_id = ? WHERE id = ?",
            (actor["id"], ground["id"]),
        )
        return f"{actor['name']}拾起了{ground['name']}。", None

    @staticmethod
    def _assert_near_location(
        actor: sqlite3.Row, location: sqlite3.Row | None
    ) -> None:
        if location is None or great_circle_distance_km(
            actor["longitude"],
            actor["latitude"],
            location["longitude"],
            location["latitude"],
        ) > 5.0:
            raise ActionRuleError("人物当前不在该地点的可交互范围内")

    @staticmethod
    def _current_location_id(actor: sqlite3.Row) -> str | None:
        if "current_location_id" in actor.keys():
            return actor["current_location_id"]
        return actor["location_id"]

    @classmethod
    def _require_current_location(cls, actor: sqlite3.Row) -> str:
        location_id = cls._current_location_id(actor)
        if not location_id:
            raise ActionRuleError("人物当前不在任何已知地点范围内")
        return location_id

    @staticmethod
    def _get_actor(connection: sqlite3.Connection, world_id: str, actor_id: str) -> sqlite3.Row:
        actor = connection.execute(
            "SELECT * FROM characters WHERE id = ? AND world_id = ?",
            (actor_id, world_id),
        ).fetchone()
        if actor is None:
            raise ActionRuleError("行动者不存在于当前世界")
        return actor

    @staticmethod
    def _actor_exists(connection: sqlite3.Connection, actor_id: str) -> bool:
        return (
            connection.execute("SELECT 1 FROM characters WHERE id = ?", (actor_id,)).fetchone()
            is not None
        )

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
            ActionType.ATTACK: 8,
            ActionType.USE: 5,
            ActionType.GATHER: 4,
            ActionType.TRAVEL: 5,
            ActionType.WORK: 4,
            ActionType.EAT: 3,
            ActionType.REST: 2,
            ActionType.IDLE: 1,
        }[action]
