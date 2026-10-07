"""奇观意向、NPC 决策、访问与有限认知；哥哥可以用正常心跳验收。"""

from __future__ import annotations

import json
import math
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from world_engine.geo import great_circle_distance_km
from world_engine.repository import to_iso, utc_now


class WonderSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    kind: str = Field(default="floating_city", min_length=1, max_length=80)
    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-89, le=89)
    ground_location_id: str
    altitude_m: float = Field(default=600, ge=0, le=10000)
    radius_km: float = Field(default=1, ge=0.1, le=20)
    construction_hours: float = Field(default=4320, gt=0, le=876000)
    description: str = Field(
        default="浮于空中的城市，街道与居所建在悬空平台上，边缘可俯瞰地面。", max_length=1000
    )


class WonderService:
    @staticmethod
    def event(c, wid, actor_id, kind, summary, at, *, location_id=None, payload=None):
        from world_engine.society import SocietyService

        eid = str(uuid4())
        data = {"information_scope": "public", **(payload or {})}
        c.execute(
            "INSERT INTO world_events(id,world_id,tick_id,occurred_at,event_type,actor_id,"
            "location_id,summary,importance,payload_j"
            "son,created_at) VALUES (?,?,?,?,?,?,?,?,"
            "?,?,?)",
            (
                eid,
                wid,
                eid,
                to_iso(at),
                kind,
                actor_id,
                location_id,
                summary,
                "major" if kind == "world.wonder_completed" else "notable",
                json.dumps(data, ensure_ascii=False),
                to_iso(utc_now()),
            ),
        )
        SocietyService.observe_event(c, eid)
        return eid

    @staticmethod
    def learn(c, wid, cid, eid, at):
        c.execute("INSERT OR IGNORE INTO event_observers VALUES (?,?,'witnessed')", (eid, cid))
        c.execute(
            "INSERT OR IGNORE INTO character_event_kn"
            "owledge VALUES (?,?,?,'experienced',NULL"
            ",1,0,?)",
            (cid, eid, wid, to_iso(at)),
        )

    @classmethod
    def offer(cls, c, wid, spec, representatives, at):
        """仅投放一次初始建设机会，哥哥的演示不能直接创建已完工城市。"""
        spec = WonderSpec.model_validate(spec)
        loc = c.execute(
            "SELECT * FROM locations WHERE id=? AND w"
            "orld_id=? AND is_active=1 AND space_laye"
            "r='ground'",
            (spec.ground_location_id, wid),
        ).fetchone()
        if (
            not loc
            or great_circle_distance_km(
                spec.longitude, spec.latitude, loc["longitude"], loc["latitude"]
            )
            > loc["area_radius_km"]
        ):
            raise ValueError("地面访问地点必须属于当前世界并覆盖建设坐标")
        if len(representatives) < 2 or len({r["character_id"] for r in representatives}) != len(
            representatives
        ):
            raise ValueError("至少需要两位不同的代表")
        for rep in representatives:
            if (
                not rep.get("polity")
                or not c.execute(
                    "SELECT 1 FROM characters WHERE id=? AND world_id=? AND is_player=0",
                    (rep["character_id"], wid),
                ).fetchone()
            ):
                raise ValueError("政体代表必须是当前世界中的 NPC")
        eid = cls.event(
            c,
            wid,
            representatives[0]["character_id"],
            "world.wonder_invitation",
            f"出现联合建设“{spec.name}”的机会，各代表须自行决定是否参与。",
            at,
            location_id=spec.ground_location_id,
            payload={"candidate": spec.model_dump()},
        )
        c.execute(
            "INSERT INTO wonder_intents VALUES (?,?,?,?,NULL)",
            (eid, wid, spec.model_dump_json(), json.dumps(representatives, ensure_ascii=False)),
        )
        return eid

    @classmethod
    def tick(cls, c, wid, at):
        from world_engine.registration import ConstructionProjectService, WorldElementRegistry
        from world_engine.registration.models import ElementRegistrationSubmit, StructureSpec

        for intent in c.execute(
            "SELECT * FROM wonder_intents WHERE world_id=? AND registration_id IS NULL", (wid,)
        ).fetchall():
            spec = WonderSpec.model_validate_json(intent["spec_json"])
            for rep in json.loads(intent["representatives_json"]):
                cid = rep["character_id"]
                if not c.execute(
                    "SELECT 1 FROM character_event_knowledge "
                    "WHERE character_id=? AND event_id=? AND "
                    "learned_world_time<=?",
                    (cid, intent["id"], to_iso(at)),
                ).fetchone():
                    continue
                if c.execute(
                    "SELECT 1 FROM wonder_decisions WHERE intent_id=? AND character_id=?",
                    (intent["id"], cid),
                ).fetchone():
                    continue
                actor = c.execute(
                    "SELECT * FROM characters WHERE id=? AND health>0", (cid,)
                ).fetchone()
                if not actor:
                    continue
                join = bool(rep.get("authorized", False))
                reason = (
                    "已获本政体建设授权，收到具体建设机会，决定参加联合项目。"
                    if join
                    else "没有本政体的建设授权，决定不承诺联合开工。"
                )
                eid = cls.event(
                    c,
                    wid,
                    cid,
                    "world.wonder_decision",
                    f"{actor['name']}：{reason}",
                    at,
                    location_id=actor["current_location_id"],
                    payload={
                        "source_event_id": intent["id"],
                        "decision": "join" if join else "decline",
                    },
                )
                c.execute(
                    "INSERT INTO wonder_decisions VALUES (?,?,?,?,?,?,?)",
                    (
                        eid,
                        wid,
                        intent["id"],
                        cid,
                        rep["polity"],
                        "join" if join else "decline",
                        reason,
                    ),
                )
            joined = c.execute(
                "SELECT * FROM wonder_decisions WHERE intent_id=? AND decision='join' ORDER BY id",
                (intent["id"],),
            ).fetchall()
            sponsors = sorted({d["polity"] for d in joined})
            if len(sponsors) < 2:
                continue
            proposer = joined[0]
            registration = WorldElementRegistry().submit(
                c,
                world_id=wid,
                request=ElementRegistrationSubmit(
                    requested_by_character_id=proposer["character_id"],
                    source_event_id=proposer["id"],
                    idempotency_key="wonder:" + intent["id"],
                    payload=StructureSpec(
                        name=spec.name,
                        structure_type=spec.kind,
                        origin_mode="constructed",
                        status="planned",
                        location_id=spec.ground_location_id,
                        longitude=spec.longitude,
                        latitude=spec.latitude,
                        provenance_claim="、".join(sponsors) + "联合建设",
                        metadata={"wonder": spec.model_dump()},
                    ),
                ),
            )
            if registration.result_entity_id is None:
                raise ValueError(f"奇观登记失败：{registration.rejection_reason}")
            sid = registration.result_entity_id
            c.execute(
                "INSERT INTO world_wonders VALUES (?,?,?,NULL,?,?,?,?,?,NULL)",
                (
                    sid,
                    wid,
                    registration.id,
                    spec.ground_location_id,
                    "wonder:" + sid,
                    spec.altitude_m,
                    spec.radius_km,
                    json.dumps(sponsors, ensure_ascii=False),
                ),
            )
            ConstructionProjectService().set_status(
                c, world_id=wid, registration_id=registration.id, status="constructing"
            )
            c.execute(
                "UPDATE wonder_intents SET registration_id=? WHERE id=?",
                (registration.id, intent["id"]),
            )
            cls.event(
                c,
                wid,
                proposer["character_id"],
                "world.wonder_started",
                f"{'、'.join(sponsors)}联合开工建设“{spec.name}”。",
                at,
                location_id=spec.ground_location_id,
                payload={"wonder_id": sid, "source_event_id": intent["id"]},
            )
        cls.observe(c, wid, at)
        cls.residence_tick(c, wid, at)

    @classmethod
    def residence_tick(cls, c, wid, at):
        """哥哥，只有本人已有入住目标、听到竣工消息且能行动，才会动身。"""
        from world_engine.life import LifeActivityService
        from world_engine.movement import MovementService

        for actor in c.execute(
            "SELECT * FROM characters WHERE world_id="
            "? AND is_player=0 AND health>0 AND space"
            "_layer='ground' AND current_room_id IS N"
            "ULL",
            (wid,),
        ).fetchall():
            goals = json.loads(actor["goals_json"] or "[]")
            for wonder in cls.view(c, wid):
                if wonder["status"] != "active" or not any(
                    "入住" in str(g) and wonder["name"] in str(g) for g in goals
                ):
                    continue
                if not c.execute(
                    "SELECT 1 FROM character_event_knowledge WHERE character_id=? AND event_id=?",
                    (actor["id"], wonder["completed_event_id"]),
                ).fetchone():
                    continue
                if (
                    LifeActivityService.running(c, actor["id"])
                    or c.execute(
                        "SELECT 1 FROM character_movements WHERE "
                        "character_id=? AND status='moving'",
                        (actor["id"],),
                    ).fetchone()
                ):
                    continue
                if (
                    great_circle_distance_km(
                        actor["longitude"],
                        actor["latitude"],
                        wonder["longitude"],
                        wonder["latitude"],
                    )
                    <= 0.1
                ):
                    cls.traverse(
                        c,
                        wid,
                        actor["id"],
                        wonder["id"],
                        "enter",
                        at,
                        reason="已得知城市竣工，按本人已有入住目标使用公共通道。",
                    )
                else:
                    MovementService().start(
                        c,
                        world_id=wid,
                        character_id=actor["id"],
                        destination_longitude=wonder["longitude"],
                        destination_latitude=wonder["latitude"],
                        world_time=at,
                    )
                break

    @classmethod
    def complete(cls, c, row, world_time, created_at):
        wonder = c.execute(
            "SELECT * FROM world_wonders WHERE id=?", (row["target_entity_id"],)
        ).fetchone()
        if wonder is None or wonder["location_id"]:
            return row["location_id"]
        spec = WonderSpec.model_validate(json.loads(row["payload_json"])["metadata"]["wonder"])
        lid = str(uuid4())
        c.execute(
            "INSERT INTO locations(id,world_id,name,k"
            "ind,longitude,latitude,area_radius_km,ar"
            "ea_priority,space_layer,altitude_m) "
            "VALUES (?,?,?,'city',?,?,?,100,?,?)",
            (
                lid,
                row["world_id"],
                spec.name,
                spec.longitude,
                spec.latitude,
                spec.radius_km,
                wonder["space_layer"],
                spec.altitude_m,
            ),
        )
        c.execute("UPDATE world_wonders SET location_id=? WHERE id=?", (lid, wonder["id"]))
        c.execute("UPDATE world_structures SET location_id=? WHERE id=?", (lid, wonder["id"]))
        c.execute(
            "INSERT INTO world_entities(id,world_id,e"
            "ntity_type,entity_id,name,registration_i"
            "d,created_at) VALUES (?,?,'settlement',?"
            ",?,?,?)",
            (
                str(uuid4()),
                row["world_id"],
                lid,
                spec.name,
                row["registration_id"],
                to_iso(created_at),
            ),
        )
        from world_engine.elements import WorldElementCatalog

        WorldElementCatalog().upsert(
            c,
            world_id=row["world_id"],
            entity_type="settlement",
            entity_id=lid,
            name=spec.name,
            source_kind="registration",
            source_registration_id=row["registration_id"],
        )
        witnesses = [
            p
            for p in c.execute(
                "SELECT * FROM characters WHERE world_id="
                "? AND health>0 AND space_layer='ground' "
                "AND current_room_id IS NULL",
                (row["world_id"],),
            )
            if great_circle_distance_km(
                p["longitude"], p["latitude"], spec.longitude, spec.latitude
            )
            <= 0.1
        ]
        onsite = witnesses[0]["id"] if witnesses else None
        eid = cls.event(
            c,
            row["world_id"],
            onsite,
            "world.wonder_completed",
            f"“{spec.name}”已经建成，城市浮于空中，平台高于地面{spec.altitude_m:g}米，公共升降通道开放。",
            world_time,
            location_id=wonder["ground_location_id"],
            payload={
                "wonder_id": wonder["id"],
                "city_location_id": lid,
                "sponsors": json.loads(wonder["sponsors_json"]),
            },
        )
        c.execute("UPDATE world_wonders SET completed_event_id=? WHERE id=?", (eid, wonder["id"]))
        cls.observe(c, row["world_id"], world_time)
        return lid

    @staticmethod
    def view(c, wid):
        rows = c.execute(
            "SELECT w.*,s.name,s.structure_type AS ki"
            "nd,CASE WHEN p.status='completed' THEN s"
            ".status ELSE p.status END AS status,s.lo"
            "ngitude,s.latitude,s.metadata_json,p.pro"
            "gress "
            "FROM world_wonders w JOIN world_structur"
            "es s ON s.id=w.id JOIN construction_proj"
            "ects p ON p.registration_id=w.registrati"
            "on_id "
            "WHERE w.world_id=? ORDER BY s.created_at,w.id",
            (wid,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["sponsors"] = json.loads(item.pop("sponsors_json"))
            item["description"] = json.loads(item.pop("metadata_json"))["wonder"]["description"]
            item["access"] = {
                "name": "公共升降通道",
                "longitude": item["longitude"],
                "latitude": item["latitude"],
                "radius_km": 0.1,
                "ground_location_id": item["ground_location_id"],
                "city_location_id": item["location_id"],
            }
            result.append(item)
        return result

    @classmethod
    def observe(cls, c, wid, at):
        for wonder in cls.view(c, wid):
            if wonder["status"] != "active" or not wonder["completed_event_id"]:
                continue
            for actor in c.execute(
                "SELECT * FROM characters WHERE world_id="
                "? AND health>0 AND current_room_id IS NU"
                "LL",
                (wid,),
            ).fetchall():
                if actor["space_layer"] == wonder["space_layer"] or (
                    actor["space_layer"] == "ground"
                    and great_circle_distance_km(
                        actor["longitude"],
                        actor["latitude"],
                        wonder["longitude"],
                        wonder["latitude"],
                    )
                    <= 12
                ):
                    cls.learn(c, wid, actor["id"], wonder["completed_event_id"], at)

    @classmethod
    def context(cls, c, wid, cid):
        actor = c.execute(
            "SELECT * FROM characters WHERE id=? AND world_id=?", (cid, wid)
        ).fetchone()
        if not actor:
            return {"environment": None, "known_wonders": []}
        known = []
        environment = None
        for w in cls.view(c, wid):
            if w["status"] != "active":
                continue
            if actor["space_layer"] == w["space_layer"]:
                environment = {
                    "name": w["name"],
                    "kind": w["kind"],
                    "altitude_m": w["altitude_m"],
                    "description": w["description"],
                }
            evidence = c.execute(
                "SELECT source_kind,source_character_id,l"
                "earned_world_time,hops,confidence FROM c"
                "haracter_event_knowledge WHERE character"
                "_id=? AND event_id=?",
                (cid, w["completed_event_id"]),
            ).fetchone()
            if evidence:
                known.append(
                    {
                        "id": w["id"],
                        "name": w["name"],
                        "kind": w["kind"],
                        "description": w["description"],
                        "evidence": dict(evidence),
                    }
                )
        return {"environment": environment, "known_wonders": known}

    @classmethod
    def traverse(cls, c, wid, cid, sid, direction, at, *, reason=None):
        from world_engine.life import LifeActivityService
        from world_engine.player_steering import PlayerSteeringService

        actor = c.execute(
            "SELECT * FROM characters WHERE id=? AND world_id=?", (cid, wid)
        ).fetchone()
        wonder = next((w for w in cls.view(c, wid) if w["id"] == sid), None)
        if actor is None or wonder is None:
            raise LookupError("人物或奇观不存在")
        if wonder["status"] != "active" or not wonder["location_id"]:
            raise ValueError("奇观尚未建成，通道没有开放")
        if direction not in {"enter", "exit"}:
            raise ValueError("未知通道方向")
        if actor["health"] <= 0:
            raise ValueError("人物无法使用通道")
        LifeActivityService.assert_available(c, cid)
        if (
            actor["current_room_id"]
            or c.execute(
                "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'", (cid,)
            ).fetchone()
        ):
            raise ValueError("请先离开房间或结束当前行程")
        source = "ground" if direction == "enter" else wonder["space_layer"]
        if actor["space_layer"] != source:
            raise ValueError("人物不在通道的出发空间")
        if (
            great_circle_distance_km(
                actor["longitude"], actor["latitude"], wonder["longitude"], wonder["latitude"]
            )
            > 0.1
        ):
            raise ValueError("请先走到公共升降通道附近（100米内）")
        if actor["is_player"]:
            PlayerSteeringService.stop(c, wid, at, "进入升降通道，停止方向移动。")
        target = wonder["location_id"] if direction == "enter" else wonder["ground_location_id"]
        layer = wonder["space_layer"] if direction == "enter" else "ground"
        altitude = wonder["altitude_m"] if direction == "enter" else 0
        c.execute(
            "UPDATE characters SET longitude=?,latitu"
            "de=?,current_location_id=?,location_id=?"
            ",space_layer=?,altitude_m=?,updated_at=?"
            " WHERE id=?",
            (
                wonder["longitude"],
                wonder["latitude"],
                target,
                target,
                layer,
                altitude,
                to_iso(utc_now()),
                cid,
            ),
        )
        cls.learn(c, wid, cid, wonder["completed_event_id"], at)
        eid = cls.event(
            c,
            wid,
            cid,
            "action.wonder_enter" if direction == "enter" else "action.wonder_exit",
            f"{actor['name']}经公共升降通道{'登上' if direction == 'enter' else '离开'}"
            f"“{wonder['name']}”。",
            at,
            location_id=target,
            payload={
                "wonder_id": sid,
                "space_layer": layer,
                "source_event_id": wonder["completed_event_id"],
                "reason": reason or "本人选择使用公共升降通道。",
            },
        )
        c.execute("UPDATE worlds SET version=version+1 WHERE id=?", (wid,))
        return {"event_id": eid, "wonder_id": sid, "space_layer": layer}

    @staticmethod
    def contains(c, wid, layer, lon, lat):
        return any(
            great_circle_distance_km(lon, lat, row["longitude"], row["latitude"])
            <= row["area_radius_km"]
            for row in c.execute(
                "SELECT * FROM locations WHERE world_id=? AND space_layer=? AND is_active=1",
                (wid, layer),
            )
        )

    @classmethod
    def visible(cls, c, wid, actor, direction):
        bearing_target = {"north": 0, "east": 90, "south": 180, "west": 270}[direction]
        result = []
        for w in cls.view(c, wid):
            if w["status"] != "active" or actor["space_layer"] == w["space_layer"]:
                continue
            distance = great_circle_distance_km(
                actor["longitude"], actor["latitude"], w["longitude"], w["latitude"]
            )
            lon1, lat1, lon2, lat2 = map(
                math.radians, (actor["longitude"], actor["latitude"], w["longitude"], w["latitude"])
            )
            bearing = (
                math.degrees(
                    math.atan2(
                        math.sin(lon2 - lon1) * math.cos(lat2),
                        math.cos(lat1) * math.sin(lat2)
                        - math.sin(lat1) * math.cos(lat2) * math.cos(lon2 - lon1),
                    )
                )
                % 360
            )
            if distance <= 12 and (
                distance < 0.05 or abs((bearing - bearing_target + 180) % 360 - 180) <= 62
            ):
                result.append(
                    {
                        "id": w["id"],
                        "type": "wonder",
                        "name": w["name"],
                        "kind": w["kind"],
                        "distance_km": round(distance, 3),
                        "altitude_m": w["altitude_m"],
                        "description": w["description"],
                    }
                )
        return result
