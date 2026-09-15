"""人物经历、情绪、相识与消息传播；派生状态始终保留事实来源。"""

from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

from world_engine.geo import great_circle_distance_km
from world_engine.proximity import same_room
from world_engine.repository import from_iso, to_iso, utc_now

SOCIETY_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_observers (
 event_id TEXT NOT NULL REFERENCES world_events(id) ON DELETE CASCADE,
 character_id TEXT NOT NULL REFERENCES characters(id),
 channel TEXT NOT NULL, PRIMARY KEY(event_id,character_id)
);
CREATE TABLE IF NOT EXISTS character_acquaintances (
 observer_id TEXT NOT NULL REFERENCES characters(id), subject_id TEXT NOT NULL REFERENCES characters(id),
 world_id TEXT NOT NULL REFERENCES worlds(id), known_name TEXT,
 encounters INTEGER NOT NULL DEFAULT 0, shared_experiences INTEGER NOT NULL DEFAULT 0,
 respect INTEGER NOT NULL DEFAULT 0, conflict INTEGER NOT NULL DEFAULT 0,
 first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, last_social_gain TEXT,
 source_event_id TEXT REFERENCES world_events(id) ON DELETE SET NULL, PRIMARY KEY(observer_id,subject_id)
);
CREATE TABLE IF NOT EXISTS character_emotions (
 character_id TEXT PRIMARY KEY REFERENCES characters(id), world_id TEXT NOT NULL REFERENCES worlds(id),
 emotion TEXT NOT NULL, intensity INTEGER NOT NULL, stress INTEGER NOT NULL DEFAULT 0,
 expires_world_time TEXT NOT NULL, source_event_id TEXT REFERENCES world_events(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS character_event_knowledge (
 character_id TEXT NOT NULL REFERENCES characters(id), event_id TEXT NOT NULL REFERENCES world_events(id) ON DELETE CASCADE,
 world_id TEXT NOT NULL REFERENCES worlds(id), source_kind TEXT NOT NULL,
 source_character_id TEXT REFERENCES characters(id), confidence REAL NOT NULL,
 hops INTEGER NOT NULL DEFAULT 0, learned_world_time TEXT NOT NULL,
 PRIMARY KEY(character_id,event_id)
);
CREATE TABLE IF NOT EXISTS player_notifications (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id), recipient_id TEXT NOT NULL REFERENCES characters(id),
 event_id TEXT REFERENCES world_events(id) ON DELETE CASCADE, title TEXT NOT NULL, read_at TEXT, created_at TEXT NOT NULL,
 UNIQUE(recipient_id,event_id)
);
CREATE TABLE IF NOT EXISTS npc_daily_states (
 character_id TEXT PRIMARY KEY REFERENCES characters(id), world_id TEXT NOT NULL REFERENCES worlds(id),
 last_slot TEXT, intention TEXT NOT NULL DEFAULT '', next_world_time TEXT,
 last_outreach_day TEXT, last_error TEXT
);
CREATE TABLE IF NOT EXISTS npc_outreach_jobs (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id), npc_id TEXT NOT NULL REFERENCES characters(id),
 player_id TEXT NOT NULL REFERENCES characters(id), contact_id TEXT REFERENCES character_contacts(id),
 channel TEXT NOT NULL, reason TEXT NOT NULL, source_event_id TEXT REFERENCES world_events(id) ON DELETE SET NULL,
 status TEXT NOT NULL DEFAULT 'pending', claim_time TEXT, attempts INTEGER NOT NULL DEFAULT 0,
 world_day TEXT NOT NULL, result_event_id TEXT REFERENCES world_events(id) ON DELETE SET NULL, error TEXT,
 UNIQUE(world_id,npc_id,player_id,world_day)
);
CREATE INDEX IF NOT EXISTS idx_knowledge_observer ON character_event_knowledge(world_id,character_id,learned_world_time);
"""


class SocietyService:
    @staticmethod
    def bootstrap(c):
        # 旧存档只回填有参与者证据的经历，不根据现在的位置猜测过去的目击者。
        for column in ("actor_id", "target_id"):
            c.execute(
                f"INSERT OR IGNORE INTO character_event_knowledge(character_id,event_id,world_id,source_kind,confidence,hops,learned_world_time) SELECT e.{column},e.id,e.world_id,'experienced',1,0,e.occurred_at FROM world_events e JOIN characters p ON p.id=e.{column} AND p.world_id=e.world_id"
            )
            c.execute(
                f"INSERT OR IGNORE INTO event_observers(event_id,character_id,channel) SELECT e.id,e.{column},'participant' FROM world_events e JOIN characters p ON p.id=e.{column} AND p.world_id=e.world_id"
            )
        for contact in c.execute(
            "SELECT k.*,e.occurred_at AS world_time FROM character_contacts k JOIN world_events e ON e.id=k.source_event_id WHERE k.status='accepted'"
        ).fetchall():
            for observer, subject in (
                (contact["requester_id"], contact["recipient_id"]),
                (contact["recipient_id"], contact["requester_id"]),
            ):
                name = c.execute("SELECT name FROM characters WHERE id=?", (subject,)).fetchone()
                if name is None:
                    continue
                c.execute(
                    "INSERT OR IGNORE INTO character_acquaintances(observer_id,subject_id,world_id,known_name,encounters,shared_experiences,first_seen,last_seen,source_event_id) VALUES (?,?,?,?,1,1,?,?,?)",
                    (
                        observer,
                        subject,
                        contact["world_id"],
                        name[0],
                        contact["world_time"],
                        contact["world_time"],
                        contact["source_event_id"],
                    ),
                )

    @staticmethod
    def _emotion(c, wid, cid, name, intensity, event_id, at):
        c.execute(
            "INSERT INTO character_emotions VALUES (?,?,?,?,?,?,?) ON CONFLICT(character_id) DO UPDATE SET "
            "emotion=excluded.emotion,intensity=excluded.intensity,stress=excluded.stress,"
            "expires_world_time=excluded.expires_world_time,source_event_id=excluded.source_event_id",
            (
                cid,
                wid,
                name,
                min(100, intensity),
                min(100, intensity) if name in {"担忧", "愤怒"} else 0,
                to_iso(at + timedelta(hours=6)),
                event_id,
            ),
        )

    @classmethod
    def observe_event(cls, c, event_id):
        from world_engine.event_history import EventHistoryService
        EventHistoryService.record(c,event_id)
        event = c.execute("SELECT * FROM world_events WHERE id=?", (event_id,)).fetchone()
        if event is None or not event["actor_id"]:
            return
        at = from_iso(event["occurred_at"])
        payload = json.loads(event["payload_json"] or "{}")
        actor = c.execute("SELECT * FROM characters WHERE id=?", (event["actor_id"],)).fetchone()
        target = (
            c.execute("SELECT * FROM characters WHERE id=?", (event["target_id"],)).fetchone()
            if event["target_id"]
            else None
        )
        if actor is None:
            return
        public = event["event_type"] in {
            "action.attack",
            "action.socialize",
            "action.travel",
            "action.movement_started",
            "action.movement_arrived",
            "action.eat",
            "action.work",
            "action.resource_harvested",
            "action.npc_activity_started",
            "action.interior_enter",
            "action.interior_exit",
        }
        speech = event["event_type"] == "action.socialize"
        voice = payload.get("delivery", "normal")
        radius = (
            ({"whisper": 0.003, "normal": 0.02, "shout": 0.1}.get(voice, 0.02)) if speech else 0.1
        )
        observers = {actor["id"]: "participant"}
        if target:
            observers[target["id"]] = "participant"
        if public:
            for person in c.execute(
                "SELECT * FROM characters WHERE world_id=? AND health>0", (event["world_id"],)
            ):
                if (
                    same_room(actor, person)
                    and great_circle_distance_km(
                        actor["longitude"],
                        actor["latitude"],
                        person["longitude"],
                        person["latitude"],
                    )
                    <= radius
                ):
                    observers.setdefault(person["id"], "heard" if speech else "witnessed")
        for cid, channel in observers.items():
            c.execute(
                "INSERT OR IGNORE INTO event_observers VALUES (?,?,?)", (event_id, cid, channel)
            )
            c.execute(
                "INSERT INTO character_event_knowledge VALUES (?,?,?,'experienced',NULL,1,0,?) ON CONFLICT(character_id,event_id) DO UPDATE SET source_kind='experienced',source_character_id=NULL,confidence=1,hops=0,learned_world_time=excluded.learned_world_time",
                (cid, event_id, event["world_id"], to_iso(at)),
            )
        payload["witness_character_ids"] = [
            cid for cid in observers if cid not in {actor["id"], event["target_id"]}
        ]
        payload["information_scope"] = "public" if public and voice != "whisper" else "private"
        c.execute(
            "UPDATE world_events SET payload_json=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False), event_id),
        )
        for cid in observers:
            if event["event_type"] in {
                "social.letter_received",
                "social.letter_sent",
                "social.message_sent",
            }:
                continue
            for subject in (actor, target):
                if subject is None or cid == subject["id"]:
                    continue
                c.execute(
                    "INSERT INTO character_acquaintances(observer_id,subject_id,world_id,encounters,first_seen,last_seen,source_event_id) "
                    "VALUES (?,?,?,1,?,?,?) ON CONFLICT(observer_id,subject_id) DO UPDATE SET "
                    "encounters=encounters+1,last_seen=excluded.last_seen,source_event_id=excluded.source_event_id",
                    (cid, subject["id"], event["world_id"], to_iso(at), to_iso(at), event_id),
                )
        if (
            target
            and event["event_type"] == "social.contact_exchange"
            and payload.get("status") == "accepted"
        ):
            for observer, subject in ((actor, target), (target, actor)):
                c.execute(
                    "UPDATE character_acquaintances SET known_name=? WHERE observer_id=? AND subject_id=?",
                    (subject["name"], observer["id"], subject["id"]),
                )
        if target and speech:
            for first, second in ((actor, target), (target, actor)):
                relation = c.execute(
                    "SELECT * FROM character_acquaintances WHERE observer_id=? AND subject_id=?",
                    (first["id"], second["id"]),
                ).fetchone()
                if not relation["last_social_gain"] or at - from_iso(
                    relation["last_social_gain"]
                ) >= timedelta(hours=6):
                    cls._relationship(c, event["world_id"], first["id"], second["id"], 3, 2)
                    c.execute(
                        "UPDATE character_acquaintances SET shared_experiences=shared_experiences+1,last_social_gain=? WHERE observer_id=? AND subject_id=?",
                        (to_iso(at), first["id"], second["id"]),
                    )
            if voice != "whisper":
                cls.exchange_news(c, event["world_id"], actor["id"], target["id"], at)
        if target and event["event_type"] == "contract.overdue":
            cls._relationship(c, event["world_id"], actor["id"], target["id"], -3, -5)
            cls._emotion(c, event["world_id"], actor["id"], "担忧", 25, event_id, at)
        if target and event["event_type"] == "action.attack":
            cls._relationship(c, event["world_id"], target["id"], actor["id"], -15, -20)
            c.execute(
                "UPDATE character_acquaintances SET conflict=MIN(100,conflict+20) WHERE observer_id=? AND subject_id=?",
                (target["id"], actor["id"]),
            )
            cls._emotion(
                c,
                event["world_id"],
                target["id"],
                "愤怒" if "勇敢" in json.loads(target["traits_json"] or "[]") else "担忧",
                50,
                event_id,
                at,
            )
        if target and event["event_type"] in {"contract.completed", "action.trade"}:
            relation = c.execute(
                "SELECT last_social_gain FROM character_acquaintances WHERE observer_id=? AND subject_id=?",
                (actor["id"], target["id"]),
            ).fetchone()
            if relation and (not relation[0] or at - from_iso(relation[0]) >= timedelta(hours=6)):
                gain = 5 if event["event_type"] == "contract.completed" else 1
                cls._relationship(c, event["world_id"], actor["id"], target["id"], gain, gain)
                c.execute(
                    "UPDATE character_acquaintances SET shared_experiences=shared_experiences+1,respect=MIN(100,respect+?),last_social_gain=? WHERE observer_id=? AND subject_id=?",
                    (gain, to_iso(at), actor["id"], target["id"]),
                )
            cls._emotion(c, event["world_id"], target["id"], "安心", 30, event_id, at)
        player = c.execute(
            "SELECT id FROM characters WHERE world_id=? AND is_player=1", (event["world_id"],)
        ).fetchone()
        if (
            player
            and player["id"] in observers
            and (
                "life_completed" in event["event_type"]
                or event["event_type"]
                in {"action.attack", "contract.expired", "contract.overdue", "contract.completed"}
            )
        ):
            c.execute(
                "INSERT OR IGNORE INTO player_notifications VALUES (?,?,?,?,?,NULL,?)",
                (
                    str(uuid4()),
                    event["world_id"],
                    player["id"],
                    event_id,
                    event["summary"][:180],
                    to_iso(at),
                ),
            )

        from world_engine.epistemics import KnowledgeService
        from world_engine.character_growth import CharacterGrowthService
        KnowledgeService.capture(c,event_id)
        CharacterGrowthService.apply(c,event_id)
        if target and speech and voice != "whisper":
            KnowledgeService.transmit(c,actor["id"],target["id"],event_id)
            KnowledgeService.transmit(c,target["id"],actor["id"],event_id)

    @staticmethod
    def _relationship(c, wid, source, target, affinity, trust):
        c.execute(
            "INSERT INTO relationships(world_id,source_character_id,target_character_id,affinity,trust,updated_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(world_id,source_character_id,target_character_id) DO UPDATE SET "
            "affinity=MAX(-100,MIN(100,affinity+excluded.affinity)),trust=MAX(-100,MIN(100,trust+excluded.trust)),updated_at=excluded.updated_at",
            (wid, source, target, affinity, trust, to_iso(utc_now())),
        )

    @staticmethod
    def exchange_news(c, wid, first, second, at):
        for source, target in ((first, second), (second, first)):
            rows = c.execute(
                "SELECT k.*,e.payload_json FROM character_event_knowledge k JOIN world_events e ON e.id=k.event_id "
                "WHERE k.world_id=? AND k.character_id=? AND k.hops<3 ORDER BY k.learned_world_time DESC LIMIT 8",
                (wid, source),
            ).fetchall()
            transmitted = 0
            for row in rows:
                if json.loads(row["payload_json"] or "{}").get("information_scope") != "public":
                    continue
                added = c.execute(
                    "INSERT OR IGNORE INTO character_event_knowledge VALUES (?,?,?,'heard',?,?,?,?)",
                    (
                        target,
                        row["event_id"],
                        wid,
                        source,
                        max(0.1, row["confidence"] * 0.8),
                        row["hops"] + 1,
                        to_iso(at),
                    ),
                ).rowcount
                transmitted += added
                if transmitted >= 2:
                    break

    @classmethod
    def personal_context(cls, c, wid, cid, at):
        emotion = c.execute(
            "SELECT * FROM character_emotions WHERE character_id=? AND expires_world_time>?",
            (cid, to_iso(at)),
        ).fetchone()
        news = [
            dict(row)
            for row in c.execute(
                "SELECT e.id,e.summary,e.occurred_at,k.source_kind,k.source_character_id,k.confidence,k.hops "
                "FROM character_event_knowledge k JOIN world_events e ON e.id=k.event_id "
                "WHERE k.world_id=? AND k.character_id=? ORDER BY k.learned_world_time DESC LIMIT 8",
                (wid, cid),
            )
        ]
        for entry in news:
            entry["summary"] = cls.mask_text(c, wid, cid, entry["summary"])
        from world_engine.epistemics import KnowledgeService
        from world_engine.character_growth import CharacterGrowthService
        beliefs=KnowledgeService.view(c,cid,at,limit=6)
        for belief in beliefs:
            belief["evidence"]=[{"method":item["method"],"quote":item["quote"][:160],"learned_at":item["learned_at"]} for item in belief["evidence"][:2]]
        growth=CharacterGrowthService.view(c,cid)
        growth["traits"]=growth["traits"][:8]
        growth["recent_changes"]=growth["recent_changes"][:3]
        from world_engine.food import FoodService
        from world_engine.npc_goals import NpcGoalService

        return {"emotion": dict(emotion) if emotion else None, "known_events": news,"beliefs":beliefs,"dispositions":growth,
                "food_discomfort":FoodService.discomfort(c,cid,at),
                "life_goals":[{"title":item["goal"]["title"],"status":item["status"],"progress":item["progress"],"target":item["goal"]["target"],"reason":item["reason"],"current_step":item["next_step"]} for item in NpcGoalService.view(c,cid)]}

    @classmethod
    def mask_text(cls, c, wid, observer, text):
        import re

        labels = {
            row["name"]: cls.label(c, observer, row["id"])
            for row in c.execute(
                "SELECT id,name FROM characters WHERE world_id=? AND id!=?", (wid, observer)
            )
        }
        if not labels or not text:
            return text
        return re.sub(
            "|".join(re.escape(name) for name in sorted(labels, key=len, reverse=True) if name),
            lambda match: labels[match.group()],
            text,
        )

    @staticmethod
    def label(c, observer, subject):
        if observer == subject:
            return c.execute("SELECT name FROM characters WHERE id=?", (subject,)).fetchone()[0]
        row = c.execute(
            "SELECT known_name FROM character_acquaintances WHERE observer_id=? AND subject_id=?",
            (observer, subject),
        ).fetchone()
        if row and row["known_name"]:
            return row["known_name"]
        from world_engine.epistemics import KnowledgeService
        return KnowledgeService.known_label(c,observer,subject)

    @classmethod
    def tick(cls, c, wid, at):
        c.execute(
            "DELETE FROM character_emotions WHERE world_id=? AND expires_world_time<=?",
            (wid, to_iso(at)),
        )
        player = c.execute(
            "SELECT * FROM characters WHERE world_id=? AND is_player=1", (wid,)
        ).fetchone()
        if player is None:
            return
        cls.goal_tick(c, wid, player, at)
        for npc in c.execute(
            "SELECT * FROM characters WHERE world_id=? AND is_player=0 AND health>0", (wid,)
        ).fetchall():
            if (
                same_room(npc, player)
                and great_circle_distance_km(
                    npc["longitude"], npc["latitude"], player["longitude"], player["latitude"]
                )
                <= 0.1
                and not c.execute(
                    "SELECT 1 FROM character_acquaintances WHERE observer_id=? AND subject_id=?",
                    (player["id"], npc["id"]),
                ).fetchone()
            ):
                from world_engine.actions import ActionService

                ActionService._record_event(
                    c,
                    world_id=wid,
                    tick_id=str(uuid4()),
                    occurred_at=at,
                    event_type="action.encounter",
                    actor_id=player["id"],
                    target_id=npc["id"],
                    location_id=player["current_location_id"] or player["location_id"],
                    summary="你留意到附近一位尚未认识的人。",
                    payload={},
                )
            contact = c.execute(
                "SELECT id FROM character_contacts WHERE world_id=? AND status='accepted' AND ((requester_id=? AND recipient_id=?) OR (requester_id=? AND recipient_id=?))",
                (wid, npc["id"], player["id"], player["id"], npc["id"]),
            ).fetchone()
            nearby = (
                same_room(npc, player)
                and great_circle_distance_km(
                    npc["longitude"], npc["latitude"], player["longitude"], player["latitude"]
                )
                <= 0.02
            )
            relation = c.execute(
                "SELECT trust,affinity FROM relationships WHERE world_id=? AND source_character_id=? AND target_character_id=?",
                (wid, npc["id"], player["id"]),
            ).fetchone()
            important = c.execute(
                "SELECT request_id FROM contract_fulfillments WHERE world_id=? AND recipient_id=? AND requester_id=? AND status='active' AND due_world_time<=?",
                (wid, npc["id"], player["id"], to_iso(at + timedelta(days=1))),
            ).fetchone()
            risk = c.execute("SELECT a.* FROM npc_schedule_assessments a JOIN contract_fulfillments f ON f.request_id=a.contract_id WHERE a.character_id=? AND f.requester_id=? AND f.status='active' AND a.reason!=''",(npc["id"],player["id"])).fetchone()
            from world_engine.character_growth import CharacterGrowthService
            if not important and not risk and CharacterGrowthService.level(c,npc["id"],"谨慎")>=60 and (not relation or relation["trust"]<10):
                continue
            cooperative=CharacterGrowthService.level(c,npc["id"],"乐于合作")>=20
            if not (
                important or risk
                or (
                    nearby
                    and (
                        ("热情" in json.loads(npc["traits_json"] or "[]") or cooperative)
                        or relation
                        and relation["trust"] >= 10
                    )
                )
            ):
                continue
            if not nearby and not contact:
                continue
            c.execute(
                "INSERT OR IGNORE INTO npc_outreach_jobs(id,world_id,npc_id,player_id,contact_id,channel,reason,world_day) VALUES (?,?,?,?,?,?,?,?)",
                (
                    str(uuid4()),
                    wid,
                    npc["id"],
                    player["id"],
                    contact["id"] if contact else None,
                    "in_person" if nearby else "letter",
                    "有临近期限的共同事务，可以决定是否提醒对方"
                    if important
                    else "遇见对方，可以根据关系和当前处境决定是否主动交谈",
                    at.date().isoformat(),
                ),
            )
            if risk:
                c.execute("UPDATE npc_outreach_jobs SET reason=?,source_event_id=? WHERE world_id=? AND npc_id=? AND player_id=? AND world_day=? AND status='pending' AND attempts=0",("有临近的约定出现困难："+risk["reason"]+"。可以商议改期，但双方确认前原时间仍然有效。",risk["source_event_id"],wid,npc["id"],player["id"],at.date().isoformat()))

    @staticmethod
    def goal_tick(c, wid, player, at):
        from world_engine.actions import ActionService

        for goal in c.execute(
            "SELECT * FROM player_life_goals WHERE world_id=? AND player_id=? AND status='active'",
            (wid, player["id"]),
        ).fetchall():
            value = 0
            if goal["kind"] == "work":
                value = c.execute(
                    "SELECT count(*) FROM world_events WHERE world_id=? AND actor_id=? AND event_type='action.work' AND json_extract(payload_json,'$.work_completed')=1 AND (? IS NULL OR location_id=?)",
                    (wid, player["id"], goal["target_id"], goal["target_id"]),
                ).fetchone()[0]
            elif goal["kind"] == "learn":
                row = c.execute(
                    "SELECT proficiency FROM character_skill_proficiencies WHERE character_id=? AND skill_name=?",
                    (player["id"], goal["target_id"]),
                ).fetchone()
                value = row[0] if row else 0
            elif goal["kind"] == "friend":
                row = c.execute(
                    "SELECT shared_experiences FROM character_acquaintances WHERE observer_id=? AND subject_id=?",
                    (player["id"], goal["target_id"]),
                ).fetchone()
                value = row[0] if row else 0
            elif goal["kind"] == "reside":
                for lease in c.execute(
                    "SELECT * FROM contract_fulfillments WHERE world_id=? AND requester_id=? AND kind='lodging' AND (? IS NULL OR asset_id=?)",
                    (wid, player["id"], goal["target_id"], goal["target_id"]),
                ):
                    value = max(
                        value,
                        max(
                            0,
                            (
                                min(at, from_iso(lease["due_world_time"]))
                                - from_iso(lease["started_world_time"])
                            ).days,
                        ),
                    )
            if value < goal["quantity"]:
                continue
            c.execute(
                "UPDATE player_life_goals SET status='completed',completed_world_time=? WHERE id=?",
                (to_iso(at), goal["id"]),
            )
            eid = ActionService._record_event(
                c,
                world_id=wid,
                tick_id=goal["id"],
                occurred_at=at,
                event_type="life.goal_completed",
                actor_id=player["id"],
                target_id=None,
                location_id=player["current_location_id"] or player["location_id"],
                summary=f"你达成了个人目标：{goal['title']}。",
                payload={"goal_id": goal["id"], "progress": value},
            )
            c.execute(
                "INSERT OR IGNORE INTO player_notifications VALUES (?,?,?,?,?,NULL,?)",
                (str(uuid4()), wid, player["id"], eid, f"目标达成：{goal['title']}", to_iso(at)),
            )
