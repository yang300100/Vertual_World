"""人物经历、情绪、相识与消息传播；派生状态始终保留事实来源。"""

from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

from world_engine.geo import great_circle_distance_km, nearby_coordinate_bounds
from world_engine.proximity import same_room
from world_engine.repository import from_iso, to_iso, utc_now


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
        from world_engine.encounters import EncounterService

        # 哥哥，只按事件发生时已更新的真实位置结束旧场次，出发意图不等于已经走远。
        EncounterService.end_separated(
            c, event["world_id"], {actor["id"], *([target["id"]] if target else [])}, at,
        )
        public = event["event_type"] in {
            "world.wonder_invitation",
            "world.wonder_decision",
            "world.wonder_started",
            "world.wonder_completed",
            "action.wonder_enter",
            "action.wonder_exit",
            "cooperation.observed",
            "cooperation.proposal",
            "world.incident_observed",
            "social.incident_supply_requested",
            "world.incident_site_restored",
            "world.major_death",
            "action.attack",
            "action.socialize",
            "action.travel",
            "action.movement_started",
            "action.movement_arrived",
            "action.approach",
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
            south, north, ranges = nearby_coordinate_bounds(
                actor['longitude'], actor['latitude'], radius,
            )
            # 哥哥，只读取同房间、同空间层的附近候选人，真实球面距离仍作最后裁定。
            longitude_filter = " OR ".join("longitude BETWEEN ? AND ?" for _ in ranges)
            parameters = [event['world_id'], actor['space_layer'], actor['current_room_id'],
                          south, north, *(value for pair in ranges for value in pair)]
            for person in c.execute(
                "SELECT * FROM characters WHERE world_id=? AND health>0 AND space_layer=? "
                "AND current_room_id IS ? AND latitude BETWEEN ? AND ? AND ("
                + longitude_filter + ")", parameters,
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
        private_room=False
        if actor["current_room_id"]:
            private_room=bool(c.execute(
                "WITH RECURSIVE chain(id,parent_room_id,access_policy,depth) AS ("
                "SELECT id,parent_room_id,access_policy,0 FROM life_rooms WHERE id=? UNION ALL "
                "SELECT r.id,r.parent_room_id,r.access_policy,chain.depth+1 FROM life_rooms r JOIN chain ON r.id=chain.parent_room_id WHERE chain.depth<4) "
                "SELECT 1 FROM chain WHERE access_policy='private' LIMIT 1",(actor["current_room_id"],)).fetchone())
        payload["perception_room_id"]=actor["current_room_id"]
        payload["information_scope"] = "public" if public and voice != "whisper" and not private_room else "private"
        c.execute(
            "UPDATE world_events SET payload_json=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False), event_id),
        )
        for cid in observers:
            if event["event_type"] == "social.public_rumor":
                continue
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
                cls.exchange_news(c, event["world_id"], actor["id"], target["id"], at,
                                  preferred_event_id=payload.get("metadata", {}).get("share_event_id"))
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
        if target and event["event_type"] in {"action.attack", "contract.overdue", "contract.completed"}:
            from world_engine.intimacy import IntimacyService

            IntimacyService.remember_event(c, event["world_id"], actor["id"], target["id"], event_id, at,
                                         "conflict" if event["event_type"] != "contract.completed" else "repair", event["summary"])
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

        from world_engine.character_growth import CharacterGrowthService
        from world_engine.epistemics import KnowledgeService
        KnowledgeService.capture(c,event_id)
        CharacterGrowthService.apply(c,event_id)
        if event["event_type"] == "world.major_death":
            for cid in observers:
                if cid not in {actor["id"], event["target_id"]}:
                    cls.plan_from_news(c, event["world_id"], None, cid, event_id, at)
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
    def exchange_news(c, wid, first, second, at, preferred_event_id=None):
        for source, target in ((first, second), (second, first)):
            rows = c.execute(
                "SELECT k.*,e.payload_json "
                "FROM character_event_knowledge k JOIN world_events e ON e.id=k.event_id "
                "WHERE k.world_id=? AND k.character_id=? AND k.hops<3 AND k.learned_world_time<=? "
                "ORDER BY (k.event_id=?) DESC,k.learned_world_time DESC LIMIT 8",
                (wid, source, to_iso(at), preferred_event_id),
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
                if added:
                    SocietyService.plan_from_news(c, wid, source, target, row["event_id"], at)
                if transmitted >= 2:
                    break

    @classmethod
    def plan_from_news(cls, c, wid, source, recipient, event_id, at, channel="in_person"):
        """Yuki 让与逝者亲近的 NPC 根据新听闻，留下可追溯的打听计划。"""
        news = c.execute(
            "SELECT id,event_type,target_id,location_id,summary FROM world_events "
            "WHERE id=? AND world_id=?",
            (event_id, wid),
        ).fetchone()
        if news is None or news["event_type"] != "world.major_death" or not news["target_id"]:
            return
        person = c.execute(
            "SELECT is_player FROM characters WHERE id=? AND world_id=? AND health>0",
            (recipient, wid),
        ).fetchone()
        if person is None or person["is_player"]:
            return
        relation = c.execute(
            "SELECT affinity,trust FROM relationships WHERE world_id=? "
            "AND source_character_id=? AND target_character_id=?",
            (wid, recipient, news["target_id"]),
        ).fetchone()
        if relation is None or max(relation["affinity"], relation["trust"]) < 20:
            return
        if c.execute(
            "SELECT 1 FROM npc_todos WHERE character_id=? AND source_event_id=?",
            (recipient, event_id),
        ).fetchone():
            return
        place = c.execute(
            "SELECT name FROM locations WHERE id=?",
            (news["location_id"],),
        ).fetchone()
        title = f"打听{place[0] if place else '附近'}的重大冲突经过"
        summary = cls.mask_text(c, wid, recipient, news["summary"])[:120]
        if channel == "public_rumor":
            details = f"听到跨地区公共传闻：{summary}。这只是传闻，行动前仍需核实。"
        elif source:
            details = f"从{cls.label(c, recipient, source)}听说：{summary}。这只是听闻，行动前仍需核实。"
        else:
            details = f"亲眼目睹：{summary}。仍需打听前因后果。"
        now = to_iso(utc_now())
        c.execute(
            "INSERT INTO npc_todos(id,world_id,character_id,title,details,due_world_time,"
            "source_event_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (str(uuid4()), wid, recipient, title, details,
             to_iso(at + timedelta(days=2)), event_id, now, now),
        )

    @classmethod
    def share_major_news(cls, c, wid, at):
        """Yuki 让相识且碰面的 NPC 有限度地转述亲历或听来的公开大事。"""
        from world_engine.actions import ActionService
        from world_engine.life import LifeActivityService

        people = {
            row["id"]: row for row in c.execute(
                "SELECT * FROM characters WHERE world_id=? AND is_player=0 AND health>0",
                (wid,),
            ).fetchall()
        }
        if len(people) < 2:
            return
        news = c.execute(
            "SELECT k.character_id,k.event_id,k.confidence,k.hops,e.location_id "
            "FROM world_events e CROSS JOIN character_event_knowledge k ON k.event_id=e.id "
            "WHERE k.world_id=? AND e.world_id=? AND e.importance='major' AND k.hops<3 "
            "AND k.learned_world_time>=? AND k.learned_world_time<=? "
            "AND json_extract(e.payload_json,'$.information_scope')='public' "
            "ORDER BY k.learned_world_time DESC LIMIT 64",
            (wid, wid, to_iso(at - timedelta(days=2)), to_iso(at)),
        ).fetchall()
        moving = {
            row[0] for row in c.execute(
                "SELECT character_id FROM character_movements WHERE status='moving' "
                "AND character_id IN (SELECT id FROM characters WHERE world_id=?)",
                (wid,),
            ).fetchall()
        }
        sent_by = set()
        for item in news:
            source = people.get(item["character_id"])
            if source is None or source["id"] in sent_by or source["id"] in moving:
                continue
            if LifeActivityService.running(c, source["id"]):
                continue
            for recipient in people.values():
                if recipient["id"] == source["id"] or recipient["id"] in moving:
                    continue
                if not same_room(source, recipient) or great_circle_distance_km(
                    source["longitude"], source["latitude"],
                    recipient["longitude"], recipient["latitude"],
                ) > 0.02:
                    continue
                relation = c.execute(
                    "SELECT trust,affinity FROM relationships WHERE world_id=? "
                    "AND source_character_id=? AND target_character_id=?",
                    (wid, source["id"], recipient["id"]),
                ).fetchone()
                if relation is None or max(relation["trust"], relation["affinity"]) < 10:
                    continue
                if c.execute(
                    "SELECT 1 FROM character_event_knowledge WHERE character_id=? AND event_id=?",
                    (recipient["id"], item["event_id"]),
                ).fetchone():
                    continue
                added = c.execute(
                    "INSERT OR IGNORE INTO character_event_knowledge "
                    "VALUES (?,?,?,'heard',?,?,?,?)",
                    (recipient["id"], item["event_id"], wid, source["id"],
                     max(0.1, item["confidence"] * 0.8), item["hops"] + 1, to_iso(at)),
                ).rowcount
                if not added:
                    continue
                ActionService._record_event(
                    c, world_id=wid, tick_id=str(uuid4()), occurred_at=at,
                    event_type="social.news_shared", actor_id=source["id"],
                    target_id=recipient["id"],
                    location_id=source["current_location_id"] or source["location_id"],
                    summary="两位相识的人当面谈起一件近期大事。",
                    payload={"source_event_id": item["event_id"], "channel": "in_person"},
                )
                cls.plan_from_news(c, wid, source["id"], recipient["id"], item["event_id"], at)
                sent_by.add(source["id"])
                break

    @classmethod
    def spread_public_rumors(cls, c, wid, at):
        """Yuki 让公开大事沿世界时间跨地区扩散，收件人保留来源与可信度。"""
        from world_engine.actions import ActionService

        places = {
            row["id"]: row for row in c.execute(
                "SELECT id,parent_location_id FROM locations WHERE world_id=? AND is_active=1",
                (wid,),
            ).fetchall()
        }

        def region(location_id):
            seen = set()
            while location_id in places and location_id not in seen:
                seen.add(location_id)
                parent = places[location_id]["parent_location_id"]
                if not parent:
                    return location_id
                location_id = parent
            return None

        people = {
            row["id"]: row for row in c.execute(
                "SELECT id,current_location_id,location_id,longitude,latitude "
                "FROM characters WHERE world_id=? AND is_player=0 AND health>0",
                (wid,),
            ).fetchall()
        }
        if len(people) < 2:
            return
        # 心跳开始时取消息快照：这轮刚听到的人不能立即再向下一地区广播。
        news = c.execute(
            "SELECT k.character_id,k.event_id,k.confidence,k.hops,k.learned_world_time "
            "FROM world_events e CROSS JOIN character_event_knowledge k ON k.event_id=e.id "
            "WHERE k.world_id=? AND e.world_id=? AND e.importance='major' AND k.hops<3 "
            "AND e.occurred_at>=? AND e.occurred_at<=? "
            "AND k.learned_world_time<=? "
            "AND json_extract(e.payload_json,'$.information_scope')='public' "
            "AND e.event_type NOT IN ('world.incident_observed','social.incident_supply_requested',"
            "'world.incident_site_restored') "
            "ORDER BY k.learned_world_time DESC LIMIT 64",
            (wid, wid, to_iso(at - timedelta(days=30)), to_iso(at), to_iso(at)),
        ).fetchall()
        delivered = 0
        for item in news:
            source = people.get(item["character_id"])
            if source is None or source["longitude"] is None or source["latitude"] is None:
                continue
            source_region = region(source["current_location_id"] or source["location_id"])
            if source_region is None:
                continue
            sent = 0
            for recipient in people.values():
                if recipient["id"] == source["id"] or recipient["longitude"] is None or recipient["latitude"] is None:
                    continue
                target_region = region(recipient["current_location_id"] or recipient["location_id"])
                if target_region is None or target_region == source_region:
                    continue
                distance = great_circle_distance_km(
                    source["longitude"], source["latitude"],
                    recipient["longitude"], recipient["latitude"],
                )
                delay = timedelta(hours=12 + distance * 24 / 1000)
                if at - from_iso(item["learned_world_time"]) < delay:
                    continue
                added = c.execute(
                    "INSERT OR IGNORE INTO character_event_knowledge "
                    "VALUES (?,?,?,'rumor',?,?,?,?)",
                    (recipient["id"], item["event_id"], wid, source["id"],
                     max(0.1, item["confidence"] * 0.65), item["hops"] + 1, to_iso(at)),
                ).rowcount
                if not added:
                    continue
                ActionService._record_event(
                    c, world_id=wid, tick_id=str(uuid4()), occurred_at=at,
                    event_type="social.public_rumor", actor_id=source["id"],
                    target_id=recipient["id"],
                    location_id=recipient["current_location_id"] or recipient["location_id"],
                    summary="一件远方的大事传到了这里。",
                    payload={"source_event_id": item["event_id"], "channel": "public_rumor",
                             "source_region_id": source_region, "target_region_id": target_region},
                )
                cls.plan_from_news(
                    c, wid, source["id"], recipient["id"], item["event_id"], at,
                    channel="public_rumor",
                )
                sent += 1
                delivered += 1
                if sent >= 3 or delivered >= 64:
                    break
            if delivered >= 64:
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
                "FROM character_event_knowledge k JOIN world_events e ON e.id=k.event_id AND e.world_id=k.world_id "
                "JOIN worlds w ON w.id=e.world_id "
                "WHERE k.world_id=? AND k.character_id=? "
                "AND e.occurred_at<=w.current_time AND k.learned_world_time<=w.current_time "
                "ORDER BY k.learned_world_time DESC LIMIT 8",
                (wid, cid),
            )
        ]
        for entry in news:
            entry["summary"] = cls.event_summary(c, wid, cid, entry["id"], entry["summary"])
        from world_engine.character_growth import CharacterGrowthService
        from world_engine.epistemics import KnowledgeService
        beliefs=KnowledgeService.view(c,cid,at,limit=6)
        for belief in beliefs:
            belief["evidence"]=[{"method":item["method"],"quote":item["quote"][:160],"learned_at":item["learned_at"]} for item in belief["evidence"][:2]]
        growth=CharacterGrowthService.view(c,cid)
        growth["traits"]=growth["traits"][:8]
        growth["recent_changes"]=growth["recent_changes"][:3]
        from world_engine.food import FoodService
        from world_engine.npc_goals import NpcGoalService
        from world_engine.wonders import WonderService
        return {"emotion": dict(emotion) if emotion else None, "known_events": news,"beliefs":beliefs,"dispositions":growth,
                "wonder_context": WonderService.context(c,wid,cid),
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

    @classmethod
    def event_summary(cls, c, wid, observer, event_id, summary):
        """哥哥，门外目击到进出，不等于知道私人房间的内部名称。"""
        event = c.execute(
            "SELECT event_type,actor_id,target_id,payload_json FROM world_events WHERE id=? AND world_id=?",
            (event_id, wid),
        ).fetchone()
        if event and observer not in {event["actor_id"], event["target_id"]} and event["event_type"] in {
            "action.interior_enter", "action.interior_exit",
        }:
            try:
                room_id = json.loads(event["payload_json"]).get("room_id")
            except (AttributeError, TypeError, ValueError):
                room_id = None
            rooms = c.execute(
                "WITH RECURSIVE chain(id,parent_room_id,access_policy,depth) AS ("
                "SELECT id,parent_room_id,access_policy,0 FROM life_rooms WHERE id=? AND world_id=? "
                "UNION ALL SELECT r.id,r.parent_room_id,r.access_policy,chain.depth+1 "
                "FROM life_rooms r JOIN chain ON r.id=chain.parent_room_id WHERE chain.depth<4) "
                "SELECT access_policy FROM chain", (room_id, wid),
            ).fetchall()
            if not rooms or any(room["access_policy"] == "private" for room in rooms):
                verb = "进入" if event["event_type"] == "action.interior_enter" else "离开"
                summary = f"看见{cls.label(c, observer, event['actor_id'])}{verb}了一处室内空间。"
        return cls.mask_text(c, wid, observer, summary)

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
        from world_engine.families import FamilyService

        FamilyService.maintain(c,wid,at)
        FamilyService.care_dependents(c,wid,at)
        c.execute(
            "DELETE FROM character_emotions WHERE world_id=? AND expires_world_time<=?",
            (wid, to_iso(at)),
        )
        cls.share_major_news(c, wid, at)
        cls.spread_public_rumors(c, wid, at)
        player = c.execute(
            "SELECT * FROM characters WHERE world_id=? AND is_player=1", (wid,)
        ).fetchone()
        if player is None:
            return
        cls.goal_tick(c, wid, player, at)
        continuity_owners = {r[0] for r in c.execute("SELECT DISTINCT owner_id FROM relationship_considerations WHERE world_id=? AND other_id=? AND status='active' AND kind IN ('concern','hope') AND updated_at<=?", (wid,player["id"],to_iso(at-timedelta(days=1))))}
        # 哥哥，先按真实旧约筛候选，避免为全世界没有共同约定的居民逐人查库。
        missed_partners = {r[0] for r in c.execute(
            "SELECT CASE WHEN proposer_id=? THEN recipient_id ELSE proposer_id END "
            "FROM social_appointments WHERE world_id=? AND status='missed' AND repaired_by IS NULL "
            "AND ends_at<=? AND (proposer_id=? OR recipient_id=?)",
            (player["id"], wid, to_iso(at), player["id"], player["id"]),
        )}
        from world_engine.encounters import EncounterService

        for npc in c.execute(
            "SELECT * FROM characters WHERE world_id=? AND is_player=0", (wid,)
        ).fetchall():
            # 哥哥，先核对本次真实接近或分离；暂停联系和离世也不会留下假场次。
            EncounterService.observe_pair(c, wid, npc["id"], player["id"], at)
            if npc["health"] <= 0:
                continue
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
            important = c.execute(
                "SELECT request_id FROM contract_fulfillments WHERE world_id=? AND recipient_id=? AND requester_id=? AND status='active' AND due_world_time<=?",
                (wid, npc["id"], player["id"], to_iso(at + timedelta(days=1))),
            ).fetchone()
            shared_plan_attention=None
            if not important:
                shared_plan_attention=c.execute("SELECT p.id FROM shared_preparation_plans p JOIN shared_preparation_roles r ON r.plan_id=p.id AND r.character_id=? WHERE p.world_id=? AND p.status='active' "
                    "AND (p.proposer_id=? OR p.recipient_id=?) AND (p.deadline_at<=? OR r.private_reason<>'') LIMIT 1",
                    (npc["id"],wid,player["id"],player["id"],to_iso(at+timedelta(days=1)))).fetchone()
                important=shared_plan_attention
            preparation_follow_up=None
            if not important:
                preparation_follow_up=c.execute(
                    "SELECT p.id FROM shared_preparation_plans p JOIN world_events e ON e.id=p.source_event_id "
                    "WHERE p.world_id=? AND p.status='completed' AND (p.proposer_id=? OR p.recipient_id=?) "
                    "AND (p.proposer_id=? OR p.recipient_id=?) AND e.occurred_at>=? "
                    "AND NOT EXISTS (SELECT 1 FROM preparation_follow_through f JOIN shared_life_moments m ON m.id=f.moment_id "
                    "WHERE f.plan_id=p.id) LIMIT 1",
                    (wid,npc["id"],npc["id"],player["id"],player["id"],to_iso(at-timedelta(days=1))),
                ).fetchone()
                important=preparation_follow_up
            risk = c.execute("SELECT a.* FROM npc_schedule_assessments a JOIN contract_fulfillments f ON f.request_id=a.contract_id WHERE a.character_id=? AND f.requester_id=? AND f.status='active' AND a.reason!=''",(npc["id"],player["id"])).fetchone()
            from world_engine.intimacy import IntimacyService

            if IntimacyService.paused(c, wid, npc["id"], player["id"]):
                continue
            close = IntimacyService.connected(c, wid, npc["id"], player["id"])
            close = close or bool(c.execute("SELECT 1 FROM family_agreements WHERE world_id=? AND status='active' AND ((proposer_id=? AND recipient_id=?) OR (proposer_id=? AND recipient_id=?))",(wid,npc["id"],player["id"],player["id"],npc["id"])).fetchone())
            pending_heart = c.execute(
                "SELECT 1 FROM intimate_bonds WHERE world_id=? AND first_id=? AND second_id=? "
                "AND proposer_id=? AND status='pending' AND expires_at>?",
                (wid, *sorted((npc["id"], player["id"])), player["id"], to_iso(at)),
            ).fetchone()
            if not pending_heart:
                pending_heart = c.execute(
                    "SELECT 1 FROM shared_life_moments WHERE world_id=? AND proposer_id=? AND recipient_id=? AND status='pending' AND expires_at>?",
                    (wid, player["id"], npc["id"], to_iso(at)),
                ).fetchone()
            if not pending_heart:
                pending_heart=c.execute("SELECT 1 FROM social_appointments WHERE world_id=? AND proposer_id=? AND recipient_id=? AND status='pending' AND starts_at>?",
                    (wid,player["id"],npc["id"],to_iso(at))).fetchone()
            if not pending_heart:
                pending_heart=c.execute("SELECT 1 FROM social_appointment_series WHERE world_id=? AND status='resume_pending' AND resume_requested_by=? AND (proposer_id=? OR recipient_id=?)",
                    (wid,player["id"],npc["id"],npc["id"])).fetchone()
            if not pending_heart:
                pending_heart=c.execute("SELECT 1 FROM shared_preparation_plans WHERE world_id=? AND proposer_id=? AND recipient_id=? AND status='pending' AND deadline_at>?",
                    (wid,player["id"],npc["id"],to_iso(at))).fetchone()
            pending_companion = None
            if not pending_heart:
                pending_companion = c.execute(
                    "SELECT id,source_event_id FROM companion_proposals WHERE world_id=? AND proposer_id=? "
                    "AND recipient_id=? AND status='pending' AND expires_at>? ORDER BY rowid DESC LIMIT 1",
                    (wid,player["id"],npc["id"],to_iso(at)),
                ).fetchone()
                pending_heart = pending_companion
            pending_group = None
            if not pending_heart:
                pending_group = c.execute(
                    "SELECT g.id,g.proposal_event_id FROM group_journeys g "
                    "JOIN group_journey_members n ON n.journey_id=g.id AND n.character_id=? "
                    "JOIN group_journey_members p ON p.journey_id=g.id AND p.character_id=? "
                    "WHERE g.world_id=? AND g.status='pending' AND g.deadline_at>? "
                    "AND n.decision='pending' AND n.left_at IS NULL AND p.left_at IS NULL "
                    "AND p.decision<>'declined' ORDER BY g.rowid LIMIT 1",
                    (npc["id"],player["id"],wid,to_iso(at)),
                ).fetchone()
                pending_heart = pending_group
            from world_engine.relationship_continuity import RelationshipContinuityService
            from world_engine.appointment_followup import AppointmentFollowupService
            # 哥哥，旧约定只提供本人可拒绝的考虑机会，不推定对方到场或失约原因。
            appointment_followup = AppointmentFollowupService.opportunity(c,wid,npc["id"],player["id"],at) if npc["id"] in missed_partners and not important and not risk and not pending_heart else None
            continuity_attention = RelationshipContinuityService.opportunity(c,wid,npc["id"],player["id"],at) if npc["id"] in continuity_owners and not important and not risk and not pending_heart and not appointment_followup else None
            from world_engine.travel_interaction import TravelInteractionService
            may_consider_scene = nearby and not (
                important or risk or pending_heart or continuity_attention or appointment_followup
            )
            travel_attention = (
                TravelInteractionService.shared_motion(c, wid, npc["id"], player["id"], at)
                if may_consider_scene else None
            )
            encounter_attention = (
                EncounterService.opportunity(c, wid, npc["id"], player["id"], at)
                if may_consider_scene and not travel_attention else None
            )
            # 哥哥，真实见面提供一次自主考虑，性格和信任留给本人决定是否开口。
            if not (
                important or risk or close or pending_heart or continuity_attention or appointment_followup
                or travel_attention or encounter_attention
            ):
                continue
            if not nearby and not contact:
                continue
            reason = (
                "共同筹备有需要商量的情况，请结合真实期限和本人实际困难，自主决定是否说明、调整或退出"
                if shared_plan_attention
                else "双方已共同核对筹备，可自主决定是否另行邀请一次真实用餐、休息或聊天，引用原计划；对方仍需独立同意，财物仍各自保管，也可以暂不联系"
                if preparation_follow_up
                else "有临近期限的共同事务，可以决定是否提醒对方"
                if important
                else "对方表达了尚待回应的关系心意或共同生活邀请，可以独立决定答应、拒绝、继续了解或暂不联系；不要替对方作决定"
                if pending_heart
                else AppointmentFollowupService.REASON
                if appointment_followup
                else "本人有跨日保留的相处事项，可以结合私密记忆自主决定是否继续商量、提出合适邀请、表达顾虑或暂不联系，不替对方作决定"
                if continuity_attention
                else "与对方有双方确认的亲密关系，可根据共同经历、界限与自身日程决定是否问候、分享近况、主动提出未来约会或商量一次真实补约；也可以表达顾虑或不联系，不替对方确认"
                if close
                else "遇见对方，可以根据关系和当前处境决定是否主动交谈"
            )
            if important or risk or pending_heart or continuity_attention or appointment_followup or travel_attention:
                EncounterService.preempt_unclaimed(c, wid, npc["id"], player["id"], at, reason)
            job_id = str(uuid4())
            inserted = c.execute(
                "INSERT OR IGNORE INTO npc_outreach_jobs(id,world_id,npc_id,player_id,contact_id,channel,reason,world_day) VALUES (?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    wid,
                    npc["id"],
                    player["id"],
                    contact["id"] if contact else None,
                    "in_person" if nearby else "letter",
                    reason,
                    at.date().isoformat(),
                ),
            ).rowcount
            if pending_companion and not important and not risk:
                # 哥哥，收到邀请只提供一次自主考虑机会，不替本人答应，也不增加每日联系额度。
                c.execute(
                    "UPDATE npc_outreach_jobs SET reason=?,source_event_id=? WHERE world_id=? AND npc_id=? "
                    "AND player_id=? AND world_day=? AND status='pending' AND attempts=0 AND source_event_id IS NULL",
                    ("对方提出了尚待回应的短时同行邀请；结合本人处境和时长，自主决定接受、婉拒、反提或暂不回应，不替玩家同意。",
                     pending_companion["source_event_id"],wid,npc["id"],player["id"],at.date().isoformat()),
                )
            if pending_group and not important and not risk:
                c.execute(
                    "UPDATE npc_outreach_jobs SET reason=?,source_event_id=? WHERE world_id=? AND npc_id=? "
                    "AND player_id=? AND world_day=? AND status='pending' AND attempts=0 AND source_event_id IS NULL",
                    ("本人收到完整有限多人旅行提议，可以独立接受、婉拒或暂不回应；一位成员的选择不代表其他人。",
                     pending_group["proposal_event_id"],wid,npc["id"],player["id"],at.date().isoformat()),
                )
            if appointment_followup:
                # 哥哥，保留已考虑的旧任务；只让尚未考虑的普通机会承接新来源。
                c.execute(
                    "UPDATE npc_outreach_jobs SET reason=?,source_event_id=? WHERE world_id=? AND npc_id=? "
                    "AND player_id=? AND world_day=? AND status='pending' AND attempts=0 AND source_event_id IS NULL "
                    "AND reason NOT LIKE '有临近%' AND NOT EXISTS(SELECT 1 FROM relationship_continuity_followups f "
                    "WHERE f.job_id=npc_outreach_jobs.id)",
                    (AppointmentFollowupService.REASON, appointment_followup, wid, npc["id"], player["id"], at.date().isoformat()),
                )
            if travel_attention:
                # 哥哥，只绑定当前真实共同移动的这一程，仍沿用原来每对人物每日一次的机会。
                c.execute(
                    "UPDATE npc_outreach_jobs SET reason=?,source_event_id=? WHERE world_id=? AND npc_id=? "
                    "AND player_id=? AND world_day=? AND status='pending' AND attempts=0 AND source_event_id IS NULL "
                    "AND reason NOT LIKE '有临近%' AND NOT EXISTS(SELECT 1 FROM relationship_continuity_followups f "
                    "WHERE f.job_id=npc_outreach_jobs.id)",
                    (TravelInteractionService.JOB_PREFIX + "；双方仍在途中，可以自愿说话、提出暂停或暂不联系；"
                     "说话本身不会停止移动，调整路线仍须双方另外决定。",
                     travel_attention["source_event_id"],wid,npc["id"],player["id"],at.date().isoformat()),
                )
            if encounter_attention:
                encounter_job = c.execute(
                    "SELECT id FROM npc_outreach_jobs WHERE world_id=? AND npc_id=? AND player_id=? "
                    "AND world_day=? AND channel='in_person' AND status='pending' AND attempts=0 "
                    "AND source_event_id IS NULL AND (reason=? OR reason LIKE '与对方有双方确认的亲密关系%') "
                    "AND NOT EXISTS(SELECT 1 FROM relationship_continuity_followups f "
                    "WHERE f.job_id=npc_outreach_jobs.id)",
                    (wid,npc["id"],player["id"],at.date().isoformat(),
                     "遇见对方，可以根据关系和当前处境决定是否主动交谈"),
                ).fetchone()
                if encounter_job:
                    EncounterService.attach(c, encounter_job["id"], encounter_attention, at)
            if inserted and continuity_attention:
                RelationshipContinuityService.attach(c,job_id,continuity_attention)
            elif continuity_attention:
                # 哥哥，清晨排下但尚未考虑的普通问候，可以承接稍后到期的持续事项。
                existing = c.execute("SELECT id FROM npc_outreach_jobs WHERE world_id=? AND npc_id=? AND player_id=? AND world_day=? AND status='pending' AND attempts=0 AND source_event_id IS NULL AND reason NOT LIKE '有临近%' AND NOT EXISTS(SELECT 1 FROM relationship_continuity_followups f WHERE f.job_id=npc_outreach_jobs.id)", (wid,npc["id"],player["id"],at.date().isoformat())).fetchone()
                if existing:
                    c.execute("UPDATE npc_outreach_jobs SET reason=? WHERE id=?",("本人有跨日保留的相处事项，可以结合私密记忆自主决定是否继续商量或暂不联系，不替对方作决定",existing[0]))
                    RelationshipContinuityService.attach(c,existing[0],continuity_attention)
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
