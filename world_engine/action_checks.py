"""服务器持久化动作检定：预览不掷骰，重试不改变同一次尝试。"""

from __future__ import annotations

import hashlib
import json
import secrets
from uuid import uuid4

from world_engine.character_growth import CharacterGrowthService
from world_engine.life import LifeActivityError
from world_engine.repository import to_iso, utc_now

RULE_VERSION = "action-check-v1"


def draw() -> int:
    return secrets.randbelow(100) + 1


def classify(chance: int, roll: int | None) -> str:
    if roll is None or roll <= chance:
        return "success"
    return "partial" if roll <= min(100, chance + 20) else "failure"


class CheckService:
    @classmethod
    def map_plan(cls, connection, actor, record_id):
        if actor["current_room_id"]:
            raise LifeActivityError("请到原观测地点的室外核对现场记录")
        record = connection.execute(
            "SELECT * FROM player_activity_records WHERE id=? AND world_id=? AND player_id=? "
            "AND step_key IN ('road_notes','field_notes') AND status='completed'",
            (record_id, actor["world_id"], actor["id"]),
        ).fetchone()
        if record is None or record["location_id"] != (
            actor["current_location_id"] or actor["location_id"]
        ):
            raise LifeActivityError("需要本人在当前地点留下的真实现场观测记录")
        content = json.loads(record["content_json"])
        observed = content.get("observation") or {}
        if observed.get("room_id"):
            raise LifeActivityError("室内记录不能当作室外测绘依据")
        if not all(key in observed for key in ("longitude", "latitude", "location")):
            raise LifeActivityError("记录缺少可核验的现场依据")
        tool = connection.execute(
            "SELECT i.* FROM item_instances i JOIN item_types t ON t.id=i.item_type_id "
            "WHERE i.world_id=? AND i.container_type='character_inventory' AND i.container_id=? "
            "AND i.owner_character_id=? AND t.category='map' AND i.condition>0 AND i.quantity>0 "
            "ORDER BY i.condition DESC,i.id LIMIT 1",
            (actor["world_id"], actor["id"], actor["id"]),
        ).fetchone()
        if tool is None:
            raise LifeActivityError("需要自己随身携带的地图作为核对参照")
        evidence = {
            "location_id": record["location_id"],
            "longitude": round(float(observed["longitude"]), 5),
            "latitude": round(float(observed["latitude"]), 5),
            "landmarks": sorted(observed.get("visible_landmarks") or []),
        }
        plan = cls.plan(
            connection,
            actor,
            "map_review",
            evidence,
            skill_name="测绘",
            difficulty=35,
            method={"method": "现场记录核对", "minutes": 30},
            tool=tool,
        )
        spec = {
            "name": "现场地图核对",
            "kind": "map_review",
            "duration_minutes": 30,
            "energy_cost": 6,
            "ingredients": [],
            "map_record_id": record_id,
            "map_source_event_id": record["source_event_id"],
            "observation": observed,
            "check_preview": cls.public_plan(plan),
        }
        return spec, plan

    @staticmethod
    def skill(connection, actor, name):
        row = connection.execute(
            "SELECT proficiency FROM character_skill_proficiencies "
            "WHERE world_id=? AND character_id=? AND skill_name=?",
            (actor["world_id"], actor["id"], name),
        ).fetchone()
        return (
            int(row["proficiency"])
            if row
            else (10 if name in json.loads(actor["skills_json"] or "[]") else 0)
        )

    @classmethod
    def plan(
        cls,
        connection,
        actor,
        kind,
        target,
        *,
        skill_name,
        difficulty,
        method,
        minimum=0,
        tool=None,
    ):
        skill = cls.skill(connection, actor, skill_name)
        if skill < minimum:
            raise LifeActivityError(f"{skill_name}熟练度不足，需要至少{minimum}")
        fatigue = -10 if actor["energy"] < 30 else 0
        injury = -10 if actor["health"] < 60 else 0
        tool_bonus = (
            (10 if tool["condition"] >= 75 else 5 if tool["condition"] >= 40 else -5) if tool else 0
        )
        from world_engine.food import FoodService
        from world_engine.repository import from_iso

        at = from_iso(connection.execute("SELECT w.current_time FROM worlds w WHERE id=?", (actor["world_id"],)).fetchone()[0])
        discomfort = -10 if FoodService.discomfort(connection, actor["id"], at) else 0
        modifier = max(-20, min(20, fatigue + injury + tool_bonus + discomfort))
        automatic = skill - difficulty >= 40 and modifier >= 0
        chance = 100 if automatic else max(5, min(95, 50 + skill - difficulty + modifier))
        snapshot = {
            "rule_version": RULE_VERSION,
            "kind": kind,
            "target": target,
            "method": method,
            "skill_name": skill_name,
            "skill": skill,
            "difficulty": difficulty,
            "modifier": modifier,
            "modifiers": {"fatigue": fatigue, "injury": injury, "tool": tool_bonus},
            "tool": {"type": tool["item_type_id"], "condition": tool["condition"]}
            if tool
            else None,
            "chance": chance,
            "automatic": automatic,
        }
        if discomfort:
            snapshot["modifiers"]["food_discomfort"] = discomfort
        encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        previous = connection.execute(
            "SELECT status FROM action_check_attempts WHERE world_id=? AND character_id=? AND fingerprint=?",
            (actor["world_id"], actor["id"], fingerprint),
        ).fetchone()
        if previous and previous["status"] == "resolved":
            raise LifeActivityError(
                "相同目标与条件已经检定；请查看原结果，或补充技能、工具与新证据后再试"
            )
        return {"fingerprint": fingerprint, "snapshot": snapshot}

    @staticmethod
    def public_plan(plan):
        snap = plan["snapshot"]
        return {
            key: snap[key]
            for key in (
                "rule_version",
                "skill_name",
                "skill",
                "difficulty",
                "modifier",
                "modifiers",
                "chance",
                "automatic",
            )
        }

    @staticmethod
    def begin(connection, actor, activity_id, plan):
        existing = connection.execute(
            "SELECT * FROM action_check_attempts WHERE world_id=? AND character_id=? AND fingerprint=?",
            (actor["world_id"], actor["id"], plan["fingerprint"]),
        ).fetchone()
        if existing:
            if existing["status"] == "resolved":
                raise LifeActivityError("当前条件已检定，不能重新掷骰")
            connection.execute(
                "UPDATE action_check_attempts SET activity_id=? WHERE id=?",
                (activity_id, existing["id"]),
            )
            return existing["id"]
        attempt_id = str(uuid4())
        roll = None if plan["snapshot"]["automatic"] else draw()
        connection.execute(
            "INSERT INTO action_check_attempts(id,world_id,character_id,fingerprint,kind,snapshot_json,roll,activity_id,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                attempt_id,
                actor["world_id"],
                actor["id"],
                plan["fingerprint"],
                plan["snapshot"]["kind"],
                json.dumps(plan["snapshot"], ensure_ascii=False),
                roll,
                activity_id,
                to_iso(utc_now()),
            ),
        )
        return attempt_id

    @classmethod
    def resolve(cls, connection, attempt_id, activity_id, event_id):
        row = connection.execute(
            "SELECT * FROM action_check_attempts WHERE id=? AND activity_id=?",
            (attempt_id, activity_id),
        ).fetchone()
        if row is None:
            raise LifeActivityError("检定尝试与活动不匹配")
        snap = json.loads(row["snapshot_json"])
        outcome = row["outcome"] or classify(snap["chance"], row["roll"])
        connection.execute(
            "UPDATE action_check_attempts SET outcome=?,status='resolved',result_event_id=? WHERE id=? AND status='pending'",
            (outcome, event_id, attempt_id),
        )
        if row["status"]=="pending":
            known=snap["skill"]>0
            at=connection.execute("SELECT occurred_at FROM world_events WHERE id=?",(event_id,)).fetchone()[0]
            day=at[:10]
            awarded=connection.execute("SELECT COALESCE(sum(amount),0) FROM skill_practice_awards WHERE world_id=? AND character_id=? AND skill_name=? AND world_day=?",(row["world_id"],row["character_id"],snap["skill_name"],day)).fetchone()[0]
            amount=min(2 if outcome=="success" else 1,max(0,3-awarded)) if known else 0
            if amount:
                connection.execute("INSERT OR IGNORE INTO skill_practice_awards VALUES (?,?,?,?,?,?)",(attempt_id,row["world_id"],row["character_id"],snap["skill_name"],amount,day))
                CharacterGrowthService.gain_skill_proficiency(
                    connection,
                    character_id=row["character_id"],
                    world_id=row["world_id"],
                    skill_name=snap["skill_name"],
                    amount=amount,
                    event_id=event_id,
                    initial_proficiency=min(100, snap["skill"] + amount),
                )
        return {
            "attempt_id": attempt_id,
            **cls.public_plan({"snapshot": snap}),
            "roll": row["roll"],
            "outcome": outcome,
        }

    @classmethod
    def list_for(cls, connection, world_id, actor_id):
        result = []
        for row in connection.execute(
            "SELECT * FROM action_check_attempts WHERE world_id=? AND character_id=? ORDER BY created_at DESC LIMIT 40",
            (world_id, actor_id),
        ):
            snap = json.loads(row["snapshot_json"])
            result.append(
                {
                    "id": row["id"],
                    "kind": row["kind"],
                    "status": row["status"],
                    "activity_id": row["activity_id"],
                    "created_at": row["created_at"],
                    **cls.public_plan({"snapshot": snap}),
                    "roll": row["roll"] if row["status"] == "resolved" else None,
                    "outcome": row["outcome"] if row["status"] == "resolved" else None,
                }
            )
        return result
