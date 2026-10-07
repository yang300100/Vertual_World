"""哥哥，这次冻结引擎后只注入初始灾害，人物研究程序必须来自模型或已收到的实测报告。"""

from __future__ import annotations

import argparse
import hashlib
import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from scripts.demo_global_incident import PROJECT, _registration
from world_engine.autonomous_research import ResearchService as R
from world_engine.config import Settings
from world_engine.database import Database
from world_engine.economy import CommoditySpec, EconomyService
from world_engine.engine import WorldEngine
from world_engine.polities import current_polities
from world_engine.repository import WorldRepository, from_iso, to_iso

DESCRIPTION = (
    "昨夜各地天空出现异常光带。今晨本地常用取水纹器突然剧烈回鸣，水流断续，"
    "旁边的背景仪表也出现陌生干扰。工坊旧记录显示事发前这些器具一直平稳工作。"
    "目前还不知道原因、其他地区情况或处理办法。"
)


class FrozenDatabase(Database):
    """每个引擎事务核对上一个提交，哥哥能核验运行中没有外部修改存档。"""

    _expected = None

    @staticmethod
    def fingerprint(connection):
        data = connection.serialize()
        # SQLite 提交会更新这两处页头计数；它们不属于世界事实，其余字节全部核对。
        return hashlib.sha256(data[:24] + bytes(4) + data[28:92] + bytes(4) + data[96:]).hexdigest()

    def arm(self):
        with super().read() as c:
            self._expected = self.fingerprint(c)

    def verify(self, c):
        if self._expected and self.fingerprint(c) != self._expected:
            raise RuntimeError("冻结运行检测到外部存档修改，禁止继续将结果认作自主模拟")

    @contextmanager
    def read(self):
        with super().read() as c:
            c.execute("BEGIN")
            self.verify(c)
            yield c

    @contextmanager
    def write(self):
        following = None
        with super().write() as c:
            self.verify(c)
            yield c
            if self._expected:
                following = self.fingerprint(c)
        if following:
            self._expected = following


def build_baseline(db):
    """先建立灾前正常工坊和通信名册；不指定任何应对角色、配方、目标程序。"""
    at = datetime(2040, 4, 1, 8, tzinfo=UTC)
    with db.write() as c:
        wid = WorldRepository().create_world(
            c,
            name="伊瑟拉·回响潮自主研究",
            minutes_per_tick=60,
            seed_demo=False,
            seed_maps=False,
            start_time=at,
        )
        net = R.create_network(c, wid, "各国日常公共取水纹器", at)
        food = EconomyService.register_item(
            c,
            wid,
            _registration(c, wid, at, "commodity", "工坊日常储粮"),
            CommoditySpec(
                name="工坊日常储粮", category="food", price=2, nutrition=60, stack_limit=100
            ),
            at,
        )
        actors = []
        for index, polity in enumerate(current_polities()):
            location = str(uuid4())
            c.execute(
                "INSERT INTO locations(id,world_id,name,kind,longitude,latitude) VALUES (?,?,?,'city',?,?)",
                (location, wid, polity["capital_name"], polity["longitude"], polity["latitude"]),
            )
            device = R.add_device(
                c,
                net,
                location,
                polity["key"],
                polity["capital_name"] + "公共取水纹器",
                physical={"center": -0.16 + index * 0.016, "width": 0.22 + (index % 4) * 0.04},
            )
            for profession, skills in (
                ("工坊技师", ["完整单纹维护", "常规仪表读数", "手工装配", "读写"]),
                ("工坊学徒", ["常规仪表读数", "读写"]),
            ):
                actor = str(uuid4())
                goals = ["照顾自己的生活", "履行日常工作，保护居民安全与公共服务"]
                c.execute(
                    "INSERT INTO characters(id,world_id,name,identity,location_id,current_location_id,longitude,"
                    "latitude,energy,satiety,money,health,skills_json,goals_json,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,100,100,200,100,?,?,?,?)",
                    (
                        actor,
                        wid,
                        polity["capital_name"] + profession,
                        profession,
                        location,
                        location,
                        polity["longitude"],
                        polity["latitude"],
                        json.dumps(skills, ensure_ascii=False),
                        json.dumps(goals, ensure_ascii=False),
                        to_iso(at),
                        to_iso(at),
                    ),
                )
                c.execute("UPDATE characters SET inventory_capacity=60 WHERE id=?", (actor,))
                c.execute(
                    "INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,"
                    "owner_character_id,quantity,condition) VALUES (?,?,?,'character_inventory',?,?,1000,100)",
                    (str(uuid4()), wid, food, actor, actor),
                )
                R.enroll(c, actor, device, capability=len(skills))
                actors.append(actor)
        # 这是灾前行业地址簿，只有联系渠道，不含远方灾情或救灾分工。
        for sender in actors:
            for recipient in actors:
                if sender != recipient:
                    R.add_contact(c, sender, recipient)
    return wid, net, at


def inject_initial_disaster(db, net, at):
    with db.write() as c:
        R.disturb(c, net, forcing=2.5, at=at, description=DESCRIPTION)


