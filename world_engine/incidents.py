"""全球危机只提供当地迹象；人物自己筹料维修，真实结果解除生产阻碍。"""

from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

from world_engine.action_checks import CheckService
from world_engine.activity_tasks import TaskService
from world_engine.event_history import EventHistoryService
from world_engine.event_log import record_event
from world_engine.geo import great_circle_distance_km
from world_engine.repository import from_iso, to_iso


class IncidentService:
    @staticmethod
    def _event(c, wid, at, kind, summary, *, actor=None, location=None, payload=None, causes=()):
        event = record_event(c, world_id=wid, tick_id=str(uuid4()), occurred_at=at,
                             event_type=kind, summary=summary, actor_id=actor,
                             location_id=location, payload=payload or {})
        for cause in causes:
            if cause:
                EventHistoryService.link(c, event, cause, strict=False)
        return event

    @classmethod
    def create(cls, c, wid, title, description, sites, at, *, idempotency_key):
        """哥哥可以注入危机事实，但不能注入人物目标、行动或完成结论。"""
        existing = c.execute("SELECT id FROM world_incidents WHERE world_id=? AND idempotency_key=?",
                             (wid, idempotency_key)).fetchone()
        if existing:
            return existing[0]
        world = c.execute("SELECT w.current_time FROM worlds w WHERE id=?", (wid,)).fetchone()
        if not world or not title or not idempotency_key or not sites:
            raise ValueError("危机需要当前世界、名称、幂等键和受影响设施")
        if at != from_iso(world[0]):
            raise ValueError("新危机只能发生在当前世界时间")
        seen = set()
        for site in sites:
            item = c.execute("SELECT * FROM item_instances WHERE id=? AND world_id=?",
                             (site["item_id"], wid)).fetchone()
            actor = c.execute("SELECT * FROM characters WHERE id=? AND world_id=? AND is_player=0",
                              (site["custodian_id"], wid)).fetchone()
            recipe = c.execute("SELECT spec_json FROM activity_recipes WHERE id=? AND world_id=?",
                               (site["recipe_id"], wid)).fetchone()
            spec = json.loads(recipe[0]) if recipe else {}
            if not c.execute("SELECT 1 FROM locations WHERE id=? AND world_id=? AND is_active=1",
                             (site["location_id"], wid)).fetchone():
                raise ValueError("受影响地点必须属于当前世界")
            if not item or not actor or item["quantity"] != 1 or item["owner_character_id"] != actor["id"] or (
                item["container_type"] != "character_inventory" or item["container_id"] != actor["id"]
            ) or spec.get("kind") != "repair" or spec.get("location_id") != site["location_id"]:
                raise ValueError("设施需要同世界的 NPC 保管原件和当地维修配方")
            category = c.execute("SELECT category FROM item_types WHERE id=?", (item["item_type_id"],)).fetchone()[0]
            if category != spec["repair_category"] or not 0 < site["damaged_condition"] < site["required_condition"] <= 100:
                raise ValueError("设施损坏程度、恢复阈值或维修类别无效")
            if item["id"] in seen or c.execute(
                "SELECT 1 FROM incident_sites s JOIN world_incidents i ON i.id=s.incident_id "
                "WHERE s.item_id=? AND i.status='active'", (item["id"],),
            ).fetchone():
                raise ValueError("同一原件不能重复承载未解决危机")
            if not site["resource_keys"] or any(not isinstance(key, str) or not key for key in site["resource_keys"]):
                raise ValueError("必须声明受阻的生产资源")
            seen.add(item["id"])
        incident = str(uuid4())
        event = cls._event(c, wid, at, "world.incident_started", description,
                          payload={"incident_id": incident, "information_scope": "author_hidden"})
        c.execute("UPDATE event_profiles SET scope_type='global',scope_id=?,impact_level=5,"
                  "status='ongoing',classification_basis='incident_rule_v1' WHERE event_id=?",
                  (wid, event))
        c.execute("INSERT INTO world_incidents(id,world_id,idempotency_key,title,description,"
                  "started_world_time,source_event_id) VALUES (?,?,?,?,?,?,?)",
                  (incident, wid, idempotency_key, title, description, to_iso(at), event))
        for site in sites:
            site_id = str(uuid4())
            c.execute("INSERT INTO incident_sites(id,incident_id,location_id,custodian_id,item_id,"
                      "recipe_id,resource_keys_json,required_condition) VALUES (?,?,?,?,?,?,?,?)",
                      (site_id, incident, site["location_id"], site["custodian_id"], site["item_id"],
                       site["recipe_id"], json.dumps(site["resource_keys"]), site["required_condition"]))
            c.execute("INSERT INTO incident_disruptions(id,site_id,started_world_time) VALUES (?,?,?)",
                      (str(uuid4()), site_id, to_iso(at)))
            c.execute("UPDATE item_instances SET condition=? WHERE id=?",
                      (site["damaged_condition"], site["item_id"]))
            c.execute("INSERT OR IGNORE INTO event_affected_entities VALUES (?,?,'location')",
                      (event, site["location_id"]))
        return incident

    @classmethod
    def tick(cls, c, wid, at):
        restored_sites = c.execute(
            "SELECT s.* FROM incident_sites s JOIN world_incidents i ON i.id=s.incident_id "
            "WHERE i.world_id=? AND i.status='active' AND s.resolution_event_id IS NOT NULL", (wid,),
        ).fetchall()
        for site in restored_sites:
            if cls._operational(c, site):
                continue
            cls._event(c, wid, at, "world.incident_site_disrupted", "先前恢复的原件再次失去运行条件。",
                       location=site["location_id"], payload={"site_id": site["id"],
                       "incident_id": site["incident_id"], "information_scope": "author_hidden"},
                       causes=(site["resolution_event_id"],))
            item = c.execute("SELECT condition FROM item_instances WHERE id=?", (site["item_id"],)).fetchone()
            # Yuki 保留完好原件的维修证据；再次损坏才需要新的目击与维修。
            observation = site["observation_event_id"] if item and item[0] >= site["required_condition"] else None
            c.execute("UPDATE incident_sites SET observation_event_id=?,request_event_id=NULL,"
                      "resolution_event_id=NULL,resolved_world_time=NULL WHERE id=?", (observation, site["id"]))
            c.execute("INSERT INTO incident_disruptions(id,site_id,started_world_time) VALUES (?,?,?)",
                      (str(uuid4()), site["id"], to_iso(at)))
        rows = c.execute("SELECT s.*,i.source_event_id,i.title,i.started_world_time FROM incident_sites s "
                         "JOIN world_incidents i ON i.id=s.incident_id WHERE i.world_id=? "
                         "AND i.status='active' AND i.started_world_time<=? AND s.resolution_event_id IS NULL",
                         (wid, to_iso(at))).fetchall()
        for row in rows:
            site = dict(row)
            condition = c.execute("SELECT condition FROM item_instances WHERE id=?", (site["item_id"],)).fetchone()
            if site["observation_event_id"] and condition and condition[0] < site["required_condition"] and c.execute(
                "SELECT 1 FROM npc_agency_goals WHERE character_id=? AND kind='incident_repair' "
                "AND status='completed' AND source_event_id=? AND json_extract(predicate_json,'$.item_id')=?",
                (site["custodian_id"], site["observation_event_id"], site["item_id"]),
            ).fetchone():
                # Yuki 也核对暂时离场期间的再次损坏，不能让旧目标挡住新维修。
                c.execute("UPDATE incident_sites SET observation_event_id=NULL,request_event_id=NULL WHERE id=?",
                          (site["id"],))
                site["observation_event_id"] = None
            if not site["observation_event_id"]:
                place = c.execute("SELECT * FROM locations WHERE id=? AND is_active=1", (site["location_id"],)).fetchone()
                if not place:
                    continue
                for actor in c.execute("SELECT * FROM characters WHERE world_id=? AND is_player=0 "
                                       "AND health>0 AND current_room_id IS NULL ORDER BY id", (wid,)):
                    if actor["longitude"] is None or actor["latitude"] is None:
                        continue
                    if great_circle_distance_km(actor["longitude"], actor["latitude"],
                                                place["longitude"], place["latitude"]) > .1:
                        continue
                    observation = cls._event(c, wid, at, "world.incident_observed",
                        f"{actor['name']}看到{place['name']}的防护原件损坏，露天生产受阻。",
                        actor=actor["id"], location=place["id"],
                        payload={"incident_id": site["incident_id"], "site_id": site["id"]},
                        causes=(site["source_event_id"],))
                    c.execute("UPDATE incident_sites SET observation_event_id=? WHERE id=?",
                              (observation, site["id"]))
                    break
            goal = c.execute("SELECT * FROM npc_agency_goals WHERE world_id=? AND character_id=? "
                             "AND kind='incident_repair' AND status='completed' "
                             "AND json_extract(predicate_json,'$.item_id')=? AND source_event_id=?",
                             (wid, site["custodian_id"], site["item_id"], site["observation_event_id"])).fetchone()
            if not goal:
                continue
            item = c.execute("SELECT * FROM item_instances WHERE id=?", (site["item_id"],)).fetchone()
            evidence = c.execute(
                "SELECT a.finish_event_id FROM npc_agency_steps s "
                "JOIN action_execution_links l ON l.source_event_id=s.event_id AND l.execution_kind='activity' "
                "JOIN character_life_activities a ON a.id=l.execution_id "
                "JOIN activity_task_details d ON d.activity_id=a.id "
                "WHERE s.goal_id=? AND s.status='completed' AND a.status='completed' "
                "AND a.kind='repair' AND json_extract(d.spec_json,'$.target_item_id')=? "
                "AND json_extract(d.result_json,'$.repair_delta')>0 "
                "AND a.finished_world_time>=? ORDER BY a.finished_world_time DESC LIMIT 1",
                (goal["id"], site["item_id"], site["started_world_time"]),
            ).fetchone()
            if not evidence or not cls._operational(c, site):
                continue
            restored = cls._event(c, wid, at, "world.incident_site_restored",
                f"{site['title']}：当地防护已由真实维修和领取核验恢复。",
                actor=site["custodian_id"], location=site["location_id"],
                payload={"incident_id": site["incident_id"], "site_id": site["id"],
                         "goal_id": goal["id"], "condition": item["condition"]},
                causes=(site["observation_event_id"], evidence[0]))
            c.execute("UPDATE incident_sites SET resolution_event_id=?,resolved_world_time=? WHERE id=?",
                      (restored, to_iso(at), site["id"]))
            c.execute("UPDATE incident_disruptions SET resolved_world_time=? "
                      "WHERE site_id=? AND resolved_world_time IS NULL", (to_iso(at), site["id"]))
        for incident in c.execute("SELECT * FROM world_incidents WHERE world_id=? AND status='active'",
                                  (wid,)).fetchall():
            sites = c.execute("SELECT resolution_event_id FROM incident_sites WHERE incident_id=?",
                              (incident["id"],)).fetchall()
            if not sites or any(not site[0] for site in sites):
                continue
            event = cls._event(c, wid, at, "world.incident_resolved",
                f"{incident['title']}的全部受影响设施已恢复，生产阻碍解除。",
                payload={"incident_id": incident["id"], "information_scope": "author_hidden"},
                causes=[site[0] for site in sites])
            c.execute("UPDATE world_incidents SET status='resolved',resolved_world_time=?,"
                      "resolution_event_id=? WHERE id=?", (to_iso(at), event, incident["id"]))
            c.execute("UPDATE event_profiles SET status='resolved' WHERE event_id=?", (incident["source_event_id"],))
            c.execute("UPDATE event_profiles SET scope_type='global',scope_id=?,status='resolved',"
                      "classification_basis='incident_rule_v1' WHERE event_id=?", (wid, event))

    @staticmethod
    def _operational(c, site):
        from world_engine.npc_operators import near_location

        item = c.execute("SELECT * FROM item_instances WHERE id=?", (site["item_id"],)).fetchone()
        actor = c.execute("SELECT * FROM characters WHERE id=?", (site["custodian_id"],)).fetchone()
        return bool(item and actor and actor["health"] > 0 and item["quantity"] > 0
                    and item["condition"] >= site["required_condition"]
                    and item["owner_character_id"] == site["custodian_id"]
                    and item["container_type"] == "character_inventory"
                    and item["container_id"] == site["custodian_id"]
                    and near_location(c, actor, site["location_id"]))

    @classmethod
    def seed_goals(cls, c, wid, actor, at):
        """Yuki 只让知情者用自身能力和可见物资选择贡献，不读取远方私有库存。"""
        from world_engine.npc_agency import NpcAgencyService

        sites = c.execute("SELECT s.* FROM incident_sites s JOIN world_incidents i ON i.id=s.incident_id "
                          "JOIN character_event_knowledge k ON k.event_id=s.observation_event_id "
                          "AND k.character_id=? AND k.learned_world_time<=? WHERE i.world_id=? "
                          "AND i.status='active' AND s.custodian_id=? AND s.resolution_event_id IS NULL",
                          (actor["id"], to_iso(at), wid, actor["id"])).fetchall()
        for site in sites:
            recipe = c.execute("SELECT spec_json FROM activity_recipes WHERE id=?", (site["recipe_id"],)).fetchone()
            spec = json.loads(recipe[0])
            item_condition = c.execute("SELECT condition FROM item_instances WHERE id=?",
                                       (site["item_id"],)).fetchone()[0]
            if item_condition >= site["required_condition"]:
                continue
            if CheckService.skill(c, actor, spec["check_skill"]) < spec["check_min_proficiency"]:
                continue
            NpcAgencyService.adopt(c, wid, actor["id"], "incident_repair",
                {"kind": "item_condition_at_least", "item_id": site["item_id"],
                 "recipe_id": site["recipe_id"], "amount": site["required_condition"]},
                "看到当地生产防护损坏，想修复它帮助大家", at, site["observation_event_id"], priority=2)
            missing = []
            required = [{**entry, "minimum_condition": 1} for entry in spec["ingredients"]]
            required += [{**entry, "quantity": 1, "minimum_condition": entry["wear"]}
                         for entry in TaskService.tool_requirements(spec)]
            supplies_state = {}
            for requirement in required:
                item_type, quantity = requirement["item_type_id"], requirement["quantity"]
                minimum = requirement["minimum_condition"]
                carried = cls._carried(c, wid, actor["id"], item_type, at, minimum_condition=minimum,
                                       excluded_item=site["item_id"])
                supplies_state[item_type] = {"available": carried, "minimum_condition": minimum}
                if carried < quantity:
                    missing.append({"item_type_id": item_type, "amount": min(20, quantity - carried),
                                    **({"minimum_condition": minimum} if "wear" in requirement else {})})
            supplies_state["target_condition"] = item_condition
            previous = c.execute("SELECT payload_json FROM world_events WHERE id=?",
                                 (site["request_event_id"],)).fetchone()
            if missing and (not previous or json.loads(previous[0]).get("supplies_state") != supplies_state):
                event = cls._event(c, wid, at, "social.incident_supply_requested",
                    f"{actor['name']}说明维修缺少物资，请愿意帮忙的人送到当地工坊。",
                    actor=actor["id"], location=actor["current_location_id"] or actor["location_id"],
                    payload={"incident_id": site["incident_id"], "site_id": site["id"],
                             "supplies": missing, "location_id": site["location_id"],
                             "supplies_state": supplies_state,
                             "custodian_id": actor["id"]}, causes=(site["observation_event_id"],))
                c.execute("UPDATE incident_sites SET request_event_id=? WHERE id=?", (event, site["id"]))
            else:
                event = site["request_event_id"]
            if missing and event:
                cls._seek_contacts(c, wid, actor, at, event)
                reached_contact = c.execute(
                    "SELECT 1 FROM npc_agency_goals WHERE character_id=? AND kind='incident_outreach' "
                    "AND source_event_id=? AND status='completed'", (actor["id"], event),
                ).fetchone()
                if reached_contact and (actor["current_location_id"] or actor["location_id"]) != site["location_id"]:
                    NpcAgencyService.adopt(c, wid, actor["id"], "incident_return",
                        {"kind": "at_location", "location_id": site["location_id"]},
                        "已经向相识者说明物资需求，想回工坊接收材料并维修", at, event, priority=3)
        if actor["health"] < 40 or actor["satiety"] < 45 or c.execute(
            "SELECT 1 FROM npc_agency_goals WHERE character_id=? AND kind='incident_supply' "
            "AND status IN ('active','blocked')", (actor["id"],),
        ).fetchone():
            return
        requests = c.execute(
            "SELECT e.id,e.payload_json,l.longitude,l.latitude FROM character_event_knowledge k "
            "JOIN world_events e ON e.id=k.event_id JOIN incident_sites s ON s.request_event_id=e.id "
            "JOIN world_incidents i ON i.id=s.incident_id JOIN locations l ON l.id=s.location_id "
            "WHERE k.world_id=? AND k.character_id=? AND k.learned_world_time<=? "
            "AND i.status='active' AND s.resolution_event_id IS NULL AND s.custodian_id!=?",
            (wid, actor["id"], to_iso(at), actor["id"]),
        ).fetchall()
        requests = sorted(requests, key=lambda r: great_circle_distance_km(
            actor["longitude"], actor["latitude"], r["longitude"], r["latitude"]))
        for request in requests:
            payload = json.loads(request["payload_json"])
            for supply in payload["supplies"]:
                amount = min(20, supply["amount"])
                minimum = supply.get("minimum_condition", 1)
                if cls._carried(c, wid, actor["id"], supply["item_type_id"], at, minimum_condition=minimum) < amount:
                    continue
                predicate = {"kind": "delivered_item", "item_type_id": supply["item_type_id"], "amount": amount,
                             "target_id": payload["custodian_id"], "location_id": payload["location_id"],
                             **({"minimum_condition": minimum} if "minimum_condition" in supply else {})}
                earlier = c.execute("SELECT predicate_json FROM npc_agency_goals WHERE character_id=? "
                                    "AND kind='incident_supply' AND source_event_id=? AND status='completed'",
                                    (actor["id"], request["id"])).fetchall()
                if any(json.loads(goal[0]) == predicate for goal in earlier):
                    continue
                NpcAgencyService.adopt(c, wid, actor["id"], "incident_supply", predicate,
                    "听到维修者求助，自己有可用物资，想送过去恢复生产", at, request["id"], priority=2)
                return

    @staticmethod
    def _seek_contacts(c, wid, actor, at, event):
        """去本人持有的新鲜地址找相识者，Yuki 不读取对方的实时坐标或私有库存。"""
        from world_engine.npc_agency import NpcAgencyService

        contacts = c.execute(
            "SELECT DISTINCT k.subject_id,k.value FROM observer_knowledge_edges k "
            "JOIN knowledge_evidence e ON e.edge_id=k.id JOIN characters p ON p.id=k.subject_id "
            "WHERE k.world_id=? AND k.observer_id=? AND k.predicate='location' "
            "AND k.status IN ('confirmed','reported') AND k.confidence>=0.5 "
            "AND e.learned_at<=? AND (e.valid_until IS NULL OR e.valid_until>?) "
            "AND p.is_player=0 AND p.id!=? ORDER BY k.last_learned_at DESC LIMIT 2",
            (wid, actor["id"], to_iso(at), to_iso(at), actor["id"]),
        ).fetchall()
        for contact in contacts:
            NpcAgencyService.adopt(c, wid, actor["id"], "incident_outreach",
                {"kind": "spoke_about_event", "event_id": event, "target_id": contact[0],
                 "location_id": contact[1], "dialogue": "当地生产防护坏了，我缺少物资，能帮忙送来吗？"},
                "维修缺少物资，想按自己知道的地址去找相识者商量", at, event, priority=3)

    @staticmethod
    def _carried(c, wid, actor, item, at, *, minimum_condition=1, excluded_item=""):
        return c.execute("SELECT COALESCE(SUM(quantity),0) FROM item_instances WHERE world_id=? "
                         "AND container_type='character_inventory' AND container_id=? "
                         "AND owner_character_id=? AND item_type_id=? AND condition>=? AND id!=? "
                         "AND (spoils_world_time IS NULL OR spoils_world_time>?)",
                         (wid, actor, actor, item, minimum_condition, excluded_item, to_iso(at))).fetchone()[0]

    @staticmethod
    def productive_days(c, wid, location, resource, start, days):
        """扣除生产受阻区间，恢复后不会补发灾害期间的产量。"""
        end = start + timedelta(days=days)
        intervals = []
        for row in c.execute("SELECT s.resource_keys_json,d.started_world_time,d.resolved_world_time "
                             "FROM incident_sites s JOIN world_incidents i ON i.id=s.incident_id "
                             "JOIN incident_disruptions d ON d.site_id=s.id "
                             "WHERE i.world_id=? AND s.location_id=? "
                             "UNION ALL SELECT b.resource_keys_json,b.started_world_time,b.resolved_world_time "
                             "FROM cooperation_production_blocks b JOIN cooperative_crises i ON i.id=b.crisis_id "
                             "WHERE i.world_id=? AND b.location_id=?", (wid, location, wid, location)):
            if resource not in json.loads(row[0]):
                continue
            left, right = max(start, from_iso(row[1])), min(end, from_iso(row[2]) if row[2] else end)
            if left < right:
                intervals.append((left, right))
        blocked = 0
        cursor = start
        for left, right in sorted(intervals):
            left = max(cursor, left)
            if right > left:
                blocked += (right - left).total_seconds()
                cursor = right
        return max(0, days - blocked / 86400)

    @staticmethod
    def view(c, wid):
        result = []
        for row in c.execute("SELECT * FROM world_incidents WHERE world_id=? ORDER BY started_world_time,id", (wid,)):
            sites = [{**dict(s), "resource_keys": json.loads(s["resource_keys_json"])}
                     for s in c.execute("SELECT * FROM incident_sites WHERE incident_id=? ORDER BY id", (row["id"],))]
            result.append({**dict(row), "sites": sites, "restored_sites": sum(bool(s["resolution_event_id"]) for s in sites)})
        return result
