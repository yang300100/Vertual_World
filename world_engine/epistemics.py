"""观察者各自持有说法与证据；新证据改变判断，旧证据留在历史中。"""

import json
import re
from datetime import timedelta
from uuid import uuid4

from world_engine.geo import great_circle_distance_km
from world_engine.proximity import VOICE_RADIUS_KM, same_room
from world_engine.repository import from_iso, to_iso

EPISTEMIC_SCHEMA = """
CREATE TABLE IF NOT EXISTS observer_knowledge_nodes (
 observer_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
 subject_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
 world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 first_learned_at TEXT NOT NULL,last_learned_at TEXT NOT NULL,
 PRIMARY KEY(observer_id,subject_id)
);
CREATE TABLE IF NOT EXISTS observer_knowledge_edges (
 id TEXT PRIMARY KEY,world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 observer_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
 subject_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
 predicate TEXT NOT NULL CHECK(predicate IN ('name','location')),
 value TEXT NOT NULL,status TEXT NOT NULL,confidence REAL NOT NULL,
 first_learned_at TEXT NOT NULL,last_learned_at TEXT NOT NULL,last_verified_at TEXT,
 UNIQUE(observer_id,subject_id,predicate,value)
);
CREATE TABLE IF NOT EXISTS knowledge_evidence (
 id TEXT PRIMARY KEY,edge_id TEXT NOT NULL REFERENCES observer_knowledge_edges(id) ON DELETE CASCADE,
 source_event_id TEXT NOT NULL REFERENCES world_events(id) ON DELETE CASCADE,
 source_character_id TEXT REFERENCES characters(id) ON DELETE SET NULL,
 origin_event_id TEXT NOT NULL REFERENCES world_events(id) ON DELETE CASCADE,
 method TEXT NOT NULL CHECK(method IN ('observed','reported','document','heard')),
 quote TEXT NOT NULL,confidence REAL NOT NULL,hops INTEGER NOT NULL,
 learned_at TEXT NOT NULL,valid_until TEXT,fact_world_time TEXT NOT NULL,
 UNIQUE(edge_id,source_event_id,origin_event_id,method)
);
CREATE INDEX IF NOT EXISTS idx_epistemic_observer ON observer_knowledge_edges(world_id,observer_id,subject_id);
"""


