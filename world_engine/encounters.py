"""哥哥，真实见面按连续在场分成场次，开口与再次同行仍由本人另行决定。"""

from __future__ import annotations

import json
import math
from datetime import timedelta
from uuid import uuid4

from world_engine.geo import great_circle_distance_km
from world_engine.proximity import same_room
from world_engine.repository import from_iso, to_iso

ENCOUNTER_SCHEMA = """
CREATE TABLE IF NOT EXISTS encounter_episodes (
 id TEXT PRIMARY KEY,
 world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 npc_id TEXT NOT NULL REFERENCES characters(id),
 player_id TEXT NOT NULL REFERENCES characters(id),
 source_event_id TEXT NOT NULL UNIQUE REFERENCES world_events(id),
 started_at TEXT NOT NULL,
 last_seen_at TEXT NOT NULL,
 ended_at TEXT,
 opportunity_expires_at TEXT NOT NULL,
 offered_job_id TEXT UNIQUE REFERENCES npc_outreach_jobs(id) ON DELETE SET NULL,
 offered_at TEXT,
 npc_seen_event_id TEXT REFERENCES world_events(id),
 player_seen_event_id TEXT REFERENCES world_events(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_encounter_open_pair
 ON encounter_episodes(world_id,npc_id,player_id) WHERE ended_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_encounter_pair_history
 ON encounter_episodes(world_id,npc_id,player_id,started_at);
"""


