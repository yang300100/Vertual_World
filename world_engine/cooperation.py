"""灰孢潮的合作与运输规则；Yuki 不替人物签署、瞬移物资或编造制作结果。"""

import json
from datetime import timedelta
from uuid import uuid4

from world_engine.event_log import record_event
from world_engine.geo import great_circle_distance_km
from world_engine.inventory import InventoryService
from world_engine.npc_operators import near_location
from world_engine.proximity import same_room
from world_engine.repository import from_iso, to_iso


class CooperationService:
    @staticmethod
    def event(c, crisis, at, kind, text, actor=None, target=None, causes=(), **payload):
        event = record_event(c, world_id=crisis["world_id"], tick_id=str(uuid4()), occurred_at=at,
            event_type=f"cooperation.{kind}", summary=text, actor_id=actor, target_id=target,
            payload={"crisis_id": crisis["id"], "information_scope": "private", **payload})
        from world_engine.event_history import EventHistoryService
        for cause in causes:
            if cause:
                EventHistoryService.link(c, event, cause, strict=False)
        return event

    @staticmethod
    def knows(c, actor, event, at):
        return bool(event and c.execute("SELECT 1 FROM character_event_knowledge WHERE character_id=? "
            "AND event_id=? AND learned_world_time<=?", (actor, event, to_iso(at))).fetchone())

    @staticmethod
    def _rows(c, cid, key=None):
        crisis = c.execute("SELECT * FROM cooperative_crises WHERE id=?", (cid,)).fetchone()
        member = c.execute("SELECT * FROM cooperation_members WHERE crisis_id=? AND key=?", (cid, key)).fetchone()
        if not crisis or (key is not None and not member):
            raise ValueError("未知的合作危机或成员")
        return crisis, member

    @classmethod
    def create(cls, c, wid, coordinator, hub, members, recipes, cure, at):
        from world_engine.polities import current_polities

        # 哥哥注入受灾事实与既有授权、配方，人物目标由下一次正常心跳形成。
        catalogue = [m["key"] for m in members]
        if (not members or len(set(catalogue)) != len(catalogue)
                or set(catalogue) != {p["key"] for p in current_polities()}):
            raise ValueError("合作危机需要完整且不重复的政体名单")
        current = c.execute("SELECT w.current_time FROM worlds w WHERE id=?", (wid,)).fetchone()
        if not current or from_iso(current[0]) != at:
            raise ValueError("合作危机只能注入当前世界时间")
        if not c.execute("SELECT 1 FROM characters WHERE id=? AND world_id=? AND is_player=0 AND health>0", (coordinator, wid)).fetchone():
            raise ValueError("协调者必须是同世界的在世 NPC")
        if not c.execute("SELECT 1 FROM locations WHERE id=? AND world_id=? AND is_active=1", (hub, wid)).fetchone():
            raise ValueError("共同工坊必须属于当前世界")
        if not recipes or len(set(recipes)) != len(recipes):
            raise ValueError("联合制作需要不重复的已登记配方")
        specs = []
        for recipe in recipes:
            registered = c.execute("SELECT spec_json FROM activity_recipes WHERE id=? AND world_id=?", (recipe, wid)).fetchone()
            if not registered:
                raise ValueError("联合配方必须属于当前世界")
            spec = json.loads(registered[0])
            if spec["kind"] != "craft" or spec["location_id"] != hub:
                raise ValueError("联合配方必须在共同工坊制作")
            specs.append(spec)
        if specs[-1]["output_item_type_id"] != cure or specs[-1]["output_quantity"] != len(members):
            raise ValueError("最终配方需要为全部政体提供抑制剂")
        sample_types = {m["sample_type_id"] for m in members}
        if len(sample_types) != len(members) or not sample_types.issubset(
                {i["item_type_id"] for spec in specs for i in spec["ingredients"]}):
            raise ValueError("联合配方需要覆盖全部不同的地区样本")
        roles = [coordinator, *(m[k] for m in members for k in ("delegate_id", "local_actor_id"))]
        if len(set(roles)) != len(roles):
            raise ValueError("协调者、各国使节与当地检验员需要独立人物")
        for m in members:
            for actor in (m["delegate_id"], m["local_actor_id"]):
                if not c.execute("SELECT 1 FROM characters WHERE id=? AND world_id=? AND is_player=0 AND health>0",
                                 (actor, wid)).fetchone():
                    raise ValueError("成员需要同世界的在世 NPC")
            if not c.execute("SELECT 1 FROM locations WHERE id=? AND world_id=? AND is_active=1", (m["capital_id"], wid)).fetchone():
                raise ValueError("成员首都必须属于当前世界")
        cid = str(uuid4())
        crisis = {"id": cid, "world_id": wid}
        source = cls.event(c, crisis, at, "started", "风携灰孢使全球粮场受阻，各地菌株需要联合采样处理。")
        c.execute("INSERT INTO cooperative_crises(id,world_id,title,started_world_time,source_event_id,"
            "coordinator_id,hub_id,catalogue_json,recipes_json,cure_type_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (cid, wid, "全球风携灰孢潮", to_iso(at), source, coordinator, hub,
             json.dumps(catalogue), json.dumps(recipes), cure))
        for m in members:
            c.execute("INSERT INTO cooperation_members(crisis_id,key,name,source,capital_id,delegate_id,"
                "local_actor_id,sample_type_id,policy_json) VALUES (?,?,?,?,?,?,?,?,?)",
                (cid, m["key"], m["name"], m["source"], m["capital_id"], m["delegate_id"], m["local_actor_id"],
                 m["sample_type_id"], json.dumps(m["policy"])))
            c.execute("INSERT INTO cooperation_production_blocks VALUES (?,?,?,? ,?,NULL,NULL)",
                (cid, m["key"], m["capital_id"], '["field_food"]', to_iso(at)))
        return cid

    @classmethod
    def dispatch(cls, c, actor, cid, key, kind, at):
        crisis, member = cls._rows(c, cid, key)
        if crisis["status"] != "active" or kind not in {"report", "remedy"}:
            raise ValueError("当前不能发送此包裹")
        if c.execute("SELECT 1 FROM cooperation_packets WHERE crisis_id=? AND member_key=? AND kind=?", (cid, key, kind)).fetchone():
            raise ValueError("此报告或抑制剂已经寄出")
        if actor["world_id"] != crisis["world_id"] or actor["health"] <= 0 or actor["current_room_id"]:
            raise ValueError("寄送者的现场状态无效")
        before = InventoryService.snapshot(c, actor["world_id"])
        item = None
        if kind == "report":
            if actor["id"] != member["local_actor_id"] or not near_location(c, actor, member["capital_id"]):
                raise ValueError("只有当地目击者能在当地寄出具名报告")
            source = member["observation_event_id"]
            if not cls.knows(c, actor["id"], source, at):
                raise ValueError("不能报告尚未目击的事实")
            recipient, destination = member["delegate_id"], crisis["hub_id"]
        else:
            if actor["id"] != crisis["coordinator_id"] or not near_location(c, actor, crisis["hub_id"]):
                raise ValueError("只有工坊协调者能寄出真实成果")
            if not cls.all_contributed(c, cid) or not cls.fabricated(c, cid):
                raise ValueError("全部政体尚未贡献，或联合制作尚未真实完成")
            source = cls.fabricated(c, cid)
            item = c.execute("SELECT * FROM item_instances WHERE id=? AND world_id=? AND owner_character_id=? "
                "AND container_type='character_inventory' AND container_id=? AND quantity>0 AND condition>0",
                (crisis["cure_origin_item_id"], actor["world_id"], actor["id"], actor["id"])).fetchone()
            if not item:
                raise ValueError("需要先领取真正制成的抑制剂")
            recipient, destination = member["local_actor_id"], member["capital_id"]
        place = c.execute("SELECT * FROM locations WHERE id=?", (destination,)).fetchone()
        distance = great_circle_distance_km(actor["longitude"], actor["latitude"], place["longitude"], place["latitude"])
        speed = 20.0
        arrival = at + timedelta(hours=2 + distance / speed)
        packet = str(uuid4())
        cargo = None
        if item:
            cargo = str(uuid4())
            InventoryService.consume(c, item, 1)
            c.execute("INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,"
                "owner_character_id,quantity,condition) VALUES (?,?,?,'cooperation_transit',?,?,1,?)",
                (cargo, actor["world_id"], item["item_type_id"], packet, actor["id"], item["condition"]))
        event = cls.event(c, crisis, at, "packet_dispatched", "人物将具名报告或实际抑制剂交付传统驿运。",
            actor["id"], causes=(source,), member_key=key, packet_kind=kind, distance_km=distance,
            arrival_world_time=to_iso(arrival), recipient_id=recipient, cargo_item_id=cargo)
        c.execute("INSERT INTO cooperation_packets(id,crisis_id,member_key,kind,sender_id,recipient_id,"
            "destination_id,distance_km,speed_kmh,dispatched_world_time,arrival_world_time,dispatch_event_id,"
            "source_event_id,item_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (packet, cid, key, kind, actor["id"], recipient, destination, distance, speed, to_iso(at), to_iso(arrival), event, source, cargo))
        InventoryService.audit(c, actor["world_id"], event, before)
        return event

    @classmethod
    def propose(cls, c, actor, cid, at):
        crisis, _ = cls._rows(c, cid)
        if actor["id"] != crisis["coordinator_id"] or not near_location(c, actor, crisis["hub_id"]):
            raise ValueError("需要协调者在工坊实际提出方案")
        known_reports = [m[0] for m in c.execute("SELECT observation_event_id FROM cooperation_members WHERE crisis_id=?", (cid,))
                         if cls.knows(c, actor["id"], m[0], at)]
        if not known_reports:
            raise ValueError("尚未听到任何当地报告，不能预知灾害")
        terms = json.loads(crisis["terms_json"]) if crisis["revision"] else {
            "equal_allocation": False, "share_method": False, "respect_autonomy": True}
        feedback = c.execute("SELECT e.id,e.payload_json FROM world_events e JOIN character_event_knowledge k "
            "ON k.event_id=e.id AND k.character_id=? WHERE e.world_id=? AND e.event_type='cooperation.policy_rejected' "
            "AND json_extract(e.payload_json,'$.crisis_id')=? AND k.learned_world_time<=?", (actor["id"], actor["world_id"], cid, to_iso(at))).fetchall()
        for f in feedback:
            terms.update({k: v for k, v in json.loads(f["payload_json"])["requirements"].items() if k in terms})
        if crisis["revision"] and terms == json.loads(crisis["terms_json"]):
            raise ValueError("未获得可以据此修改条款的新反馈")
        revision = crisis["revision"] + 1
        event = cls.event(c, crisis, at, "proposal", "提出联合采样、共同工坊制作与逐国分配方案。",
            actor["id"], causes=(*known_reports, *(f["id"] for f in feedback)), revision=revision, terms=terms)
        c.execute("UPDATE cooperative_crises SET proposal_event_id=?,revision=?,terms_json=? WHERE id=?",
                  (event, revision, json.dumps(terms), cid))
        c.execute("UPDATE cooperation_members SET consent_event_id=NULL,consent_revision=NULL WHERE crisis_id=?", (cid,))
        # Yuki 废弃旧版决策与旧版方案宣讲目标，避免它们永远挤在新版前面。
        c.execute("UPDATE npc_agency_goals SET status='failed' WHERE world_id=? AND kind IN "
            "('cooperation_decision','cooperation_proposal_share') AND status IN ('active','blocked') "
            "AND json_extract(predicate_json,'$.crisis_id')=? AND source_event_id!=?", (actor["world_id"], cid, event))
        return event

    @classmethod
    def ratify(cls, c, actor, cid, key, at, revision=None):
        crisis, m = cls._rows(c, cid, key)
        coordinator = c.execute("SELECT * FROM characters WHERE id=?", (crisis["coordinator_id"],)).fetchone()
        if actor["id"] != m["delegate_id"] or actor["world_id"] != crisis["world_id"] or actor["health"] <= 0:
            raise ValueError("人物没有该政体的救灾签署授权")
        if not near_location(c, actor, crisis["hub_id"]) or not same_room(actor, coordinator) or not near_location(c, coordinator, crisis["hub_id"]):
            raise ValueError("代表需要与协调者实际在场协商")
        if (not cls.knows(c, actor["id"], m["report_delivery_event_id"], at)
                or not cls.knows(c, actor["id"], m["observation_event_id"], at)
                or not cls.knows(c, actor["id"], crisis["proposal_event_id"], at)):
            raise ValueError("需要本国报告实际送达并获知当前方案")
        if revision is not None and revision != crisis["revision"]:
            raise ValueError("旧版方案已经失效")
        if m["consent_revision"] == crisis["revision"] and m["consent_event_id"]:
            return m["consent_event_id"]
        policy, terms = json.loads(m["policy_json"]), json.loads(crisis["terms_json"])
        requirements = {k: True for k in ("equal_allocation", "share_method", "respect_autonomy")
                        if policy.get(k) and not terms.get(k)}
        if not policy.get("participate", True):
            requirements["participate"] = False
        if requirements:
            return cls.event(c, crisis, at, "policy_rejected", "代表依据自身授权拒绝当前条款，并向在场协调者说明条件。",
                actor["id"], coordinator["id"], causes=(crisis["proposal_event_id"], m["report_delivery_event_id"]),
                member_key=key, revision=crisis["revision"], requirements=requirements)
        event = cls.event(c, crisis, at, "consented", "代表在了解本国风险与当前分配条件后，自主同意合作。",
            actor["id"], coordinator["id"], causes=(crisis["proposal_event_id"], m["report_delivery_event_id"]),
            member_key=key, revision=crisis["revision"], terms=terms)
        c.execute("UPDATE cooperation_members SET consent_event_id=?,consent_revision=? WHERE crisis_id=? AND key=?",
                  (event, crisis["revision"], cid, key))
        return event

    @classmethod
    def all_contributed(cls, c, cid):
        crisis, _ = cls._rows(c, cid)
        members = c.execute("SELECT * FROM cooperation_members WHERE crisis_id=?", (cid,)).fetchall()
        return (set(json.loads(crisis["catalogue_json"])) == {m["key"] for m in members}
            and all(m["consent_event_id"] and m["consent_revision"] == crisis["revision"]
                    and m["contribution_event_id"] for m in members))

    @classmethod
    def fabricated(cls, c, cid):
        crisis, _ = cls._rows(c, cid)
        finish = None
        for recipe in json.loads(crisis["recipes_json"]):
            finish = cls.recipe_finished(c, crisis, recipe)
            if not finish:
                return None
        return finish

    @staticmethod
    def recipe_finished(c, crisis, recipe):
        activity = c.execute("SELECT a.finish_event_id FROM character_life_activities a JOIN activity_task_details d "
            "ON d.activity_id=a.id JOIN action_execution_links l ON l.execution_id=a.id AND l.execution_kind='activity' "
            "JOIN npc_agency_goals g ON g.id=l.goal_id JOIN world_events e ON e.id=l.source_event_id "
            "WHERE a.world_id=? AND a.character_id=? AND g.world_id=a.world_id AND g.character_id=a.character_id "
            "AND g.kind='cooperation_craft' AND json_extract(g.predicate_json,'$.crisis_id')=? "
            "AND json_extract(g.predicate_json,'$.recipe_id')=? AND a.started_world_time>=? "
            "AND a.status='completed' AND a.kind='craft' AND json_extract(e.payload_json,'$.metadata.recipe_id')=? "
            "AND json_extract(d.result_json,'$.completed')=1 ORDER BY a.finished_world_time DESC LIMIT 1",
            (crisis["world_id"], crisis["coordinator_id"], crisis["id"], recipe, crisis["started_world_time"], recipe)).fetchone()
        return activity[0] if activity else None

    @classmethod
    def apply(cls, c, actor, cid, key, at):
        crisis, m = cls._rows(c, cid, key)
        if actor["id"] != m["local_actor_id"] or not near_location(c, actor, m["capital_id"]) or actor["health"] <= 0:
            raise ValueError("只有当地检验员能实际施用")
        if m["application_event_id"]:
            raise ValueError("此地已经施用，不能重复消耗")
        packet = c.execute("SELECT * FROM cooperation_packets WHERE crisis_id=? AND member_key=? AND kind='remedy'",
                           (cid, key)).fetchone()
        if not packet or not cls.knows(c, actor["id"], packet["delivery_event_id"], at) or not cls.fabricated(c, cid):
            raise ValueError("联合制作的抑制剂尚未送达")
        item = c.execute("SELECT * FROM item_instances WHERE id=? AND container_type='character_inventory' "
            "AND container_id=? AND owner_character_id=? AND quantity=1 AND condition>0", (packet["item_id"], actor["id"], actor["id"])).fetchone()
        if not item:
            raise ValueError("未持有实际到货的抑制剂")
        before = InventoryService.snapshot(c, actor["world_id"])
        InventoryService.consume(c, item)
        event = cls.event(c, crisis, at, "applied", "当地检验员实际消耗抑制剂，开始观察粮场灰孢变化。",
            actor["id"], causes=(packet["delivery_event_id"],), member_key=key, consumed_item_id=item["id"])
        c.execute("UPDATE cooperation_members SET application_event_id=?,applied_world_time=? WHERE crisis_id=? AND key=?",
                  (event, to_iso(at), cid, key))
        InventoryService.audit(c, actor["world_id"], event, before)
        return event

    @classmethod
    def ready(cls, c, cid):
        if not cls.all_contributed(c, cid) or not cls.fabricated(c, cid):
            return False
        return not c.execute("SELECT 1 FROM cooperation_members m JOIN cooperation_production_blocks b "
            "ON b.crisis_id=m.crisis_id AND b.member_key=m.key WHERE m.crisis_id=? "
            "AND (m.application_event_id IS NULL OR b.recovery_event_id IS NULL)", (cid,)).fetchone()

    @classmethod
    def tick(cls, c, wid, at):
        for crisis in c.execute("SELECT * FROM cooperative_crises WHERE world_id=? AND status='active'", (wid,)).fetchall():
            cid = crisis["id"]
            for m in c.execute("SELECT * FROM cooperation_members WHERE crisis_id=?", (cid,)).fetchall():
                local = c.execute("SELECT * FROM characters WHERE id=?", (m["local_actor_id"],)).fetchone()
                if not m["observation_event_id"] and local and local["health"] > 0 and near_location(c, local, m["capital_id"]):
                    event = cls.event(c, crisis, at, "observed", "当地检验员目击粮场出现灰孢，核对本地留存菌株。",
                        local["id"], causes=(crisis["source_event_id"],), member_key=m["key"])
                    c.execute("UPDATE cooperation_members SET observation_event_id=? WHERE crisis_id=? AND key=?", (event, cid, m["key"]))
                if not m["contribution_event_id"] and m["consent_event_id"]:
                    transfer = c.execute("SELECT e.id FROM npc_agency_steps s JOIN npc_agency_goals g ON g.id=s.goal_id "
                        "JOIN world_events e ON e.id=s.event_id WHERE g.character_id=? AND g.kind='cooperation_contribute' "
                        "AND g.world_id=? AND json_extract(g.predicate_json,'$.crisis_id')=? "
                        "AND g.source_event_id=? AND s.world_time>=? "
                        "AND s.action_type='transfer' AND s.status='accepted' AND e.target_id=? "
                        "AND json_extract(e.payload_json,'$.metadata.item_type_id')=? "
                        "AND json_extract(e.payload_json,'$.metadata.quantity')=1 LIMIT 1",
                        (m["delegate_id"], wid, cid, m["consent_event_id"], crisis["started_world_time"],
                         crisis["coordinator_id"], m["sample_type_id"])).fetchone()
                    if transfer:
                        c.execute("UPDATE cooperation_members SET contribution_event_id=? WHERE crisis_id=? AND key=?", (transfer[0], cid, m["key"]))
                if m["applied_world_time"] and at >= from_iso(m["applied_world_time"]) + timedelta(hours=6):
                    block = c.execute("SELECT * FROM cooperation_production_blocks WHERE crisis_id=? AND member_key=?", (cid, m["key"])).fetchone()
                    if not block["recovery_event_id"]:
                        event = cls.event(c, crisis, at, "resource_recovered", "当地观察期通过，灰孢受控，粮场重新生产。",
                            local["id"], causes=(m["application_event_id"],), member_key=m["key"])
                        c.execute("UPDATE cooperation_production_blocks SET resolved_world_time=?,recovery_event_id=? WHERE crisis_id=? AND member_key=?",
                            (to_iso(at), event, cid, m["key"]))
            for p in c.execute("SELECT * FROM cooperation_packets WHERE crisis_id=? AND delivery_event_id IS NULL AND arrival_world_time<=?",
                               (cid, to_iso(at))).fetchall():
                recipient = c.execute("SELECT * FROM characters WHERE id=?", (p["recipient_id"],)).fetchone()
                if not recipient or recipient["health"] <= 0 or recipient["current_room_id"] or not near_location(c, recipient, p["destination_id"]):
                    continue
                before = InventoryService.snapshot(c, wid)
                if p["kind"] == "remedy":
                    cargo = c.execute("SELECT i.*,t.stack_limit,t.slot_size FROM item_instances i JOIN item_types t ON t.id=i.item_type_id "
                        "WHERE i.id=? AND i.container_id=? AND i.container_type='cooperation_transit' AND i.quantity=1",
                        (p["item_id"], p["id"])).fetchone()
                    if not cargo:
                        continue
                    try:
                        InventoryService.transfer(c, cargo, recipient["id"], preserve_identity=True)
                    except ValueError:
                        continue
                event = cls.event(c, crisis, at, "packet_received", "指定收件人在约定地点收到实际完成驿运的报告或实物。",
                    recipient["id"], causes=(p["dispatch_event_id"],), member_key=p["member_key"], packet_kind=p["kind"], sender_id=p["sender_id"])
                c.execute("UPDATE cooperation_packets SET delivery_event_id=?,delivered_world_time=? WHERE id=?", (event, to_iso(at), p["id"]))
                c.execute("INSERT OR IGNORE INTO character_event_knowledge VALUES (?,?,?,'heard',?,0.9,1,?)",
                    (recipient["id"], p["source_event_id"], wid, p["sender_id"], to_iso(at)))
                if p["kind"] == "report":
                    c.execute("UPDATE cooperation_members SET report_delivery_event_id=? WHERE crisis_id=? AND key=?", (event, cid, p["member_key"]))
                InventoryService.audit(c, wid, event, before)
            manufactured = cls.fabricated(c, cid)
            if manufactured and not crisis["cure_origin_item_id"]:
                original = c.execute("SELECT item_instance_id FROM inventory_changes WHERE source_event_id=? AND before_json='null' "
                    "AND json_extract(after_json,'$.item_type_id')=? AND json_extract(after_json,'$.owner_character_id')=? "
                    "AND json_extract(after_json,'$.quantity')=?", (manufactured, crisis["cure_type_id"], crisis["coordinator_id"],
                    len(json.loads(crisis["catalogue_json"])))).fetchone()
                if original:
                    c.execute("UPDATE cooperative_crises SET cure_origin_item_id=? WHERE id=?", (original[0], cid))
            if cls.ready(c, cid):
                members = c.execute("SELECT application_event_id FROM cooperation_members WHERE crisis_id=?", (cid,)).fetchall()
                event = cls.event(c, crisis, at, "resolved", "全部政体共同贡献、制作和施用后，全球灰孢危机解除。",
                    causes=[m[0] for m in members])
                c.execute("UPDATE cooperative_crises SET status='resolved',resolved_world_time=?,resolution_event_id=? WHERE id=?",
                          (to_iso(at), event, cid))

    @classmethod
    def view(cls, c, cid):
        crisis, _ = cls._rows(c, cid)
        return {**dict(crisis), "members": [dict(m) for m in c.execute("SELECT * FROM cooperation_members WHERE crisis_id=? ORDER BY key", (cid,))]}