def build_scenario(db):
    wid, net, at = build_baseline(db)
    inject_initial_disaster(db, net, at)
    return wid, net, at


def source_manifest():
    paths = list((PROJECT / "world_engine").rglob("*.py"))
    paths += list((PROJECT / "scripts").glob("*.py"))
    paths += [PROJECT / "docs/world/locations.json", PROJECT / "docs/world/map_features.json"]
    return {
        str(p.relative_to(PROJECT)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(paths)
    }


def frozen_check(manifest):
    if set(source_manifest()) != set(manifest):
        raise RuntimeError("运行已冻结，源文件集合发生变化")
    for relative, expected in manifest.items():
        p = PROJECT / relative
        if not p.exists() or hashlib.sha256(p.read_bytes()).hexdigest() != expected:
            raise RuntimeError(f"运行已冻结，检测到源文件变化：{relative}")


def export_report(db, wid, net, output, manifest, *, stop_reason):
    with db.read() as c:
        view = R.network_view(c, net)
        world_time = c.execute(
            "SELECT w.current_time FROM worlds w WHERE w.id=?", (wid,)
        ).fetchone()[0]
        programs = [
            dict(p)
            for p in c.execute(
                "SELECT p.*,a.name FROM research_programs p JOIN characters a "
                "ON a.id=p.character_id WHERE a.world_id=? ORDER BY p.created_at,p.rowid",
                (wid,),
            )
        ]
        calls = [
            dict(r)
            for r in c.execute(
                "SELECT * FROM research_calls WHERE world_id=? ORDER BY rowid", (wid,)
            )
        ]
        steps = [
            dict(s)
            for s in c.execute(
                "SELECT s.*,p.character_id,a.name FROM research_steps s "
                "JOIN research_programs p ON p.id=s.program_id JOIN characters a "
                "ON a.id=p.character_id WHERE a.world_id=? ORDER BY s.world_time,s.rowid",
                (wid,),
            )
        ]
        messages = [
            dict(m)
            for m in c.execute(
                "SELECT * FROM research_messages WHERE network_id=? ORDER BY sent_at,rowid", (net,)
            )
        ]
        people = [
            dict(p)
            for p in c.execute(
                "SELECT a.id,a.name,a.identity,a.health,a.satiety,a.energy,r.device_id "
                "FROM characters a JOIN research_people r ON r.character_id=a.id WHERE a.world_id=?",
                (wid,),
            )
        ]
        observations = [
            dict(e)
            for e in c.execute(
                "SELECT id,occurred_at,actor_id,event_type,summary,payload_json FROM world_events "
                "WHERE world_id=? AND event_type LIKE 'research.%' ORDER BY occurred_at,rowid",
                (wid,),
            )
        ]
        integrity = c.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = [tuple(r) for r in c.execute("PRAGMA foreign_key_check")]
    frozen_check(manifest)
    usage = {
        "calls": len(calls),
        "known_input_tokens": sum(r["input_tokens"] or 0 for r in calls),
        "known_output_tokens": sum(r["output_tokens"] or 0 for r in calls),
        "unknown_usage_calls": sum(
            r["input_tokens"] is None or r["output_tokens"] is None for r in calls
        ),
    }
    report = {
        "world_id": wid,
        "network_id": net,
        "world_time": world_time,
        "view": view,
        "world_hours": (from_iso(world_time) - from_iso(view["started_at"])).total_seconds() / 3600,
        "stop_reason": stop_reason,
        "usage": usage,
        "programs": programs,
        "calls": calls,
        "steps": steps,
        "messages": messages,
        "people": people,
        "events": observations,
        "source_unchanged": True,
        "integrity_check": integrity,
        "foreign_key_errors": foreign_keys,
    }
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# 回响潮：冻结后由 NPC 自主研究",
        "",
        f"状态：{view['status']}；世界时间：{world_time}；耗时：{report['world_hours']} 小时。",
        f"模型调用 {usage['calls']} 次；已知输入 {usage['known_input_tokens']} tokens，输出 {usage['known_output_tokens']} tokens；用量未知 {usage['unknown_usage_calls']} 次。",
        "",
        "初始灾害只改变环境激励并产生本地目击。没有注册救灾配方、预置程序或合作步骤。",
        "人物自拟程序、模型响应诊断与用量、实际实验、储备扣减、送信与收到时间均在 JSON 和独立数据库中。",
        "通用执行层提供固定的共振器物理模型和操作接口；这是有限物理模型内的研究，不代表任意科学发现。",
        "42 名灾前工坊人员，覆盖 20 个正式政体和阿德伦地方政权。每处都须保持真实供水能力和安全运行。",
        "人物按已收到的实测方法自行重复本地实验；这一证据采纳规则由通用引擎实现，未调用模型的判断不是开放式语言推理。",
        "",
        "| 政体 | 本地干扰 | 总风险 | 实际服务 | 剩余储备 | 实验执行 | 实际安装 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for device in view["devices"]:
        ids = {p["id"] for p in people if p["device_id"] == device["id"]}
        local_steps = [s for s in steps if s["character_id"] in ids]
        lines.append(
            f"| {device['name']} | {device['local_noise']:.4f} | {device['hazard']:.4f} | {device['service']:.4f} | "
            f"{device['reserve']} | {sum(s['operation'] == 'experiment' for s in local_steps)} | "
            f"{sum(s['operation'] == 'configure' for s in local_steps)} |"
        )
    lines += [
        "",
        f"停止原因：{stop_reason}。源文件冻结核验：通过；SQLite：{integrity}。",
        "",
        "通信按大圆距离、20 km/h 驿运和两小时装卸结算；不包含逐段道路、驿站劳工和地形模拟。",
        "[完整证据](report.json) · [独立存档](world.db) · [灾前存档](baseline-world.db) · [冻结清单](freeze.json)",
        "",
    ]
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="禁止模型网络，初始灾害不会自行解决")
    parser.add_argument("--init-only", action="store_true", help="只生成初始灾害与证据，不调用模型")
    parser.add_argument("--hours", type=int, default=2400)
    parser.add_argument("--max-calls", type=int, default=24)
    parser.add_argument("--token-limit", type=int, default=60000)
    parser.add_argument("--output-dir", type=Path, default=PROJECT / "run/autonomous-disaster")
    args = parser.parse_args()
    output = args.output_dir.resolve() / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    output.mkdir(parents=True, exist_ok=False)
    db = FrozenDatabase(output / "world.db", legacy_world_defaults=False)
    db.initialize()
    manifest = source_manifest()
    wid, net, at = build_baseline(db)
    with db.read() as c:
        import sqlite3

        with sqlite3.connect(output / "baseline-world.db") as baseline:
            c.backup(baseline)
        baseline_view = R.network_view(c, net)
    settings = (
        Settings(database_path=db.path) if args.offline or args.init_only else Settings.from_env()
    )
    settings = replace(
        settings,
        database_path=db.path,
        decision_provider="rules",
        world_agent_enabled=False,
        research_api_enabled=not args.offline and not args.init_only,
        deepseek_max_retries=0,
        debug_model_io=False,
        history_logging_enabled=False,
        knowledge_enabled=False,
        active_character_limit=100,
        adjudication_interval_minutes=180000,
        research_max_calls=args.max_calls,
        research_token_limit=args.token_limit,
    )
    if settings.research_api_enabled and not settings.deepseek_api_key:
        raise RuntimeError("未配置模型密钥，不能假装人物已经完成自主研究")
    freeze = {
        "manifest": manifest,
        "world_id": wid,
        "network_id": net,
        "at": to_iso(at),
        "baseline": baseline_view,
        "model": settings.deepseek_model,
        "limits": {
            "calls": args.max_calls,
            "tokens": args.token_limit,
            "output_tokens": settings.deepseek_max_output_tokens,
        },
        "hours": args.hours,
        "intervention_policy": "注入初始扰动后，只调用正常心跳；禁止更改源码、参数、人物程序和库存。",
    }
    (output / "freeze.json").write_text(
        json.dumps(freeze, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    frozen_check(manifest)
    inject_initial_disaster(db, net, at)
    reason = "仅初始化"
    engine = WorldEngine(db, settings)
    db.arm()
    try:
        if not args.init_only:
            reason = "达到既定世界时间上限"
            with db.read() as c:
                scale = c.execute(
                    "SELECT time_scale FROM world_clock WHERE world_id=?", (wid,)
                ).fetchone()[0]
            if scale <= 0:
                raise RuntimeError("世界时钟未启动")
            for hour in range(1, args.hours + 1):
                frozen_check(manifest)
                engine.heartbeat(wid, elapsed_seconds=3600 / scale)
                with db.read() as c:
                    view = R.network_view(c, net)
                    usage_calls = c.execute(
                        "SELECT count(*) FROM research_calls WHERE world_id=?", (wid,)
                    ).fetchone()[0]
                    experimental = c.execute(
                        "SELECT count(*) FROM research_steps WHERE operation='experiment'"
                    ).fetchone()[0]
                    installed = c.execute(
                        "SELECT count(*) FROM research_steps WHERE operation='configure'"
                    ).fetchone()[0]
                if hour == 1 or hour % 24 == 0 or view["status"] == "resolved":
                    print(
                        json.dumps(
                            {
                                "hour": hour,
                                "status": view["status"],
                                "api_calls": usage_calls,
                                "experiments": experimental,
                                "installations": installed,
                            },
                            ensure_ascii=True,
                        ),
                        flush=True,
                    )
                if view["status"] == "resolved":
                    reason = "全部政体装置连续安全运行并恢复公共服务"
                    break
                failure = engine.research_runner.failure_reason(wid)
                if failure:
                    reason = "模型接口技术故障，结束本轮以避免重复请求：" + failure
                    break
    finally:
        report = export_report(db, wid, net, output, manifest, stop_reason=reason)
        engine.close()
        print(
            json.dumps(
                {
                    "status": report["view"]["status"],
                    "usage": report["usage"],
                    "report": str(output / "report.md"),
                },
                ensure_ascii=True,
            ),
            flush=True,
        )
    return 0 if report["view"]["status"] == "resolved" or args.init_only or args.offline else 1


if __name__ == "__main__":
    raise SystemExit(main())
