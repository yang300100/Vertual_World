"""Yuki 在独立存档模拟所有政体的灰孢协作，禁止模型网络与 .env 读取。"""

import argparse
import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from scripts.demo_global_incident import PROJECT, _registration
from world_engine.activity_tasks import ActivityRecipeSpec, TaskService
from world_engine.clock import WorldClockService
from world_engine.config import Settings
from world_engine.cooperation import CooperationService as C
from world_engine.database import Database
from world_engine.economy import CommoditySpec, EconomyService
from world_engine.engine import WorldEngine
from world_engine.polities import current_polities
from world_engine.repository import WorldRepository, from_iso, to_iso


def build_scenario(db, *, withhold=None):
    at = datetime(2040, 4, 1, 8, tzinfo=UTC)
    catalogue = current_polities()
    places = {p["key"]: p for p in json.loads((PROJECT / "docs/world/locations.json").read_text(encoding="utf-8"))}
    with (PROJECT / "docs/worldbuilding/maps/map_new/data/Noryia Burgs 2026-08-29-11-38.csv").open(encoding="utf-8-sig", newline="") as handle:
        neutral = next(p for p in csv.DictReader(handle) if p["State"] == "Neutrals")
    hub_place = places[f"burg-{neutral['Id']}"]
    with db.write() as c:
        wid = WorldRepository().create_world(c, name="伊瑟拉·全部政体灰孢协作验收", minutes_per_tick=60,
            seed_demo=False, seed_maps=False, start_time=at)

        def item(name, category, **kwargs):
            return EconomyService.register_item(c, wid, _registration(c, wid, at, "commodity", name),
                CommoditySpec(name=name, category=category, price=1, stack_limit=100, **kwargs), at)

        food = item("会期与驿站储备口粮", "food", nutrition=60)
        cure = item("全菌株灰孢抑制剂", "material")
        hub = str(uuid4())
        c.execute("INSERT INTO locations(id,world_id,name,kind,longitude,latitude) VALUES (?,?,?,'place',?,?)",
            (hub, wid, hub_place["name"] + "·中立贸易会场与共同工坊", hub_place["longitude"], hub_place["latitude"]))

        def npc(name, identity, place, lon, lat):
            actor = str(uuid4())
            c.execute("INSERT INTO characters(id,world_id,name,identity,location_id,current_location_id,longitude,latitude,"
                "energy,satiety,money,health,skills_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,100,100,200,100,'[]',?,?)",
                (actor, wid, name, identity, place, place, lon, lat, to_iso(at), to_iso(at)))
            c.execute("UPDATE characters SET inventory_capacity=60 WHERE id=?", (actor,))
            c.execute("INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,owner_character_id,quantity,condition) "
                "VALUES (?,?,?,'character_inventory',?,?,200,100)", (str(uuid4()), wid, food, actor, actor))
            return actor

        coordinator = npc("中立工坊协调者·沐禾", "植物病害工坊协调者", hub, hub_place["longitude"], hub_place["latitude"])
        members = []
        for index, p in enumerate(catalogue):
            capital = str(uuid4())
            c.execute("INSERT INTO locations(id,world_id,name,kind,longitude,latitude) VALUES (?,?,?,'city',?,?)",
                      (capital, wid, p["capital_name"], p["longitude"], p["latitude"]))
            local = npc(p["name"] + "检验员", "本地粮场检验员", capital, p["longitude"], p["latitude"])
            delegate = npc(p["name"] + "使节", "经本国授权的救灾使节", hub, hub_place["longitude"], hub_place["latitude"])
            sample = item(p["name"] + "留存菌株样本", "material")
            c.execute("INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,owner_character_id,quantity,condition) "
                "VALUES (?,?,?,'character_inventory',?,?,1,100)", (str(uuid4()), wid, sample, delegate, delegate))
            item(p["name"] + "粮场收获", "food", nutrition=30, resource_location_id=capital,
                 resource_key="field_food", initial_resource=0, daily_growth=20, resource_capacity=500)
            members.append({**p, "capital_id": capital, "delegate_id": delegate, "local_actor_id": local,
                "sample_type_id": sample, "policy": {"equal_allocation": True, "share_method": index % 2 == 0,
                    "respect_autonomy": True, "participate": p["key"] != withhold}})
        recipes, batches = [], []
        for batch in range(3):
            output = item(f"联合样本第{batch + 1}批检验液", "material")
            batches.append(output)
            recipes.append(TaskService.register(c, wid, _registration(c, wid, at, "activity_recipe", "菌株检验"),
                ActivityRecipeSpec(name=f"第{batch + 1}批七政体样本处理", kind="craft", location_id=hub,
                    duration_minutes=120, energy_cost=5, output_item_type_id=output,
                    ingredients=[{"item_type_id": m["sample_type_id"], "quantity": 1} for m in members[batch * 7:(batch + 1) * 7]])))
        recipes.append(TaskService.register(c, wid, _registration(c, wid, at, "activity_recipe", "联合抑制剂"),
            ActivityRecipeSpec(name="全部政体全菌株抑制剂合制", kind="craft", location_id=hub,
                duration_minutes=240, energy_cost=5, output_item_type_id=cure, output_quantity=len(members),
                ingredients=[{"item_type_id": iid, "quantity": 1} for iid in batches])))
        cid = C.create(c, wid, coordinator, hub, members, recipes, cure, at)
    return wid, cid, at


