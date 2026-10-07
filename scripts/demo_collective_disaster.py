"""哥哥，灰雨场景从灾前日常生活开始，只注入一次有限外部负荷。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from world_engine.collective_regions import RegionService
from world_engine.collective_research import CollectiveResearchService as R
from world_engine.material_physics import MaterialState, fraction
from world_engine.materials import MaterialService as M
from world_engine.materials import encoded
from world_engine.polities import current_polities
from world_engine.repository import WorldRepository, to_iso
from world_engine.spatial import great_circle_distance_km

PROJECT = Path(__file__).resolve().parents[1]


def _item(
    c,
    wid,
    owner,
    name,
    state,
    quantity,
    location,
    *,
    category="material",
    container="ground",
    slot_size=1,
):
    tid, iid = str(uuid4()), str(uuid4())
    c.execute(
        "INSERT INTO item_types(id,name,category,stack_limit,slot_size,usable) VALUES (?,?,?,1000000,?,1)",
        (tid, name, category, slot_size),
    )
    c.execute(
        "INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,owner_character_id,quantity,condition) VALUES (?,?,?,?,?,?,?,100)",
        (iid, wid, tid, container, location, owner, quantity),
    )
    if state is not None:
        M.attach(c, iid, state)
    return iid, tid


def _material(
    c,
    wid,
    owner,
    name,
    carrier,
    mass,
    loc,
    *,
    nutrition=0,
    water=0,
    quantity=None,
    container="ground",
    category="material",
    ph=7,
):
    return _item(
        c,
        wid,
        owner,
        name,
        MaterialState(
            carrier=carrier,
            mass=mass,
            water=water,
            nutrition=nutrition,
            ph=ph,
            viability=1 if carrier == "seed" else 0,
        ),
        quantity or max(1, round(mass + water)),
        loc,
        container=container,
        category=category,
    )[0]


def build_baseline(db):
    """哥哥，这些是普通仓务、工艺和农作岗位，不登记救灾方案或应对领袖。"""
    at = datetime(2040, 4, 1, 8, tzinfo=UTC)
    with db.write() as c:
        c.executescript("""CREATE TABLE IF NOT EXISTS collective_scenario (
            world_id TEXT PRIMARY KEY, reservoir_id TEXT NOT NULL, injected_at TEXT,
            exposure_json TEXT NOT NULL, polity_json TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS collective_scenario_single_injection
            BEFORE UPDATE OF injected_at ON collective_scenario WHEN OLD.injected_at IS NOT NULL
            BEGIN SELECT RAISE(ABORT,'initial disaster already injected'); END;""")
        wid = WorldRepository().create_world(
            c,
            name="伊瑟拉·灰雨粮土危机",
            minutes_per_tick=60,
            seed_demo=False,
            seed_maps=False,
            start_time=at,
        )
        c.execute(
            "INSERT INTO material_modes VALUES (?,?)",
            (wid, encoded({"mode": "collective", "satiety_loss_per_world_hour": 3})),
        )
        people = []
        exposures = []
        for index, polity in enumerate(current_polities()):
            loc = str(uuid4())
            pop = 10
            daily = 200
            c.execute(
                "INSERT INTO locations(id,world_id,name,kind,longitude,latitude) VALUES (?,?,?,'city',?,?)",
                (loc, wid, polity["capital_name"], polity["longitude"], polity["latitude"]),
            )
            local = []
            roles = (
                ("粮仓管事", {"仓务": 80, "农作": 45, "工艺": 40, "检验": 30}),
                ("手工师傅", {"工艺": 85, "仓务": 40, "检验": 75, "农作": 20}),
                ("农艺师", {"农作": 85, "仓务": 45, "工艺": 35, "检验": 65}),
            )
            for role, skills in roles:
                actor = str(uuid4())
                local.append(actor)
                people.append((actor, loc, polity))
                c.execute(
                    "INSERT INTO characters(id,world_id,name,identity,location_id,current_location_id,longitude,latitude,energy,satiety,money,health,skills_json,goals_json,inventory_capacity,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,100,100,200,100,?,?,30,?,?)",
                    (
                        actor,
                        wid,
                        polity["capital_name"] + role,
                        role,
                        loc,
                        loc,
                        polity["longitude"],
                        polity["latitude"],
                        encoded(list(skills)),
                        encoded(["照顾自己与当地人的生活", "履行本人日常职业职责"]),
                        to_iso(at),
                        to_iso(at),
                    ),
                )
                c.execute("INSERT INTO collective_people VALUES (?,?,?,1)", (actor, wid, loc))
                c.execute("INSERT INTO collective_carry_limits VALUES (?,40)", (actor,))
                for skill, value in skills.items():
                    c.execute(
                        "INSERT INTO character_skill_proficiencies(world_id,character_id,skill_name,proficiency,updated_at) VALUES (?,?,?,?,?)",
                        (wid, actor, skill, value, to_iso(at)),
                    )
                private = _material(
                    c,
                    wid,
                    actor,
                    "私人封口日常干粮",
                    "grain",
                    80,
                    loc,
                    nutrition=1600,
                    category="food",
                )
                c.execute(
                    "INSERT INTO collective_food_decisions VALUES (?,?,?,?,?)",
                    (actor, private, "allow", "灾前日常从本人粮柜取饭", to_iso(at)),
                )
                _material(
                    c,
                    wid,
                    actor,
                    "随身日常干粮",
                    "grain",
                    5,
                    actor,
                    nutrition=100,
                    category="food",
                    container="character_inventory",
                )
            manager, maker, farmer = local
            soil = _material(
                c, wid, manager, "代表性农田耕作层", "soil", 4000, loc, ph=6.5 + (index % 3) * 0.25
            )
            source = _material(
                c,
                wid,
                manager,
                "田间有限生长物质储量",
                "growth_reserve",
                7000,
                loc,
                nutrition=140000,
            )
            water = _material(c, wid, manager, "日常露天蓄水池", "water", 0, loc, water=8000)
            rid = RegionService.register(
                c,
                wid,
                loc,
                manager,
                at,
                population=pop,
                daily_nutrition=daily,
                soil_id=soil,
                growth_source_id=source,
                water_id=water,
            )
            # 哥哥，灾前公共岗位许可只说明日常职责，不会转让任何私人物品。
            for actor, operations in (
                (farmer, ["plant", "harvest", "land", "water", "gather"]),
                (maker, ["dispose", "maintain"]),
            ):
                for op in operations:
                    c.execute(
                        "INSERT INTO collective_region_permissions VALUES (?,?,?,?,?,?)",
                        (rid, actor, op, manager, "灾前日常岗位权限", to_iso(at)),
                    )
            clean = _material(
                c,
                wid,
                manager,
                "粮仓封口周转粮",
                "grain",
                210,
                loc,
                nutrition=4200,
                category="food",
            )
            c.execute(
                "UPDATE item_instances SET container_type='region_food',container_id=? WHERE id=?",
                (rid, clean),
            )
            grain = _material(
                c,
                wid,
                manager,
                "粮仓晾晒原粮",
                "grain",
                4000,
                loc,
                nutrition=80000,
                category="food",
            )
            seed = _material(c, wid, farmer, "农户待播种子", "seed", 60, loc, nutrition=1200)
            for carrier, amount in (("clay", 500), ("charcoal", 500), ("limestone", 500)):
                _material(
                    c,
                    wid,
                    maker,
                    "工坊日常"
                    + {"clay": "黏土", "charcoal": "木炭", "limestone": "石灰石"}[carrier],
                    carrier,
                    amount,
                    loc,
                )
            _material(c, wid, maker, "工坊密封日常用水", "water", 0, loc, water=12000)
            _material(c, wid, farmer, "苗圃密封日常用水", "water", 0, loc, water=2000)
            for actor in (manager, farmer):
                # 有限自然来源照常可采集；不能为迫使合作而禁掉其他食物。
                natural = _material(
                    c,
                    wid,
                    actor,
                    "本地有限果蔬来源",
                    "vegetable",
                    250,
                    loc,
                    nutrition=2500,
                    container="natural_resource",
                    category="food",
                )
                if index % 2:
                    exposures.append((natural, 0.004, 0.012))
            fuel, fueltid = _item(
                c, wid, maker, "工坊燃料与灯油", MaterialState(carrier="fuel", mass=200), 200, loc
            )
            testwater, watertid = _item(
                c,
                wid,
                farmer,
                "萌发盘日常供水",
                MaterialState(carrier="water", mass=0, water=100, viability=0),
                100,
                loc,
            )
            liner, linertid = _item(
                c,
                wid,
                maker,
                "工坊仓储陶罐及密封衬材",
                MaterialState(carrier="ceramic", mass=100),
                100,
                loc,
            )
            c.execute(
                "INSERT INTO collective_liners(item_type_id,capacity_per_unit,leak_fraction,maintenance_hours,maintenance_interval_hours,observation_window_hours) VALUES (?,1000,.0000001,1,168,336)",
                (linertid,),
            )
            instruments = (
                (manager, "particles", "筛分称量器", 0),
                (maker, "extractable_salt", "浸提蒸干称量器", 0),
                (farmer, "germination", "苗圃萌发观察盘", 72),
                (farmer, "soil_ph", "土样酸碱比对器", 0),
                (maker, "moisture", "干燥称量器", 0),
            )
            for actor, method, name, incubation in instruments:
                tool, _ = _item(
                    c,
                    wid,
                    actor,
                    name,
                    None,
                    1,
                    actor,
                    category="tool",
                    container="character_inventory",
                )
                supplies = (
                    [{"item_type_id": watertid, "quantity": 1, "role": "hydration"}]
                    if method == "germination"
                    else []
                )
                c.execute(
                    "INSERT INTO collective_instruments(item_id,world_id,method,skill_name,min_proficiency,duration_hours,detection_limit,precision,supplies_json,incubation_hours,sample_capacity) VALUES (?,?,?,'检验',30,1,.0001,.0001,?,?,5)",
                    (tool, wid, method, encoded(supplies), incubation),
                )
            for name, ops, capacity, throughput, supply, stations in (
                ("手筛与沉降槽", ["screen", "filter", "settle"], 1000, 250, [], []),
                (
                    "手工搅拌与浸洗槽",
                    ["wash", "mix", "adsorb", "bind", "store"],
                    15000,
                    2000,
                    [],
                    [
                        {"skill_name": "工艺", "min_proficiency": 30, "minimum_load": 300},
                        {"skill_name": "仓务", "min_proficiency": 30, "minimum_load": 300},
                    ],
                ),
                (
                    "日常烘干窑与通风架",
                    ["heat", "dry", "store"],
                    5000,
                    500,
                    [{"item_type_id": fueltid, "quantity": 1}],
                    [
                        {"skill_name": "工艺", "min_proficiency": 40, "minimum_load": 300},
                        {"skill_name": "仓务", "min_proficiency": 30, "minimum_load": 300},
                    ],
                ),
            ):
                tool, _ = _item(c, wid, maker, name, None, 1, loc, category="tool", slot_size=10)
                c.execute(
                    "INSERT INTO collective_apparatus(item_id,world_id,operations_json,capacity,throughput,max_power,skill_name,min_proficiency,supplies_json,stations_json) VALUES (?,?,?,?,?,100,'工艺',30,?,?)",
                    (
                        tool,
                        wid,
                        encoded(ops),
                        capacity,
                        throughput,
                        encoded(supply),
                        encoded(stations),
                    ),
                )
            # 只有暴露程度作为外部事件输入，公开上下文不会给出这些隐藏负荷。
            variation = 0.8 + (index % 5) * 0.1
            exposures.extend(
                (
                    (grain, 0.02 * variation, 0.04 * variation),
                    (seed, 0.025 * variation, 0.04 * variation),
                    (soil, 0.025 * variation, 0.04 * variation),
                    (water, 0.012 * variation, 0.018 * variation),
                )
            )
        for sender, sloc, sp in people:
            for recipient, rloc, rp in people:
                if sender == recipient:
                    continue
                distance = great_circle_distance_km(
                    sp["longitude"], sp["latitude"], rp["longitude"], rp["latitude"]
                )
                hours = 0.25 if sloc == rloc else max(12, distance / 10)
                c.execute(
                    "INSERT INTO collective_contacts VALUES (?,?,?)", (sender, recipient, hours)
                )
                c.execute("INSERT INTO collective_route_limits VALUES (?,?,2)", (sender, recipient))
        salt = sum((M.state(c, iid).mass + M.state(c, iid).water) * sr for iid, sr, pr in exposures)
        particles = sum(
            (M.state(c, iid).mass + M.state(c, iid).water) * pr for iid, sr, pr in exposures
        )
        # 外部物质预先登记为源，不在正式运行中追加 origin。
        reservoir, _ = _item(
            c,
            wid,
            None,
            "本次外部有限矿物气溶胶负荷",
            MaterialState(carrier="atmosphere", mass=0, mobile=salt, particles=particles),
            1,
            wid,
            container="world_atmosphere",
        )
        c.execute(
            "INSERT INTO collective_scenario VALUES (?,?,NULL,?,?)",
            (wid, reservoir, encoded(exposures), encoded(current_polities())),
        )
    return wid, at


def inject_initial_disaster(db, world_id, at):
    with db.write() as c:
        scene = c.execute(
            "SELECT * FROM collective_scenario WHERE world_id=?", (world_id,)
        ).fetchone()
        if scene is None or scene["injected_at"] is not None:
            raise ValueError("场景只允许一次初始灾害注入")
        reservoir = M.state(c, scene["reservoir_id"])
        updates = {}
        for iid, salt_ratio, particle_ratio in json.loads(scene["exposure_json"]):
            state = M.state(c, iid)
            scale = state.mass + state.water
            salt = scale * salt_ratio
            particles = scale * particle_ratio
            updates[iid] = replace(
                state,
                mobile=state.mobile + salt * 0.65,
                adsorbed=state.adsorbed + salt * 0.35,
                particles=state.particles + particles,
            )
        updates[scene["reservoir_id"]] = fraction(reservoir, 0)
        M.rebalance(c, updates, operation="initial_external_gray_rain")
        c.execute(
            "UPDATE collective_scenario SET injected_at=? WHERE world_id=?", (to_iso(at), world_id)
        )
        for actor in c.execute(
            "SELECT a.* FROM characters a JOIN collective_people p ON p.character_id=a.id WHERE a.world_id=?",
            (world_id,),
        ).fetchall():
            loc = actor["current_location_id"] or actor["location_id"]
            R.record_observation(
                c,
                actor["id"],
                at,
                "本人看见本地降下灰雨，露天物品和地表留下细灰",
                {
                    "kind": "eyewitness",
                    "location_id": loc,
                    "appearance": "灰色细粒沉积在露天晾晒物、农地及蓄水池；封口物品未见灰层",
                    "uncertainty": "本人不知道原因、远方情况、可食性或处理办法",
                },
            )


def source_manifest():
    paths = list((PROJECT / "world_engine").rglob("*.py")) + list(
        (PROJECT / "scripts").glob("*.py")
    )
    paths.extend(
        [
            PROJECT / "docs/world/locations.json",
            PROJECT / "docs/world/map_features.json",
            PROJECT / "docs/worldbuilding/maps/map_new/data/Noryia States 2026-08-29-11-38.csv",
        ]
    )
    return {
        p.relative_to(PROJECT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(paths)
    }


def frozen_check(manifest):
    if source_manifest() != manifest:
        raise RuntimeError("运行已冻结，源文件或正典目录发生变化")


def runtime_manifest():
    import platform
    import sys
    from importlib.metadata import distributions

    return {
        "python": sys.version,
        "implementation": platform.python_implementation(),
        "executable_sha256": hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
        "sqlite": sqlite3.sqlite_version,
        "packages": dict(
            sorted(
                (d.metadata["Name"].lower(), d.version)
                for d in distributions()
                if d.metadata["Name"]
            )
        ),
    }


def export_report(db, wid, output, freeze, runner, reason):
    with db.read() as c:
        at = c.execute("SELECT w.current_time FROM worlds w WHERE w.id=?", (wid,)).fetchone()[0]
        regions = RegionService.view(c, wid, datetime.fromisoformat(at))
        report = {
            "world_id": wid,
            "world_time": at,
            "stop_reason": reason,
            "goal_achieved": False,
            "audit_status": "尚须独立恢复、实质贡献及反事实审计",
            "regions": regions,
            "material_audit": M.audit(c, wid),
            "usage": runner.usage(wid),
            "deaths": [
                dict(r)
                for r in c.execute("SELECT * FROM collective_deaths WHERE world_id=?", (wid,))
            ],
            "calls": [
                dict(r)
                for r in c.execute(
                    "SELECT id,actor_id,status,reason,created_at,input_tokens,output_tokens,error FROM collective_model_calls WHERE world_id=? ORDER BY rowid",
                    (wid,),
                )
            ],
            "programs": [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM collective_programs WHERE world_id=? ORDER BY rowid", (wid,)
                )
            ],
            "observations": [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM collective_observations WHERE world_id=? ORDER BY world_time,rowid",
                    (wid,),
                )
            ],
        }
    frozen_check(freeze["manifest"])
    from world_engine.collective_audit import audit_run

    audit = audit_run(db.path, output / "baseline-world.db", freeze)
    report["goal_achieved"] = audit["goal_achieved"]
    report["audit_status"] = (
        "完整验收通过" if audit["goal_achieved"] else "完整验收未通过，保留实际未解决结果"
    )
    (output / "audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    checks = [
        ("真实地区恢复", audit["recovery"]["passed"]),
        ("三位实质研究贡献", audit["contribution"]["passed"]),
        ("有利单人的全信息反事实", audit["single_person"]["proved_impossible"]),
        ("独立副本后续维护", audit["future_maintenance"]["passed"]),
        ("正式存档未被审计修改", audit["formal_database_unchanged"]),
        ("正式冻结保护", audit["autonomy"]["formal_frozen_guard"]),
        ("冻结源码一致", audit["autonomy"]["source_manifest_matches"]),
    ]
    (output / "verification.md").write_text(
        "# 实际验收\n\n"
        + "\n".join(f"- {name}：{'通过' if passed else '未通过'}" for name, passed in checks)
        + "\n\n审计没有调用 API；完整证据见 audit.json。\n",
        encoding="utf-8",
    )
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# 灰雨自主模拟运行记录",
        "",
        f"停止原因：{reason}",
        f"当前世界时间：{at}",
        f"真实 API 调用：{report['usage']['calls']}；已知用量：{report['usage']['known_tokens']} tokens。",
        report["audit_status"],
        "",
        "全部区域采用汇总消费群体，NPC 身体另行结算；没有模拟星球每位居民。",
        "完整输入、响应、实测和物质账保存在 world.db。",
        "",
        "| 地区 | 连续安全供给日 | 安全储粮日 | 完成粮作周期 | 下一周期供给日 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for region in regions:
        lines.append(
            f"| {region['id']} | {region['safe_days']} | {region['safe_reserve_days']:.2f} | {region['crop_cycles']} | {region['cycle_supply_days']:.2f} |"
        )
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def run_scenario(
    output,
    *,
    offline=False,
    init_only=False,
    days=240,
    max_calls=120,
    token_limit=1500000,
    max_output_tokens=131072,
    timeout_seconds=600,
):
    from scripts.demo_autonomous_disaster import FrozenDatabase
    from world_engine.collective_clock import next_boundary
    from world_engine.config import Settings
    from world_engine.database import Database
    from world_engine.engine import WorldEngine

    if not 0 < days <= 240:
        raise ValueError("世界观察期限须为1至240日")
    if max_output_tokens < 1 or timeout_seconds <= 0:
        raise ValueError("模型输出预算与超时必须为正数")
    manifest = source_manifest()
    if not offline and not init_only:
        preflight = PROJECT / "run/collective-development/preflight.json"
        if not preflight.exists():
            raise RuntimeError("尚未完成正式模拟的整体审查与离线验收")
        check = json.loads(preflight.read_text(encoding="utf-8"))
        if check.get("passed") is not True or check.get("manifest") != manifest:
            raise RuntimeError("正式模拟必须使用已完成验收的同一份冻结源码")
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    dbclass = Database if offline else FrozenDatabase
    db = dbclass(output / "world.db", legacy_world_defaults=False)
    db.initialize()
    wid, start = build_baseline(db)
    with db.read() as c:
        from contextlib import closing

        with closing(sqlite3.connect(output / "baseline-world.db")) as baseline:
            c.backup(baseline)
        baseline_audit = M.audit(c, wid)
    settings = Settings(database_path=db.path) if offline or init_only else Settings.from_env()
    settings = replace(
        settings,
        database_path=db.path,
        decision_provider="rules",
        world_agent_enabled=False,
        research_api_enabled=False,
        collective_api_enabled=not offline and not init_only,
        deepseek_max_retries=0,
        deepseek_timeout_seconds=timeout_seconds,
        deepseek_max_output_tokens=max_output_tokens,
        collective_max_calls=max_calls,
        collective_token_limit=token_limit,
        collective_max_output_tokens=max_output_tokens,
        debug_model_io=False,
        history_logging_enabled=False,
        knowledge_enabled=False,
        satiety_loss_per_world_hour=3,
        active_character_limit=100,
        adjudication_interval_minutes=180000,
    )
    if settings.collective_api_enabled and not settings.deepseek_api_key:
        raise RuntimeError("正式运行没有可用模型密钥，不能伪造自主判断")
    freeze = {
        "runtime": runtime_manifest(),
        "manifest": manifest,
        "world_id": wid,
        "at": to_iso(start),
        "model": settings.deepseek_model,
        "limits": {
            "calls": max_calls,
            "tokens": token_limit,
            "output_tokens": max_output_tokens,
            "timeout_seconds": timeout_seconds,
        },
        "baseline_sha256": hashlib.sha256((output / "baseline-world.db").read_bytes()).hexdigest(),
        "baseline_material_audit": baseline_audit,
        "days": days,
        "time_policy": {
            "max_heartbeat_hours": 1,
            "split_earlier_events": True,
            "physical_time_unchanged": True,
        },
        "intervention_policy": "一次注入后仅冻结心跳、本人模型判断及只读报告；禁止改源码、参数、库存或给人物提示。",
        "offline": offline,
        "database_guard": "逐事务完整存档核对"
        if not offline
        else "离线能力对照；仅标准事务和冻结源码检查，不是正式自主运行证据",
        "init_only": init_only,
    }
    (output / "freeze.json").write_text(
        json.dumps(freeze, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    inject_initial_disaster(db, wid, start)
    engine = WorldEngine(db, settings)
    if isinstance(db, FrozenDatabase):
        db.arm()
    at = start
    reason = "仅初始化，未启动自主研究"
    try:
        if not init_only:
            reason = "达到预先冻结的观察期限，等待独立审计"
            endpoint = start + timedelta(days=days)
            last_day = -1
            while at < endpoint:
                frozen_check(manifest)
                with db.read() as c:
                    boundary = min(next_boundary(c, wid, at), endpoint)
                    scale = c.execute(
                        "SELECT time_scale FROM world_clock WHERE world_id=?", (wid,)
                    ).fetchone()[0]
                if scale <= 0:
                    raise RuntimeError("冻结世界时钟未启动")
                result = engine.heartbeat(
                    wid, elapsed_seconds=(boundary - at).total_seconds() / scale
                )
                at = result.current_time
                day = (at - start).days
                if day != last_day:
                    last_day = day
                    print(
                        json.dumps(
                            {
                                "day": day,
                                "world_time": to_iso(at),
                                "usage": engine.collective_runner.usage(wid),
                            },
                            ensure_ascii=True,
                        ),
                        flush=True,
                    )
                failure = engine.collective_runner.failure_reason(wid)
                if failure:
                    reason = "冻结轮次停止：" + failure
                    break
    except Exception as exc:
        reason = "冻结轮次技术故障：" + type(exc).__name__ + ": " + str(exc)
        raise
    finally:
        try:
            export_report(db, wid, output, freeze, engine.collective_runner, reason)
        finally:
            engine.close()
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--init-only", action="store_true")
    parser.add_argument("--days", type=int, default=240)
    parser.add_argument("--max-calls", type=int, default=120)
    parser.add_argument("--token-limit", type=int, default=1500000)
    parser.add_argument("--max-output-tokens", type=int, default=131072)
    parser.add_argument("--timeout-seconds", type=float, default=600)
    parser.add_argument("--output-dir", type=Path, default=PROJECT / "run/collective-disaster")
    args = parser.parse_args()
    output = args.output_dir.resolve() / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    run_scenario(
        output,
        offline=args.offline,
        init_only=args.init_only,
        days=args.days,
        max_calls=args.max_calls,
        token_limit=args.token_limit,
        max_output_tokens=args.max_output_tokens,
        timeout_seconds=args.timeout_seconds,
    )
    print(str(output), flush=True)


if __name__ == "__main__":
    main()