class KnowledgeService:
    @staticmethod
    def _visible(first, second, radius=0.1):
        return (
            same_room(first, second)
            and great_circle_distance_km(
                first["longitude"], first["latitude"], second["longitude"], second["latitude"]
            )
            <= radius
        )

    @classmethod
    def add_evidence(
        cls,
        c,
        *,
        observer_id,
        subject_id,
        predicate,
        value,
        event_id,
        method,
        quote,
        source_id=None,
        confidence=None,
        hops=0,
        origin_id=None,
    ):
        event = c.execute("SELECT * FROM world_events WHERE id=?", (event_id,)).fetchone()
        if (
            predicate not in {"name", "location"}
            or method not in {"observed", "reported", "document", "heard"}
            or event is None
        ):
            raise ValueError("知识证据类型或来源事件无效")
        people = c.execute(
            "SELECT id FROM characters WHERE world_id=? AND id IN (?,?)",
            (event["world_id"], observer_id, subject_id),
        ).fetchall()
        if len({row[0] for row in people}) != len({observer_id, subject_id}):
            raise ValueError("知识证据必须属于观察者所在世界")
        if not c.execute(
            "SELECT 1 FROM character_event_knowledge WHERE character_id=? AND event_id=?",
            (observer_id, event_id),
        ).fetchone():
            raise ValueError("观察者尚未获得这条证据")
        if not isinstance(value, str) or not value.strip() or len(value) > 100:
            raise ValueError("知识说法的值无效")
        if (
            predicate == "location"
            and not c.execute(
                "SELECT 1 FROM locations WHERE world_id=? AND id=?", (event["world_id"], value)
            ).fetchone()
        ):
            raise ValueError("地点证据不能创建不存在的地点")
        origin_id = origin_id or event_id
        origin = c.execute(
            "SELECT occurred_at FROM world_events WHERE id=? AND world_id=?",
            (origin_id, event["world_id"]),
        ).fetchone()
        if not origin:
            raise ValueError("证据的原始来源不属于同一世界")
        if (
            source_id
            and not c.execute(
                "SELECT 1 FROM characters WHERE id=? AND world_id=?", (source_id, event["world_id"])
            ).fetchone()
        ):
            raise ValueError("消息来源不属于同一世界")
        strength = {"observed": 1.0, "document": 0.95, "reported": 0.6, "heard": 0.45}[method]
        if confidence is not None:
            strength = min(strength, max(0.1, float(confidence)))
        learned = event["occurred_at"]
        expiry = (
            to_iso(from_iso(origin[0]) + timedelta(hours=6)) if predicate == "location" else None
        )
        c.execute(
            "INSERT INTO observer_knowledge_nodes VALUES (?,?,?,?,?) ON CONFLICT(observer_id,subject_id) DO UPDATE SET last_learned_at=MAX(last_learned_at,excluded.last_learned_at)",
            (observer_id, subject_id, event["world_id"], learned, learned),
        )
        c.execute(
            "INSERT OR IGNORE INTO observer_knowledge_edges VALUES (?,?,?,?,?,?,'unverified',?,?,?,?)",
            (
                str(uuid4()),
                event["world_id"],
                observer_id,
                subject_id,
                predicate,
                value,
                strength,
                learned,
                learned,
                learned if method in {"observed", "document"} else None,
            ),
        )
        edge = c.execute(
            "SELECT id FROM observer_knowledge_edges WHERE observer_id=? AND subject_id=? AND predicate=? AND value=?",
            (observer_id, subject_id, predicate, value),
        ).fetchone()[0]
        c.execute(
            "INSERT OR IGNORE INTO knowledge_evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(uuid4()),
                edge,
                event_id,
                source_id,
                origin_id,
                method,
                quote[:500],
                strength,
                min(3, max(0, hops)),
                learned,
                expiry,
                origin[0],
            ),
        )
        cls.reconcile(c, observer_id, subject_id, predicate, from_iso(learned))
        return edge

    @staticmethod
    def reconcile(c, observer, subject, predicate, at):
        rows = c.execute(
            "SELECT k.*,e.method,e.confidence AS weight,e.learned_at,e.valid_until,e.fact_world_time,e.id AS evidence_id FROM observer_knowledge_edges k JOIN knowledge_evidence e ON e.edge_id=k.id WHERE k.observer_id=? AND k.subject_id=? AND k.predicate=? ORDER BY e.learned_at DESC,e.rowid DESC",
            (observer, subject, predicate),
        ).fetchall()
        fresh = [
            row for row in rows if row["valid_until"] is None or from_iso(row["valid_until"]) > at
        ]
        if not fresh:
            c.execute(
                "UPDATE observer_knowledge_edges SET status='stale' WHERE observer_id=? AND subject_id=? AND predicate=?",
                (observer, subject, predicate),
            )
            return
        # 位置会随时间变化，优先最近的观测；身份署名以可核对的联络文书优先。
        winner = (
            max(fresh, key=lambda row: (row["fact_world_time"], row["weight"]))
            if predicate == "location"
            else max(fresh, key=lambda row: (row["weight"], row["learned_at"]))
        )
        contested = any(
            row["value"] != winner["value"]
            and row["weight"] >= winner["weight"]
            and row["learned_at"] == winner["learned_at"]
            for row in fresh
        )
        for edge_id in {row["id"] for row in rows}:
            evidence = [row for row in rows if row["id"] == edge_id]
            latest = max(evidence, key=lambda row: row["learned_at"])
            selected = edge_id == winner["id"]
            status = (
                "disputed"
                if selected and contested
                else "confirmed"
                if selected and winner["method"] in {"observed", "document"}
                else "reported"
                if selected
                else "superseded"
            )
            c.execute(
                "UPDATE observer_knowledge_edges SET status=?,confidence=?,last_learned_at=?,last_verified_at=? WHERE id=?",
                (
                    status,
                    winner["weight"] if selected else latest["weight"],
                    latest["learned_at"],
                    max(
                        (
                            row["learned_at"]
                            for row in evidence
                            if row["method"] in {"observed", "document"}
                        ),
                        default=None,
                    ),
                    edge_id,
                ),
            )
        if predicate == "name":
            c.execute(
                "UPDATE character_acquaintances SET known_name=? WHERE observer_id=? AND subject_id=?",
                (winner["value"], observer, subject),
            )

    @classmethod
    def capture(cls, c, event_id):
        event = c.execute("SELECT * FROM world_events WHERE id=?", (event_id,)).fetchone()
        if not event or not event["actor_id"]:
            return
        actor = c.execute("SELECT * FROM characters WHERE id=?", (event["actor_id"],)).fetchone()
        target = (
            c.execute("SELECT * FROM characters WHERE id=?", (event["target_id"],)).fetchone()
            if event["target_id"]
            else None
        )
        payload = json.loads(event["payload_json"] or "{}")
        observers = [
            row[0]
            for row in c.execute(
                "SELECT character_id FROM event_observers WHERE event_id=?", (event_id,)
            )
        ]
        physical = (
            event["event_type"].startswith("action.")
            and event["event_type"] not in {"action.rejected", "action.reaction"}
            or event["event_type"] in {"knowledge.observed", "social.contact_exchange"}
        )
        if physical:
            for oid in observers:
                observer = c.execute("SELECT * FROM characters WHERE id=?", (oid,)).fetchone()
                for person in (actor, target):
                    if person is None or person["id"] == oid or not cls._visible(observer, person):
                        continue
                    loc = person["current_location_id"] or person["location_id"]
                    place = c.execute("SELECT name FROM locations WHERE id=?", (loc,)).fetchone()
                    if place:
                        prior = c.execute(
                            "SELECT e.fact_world_time FROM observer_knowledge_edges k JOIN knowledge_evidence e ON e.edge_id=k.id WHERE k.observer_id=? AND k.subject_id=? AND k.predicate='location' AND k.value=? AND k.status='confirmed' AND e.method='observed' ORDER BY e.fact_world_time DESC LIMIT 1",
                            (oid, person["id"], loc),
                        ).fetchone()
                        if (
                            event["event_type"] != "knowledge.observed"
                            and prior
                            and from_iso(event["occurred_at"]) - from_iso(prior[0])
                            < timedelta(hours=1)
                        ):
                            continue
                        cls.add_evidence(
                            c,
                            observer_id=oid,
                            subject_id=person["id"],
                            predicate="location",
                            value=loc,
                            event_id=event_id,
                            method="observed",
                            quote="亲眼看见对方位于" + place[0],
                        )
        if (
            event["event_type"] == "social.contact_exchange"
            and payload.get("status") == "accepted"
            and target
        ):
            for observer, subject in ((actor, target), (target, actor)):
                cls.add_evidence(
                    c,
                    observer_id=observer["id"],
                    subject_id=subject["id"],
                    predicate="name",
                    value=subject["name"],
                    event_id=event_id,
                    method="document",
                    quote="交换的联络信笺署名为" + subject["name"],
                    source_id=subject["id"],
                )
        if event["event_type"] != "action.socialize" or target is None:
            return
        radius = VOICE_RADIUS_KM.get(payload.get("delivery", "normal"), 0.02)
        for speaker, text in (
            (actor, payload.get("dialogue") or ""),
            (target, payload.get("reply") or ""),
        ):
            if not text:
                continue
            places = (
                [
                    dict(row)
                    for row in c.execute(
                        "SELECT id,name FROM locations WHERE world_id=? AND is_active=1",
                        (event["world_id"],),
                    )
                    if row["name"] in text
                ]
                if "在" in text
                else []
            )
            claims = []
            match = re.search(
                r"(?:^|[，。！；\n])\s*(?:我叫|我的名字是|叫我)([^，。！？；：、\s]{1,20})(?=[，。！；\s]|$)", text
            )
            if match and not any(word in match[1] for word in ("不是","如果","假如","是不是")):
                claims.append((speaker["id"], "name", match[1], match[0]))
            subjects = [dict(speaker)]
            for row in c.execute(
                "SELECT p.* FROM character_acquaintances a JOIN characters p ON p.id=a.subject_id WHERE a.observer_id=? AND p.world_id=?",
                (speaker["id"], event["world_id"]),
            ):
                if row["id"] != speaker["id"]:
                    subjects.append(dict(row))
            for subject in subjects:
                label = (
                    "我"
                    if subject["id"] == speaker["id"]
                    else cls.known_label(c, speaker["id"], subject["id"])
                )
                if label == "尚未认识的人":
                    continue
                for place in places:
                    match = re.search(
                        r"(?:^|[，。！；\n])\s*(?:听说)?" + re.escape(label) + r"(?:现在|目前)?在" + re.escape(place["name"])+r"(?=[，。！；\s]|$)", text
                    )
                    if match:
                        claims.append((subject["id"], "location", place["id"], match[0]))
            for oid in observers:
                observer = c.execute("SELECT * FROM characters WHERE id=?", (oid,)).fetchone()
                if oid == speaker["id"] or not cls._visible(observer, speaker, radius):
                    continue
                for subject, predicate, value, quote in claims:
                    cls.add_evidence(
                        c,
                        observer_id=oid,
                        subject_id=subject,
                        predicate=predicate,
                        value=value,
                        event_id=event_id,
                        method="reported",
                        quote=quote,
                        source_id=speaker["id"],
                    )
                    if (
                        subject != speaker["id"]
                        and cls.known_label(c, oid, subject) == "尚未认识的人"
                    ):
                        label = cls.known_label(c, speaker["id"], subject)
                        if label != "尚未认识的人":
                            cls.add_evidence(
                                c,
                                observer_id=oid,
                                subject_id=subject,
                                predicate="name",
                                value=label,
                                event_id=event_id,
                                method="reported",
                                quote=quote,
                                source_id=speaker["id"],
                            )

    @staticmethod
    def known_label(c, observer, subject):
        row = c.execute(
            "SELECT known_name FROM character_acquaintances WHERE observer_id=? AND subject_id=?",
            (observer, subject),
        ).fetchone()
        if row and row[0]:
            return row[0]
        belief = c.execute(
            "SELECT value FROM observer_knowledge_edges WHERE observer_id=? AND subject_id=? AND predicate='name' AND status IN ('confirmed','reported','disputed') ORDER BY last_learned_at DESC LIMIT 1",
            (observer, subject),
        ).fetchone()
        return belief[0] if belief else "尚未认识的人"

    @classmethod
    def transmit(cls, c, source, target, conversation_event_id):
        # 转述保留最初证据的标识；同一传闻兜圈子不会变成独立证据。
        at = c.execute(
            "SELECT occurred_at FROM world_events WHERE id=?", (conversation_event_id,)
        ).fetchone()[0]
        rows = c.execute(
            "SELECT k.*,e.id AS evidence_id,e.origin_event_id,e.source_event_id,e.quote,e.confidence AS weight,e.hops FROM observer_knowledge_edges k JOIN knowledge_evidence e ON e.edge_id=k.id JOIN world_events w ON w.id=e.source_event_id JOIN world_events origin ON origin.id=e.origin_event_id WHERE k.observer_id=? AND k.predicate='location' AND k.status IN ('confirmed','reported') AND e.hops<3 AND (e.valid_until IS NULL OR e.valid_until>?) AND json_extract(w.payload_json,'$.information_scope')='public' AND json_extract(origin.payload_json,'$.information_scope')='public' ORDER BY e.learned_at DESC LIMIT 8",
            (source, at),
        ).fetchall()
        sent = 0
        for row in rows:
            if row["subject_id"] == target:
                continue
            if c.execute(
                "SELECT 1 FROM knowledge_evidence e JOIN observer_knowledge_edges k ON k.id=e.edge_id WHERE k.observer_id=? AND k.subject_id=? AND k.predicate=? AND e.origin_event_id=?",
                (target, row["subject_id"], row["predicate"], row["origin_event_id"]),
            ).fetchone():
                continue
            cls.add_evidence(
                c,
                observer_id=target,
                subject_id=row["subject_id"],
                predicate=row["predicate"],
                value=row["value"],
                event_id=conversation_event_id,
                method="heard",
                quote="交谈中听说：" + row["quote"],
                source_id=source,
                confidence=row["weight"] * 0.8,
                hops=row["hops"] + 1,
                origin_id=row["origin_event_id"],
            )
            sent += 1
            if sent >= 2:
                break

    @classmethod
    def view(cls, c, observer, at, limit=40):
        edges = c.execute(
            "SELECT * FROM observer_knowledge_edges WHERE observer_id=? ORDER BY last_learned_at DESC,rowid DESC LIMIT ?",
            (observer, limit),
        ).fetchall()
        output = []
        for row in edges:
            evidence = [
                dict(item)
                for item in c.execute(
                    "SELECT id,source_event_id,source_character_id,method,quote,confidence,hops,learned_at,valid_until,fact_world_time FROM knowledge_evidence WHERE edge_id=? ORDER BY learned_at DESC,rowid DESC LIMIT 8",
                    (row["id"],),
                )
            ]
            fresh = any(
                not item["valid_until"] or from_iso(item["valid_until"]) > at for item in evidence
            )
            value = row["value"]
            if row["predicate"] == "location":
                place = c.execute("SELECT name FROM locations WHERE id=?", (value,)).fetchone()
                value = place[0] if place else "此前提及的地点"
            output.append(
                {
                    "id": row["id"],
                    "subject_id": row["subject_id"],
                    "subject_name": cls.known_label(c, observer, row["subject_id"]),
                    "predicate": row["predicate"],
                    "value": value,
                    "status": row["status"] if fresh else "stale",
                    "confidence": row["confidence"],
                    "last_verified_at": row["last_verified_at"],
                    "evidence": evidence,
                }
            )
        return output