def simulate(db, settings, wid, cid, at, *, hours=2400):
    if hours < 2:
        raise ValueError("离线模拟至少需要两个世界小时")
    # 哥哥，细心跳让生活目标有机会完成，跨境报告与包裹仍按真实距离等待。
    clock, engine = WorldClockService(db, settings), WorldEngine(db, settings)
    with db.read() as c:
        time_scale = c.execute("SELECT time_scale FROM world_clock WHERE world_id=?", (wid,)).fetchone()[0]
    if time_scale <= 0:
        raise ValueError("请先启动验收世界的时钟")
    progress = []
    for hour in range(2, hours + 1, 2):
        heartbeat = clock.heartbeat(wid, elapsed_seconds=2 * 3600 / time_scale)
        if heartbeat.adjudication_due:
            engine.adjudicate(wid, trigger="scheduled_12h")
        with db.read() as c:
            view = C.view(c, cid)
        counts = {"reports": sum(bool(m["report_delivery_event_id"]) for m in view["members"]),
                  "consents": sum(bool(m["consent_event_id"]) for m in view["members"]),
                  "contributions": sum(bool(m["contribution_event_id"]) for m in view["members"]),
                  "applications": sum(bool(m["application_event_id"]) for m in view["members"]), "status": view["status"]}
        if not progress or any(counts[k] != progress[-1][k] for k in counts):
            progress.append({"hour": hour, **counts})
        if view["status"] == "resolved":
            break
    if view["status"] == "resolved":
        for _ in range(12):
            heartbeat = clock.heartbeat(wid, elapsed_seconds=2 * 3600 / time_scale)
            if heartbeat.adjudication_due:
                engine.adjudicate(wid, trigger="scheduled_12h")
    with db.read() as c:
        view = C.view(c, cid)
        activities = [dict(a) for a in c.execute("SELECT a.id,a.status,a.started_world_time,a.finished_world_time,a.finish_event_id,"
            "d.spec_json,d.result_json FROM character_life_activities a JOIN activity_task_details d ON d.activity_id=a.id "
            "WHERE a.world_id=? AND a.kind='craft' AND a.character_id=? ORDER BY a.started_world_time", (wid, view["coordinator_id"]))]
        steps = [dict(s) for s in c.execute("SELECT p.name,g.kind,g.motive,g.source_event_id,s.* FROM npc_agency_steps s "
            "JOIN npc_agency_goals g ON g.id=s.goal_id JOIN characters p ON p.id=g.character_id "
            "WHERE g.world_id=? AND g.kind LIKE 'cooperation_%' ORDER BY s.world_time", (wid,))]
        events = [dict(e) for e in c.execute("SELECT id,occurred_at,event_type,actor_id,target_id,summary,payload_json "
            "FROM world_events WHERE world_id=? AND event_type LIKE 'cooperation.%' ORDER BY occurred_at", (wid,))]
        recoveries = [dict(b) for b in c.execute("SELECT * FROM cooperation_production_blocks WHERE crisis_id=? AND recovery_event_id IS NOT NULL", (cid,))]
        regeneration = [dict(r) for r in c.execute("SELECT location_id,occurred_at,summary,payload_json FROM world_events WHERE world_id=? "
            "AND event_type='world.resource_regenerated' AND json_extract(payload_json,'$.resource')='field_food'", (wid,))]
        packets = [dict(p) for p in c.execute("SELECT * FROM cooperation_packets WHERE crisis_id=? ORDER BY dispatched_world_time", (cid,))]
        diagnostic = {"pending_members": [m["key"] for m in view["members"] if not m["application_event_id"]],
            "revision": view["revision"], "cure_origin": view["cure_origin_item_id"], "craft_count": len(activities),
            "blocked": [dict(g) for g in c.execute("SELECT kind,status,next_step_json FROM npc_agency_goals WHERE world_id=? "
                "AND kind LIKE 'cooperation_%' AND status IN ('active','blocked') LIMIT 15", (wid,))],
            "recent_steps": steps[-15:]}
    with db.read() as c:
        end = view["resolved_world_time"] or c.execute("SELECT w.current_time FROM worlds w WHERE id=?", (wid,)).fetchone()[0]
    return {"world_id": wid, "crisis_id": cid, "status": view["status"], "world_hours": (from_iso(end) - at).total_seconds() / 3600,
            "model_api_calls": 0, "members": view["members"], "proposal_revisions": view["revision"],
            "policy_rejections": sum(e["event_type"] == "cooperation.policy_rejected" for e in events),
            "craft_activities": activities, "resource_recoveries": recoveries, "regeneration_events": regeneration,
            "progress": progress, "steps": steps, "events": events, "packets": packets, "diagnostic": diagnostic}


