from __future__ import annotations

import hashlib
import json
import random
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from world_engine.domain import ActionOutcome, ActionProposal, ActionType
from world_engine.geo import great_circle_distance_km
from world_engine.proximity import same_room
from world_engine.repository import to_iso, utc_now

# 战斗交互与撤退出战距离阈值(公里)，与现有 action 规则保持一致。
COMBAT_RANGE_KM = 5.0
WITHDRAW_RANGE_KM = 12.0


@dataclass(slots=True)
class CombatRoundResult:
    """一个确定性战斗回合的产出：结果事件、伤害汇总与是否结束。"""

    outcome: ActionOutcome
    actor_health: int
    target_health: int
    withdrawn: bool
    encounter_status: str
    round_seed: int
    action_points: int = 2
    winner_id: str | None = None
    initiative_order: tuple[str, str] = ()


class CombatResolver:
    """战斗规则裁判器，独立于叙事 Agent。

    随机必须使用保存到 combat_encounters.random_seed 的本地种子，保证回放与调试可复现。
    模型(CombatTacticalAgent)只产出战术偏好，绝不生成命中、伤害、掉落或死亡结论。
    """

    def __init__(self) -> None:
        pass

    def resolve_proposal(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        tick_id: str,
        occurred_at: datetime,
        proposal: ActionProposal,
        intents: list[Any] | None = None,
    ) -> CombatRoundResult:
        """把一次 attack 提案作为确定性战斗回合结算。"""
        if proposal.action is not ActionType.ATTACK:
            raise ValueError("CombatResolver只负责attack动作")
        actor = self._get_character(connection, world_id, proposal.actor_id)
        if actor is None:
            return self._reject(proposal, "攻击者不存在于当前世界")
        if not proposal.target_id:
            return self._reject(proposal, "攻击需要指定目标")
        target = self._get_character(connection, world_id, proposal.target_id)
        if target is None:
            return self._reject(proposal, "目标不存在于当前世界")
        if target["id"] == actor["id"]:
            return self._reject(proposal, "不能攻击自己")
        if not same_room(actor, target):
            return self._reject(proposal, "目标不在同一个室内外空间")
        distance = great_circle_distance_km(
            actor["longitude"], actor["latitude"], target["longitude"], target["latitude"]
        )
        if distance > COMBAT_RANGE_KM:
            return self._reject(proposal, "攻击目标距离超过战斗范围")
        if int(target["health"]) <= 0:
            return self._reject(proposal, "目标已无战力")

        encounter = self._ensure_encounter(connection, world_id, actor, target, occurred_at)
        seed, rng = self._round_rng(encounter, occurred_at)
        return self._resolve_round(
            connection,
            world_id=world_id,
            tick_id=tick_id,
            occurred_at=occurred_at,
            proposal=proposal,
            actor=actor,
            target=target,
            encounter=encounter,
            rng=rng,
            round_seed=seed,
            intents=intents or [],
        )

    def ensure_encounter(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        actor_id: str,
        target_id: str,
        occurred_at: datetime,
    ) -> dict[str, Any] | None:
        actor = self._get_character(connection, world_id, actor_id)
        target = self._get_character(connection, world_id, target_id)
        if actor is None or target is None:
            return None
        return self._ensure_encounter(connection, world_id, actor, target, occurred_at)

    def generate_seed(self, world_id: str, actor_id: str, target_id: str) -> int:
        digest = hashlib.sha256(f"{world_id}:{actor_id}:{target_id}".encode()).digest()
        return int.from_bytes(digest[:8], "big") & 0x7FFFFFFF

    def _ensure_encounter(
        self,
        connection: sqlite3.Connection,
        world_id: str,
        actor: sqlite3.Row,
        target: sqlite3.Row,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        participants = {actor["id"], target["id"]}
        active = connection.execute(
            "SELECT * FROM combat_encounters WHERE world_id = ? AND status = 'active'",
            (world_id,),
        ).fetchall()
        for row in active:
            try:
                ids = set(json.loads(row["participants_json"]))
            except (json.JSONDecodeError, TypeError):
                continue
            if participants <= ids:
                return dict(row)
        encounter_id = str(uuid4())
        seed = self.generate_seed(world_id, actor["id"], target["id"])
        now = to_iso(utc_now())
        connection.execute(
            """
            INSERT INTO combat_encounters(
                id, world_id, status, random_seed, started_at_world,
                participants_json, location_id, created_at, updated_at
            ) VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?)
            """,
            (
                encounter_id,
                world_id,
                seed,
                to_iso(occurred_at),
                json.dumps(sorted(participants), ensure_ascii=False),
                self._location_of(actor),
                now,
                now,
            ),
        )
        return {
            "id": encounter_id,
            "world_id": world_id,
            "status": "active",
            "random_seed": seed,
            "started_at_world": to_iso(occurred_at),
            "participants_json": json.dumps(sorted(participants), ensure_ascii=False),
            "location_id": self._location_of(actor),
        }

    def _resolve_round(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        tick_id: str,
        occurred_at: datetime,
        proposal: ActionProposal,
        actor: sqlite3.Row,
        target: sqlite3.Row,
        encounter: dict[str, Any],
        rng: random.Random,
        round_seed: int,
        intents: list[Any] | None = None,
    ) -> CombatRoundResult:
        intent_by_actor = {
            getattr(item, "actor_id", None): getattr(item, "intent", None)
            for item in (intents or [])
        }
        # 撤退意图：任一方选择后撤时，遭遇以 withdrawn 结束，不交换伤害。
        if self._wants_withdraw(intent_by_actor, actor["id"], target["id"]):
            return self._withdraw_round(
                connection,
                world_id=world_id,
                tick_id=tick_id,
                occurred_at=occurred_at,
                proposal=proposal,
                actor=actor,
                target=target,
                encounter=encounter,
                round_seed=round_seed,
            )

        initiative = {actor["id"]: rng.randint(1, 20), target["id"]: rng.randint(1, 20)}
        first, second = self._order_by_initiative(actor, target, initiative)
        initiative_order = (first["id"], second["id"])

        atk_bonus = self._equipment_bonus(connection, actor, "attack")
        def_bonus = self._equipment_bonus(connection, target, "defense")
        dmg = max(1, rng.randint(10, 20) + int(actor["energy"]) // 10 + atk_bonus - def_bonus)
        target_health = max(0, int(target["health"]) - dmg)
        actor_health = int(actor["health"])
        reply_text = ""
        if target_health > 0:
            t_atk = self._equipment_bonus(connection, target, "attack")
            a_def = self._equipment_bonus(connection, actor, "defense")
            ret = max(1, rng.randint(6, 16) + int(target["energy"]) // 10 + t_atk - a_def)
            actor_health = max(0, actor_health - ret)
            reply_text = f"「{target['name']}」反手回击，造成 {ret} 点伤害。"
        self._update_character(connection, actor["id"], health=actor_health)
        self._update_character(connection, target["id"], health=target_health)

        summary = (
            f"{actor['name']}向{target['name']}发起攻击，造成 {dmg} 点伤害。{reply_text}"
            f"（{first['name']}先手，战斗持续）"
        )
        withdrawn = actor_health <= 0 or target_health <= 0
        status = "resolved" if withdrawn else "active"
        winner_id = None
        if withdrawn:
            winner_id = target["id"] if target_health <= 0 else actor["id"]
            self._resolve_encounter(connection, encounter["id"], occurred_at, summary)

        event_id = self._record_event(
            connection,
            world_id=world_id,
            tick_id=tick_id,
            occurred_at=occurred_at,
            event_type="action.attack",
            actor_id=actor["id"],
            target_id=target["id"],
            location_id=self._current_location_id(actor),
            summary=summary,
            payload={
                "reason": proposal.reason,
                "metadata": proposal.metadata,
                "dialogue": proposal.dialogue,
                "reply": proposal.reply,
                "round_seed": round_seed,
                "encounter_id": encounter["id"],
                "initiative": initiative_order,
                "action_points": 2,
            },
        )
        if withdrawn:
            loser = target if target_health <= 0 else actor
            event_type = "world.major_death" if self._is_important(loser) else "action.target_down"
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
                    f"{loser['name']}被击倒。"
                    if event_type == "action.target_down"
                    else f"重要人物{loser['name']}在冲突中身亡，此事将载入史册。"
                ),
                payload={"reason": proposal.reason},
            )
        self._record_memory_task(connection, world_id, event_id, actor["id"], target["id"])

        # 公开攻击引来守卫/河务介入(仅在未撤退时发生)。
        location_kind = self._location_kind(connection, self._current_location_id(actor))
        if location_kind in ("city", "public"):
            self._record_event(
                connection,
                world_id=world_id,
                tick_id=str(uuid4()),
                occurred_at=utc_now(),
                event_type="world.warden_intervention",
                actor_id=actor["id"],
                target_id=target["id"],
                location_id=self._current_location_id(actor),
                summary=f"守卫与河务的注意被惊动：{actor['name']}的公开攻击引来了追究。",
                payload={"reason": proposal.reason},
            )

        return CombatRoundResult(
            outcome=ActionOutcome(
                accepted=True,
                actor_id=proposal.actor_id,
                action=proposal.action,
                summary=summary,
                event_id=event_id,
            ),
            actor_health=actor_health,
            target_health=target_health,
            withdrawn=withdrawn,
            encounter_status=status,
            round_seed=round_seed,
            action_points=2,
            winner_id=winner_id,
            initiative_order=initiative_order,
        )

    def _withdraw_round(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        tick_id: str,
        occurred_at: datetime,
        proposal: ActionProposal,
        actor: sqlite3.Row,
        target: sqlite3.Row,
        encounter: dict[str, Any],
        round_seed: int,
    ) -> CombatRoundResult:
        summary = f"{target['name']}选择后撤，{actor['name']}的攻击暂告一段落，冲突转为周旋。"
        connection.execute(
            """
            UPDATE combat_encounters
            SET status = 'withdrawn', resolved_at_world = ?, summary = ?, updated_at = ?
            WHERE id = ?
            """,
            (to_iso(occurred_at), summary, to_iso(utc_now()), encounter["id"]),
        )
        event_id = self._record_event(
            connection,
            world_id=world_id,
            tick_id=tick_id,
            occurred_at=occurred_at,
            event_type="combat.withdraw",
            actor_id=None,
            target_id=None,
            location_id=self._current_location_id(actor),
            summary=summary,
            payload={"encounter_id": encounter["id"], "round_seed": round_seed},
        )
        return CombatRoundResult(
            outcome=ActionOutcome(
                accepted=True,
                actor_id=proposal.actor_id,
                action=proposal.action,
                summary=summary,
                event_id=event_id,
            ),
            actor_health=int(actor["health"]),
            target_health=int(target["health"]),
            withdrawn=True,
            encounter_status="withdrawn",
            round_seed=round_seed,
            action_points=0,
            initiative_order=(),
        )

    @staticmethod
    def _wants_withdraw(intent_by_actor: dict[Any, Any], actor_id: str, target_id: str) -> bool:
        return (
            intent_by_actor.get(target_id) == "withdraw"
            or intent_by_actor.get(actor_id) == "withdraw"
        )

    @staticmethod
    def _order_by_initiative(
        actor: sqlite3.Row, target: sqlite3.Row, initiative: dict[str, int]
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        if initiative[target["id"]] > initiative[actor["id"]]:
            return target, actor
        return actor, target

    def apply_combat_intent_preference(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        encounter_id: str,
        intents: list[Any],
        occurred_at: datetime,
    ) -> None:
        """记录战斗战术偏好到遭遇的参与者信息，不改变随机种子与验证。"""
        if not intents:
            return
        payload = [intent.model_dump(mode="json") for intent in intents]
        now = to_iso(utc_now())
        connection.execute(
            "UPDATE combat_encounters SET updated_at = ? WHERE id = ? AND world_id = ?",
            (now, encounter_id, world_id),
        )
        # 偏好只作为审计信息附加到 summary，不参与伤害计算。
        summary = f"战术Agent提交了{len(payload)}个战术偏好。"
        self._record_event(
            connection,
            world_id=world_id,
            tick_id=str(uuid4()),
            occurred_at=occurred_at,
            event_type="combat.tactical_preference",
            actor_id=None,
            target_id=None,
            location_id=None,
            summary=summary,
            payload={"encounter_id": encounter_id, "intents": payload},
        )

    def _round_rng(
        self, encounter: dict[str, Any], occurred_at: datetime
    ) -> tuple[int, random.Random]:
        base_seed = int(encounter["random_seed"])
        round_seed = (base_seed + int(occurred_at.timestamp())) & 0x7FFFFFFF
        return round_seed, random.Random(round_seed)

    def _reject(self, proposal: ActionProposal, reason: str) -> CombatRoundResult:
        return CombatRoundResult(
            outcome=ActionOutcome(
                accepted=False,
                actor_id=proposal.actor_id,
                action=proposal.action,
                summary=f"{proposal.actor_id}的{proposal.action.value}行动未能成立：{reason}",
                rejection_reason=reason,
            ),
            actor_health=0,
            target_health=0,
            withdrawn=False,
            encounter_status="active",
            round_seed=0,
        )

    @staticmethod
    def _get_character(
        connection: sqlite3.Connection, world_id: str, character_id: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM characters WHERE id = ? AND world_id = ?",
            (character_id, world_id),
        ).fetchone()

    @staticmethod
    def _location_of(actor: sqlite3.Row) -> str | None:
        location_id = actor["location_id"]
        try:
            return str(location_id)
        except (KeyError, TypeError):
            return None

    @staticmethod
    def _current_location_id(actor: sqlite3.Row) -> str | None:
        if "current_location_id" in actor.keys() and actor["current_location_id"]:
            return actor["current_location_id"]
        return actor["location_id"]

    @staticmethod
    def _location_kind(connection: sqlite3.Connection, location_id: str | None) -> str | None:
        if not location_id:
            return None
        row = connection.execute(
            "SELECT kind FROM locations WHERE id = ?", (location_id,)
        ).fetchone()
        return row["kind"] if row else None

    @staticmethod
    def _equipment_bonus(
        connection: sqlite3.Connection, character_row: sqlite3.Row, kind: str
    ) -> int:
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
    def _resolve_encounter(
        connection: sqlite3.Connection, encounter_id: str, occurred_at: datetime, summary: str
    ) -> None:
        connection.execute(
            """
            UPDATE combat_encounters
            SET status = 'resolved', resolved_at_world = ?, summary = ?, updated_at = ?
            WHERE id = ?
            """,
            (to_iso(occurred_at), summary, to_iso(utc_now()), encounter_id),
        )

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
                "major" if event_type == "world.major_death" else "routine",
                json.dumps(payload, ensure_ascii=False),
                to_iso(utc_now()),
            ),
        )
        return event_id

    @staticmethod
    def _record_memory_task(
        connection: sqlite3.Connection,
        world_id: str,
        event_id: str,
        actor_id: str,
        target_id: str,
    ) -> None:
        now = to_iso(utc_now())
        connection.execute(
            """
            INSERT OR IGNORE INTO memory_jobs(
                id, world_id, event_id, status, attempt_count, created_at, updated_at
            ) VALUES (?, ?, ?, 'pending', 0, ?, ?)
            """,
            ("memjob:" + event_id, world_id, event_id, now, now),
        )


def build_combat_resolver() -> CombatResolver:
    return CombatResolver()
