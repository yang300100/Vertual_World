"""Yuki 用独立存档演示全球危机；只推进正常心跳，不连接模型或读取 .env。"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from world_engine.activity_tasks import ActivityRecipeSpec, TaskService
from world_engine.clock import WorldClockService
from world_engine.config import Settings
from world_engine.database import Database
from world_engine.economy import CommoditySpec, EconomyService
from world_engine.engine import WorldEngine
from world_engine.epistemics import KnowledgeService
from world_engine.event_log import record_event
from world_engine.incidents import IncidentService
from world_engine.repository import WorldRepository, to_iso

PROJECT = Path(__file__).resolve().parents[1]


def _registration(c, wid, at, kind, name):
    event = record_event(c, world_id=wid, tick_id=str(uuid4()), occurred_at=at,
                         event_type="world.test_fixture_initialized", summary=f"验收初始内容：{name}")
    rid = str(uuid4())
    c.execute("INSERT INTO element_registration_requests(id,world_id,element_type,source_event_id,"
              "idempotency_key,status,payload_json,input_world_version,created_at,updated_at) "
              "VALUES (?,?,?,?,?,'applied','{}',0,?,?)",
              (rid, wid, kind, event, rid, to_iso(at), to_iso(at)))
    return rid


def build_scenario(database):
    """仅布置初始地理、职业、技能和库存，Yuki 不预写任何救灾目标。"""
    at = datetime(2040, 4, 1, 8, tzinfo=UTC)
    locations = json.loads((PROJECT / "docs/world/locations.json").read_text(encoding="utf-8"))
    cities = [p for p in locations if p["kind"] == "city"]
    selected = [max(cities, key=lambda p: p["latitude"]),
                min(cities, key=lambda p: p["longitude"]),
                min(cities, key=lambda p: p["latitude"])]
    with database.write() as c:
        wid = WorldRepository().create_world(c, name="伊瑟拉·全球灰幕离线验收",
            minutes_per_tick=60, seed_demo=False, seed_maps=False, start_time=at)
        food = EconomyService.register_item(c, wid,
            _registration(c, wid, at, "commodity", "应急口粮"),
            CommoditySpec(name="应急口粮", category="food", price=3, nutrition=60,
                          stack_limit=100), at)
        material = EconomyService.register_item(c, wid,
            _registration(c, wid, at, "commodity", "防灰滤布"),
            CommoditySpec(name="防灰滤布", category="material", price=4, stack_limit=100), at)
        filter_type = EconomyService.register_item(c, wid,
            _registration(c, wid, at, "commodity", "粮场防灰滤网"),
            CommoditySpec(name="粮场防灰滤网", category="tool", price=20), at)
        sites = []
        for index, city in enumerate(selected):
            place, camp = str(uuid4()), str(uuid4())
            for loc, name, lon in ((place, city["name"], city["longitude"]),
                                   (camp, city["name"] + "运料营地", city["longitude"] + .004)):
                c.execute("INSERT INTO locations(id,world_id,name,kind,longitude,latitude) "
                          "VALUES (?,?,?,'city',?,?)", (loc, wid, name, lon, city["latitude"]))
            engineer, hauler = str(uuid4()), str(uuid4())
            for actor, name, identity, loc, lon, skills in (
                (engineer, f"{city['name']}维修匠", "粮场维修匠", place, city["longitude"], ["修理"]),
                (hauler, f"{city['name']}运料人", "运料人", camp, city["longitude"] + .004, []),
            ):
                c.execute("INSERT INTO characters(id,world_id,name,identity,location_id,current_location_id,"
                          "longitude,latitude,energy,satiety,money,health,skills_json,created_at,updated_at) "
                          "VALUES (?,?,?,?,?,?,?,?,90,100,200,100,?,?,?)",
                          (actor, wid, name, identity, loc, loc, lon, city["latitude"],
                           json.dumps(skills), to_iso(at), to_iso(at)))
                c.execute("UPDATE characters SET inventory_capacity=6 WHERE id=?", (actor,))
                c.execute("INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,"
                          "owner_character_id,quantity,condition) VALUES (?,?,?,'character_inventory',?,?,20,100)",
                          (str(uuid4()), wid, food, actor, actor))
            c.execute("INSERT INTO character_skill_proficiencies(character_id,world_id,skill_name,proficiency,"
                      "updated_at) VALUES (?,?,'修理',60,?)", (engineer, wid, to_iso(at)))
            address = record_event(c, world_id=wid, tick_id=str(uuid4()), occurred_at=at,
                event_type="world.test_address_known", actor_id=engineer,
                summary="工坊运料名单记录同事所在营地")
            KnowledgeService.add_evidence(c, observer_id=engineer, subject_id=hauler,
                predicate="location", value=camp, event_id=address, method="document",
                quote="同事在运料营地")
            c.execute("INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,"
                      "owner_character_id,quantity,condition) VALUES (?,?,?,'character_inventory',?,?,1,100)",
                      (str(uuid4()), wid, material, hauler, hauler))
            item = str(uuid4())
            c.execute("INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,"
                      "owner_character_id,quantity,condition) VALUES (?,?,?,'character_inventory',?,?,1,100)",
                      (item, wid, filter_type, engineer, engineer))
            recipe = TaskService.register(c, wid, _registration(c, wid, at, "activity_recipe", "滤网修复"),
                ActivityRecipeSpec(name=f"{city['name']}滤网修复", kind="repair", location_id=place,
                    duration_minutes=120 + index * 60, energy_cost=10,
                    ingredients=[{"item_type_id": material, "quantity": 1}],
                    repair_category="tool", repair_points=50, check_difficulty=0,
                    check_min_proficiency=10))
            EconomyService.register_item(c, wid,
                _registration(c, wid, at, "commodity", "露天粮场"),
                CommoditySpec(name=f"{city['name']}粮场收获", category="food", price=3,
                    nutrition=30, stack_limit=100, resource_location_id=place,
                    resource_key="field_food", initial_resource=0, daily_growth=20,
                    resource_capacity=100), at)
            sites.append({"location_id": place, "custodian_id": engineer, "item_id": item,
                          "recipe_id": recipe, "resource_keys": ["field_food"],
                          "damaged_condition": 40, "required_condition": 80})
        incident = IncidentService.create(c, wid, "全球灰幕·粮场防护危机",
            "大规模火山灰进入高空环流，远隔各地的露天粮场防护滤网堵塞受损。"
            "现有口粮仍可使用；恢复各地防护与生产是解除粮食危机的核验条件。",
            sites, at, idempotency_key="global-gray-veil")
    return wid, incident, at


def simulate(database, settings, wid, at, *, hours=96):
    clock = WorldClockService(database, settings)
    engine = WorldEngine(database, settings)
    progress = []
    for hour in range(1, hours + 1):
        heartbeat = clock.heartbeat(wid, elapsed_seconds=3600)
        if heartbeat.adjudication_due:
            engine.adjudicate(wid, trigger="scheduled_12h")
        with database.read() as c:
            incident = IncidentService.view(c, wid)[0]
            count = incident["restored_sites"]
            if not progress or count != progress[-1]["restored_sites"]:
                progress.append({"hour": hour, "restored_sites": count, "status": incident["status"]})
            if incident["status"] == "resolved":
                break
    if incident["status"] == "resolved":
        for _ in range(24):
            heartbeat = clock.heartbeat(wid, elapsed_seconds=3600)
            if heartbeat.adjudication_due:
                engine.adjudicate(wid, trigger="scheduled_12h")
    with database.read() as c:
        sites = [{**s, "name": c.execute("SELECT name FROM locations WHERE id=?", (s["location_id"],)).fetchone()[0]}
                 for s in incident["sites"]]
        steps = [dict(row) for row in c.execute(
            "SELECT p.name,g.kind,g.motive,g.source_event_id,s.world_time,s.action_type,s.status,"
            "s.reason,s.event_id FROM npc_agency_steps s JOIN npc_agency_goals g ON g.id=s.goal_id "
            "JOIN characters p ON p.id=g.character_id WHERE g.world_id=? "
            "AND g.kind LIKE 'incident_%' ORDER BY s.world_time,p.name", (wid,))]
        transmissions = [dict(row) for row in c.execute(
            "SELECT e.id,e.occurred_at,e.event_type,e.actor_id,e.target_id,e.payload_json "
            "FROM world_events e WHERE e.world_id=? AND e.event_type IN "
            "('social.news_shared','social.public_rumor','action.socialize') "
            "AND COALESCE(json_extract(e.payload_json,'$.metadata.share_event_id'),"
            "json_extract(e.payload_json,'$.source_event_id')) IN "
            "(SELECT request_event_id FROM incident_sites WHERE incident_id=?) ORDER BY e.occurred_at",
            (wid, incident["id"]))]
        production = [{"location": s["name"], "productive_days": IncidentService.productive_days(
            c, wid, s["location_id"], "field_food", at + timedelta(hours=hour), 1)} for s in sites]
        regeneration = [dict(row) for row in c.execute(
            "SELECT occurred_at,location_id,summary,payload_json FROM world_events WHERE world_id=? "
            "AND event_type='world.resource_regenerated' "
            "AND json_extract(payload_json,'$.resource')='field_food' ORDER BY occurred_at", (wid,))]
        causes = [dict(row) for row in c.execute(
            "SELECT x.event_id,x.cause_event_id,x.relation FROM event_causes x "
            "JOIN world_events e ON e.id=x.event_id WHERE e.world_id=? "
            "AND (e.event_type LIKE '%incident%' OR x.event_id IN "
            "(SELECT s.event_id FROM npc_agency_steps s JOIN npc_agency_goals g ON g.id=s.goal_id "
            "WHERE g.world_id=? AND g.kind LIKE 'incident_%'))", (wid, wid))]
    return {"world_id": wid, "incident_id": incident["id"], "status": incident["status"],
            "source_event_id": incident["source_event_id"], "resolution_event_id": incident["resolution_event_id"],
            "model_api_calls": 0, "world_hours": hour, "sites": sites,
            "progress": progress, "steps": steps, "transmissions": transmissions,
            "production_after_resolution": production, "regeneration_events": regeneration,
            "causes": causes}


def write_summary(output, report):
    """Yuki 把数据库证据写成哥哥能直接阅读的验收记录。"""
    lines = ["# 全球灰幕自主应对验收", "",
             f"结果：{report['status']}；解决耗时：{report['world_hours']} 个世界小时；模型 API 调用：0 次。", "",
             "火山灰通过高空环流影响露天粮场。新增产量受阻，现有口粮仍可使用。"
             "人物从当地目击获知迹象，维修者按自己知道的地址走访同事并交谈求助；维修者依据技能与原件形成目标，"
             "运料人听闻求助后判断自己能否贡献物资。送料、行旅、维修检定和领取均由现有领域服务结算。", "",
             "这是以当前世界包城市为地理依据的独立缩小存档：三个远隔地区的全部登记粮场均受影响，"
             "六位验收 NPC 的职业、技能和初始库存在事件前布置。未向日常主存档注入灾害。", "",
             "| 世界小时 | 已恢复地点 | 全球事件状态 |", "| --- | --- | --- |"]
    lines += [f"| {p['hour']} | {p['restored_sites']}/3 | {p['status']} |" for p in report["progress"]]
    lines += ["", "| 地点 | 目击证据 | 物资求助 | 恢复证据 |", "| --- | --- | --- | --- |"]
    lines += [f"| {s['name']} | {s['observation_event_id']} | {s['request_event_id']} | {s['resolution_event_id']} |"
              for s in report["sites"]]
    lines += ["", "实际行动：", "", "| 人物 | 世界时间 | 行动 | 结算状态 |", "| --- | --- | --- | --- |"]
    lines += [f"| {s['name']} | {s['world_time']} | {s['action_type']} | {s['status']} |" for s in report["steps"]]
    lines += ["", f"恢复后继续推进 24 个世界小时，记录到 {len(report['regeneration_events'])} 条真实粮场再生事件。", "",
              "完成条件：全部登记地点的真实维修结束、原件领取与耐久阈值均满足。只改物品耐久、"
              "单处修复、未来事件注入或重复心跳都不能伪造全球解决。灾害期间损失的产量不会补发。", "",
              "边界：当前验证的是规则规划驱动的物资与维修协作，未验证付费模型的开放式推理，"
              "也不代表任意种类的全球事件都能自行解决。", "",
              "完整证据：[report.json](report.json)；独立存档：[world.db](world.db)。", "",
              "复现命令：`.venv/Scripts/python.exe -m scripts.demo_global_incident`。"]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=PROJECT / "run/global-incident-demo")
    args = parser.parse_args()
    output = args.output_dir.resolve() / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    output.mkdir(parents=True, exist_ok=False)
    database = Database(output / "world.db", legacy_world_defaults=False)
    database.initialize()
    settings = Settings(database_path=database.path, decision_provider="rules", world_agent_enabled=False)
    wid, _, at = build_scenario(database)
    report = simulate(database, settings, wid, at)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_summary(output, report)
    print(json.dumps({"status": report["status"], "world_hours": report["world_hours"],
                      "restored_sites": sum(bool(s["resolution_event_id"]) for s in report["sites"]),
                      "model_api_calls": 0, "report": str(output / "report.json")}, ensure_ascii=True))
    raise SystemExit(0 if report["status"] == "resolved" else 1)


if __name__ == "__main__":
    main()