def write_summary(output, report, *, database_name="world.db"):
    times = {e["id"]: e["occurred_at"][:16].replace("T", " ") for e in report["events"]}
    times.update({s["event_id"]: s["world_time"][:16].replace("T", " ") for s in report["steps"] if s["event_id"]})

    def evidence(event_id):
        return times.get(event_id, "未完成")

    lines = ["# 全部政体自主应对灰孢潮", "", f"结果：{report['status']}；世界耗时：{report['world_hours']} 小时；模型 API：0 次。", "",
        "覆盖诺里亚全部 20 个正式政体与保留的阿德伦地方政权，共 21 个；中立地带仅作会场。", "",
        "43 名验收 NPC。使节因事件前贸易会议在场并携带留存样本，当地检验员仍在真实首都。初始事实不包含救灾目标或签署。"
        "正常心跳推动目击、寄报、实际交流、按本国授权拒绝/同意、样本交付、三批加工与最终合制、逐国寄送和当地施用。", "",
        f"方案版本：{report['proposal_revisions']}；政策拒绝：{report['policy_rejections']} 次；真实制作活动：{len(report['craft_activities'])} 次。", "",
        "下表为对应事实发生的世界 UTC 时间；完整事件编号、动机与库存证据见 JSON。", "",
        "| 政体 | 报告送达 | 当前同意 | 实际样本交付 | 当地施用 |", "| --- | --- | --- | --- | --- |"]
    lines += [f"| {m['name']} | {evidence(m['report_delivery_event_id'])} | {evidence(m['consent_event_id'])} | "
              f"{evidence(m['contribution_event_id'])} | {evidence(m['application_event_id'])} |" for m in report["members"]]
    lines += ["", "| 世界小时 | 报告 | 当前同意 | 样本交付 | 施用 | 状态 |", "| --- | --- | --- | --- | --- | --- |"]
    lines += [f"| {p['hour']} | {p['reports']}/21 | {p['consents']}/21 | {p['contributions']}/21 | {p['applications']}/21 | {p['status']} |" for p in report["progress"]]
    lines += ["", "驿运按首都到会场的测地距离、20 km/h 的传统驿运平均速度与两小时装卸计算；包裹到期并在指定地址见到收件人才实际交付。"
              "驿站中转人员与地形绕行尚未逐人逐段模拟。收到实物不等于已经施用，施用后再观察六小时。", "",
              f"恢复地点 {len(report['resource_recoveries'])}/21；真实粮场再生记录 {len(report['regeneration_events'])} 条。", "",
              "本次是规则规划的独立验收世界；不是主存档，也不代表付费模型开放式外交推理。", "",
              f"完整证据：[report.json](report.json)；独立存档：[{database_name}]({database_name})。", "",
              "复现：`.venv/Scripts/python.exe -m scripts.demo_global_cooperation`；缺席反事实增加 `--withhold state-13`。"]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--withhold", choices=[p["key"] for p in current_polities()])
    parser.add_argument("--hours", type=int, default=2400)
    parser.add_argument("--output-dir", type=Path, default=PROJECT / "run/global-cooperation-demo")
    args = parser.parse_args()
    output = args.output_dir.resolve() / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    output.mkdir(parents=True, exist_ok=False)
    db = Database(output / "world.db", legacy_world_defaults=False)
    db.initialize()
    settings = Settings(database_path=db.path, decision_provider="rules", world_agent_enabled=False, active_character_limit=100)
    wid, cid, at = build_scenario(db, withhold=args.withhold)
    report = simulate(db, settings, wid, cid, at, hours=args.hours)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_summary(output, report)
    print(json.dumps({"status": report["status"], "world_hours": report["world_hours"], "members": len(report["members"]),
                      "model_api_calls": 0, "report": str(output / "report.json")}, ensure_ascii=True))
    raise SystemExit(0 if report["status"] == "resolved" or args.withhold else 1)


if __name__ == "__main__":
    main()
