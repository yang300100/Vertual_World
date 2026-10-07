"""Yuki 把合作判断与驿运接回 NPC 的同一个持久目标和有限步规划器。"""

import json

from world_engine.cooperation import CooperationService as C
from world_engine.domain import ActionType
from world_engine.npc_planning import Condition, DomainOperation, Effect, PlanningOperator
from world_engine.repository import to_iso


def seed(c, wid, actor, at):
    from world_engine.npc_agency import NpcAgencyService as Agency

    def adopt(crisis, kind, predicate, motive, source, priority=2):
        Agency.adopt(c, wid, actor["id"], kind, {"crisis_id": crisis["id"], **predicate}, motive, at, source, priority)

    def operation(crisis, op, source, key=None, **extra):
        predicate = {"kind": "cooperation_operation", "operation": op, "location_id": crisis["hub_id"], **extra}
        if key:
            predicate["member_key"] = key
        if op in {"report", "apply"}:
            member = C._rows(c, crisis["id"], key)[1]
            predicate["location_id"] = member["capital_id"]
        adopt(crisis, f"cooperation_{'decision' if op == 'ratify' else op}", predicate,
            {"report": "目击本地灰孢，想寄出报告请本国使节商量", "propose": "听到具体灾情，想提出或修订共同救灾条件",
             "ratify": "知道本国风险，想按自己的授权判断是否同意当前条款", "remedy": "联合工坊已有实际成果，想寄回受灾地区",
             "apply": "已收到实际抑制剂，想施用以恢复本地粮场"}[op], source)

    for crisis in c.execute("SELECT * FROM cooperative_crises WHERE world_id=? AND status='active'", (wid,)).fetchall():
        members = c.execute("SELECT * FROM cooperation_members WHERE crisis_id=? ORDER BY key", (crisis["id"],)).fetchall()
        for m in members:
            if actor["id"] == m["local_actor_id"]:
                if C.knows(c, actor["id"], m["observation_event_id"], at) and not c.execute(
                    "SELECT 1 FROM cooperation_packets WHERE crisis_id=? AND member_key=? AND kind='report'", (crisis["id"], m["key"])).fetchone():
                    operation(crisis, "report", m["observation_event_id"], m["key"])
                packet = c.execute("SELECT delivery_event_id FROM cooperation_packets WHERE crisis_id=? AND member_key=? "
                    "AND kind='remedy'", (crisis["id"], m["key"])).fetchone()
                if packet and packet[0] and not m["application_event_id"]:
                    operation(crisis, "apply", packet[0], m["key"])
            if actor["id"] == m["delegate_id"] and m["report_delivery_event_id"]:
                if not C.knows(c, crisis["coordinator_id"], m["observation_event_id"], at):
                    adopt(crisis, "cooperation_brief", {"kind": "spoke_about_event", "event_id": m["observation_event_id"],
                        "target_id": crisis["coordinator_id"], "location_id": crisis["hub_id"],
                        "dialogue": "本国粮场出现灰孢，报告已由驿路送达。我们需要共同研究。"},
                        "本国报告已送达，想当面与其他地区讨论救灾", m["observation_event_id"])
                if C.knows(c, actor["id"], crisis["proposal_event_id"], at) and m["consent_revision"] != crisis["revision"]:
                    operation(crisis, "ratify", crisis["proposal_event_id"], m["key"], revision=crisis["revision"])
                if m["consent_event_id"] and not m["contribution_event_id"]:
                    adopt(crisis, "cooperation_contribute", {"kind": "delivered_item", "item_type_id": m["sample_type_id"],
                        "amount": 1, "target_id": crisis["coordinator_id"], "location_id": crisis["hub_id"]},
                        "认可合作条件，想把本国留存样本交给共同工坊", m["consent_event_id"])
        if actor["id"] != crisis["coordinator_id"]:
            continue
        reports = [m["observation_event_id"] for m in members if C.knows(c, actor["id"], m["observation_event_id"], at)]
        feedback = c.execute("SELECT e.id,e.payload_json FROM world_events e JOIN character_event_knowledge k ON k.event_id=e.id "
            "WHERE k.character_id=? AND k.learned_world_time<=? AND e.event_type='cooperation.policy_rejected' "
            "AND json_extract(e.payload_json,'$.crisis_id')=? ORDER BY e.occurred_at DESC",
            (actor["id"], to_iso(at), crisis["id"])).fetchall()
        terms = json.loads(crisis["terms_json"])
        amended = dict(terms)
        for f in feedback:
            amended.update({k: v for k, v in json.loads(f["payload_json"])["requirements"].items() if k in terms})
        if reports and (not crisis["revision"] or amended != terms):
            operation(crisis, "propose", feedback[0]["id"] if feedback else reports[0], revision=crisis["revision"] + 1)
        if crisis["proposal_event_id"]:
            for m in members:
                if not m["report_delivery_event_id"] or not C.knows(c, actor["id"], m["observation_event_id"], at):
                    continue
                if not C.knows(c, m["delegate_id"], crisis["proposal_event_id"], at):
                    adopt(crisis, "cooperation_proposal_share", {"kind": "spoke_about_event", "event_id": crisis["proposal_event_id"],
                        "target_id": m["delegate_id"], "location_id": crisis["hub_id"], "dialogue": "请核对这版共同救灾的技术共享、分配与主权条款。"},
                        "已知当地风险，想向该国代表当面说明当前方案", crisis["proposal_event_id"], priority=3)
        if C.all_contributed(c, crisis["id"]):
            for recipe_id in json.loads(crisis["recipes_json"]):
                if C.recipe_finished(c, crisis, recipe_id):
                    continue
                spec = json.loads(c.execute("SELECT spec_json FROM activity_recipes WHERE id=?", (recipe_id,)).fetchone()[0])
                adopt(crisis, "cooperation_craft", {"kind": "inventory_at_least", "item_type_id": spec["output_item_type_id"],
                    "amount": 1, "recipe_id": recipe_id}, "各国实际样本已凑齐，想在共同工坊按配方制作", crisis["proposal_event_id"], priority=3)
                break
            # 哥哥，先让领取目标完成，再产生寄送目标，免得高优先级寄送反复抢占领取。
            held_cure = c.execute("SELECT 1 FROM item_instances WHERE id=? AND world_id=? "
                "AND container_type='character_inventory' AND container_id=? AND owner_character_id=? AND quantity>0",
                (crisis["cure_origin_item_id"], wid, actor["id"], actor["id"])).fetchone()
            if held_cure and C.fabricated(c, crisis["id"]):
                for m in members:
                    if not c.execute("SELECT 1 FROM cooperation_packets WHERE crisis_id=? AND member_key=? AND kind='remedy'",
                                     (crisis["id"], m["key"])).fetchone():
                        operation(crisis, "remedy", C.fabricated(c, crisis["id"]), m["key"])


