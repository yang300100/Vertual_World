"""长期倾向由真实经历缓慢改变；原始人格标签保留，不按闲聊刷出新人格。"""

import json
from uuid import uuid4

from world_engine.repository import to_iso, utc_now


class CharacterGrowthService:
    @staticmethod
    def seed_character(c, cid):
        actor = c.execute(
            "SELECT p.*,w.current_time AS world_time FROM characters p JOIN worlds w ON w.id=p.world_id WHERE p.id=?",
            (cid,),
        ).fetchone()
        if actor is None:
            return
        for trait in json.loads(actor["traits_json"] or "[]"):
            if isinstance(trait, str) and trait:
                c.execute(
                    "INSERT OR IGNORE INTO character_traits VALUES (?,?,?,?, 'initial',NULL,?)",
                    (cid, actor["world_id"], trait, 50, actor["world_time"]),
                )

    @classmethod
    def bootstrap(cls, c):
        for row in c.execute("SELECT id FROM characters").fetchall():
            cls.seed_character(c, row[0])

    @classmethod
    def apply(cls, c, event_id):
        event = c.execute("SELECT * FROM world_events WHERE id=?", (event_id,)).fetchone()
        if not event:
            return
        for cid in {event["actor_id"], event["target_id"]} - {None}:
            cls.seed_character(c, cid)
        changes = []
        if event["event_type"] == "action.attack" and event["target_id"]:
            changes.append((event["target_id"], "谨慎", 3, "遭遇真实攻击后更留意风险"))
        elif event["event_type"] == "contract.completed":
            changes.extend(
                [
                    (event["target_id"], "乐于合作", 2, "完成约定，积累合作经历"),
                    (event["actor_id"], "谨慎", -1, "共同约定得到履行，戒备略有缓和"),
                ]
            )
        for cid, trait, delta, reason in changes:
            if (
                not cid
                or c.execute(
                    "SELECT 1 FROM character_trait_changes WHERE character_id=? AND trait=? AND event_id=?",
                    (cid, trait, event_id),
                ).fetchone()
            ):
                continue
            cls.seed_character(c, cid)
            row = c.execute(
                "SELECT intensity FROM character_traits WHERE character_id=? AND trait=?",
                (cid, trait),
            ).fetchone()
            before = row[0] if row else 0
            used = c.execute(
                "SELECT COALESCE(sum(abs(after_value-before_value)),0) FROM character_trait_changes WHERE character_id=? AND trait=? AND world_day=?",
                (cid, trait, event["occurred_at"][:10]),
            ).fetchone()[0]
            change = min(abs(delta), max(0, 5 - used)) * (1 if delta > 0 else -1)
            after = max(0, min(100, before + change))
            if before == after:
                continue
            c.execute(
                "INSERT INTO character_traits VALUES (?,?,?,?,'acquired',?,?) ON CONFLICT(character_id,trait) DO UPDATE SET intensity=excluded.intensity,updated_world_time=excluded.updated_world_time",
                (cid, event["world_id"], trait, after, event_id, event["occurred_at"]),
            )
            c.execute(
                "INSERT INTO character_trait_changes VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    str(uuid4()),
                    cid,
                    event["world_id"],
                    trait,
                    event_id,
                    before,
                    after,
                    event["occurred_at"][:10],
                    event["occurred_at"],
                    reason,
                ),
            )

    @staticmethod
    def gain_skill_proficiency(
        c,
        *,
        character_id,
        world_id,
        skill_name,
        amount,
        event_id=None,
        initial_proficiency=None,
    ):
        """提升技能熟练度（上限 100），并用来源事件覆盖追溯依据。

        此前有四处各写一份 UPSERT，增量值 5/10/amount 散落在调用点。
        `initial_proficiency` 用于首次入库：检定路径会用检定快照里的技能值起算，
        其余路径从增量本身起算。
        """
        if amount <= 0:
            return
        now = to_iso(utc_now())
        start = amount if initial_proficiency is None else initial_proficiency
        c.execute(
            """
            INSERT INTO character_skill_proficiencies(
                character_id, world_id, skill_name, proficiency, source_event_id, updated_at
            ) VALUES (?, ?, ?, MIN(100, ?), ?, ?)
            ON CONFLICT(character_id, skill_name) DO UPDATE SET
                proficiency = MIN(100, character_skill_proficiencies.proficiency + ?),
                source_event_id = excluded.source_event_id,
                updated_at = excluded.updated_at
            """,
            (character_id, world_id, skill_name, start, event_id, now, amount),
        )

    @staticmethod
    def level(c, cid, trait):
        row = c.execute(
            "SELECT intensity FROM character_traits WHERE character_id=? AND trait=?", (cid, trait)
        ).fetchone()
        return row[0] if row else 0

    @staticmethod
    def view(c, cid):
        traits = [
            dict(row)
            for row in c.execute(
                "SELECT trait,intensity,origin_type,origin_event_id,updated_world_time FROM character_traits WHERE character_id=? ORDER BY intensity DESC,trait",
                (cid,),
            )
        ]
        changes = [
            dict(row)
            for row in c.execute(
                "SELECT trait,before_value,after_value,event_id,occurred_at,reason FROM character_trait_changes WHERE character_id=? ORDER BY occurred_at DESC,rowid DESC LIMIT 12",
                (cid,),
            )
        ]
        return {"traits": traits, "recent_changes": changes}
