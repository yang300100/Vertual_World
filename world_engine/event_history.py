"""客观事件的范围、后果和有证据的因果连接，不推测旧历史的隐藏原因。"""

import json

from world_engine.repository import from_iso

EVENT_HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_profiles (
 event_id TEXT PRIMARY KEY REFERENCES world_events(id) ON DELETE CASCADE,
 world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 scope_type TEXT NOT NULL CHECK(scope_type IN ('global','regional','local','interpersonal')),
 scope_id TEXT, impact_level INTEGER NOT NULL CHECK(impact_level BETWEEN 1 AND 5),
 status TEXT NOT NULL CHECK(status IN ('confirmed','ongoing','resolved','archived')),
 classification_basis TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_causes (
 event_id TEXT NOT NULL REFERENCES world_events(id) ON DELETE CASCADE,
 cause_event_id TEXT NOT NULL REFERENCES world_events(id) ON DELETE CASCADE,
 relation TEXT NOT NULL, PRIMARY KEY(event_id,cause_event_id), CHECK(event_id!=cause_event_id)
);
CREATE TABLE IF NOT EXISTS event_affected_entities (
 event_id TEXT NOT NULL REFERENCES world_events(id) ON DELETE CASCADE,
 entity_id TEXT NOT NULL, entity_type TEXT NOT NULL,
 PRIMARY KEY(event_id,entity_id,entity_type)
);
CREATE INDEX IF NOT EXISTS idx_event_profile_scope ON event_profiles(world_id,scope_type,scope_id);
"""


class EventHistoryService:
    @staticmethod
    def bootstrap(c):
        # 对旧事件只作保守归类，不猜测过去的世界影响、目击者或因果关系。
        c.execute(
            "INSERT OR IGNORE INTO event_profiles SELECT id,world_id,CASE WHEN actor_id IS NOT NULL THEN 'interpersonal' ELSE 'local' END,location_id,1,'confirmed','legacy_conservative' FROM world_events"
        )
        for key, kind in (
            ("actor_id", "character"),
            ("target_id", "character"),
            ("location_id", "location"),
        ):
            c.execute(
                f"INSERT OR IGNORE INTO event_affected_entities SELECT id,{key},? FROM world_events WHERE {key} IS NOT NULL",
                (kind,),
            )

    @classmethod
    def record(cls, c, event_id):
        event = c.execute("SELECT * FROM world_events WHERE id=?", (event_id,)).fetchone()
        if event is None:
            return
        payload = json.loads(event["payload_json"] or "{}")
        kind = event["event_type"]
        local = kind.startswith(("world.resource_", "world.element_", "construction."))
        impact = 2 if kind in {"action.attack", "contract.overdue", "world.player_defeat"} else 1
        if kind == "world.major_death":
            local, impact = True, 3
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        activity_id = payload.get("activity_id") or metadata.get("life_activity_id")
        activity = (
            c.execute(
                "SELECT * FROM character_life_activities WHERE id=? AND world_id=?",
                (activity_id, event["world_id"]),
            ).fetchone()
            if isinstance(activity_id, str)
            else None
        )
        status = (
            "ongoing"
            if activity
            and activity["status"] == "running"
            and activity["finish_event_id"] != event_id
            else "confirmed"
        )
        if kind in {
            "action.life_completed",
            "action.life_cancelled",
            "action.life_interrupted",
            "contract.completed",
            "contract.expired",
            "contract.cancelled",
        } or payload.get("work_completed"):
            status = "resolved"
        c.execute(
            "INSERT OR IGNORE INTO event_profiles VALUES (?,?,?,?,?,?,?)",
            (
                event_id,
                event["world_id"],
                "local" if local else "interpersonal",
                event["location_id"] if local else event["actor_id"],
                impact,
                status,
                "local_rule_v1",
            ),
        )
        c.execute(
            "UPDATE event_profiles SET status=? WHERE event_id=? AND classification_basis='local_rule_v1' AND status!='archived'",
            (status, event_id),
        )
        for entity_type, key in (
            ("character", "actor_id"),
            ("character", "target_id"),
            ("location", "location_id"),
        ):
            if event[key]:
                c.execute(
                    "INSERT OR IGNORE INTO event_affected_entities VALUES (?,?,?)",
                    (event_id, event[key], entity_type),
                )
        sources = []
        if activity:
            if activity["source_event_id"]:
                sources.append((activity["source_event_id"], "activity_result"))
                if status == "resolved":
                    c.execute(
                        "UPDATE event_profiles SET status='resolved' WHERE event_id=?",
                        (activity["source_event_id"],),
                    )
        if (kind.startswith("contract.") or kind == "social.long_term_applied") and isinstance(
            payload.get("request_id"), str
        ):
            request = c.execute(
                "SELECT source_event_id FROM long_term_operation_requests WHERE id=? AND world_id=?",
                (payload["request_id"], event["world_id"]),
            ).fetchone()
            if request and request[0]:
                sources.append((request[0], "contract_result"))
        if kind == "action.reaction" and isinstance(payload.get("source_action_event_id"), str):
            sources.append((payload["source_action_event_id"], "observed_reaction"))
        for source, relation in sources:
            if source != event_id:
                cls.link(c, event_id, source, relation, strict=False)

    @staticmethod
    def link(c, event_id, cause_id, relation="caused_by", *, strict=True):
        event = c.execute("SELECT * FROM world_events WHERE id=?", (event_id,)).fetchone()
        cause = c.execute("SELECT * FROM world_events WHERE id=?", (cause_id,)).fetchone()
        invalid = event is None or cause is None or event_id == cause_id
        if not invalid:
            invalid = event["world_id"] != cause["world_id"] or from_iso(
                cause["occurred_at"]
            ) > from_iso(event["occurred_at"])
        if not invalid:
            invalid = bool(
                c.execute(
                    "WITH RECURSIVE ancestors(id) AS (SELECT cause_event_id FROM event_causes WHERE event_id=? UNION SELECT r.cause_event_id FROM event_causes r JOIN ancestors a ON r.event_id=a.id) SELECT 1 FROM ancestors WHERE id=?",
                    (cause_id, event_id),
                ).fetchone()
            )
        if invalid:
            if strict:
                raise ValueError("因果连接必须属于同一世界，原因不能在结果之后或形成循环")
            return False
        c.execute(
            "INSERT OR IGNORE INTO event_causes VALUES (?,?,?)", (event_id, cause_id, relation)
        )
        return True

    @staticmethod
    def view(c, event_id, observer_id=None):
        profile = c.execute("SELECT * FROM event_profiles WHERE event_id=?", (event_id,)).fetchone()
        if profile is None:
            return None
        if (
            observer_id
            and not c.execute(
                "SELECT 1 FROM character_event_knowledge WHERE character_id=? AND event_id=?",
                (observer_id, event_id),
            ).fetchone()
        ):
            return None
        causes = []
        for row in c.execute(
            "SELECT r.cause_event_id,r.relation,e.summary,e.occurred_at FROM event_causes r JOIN world_events e ON e.id=r.cause_event_id WHERE r.event_id=? ORDER BY e.occurred_at",
            (event_id,),
        ):
            if (
                observer_id
                and not c.execute(
                    "SELECT 1 FROM character_event_knowledge WHERE character_id=? AND event_id=?",
                    (observer_id, row["cause_event_id"]),
                ).fetchone()
            ):
                continue
            item = dict(row)
            if observer_id:
                from world_engine.society import SocietyService

                item["summary"] = SocietyService.mask_text(
                    c, profile["world_id"], observer_id, item["summary"]
                )
            causes.append(item)
        return {
            "scope_type": profile["scope_type"],
            "scope_id": profile["scope_id"] if not observer_id else None,
            "impact_level": profile["impact_level"],
            "status": profile["status"],
            "causes": causes,
        }