def satisfied(c, actor, predicate, at):
    crisis, member = C._rows(c, predicate["crisis_id"], predicate.get("member_key"))
    op = predicate["operation"]
    if op in {"report", "remedy"}:
        return bool(c.execute("SELECT 1 FROM cooperation_packets WHERE crisis_id=? AND member_key=? AND kind=?",
                              (crisis["id"], member["key"], op)).fetchone())
    if op == "apply":
        return bool(member["application_event_id"])
    if op == "propose":
        return crisis["revision"] >= predicate["revision"]
    if op == "ratify":
        return bool((member["consent_event_id"] and member["consent_revision"] == predicate["revision"]) or c.execute(
            "SELECT 1 FROM world_events WHERE event_type='cooperation.policy_rejected' AND actor_id=? "
            "AND json_extract(payload_json,'$.crisis_id')=? AND json_extract(payload_json,'$.revision')=?",
            (actor["id"], crisis["id"], predicate["revision"])).fetchone())
    return False


def extend(c, actor, predicate, motive, at, state, operators):
    from world_engine.npc_planner_domains import _travel

    _travel(c, actor, predicate["location_id"], motive, state, operators)
    state["cooperation_done"] = satisfied(c, actor, predicate, at)
    operators.append(PlanningOperator("cooperation_operation", (Condition("place", predicate["location_id"]),),
        (Effect("cooperation_done", True),), DomainOperation("cooperation_operation", ActionType.USE, predicate), 1))
    return (Condition("cooperation_done", True),)


def execute(c, actor, args, at):
    cid, key, op = args["crisis_id"], args.get("member_key"), args["operation"]
    if op in {"report", "remedy"}:
        return C.dispatch(c, actor, cid, key, op, at)
    if op == "propose":
        return C.propose(c, actor, cid, at)
    if op == "ratify":
        return C.ratify(c, actor, cid, key, at, args["revision"])
    return C.apply(c, actor, cid, key, at)