class EncounterService:
    JOB_PREFIX = "真实见面中的交谈机会"
    OPPORTUNITY_MINUTES = 60
    # 哥哥，信件、传闻和关系提议不能证明以前亲眼见过；只沿用真实接触的事件。
    SEEN_EVENT_TYPES = (
        "action.encounter", "action.movement_encounter", "action.socialize",
        "action.approach", "action.attack", "action.trade", "action.work",
        "action.eat", "action.movement_started", "action.movement_arrived",
        "action.npc_activity_started", "action.interior_enter", "action.interior_exit",
        "social.companion_started", "social.appointment_journey_departed",
        "social.appointment_journey_arrived",
    )

    @staticmethod
    def _people(c, wid, npc_id, player_id):
        people = {r["id"]: r for r in c.execute(
            "SELECT * FROM characters WHERE world_id=? AND id IN (?,?)",
            (wid, npc_id, player_id),
        )}
        if len(people) != 2:
            return None
        npc, player = people[npc_id], people[player_id]
        if npc["is_player"] or not player["is_player"]:
            return None
        return npc, player

    @staticmethod
    def _near(people):
        if not people:
            return False
        npc, player = people
        if npc["health"] <= 0 or player["health"] <= 0 or not same_room(npc, player):
            return False
        coordinates = (npc["longitude"], npc["latitude"], player["longitude"], player["latitude"])
        if any(value is None or not math.isfinite(value) for value in coordinates):
            return False
        return great_circle_distance_km(*coordinates) <= 0.02

    @classmethod
    def _seen_before(cls, c, wid, observer, subject, at, *, event_id=None):
        placeholders = ",".join("?" for _ in cls.SEEN_EVENT_TYPES)
        row = c.execute(
            "SELECT e.id FROM world_events e "
            "JOIN character_event_knowledge k ON k.event_id=e.id AND k.character_id=? "
            "JOIN event_observers o ON o.event_id=e.id AND o.character_id=k.character_id "
            "WHERE e.world_id=? AND k.world_id=? AND k.source_kind='experienced' "
            "AND o.channel IN ('participant','witnessed') AND e.occurred_at<=? "
            "AND k.learned_world_time<=? AND (e.actor_id=? OR e.target_id=?) "
            "AND (? IS NULL OR e.id=?) "
            "AND e.event_type IN (" + placeholders + ") "
            "ORDER BY e.occurred_at DESC,e.rowid DESC LIMIT 1",
            (observer, wid, wid, to_iso(at), to_iso(at), subject, subject,
             event_id, event_id, *cls.SEEN_EVENT_TYPES),
        ).fetchone()
        return row[0] if row else None

    @staticmethod
    def _binding(row):
        return {"episode_id": row["id"], **{
            key: row[key] for key in (
                "world_id", "npc_id", "player_id", "source_event_id", "started_at",
                "last_seen_at", "opportunity_expires_at", "offered_job_id", "offered_at",
                "npc_seen_event_id", "player_seen_event_id",
            )
        }}

    @classmethod
    def end_separated(cls, c, wid, character_ids, at):
        """哥哥，实际位置已变化的事件可以结束旧场次，不在事件观察中递归新建见面。"""
        character_ids = tuple(character_ids)
        if not character_ids:
            return
        placeholders = ",".join("?" for _ in character_ids)
        for row in c.execute(
            "SELECT * FROM encounter_episodes WHERE world_id=? AND ended_at IS NULL "
            "AND (npc_id IN (" + placeholders + ") OR player_id IN (" + placeholders + "))",
            (wid, *character_ids, *character_ids),
        ).fetchall():
            if at >= from_iso(row["last_seen_at"]) and not cls._near(
                cls._people(c, wid, row["npc_id"], row["player_id"])
            ):
                c.execute(
                    "UPDATE encounter_episodes SET ended_at=? WHERE id=? AND ended_at IS NULL",
                    (to_iso(at), row["id"]),
                )

    @classmethod
    def observe_pair(cls, c, wid, npc_id, player_id, at):
        """按哥哥眼前的真实位置更新场次；没有观察到分离时，不猜测两人曾经离开。"""
        latest = c.execute(
            "SELECT * FROM encounter_episodes WHERE world_id=? AND npc_id=? AND player_id=? "
            "ORDER BY started_at DESC,rowid DESC LIMIT 1", (wid, npc_id, player_id),
        ).fetchone()
        if latest and at < from_iso(latest["ended_at"] or latest["last_seen_at"]):
            return None
        people = cls._people(c, wid, npc_id, player_id)
        if not cls._near(people):
            if latest and latest["ended_at"] is None:
                c.execute("UPDATE encounter_episodes SET ended_at=? WHERE id=? AND ended_at IS NULL",
                          (to_iso(at), latest["id"]))
            return None
        if latest and latest["ended_at"] is None:
            c.execute("UPDATE encounter_episodes SET last_seen_at=? WHERE id=?", (to_iso(at), latest["id"]))
            return cls.current(c, wid, npc_id, player_id, at)
        npc, player = people
        seen_npc = cls._seen_before(c, wid, npc_id, player_id, at)
        seen_player = cls._seen_before(c, wid, player_id, npc_id, at)
        episode_id = str(uuid4())
        c.execute("SAVEPOINT encounter_observe_pair")
        try:
            from world_engine.event_log import record_event

            event_id = record_event(
                c, world_id=wid, tick_id=str(uuid4()), occurred_at=at,
                event_type="action.encounter", actor_id=npc_id, target_id=player_id,
                location_id=npc["current_location_id"],
                summary="两人在同一场景的实际交谈距离内见面，是否开口仍由各自决定。",
                payload={"encounter_episode_id": episode_id, "talk_range_m": 20},
            )
            c.execute(
                "INSERT INTO encounter_episodes(id,world_id,npc_id,player_id,source_event_id,"
                "started_at,last_seen_at,opportunity_expires_at,npc_seen_event_id,player_seen_event_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (episode_id, wid, npc_id, player_id, event_id, to_iso(at), to_iso(at),
                 to_iso(at + timedelta(minutes=cls.OPPORTUNITY_MINUTES)), seen_npc, seen_player),
            )
            c.execute("RELEASE encounter_observe_pair")
        except Exception:
            c.execute("ROLLBACK TO encounter_observe_pair")
            c.execute("RELEASE encounter_observe_pair")
            raise
        return cls.current(c, wid, npc_id, player_id, at)

    @classmethod
    def current(cls, c, wid, npc_id, player_id, at):
        """哥哥，这里只读真实在场；联系时机过期不等于又发生一次见面。"""
        row = c.execute(
            "SELECT * FROM encounter_episodes WHERE world_id=? AND npc_id=? AND player_id=? "
            "AND ended_at IS NULL", (wid, npc_id, player_id),
        ).fetchone()
        if not row or at < from_iso(row["last_seen_at"]) or not cls._near(cls._people(c, wid, npc_id, player_id)):
            return None
        event = c.execute("SELECT * FROM world_events WHERE id=? AND world_id=?",
                          (row["source_event_id"], wid)).fetchone()
        try:
            valid = bool(
                event and event["event_type"] == "action.encounter"
                and event["actor_id"] == npc_id and event["target_id"] == player_id
                and event["occurred_at"] == row["started_at"]
                and json.loads(event["payload_json"]).get("encounter_episode_id") == row["id"]
                and c.execute(
                    "SELECT COUNT(DISTINCT k.character_id) FROM character_event_knowledge k "
                    "JOIN event_observers o ON o.event_id=k.event_id AND o.character_id=k.character_id "
                    "WHERE k.event_id=? AND k.world_id=? AND k.character_id IN (?,?) "
                    "AND k.source_kind='experienced' AND o.channel='participant' "
                    "AND k.learned_world_time>=? AND k.learned_world_time<=?",
                    (row["source_event_id"], wid, npc_id, player_id,
                     row["started_at"], to_iso(at)),
                ).fetchone()[0] == 2
            )
        except (AttributeError, TypeError, ValueError):
            valid = False
        return cls._binding(row) if valid else None

    @classmethod
    def opportunity(cls, c, wid, npc_id, player_id, at):
        current = cls.current(c, wid, npc_id, player_id, at)
        return current if current and not current["offered_at"] and at < from_iso(
            current["opportunity_expires_at"]
        ) else None

    @classmethod
    def preempt_unclaimed(cls, c, wid, npc_id, player_id, at, reason):
        """哥哥，尚未考虑的见面可给后来发生的正事让位，仍保留本场已经排过机会的记录。"""
        job = c.execute(
            "SELECT j.id FROM npc_outreach_jobs j JOIN encounter_episodes e "
            "ON e.offered_job_id=j.id AND e.source_event_id=j.source_event_id "
            "WHERE j.world_id=? AND j.npc_id=? AND j.player_id=? AND j.world_day=? "
            "AND j.channel='in_person' AND j.status='pending' AND j.attempts=0 "
            "AND j.reason LIKE ?",
            (wid, npc_id, player_id, at.date().isoformat(), cls.JOB_PREFIX + "%"),
        ).fetchone()
        if not job:
            return False
        c.execute("SAVEPOINT encounter_preempt")
        try:
            c.execute("UPDATE encounter_episodes SET offered_job_id=NULL WHERE offered_job_id=?",
                      (job["id"],))
            c.execute("UPDATE npc_outreach_jobs SET source_event_id=NULL,reason=? WHERE id=?",
                      (reason, job["id"]))
            c.execute("RELEASE encounter_preempt")
        except Exception:
            c.execute("ROLLBACK TO encounter_preempt")
            c.execute("RELEASE encounter_preempt")
            raise
        return True

    @classmethod
    def attach(cls, c, job_id, binding, at):
        """只给已有待考虑任务附上本次来源，哥哥原来的次数与优先事项都保留。"""
        if not binding:
            return False
        current = cls.current(c, binding["world_id"], binding["npc_id"], binding["player_id"], at)
        if not current or any(current[key] != binding[key] for key in ("episode_id", "source_event_id")):
            return False
        if at >= from_iso(current["opportunity_expires_at"]):
            return False
        job = c.execute("SELECT * FROM npc_outreach_jobs WHERE id=?", (job_id,)).fetchone()
        if not job or any(job[key] != current[key] for key in ("world_id", "npc_id", "player_id")):
            return False
        if current["offered_job_id"]:
            return bool(current["offered_job_id"] == job_id and cls.for_job(c, job, at))
        if current["offered_at"]:
            return False
        if (
            job["channel"] != "in_person" or job["status"] != "pending" or job["attempts"] != 0
            or job["world_day"] != at.date().isoformat()
            or job["source_event_id"] or (job["reason"] or "").startswith("有临近")
            or c.execute("SELECT 1 FROM relationship_continuity_followups WHERE job_id=?", (job_id,)).fetchone()
        ):
            return False
        c.execute("SAVEPOINT encounter_attach")
        try:
            changed = c.execute(
                "UPDATE npc_outreach_jobs SET source_event_id=?,reason=? WHERE id=? "
                "AND status='pending' AND attempts=0 AND source_event_id IS NULL",
                (current["source_event_id"], cls.JOB_PREFIX + "；可以结合真实处境自主决定交谈、提出邀请或暂不联系。", job_id),
            ).rowcount
            bound = c.execute(
                "UPDATE encounter_episodes SET offered_job_id=?,offered_at=? "
                "WHERE id=? AND offered_at IS NULL",
                (job_id, to_iso(at), current["episode_id"]),
            ).rowcount
            if changed != 1 or bound != 1:
                c.execute("ROLLBACK TO encounter_attach")
            c.execute("RELEASE encounter_attach")
            return changed == bound == 1
        except Exception:
            c.execute("ROLLBACK TO encounter_attach")
            c.execute("RELEASE encounter_attach")
            raise

    @classmethod
    def is_encounter_job(cls, job):
        return bool(job["reason"] and job["reason"].startswith(cls.JOB_PREFIX))

    @classmethod
    def for_job(cls, c, job, at):
        if not cls.is_encounter_job(job) or job["channel"] != "in_person":
            return None
        current = cls.current(c, job["world_id"], job["npc_id"], job["player_id"], at)
        actual = c.execute("SELECT * FROM npc_outreach_jobs WHERE id=?", (job["id"],)).fetchone()
        if (
            not current or not actual or at >= from_iso(current["opportunity_expires_at"])
            or current["offered_job_id"] != job["id"]
            or current["source_event_id"] != job["source_event_id"]
            or actual["status"] not in {"pending", "processing", "failed"}
            or actual["world_day"] != at.date().isoformat()
            or any(actual[key] != job[key] for key in ("world_id", "npc_id", "player_id", "source_event_id", "channel"))
            or not cls.is_encounter_job(actual)
        ):
            return None
        return current

    @classmethod
    def current_job(cls, c, job, at):
        return not cls.is_encounter_job(job) or cls.for_job(c, job, at) is not None

    @classmethod
    def context(cls, c, binding, observer_id):
        """哥哥，只把本人认得的称呼和双方真实共有的往事放进本次见面。"""
        if not binding or observer_id not in {binding["npc_id"], binding["player_id"]}:
            return None
        row = c.execute(
            "SELECT * FROM encounter_episodes WHERE id=? AND world_id=? AND source_event_id=?",
            (binding["episode_id"], binding["world_id"], binding["source_event_id"]),
        ).fetchone()
        if not row or observer_id not in {row["npc_id"], row["player_id"]}:
            return None
        current = cls.current(
            c, row["world_id"], row["npc_id"], row["player_id"], from_iso(row["last_seen_at"]),
        )
        if not current or current["episode_id"] != row["id"]:
            return None
        from world_engine.society import SocietyService

        other_id = row["player_id"] if observer_id == row["npc_id"] else row["npc_id"]
        previous = row["npc_seen_event_id"] if observer_id == row["npc_id"] else row["player_seen_event_id"]
        if previous:
            # 哥哥，旧认知被纠正后，不能靠场次里留下的旧引用继续声称认识对方。
            previous = cls._seen_before(
                c, row["world_id"], observer_id, other_id, from_iso(row["started_at"]),
                event_id=previous,
            )
        shared = [dict(event) for event in c.execute(
            "SELECT e.id AS source_event_id,e.event_type,e.occurred_at,e.summary FROM world_events e "
            "JOIN character_event_knowledge a ON a.event_id=e.id AND a.character_id=? "
            "JOIN character_event_knowledge b ON b.event_id=e.id AND b.character_id=? "
            "JOIN event_observers x ON x.event_id=e.id AND x.character_id=a.character_id "
            "JOIN event_observers y ON y.event_id=e.id AND y.character_id=b.character_id "
            "WHERE e.world_id=? AND a.world_id=e.world_id AND b.world_id=e.world_id "
            "AND e.id<>? AND e.occurred_at<=? "
            "AND a.learned_world_time<=? AND b.learned_world_time<=? "
            "AND a.source_kind='experienced' AND b.source_kind='experienced' "
            "AND x.channel IN ('participant','witnessed') AND y.channel IN ('participant','witnessed') "
            "ORDER BY e.occurred_at DESC,e.rowid DESC LIMIT 3",
            (observer_id, other_id, row["world_id"], row["source_event_id"], row["started_at"],
             row["started_at"], row["started_at"]),
        )]
        for event in shared:
            event["summary"] = SocietyService.event_summary(
                c, row["world_id"], observer_id, event["source_event_id"], event["summary"],
            )
        return {
            "episode_id": row["id"], "source_event_id": row["source_event_id"],
            "started_at": row["started_at"], "kind": "reunion" if previous else "first",
            "other_id": other_id, "other_name": SocietyService.label(c, observer_id, other_id),
            "prior_seen_event_id": previous, "shared_history": shared,
            "scope": "这是当前真实近距见面；以前见过不代表已知姓名、愿意交谈或同意再次同行。",
        }
