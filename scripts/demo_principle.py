"""哥哥，独立世界从零科技开始，由NPC提案、独立评审，再观察真实应用。"""

import argparse
import hashlib
import json
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from world_engine.agent_llm import build_agent_model_backend  # noqa: E402
from world_engine.config import Settings  # noqa: E402
from world_engine.database import Database  # noqa: E402
from world_engine.knowledge import WorldKnowledgeBase  # noqa: E402
from world_engine.materials import encoded  # noqa: E402
from world_engine.principle_applications import PrincipleApplications  # noqa: E402
from world_engine.principle_exports import export_principle  # noqa: E402
from world_engine.principle_runner import PrincipleRunner  # noqa: E402
from world_engine.repository import from_iso, to_iso  # noqa: E402
from world_engine.science_freeze import backup_world, file_manifest  # noqa: E402


def settings(output):
    load_dotenv(ROOT / ".env", override=False)
    return replace(
        Settings.from_env(),
        database_path=output / "world.db",
        world_pack_directory=None,
        world_agent_enabled=False,
        research_api_enabled=False,
        collective_api_enabled=False,
        science_api_enabled=False,
        decision_provider="rules",
        history_logging_enabled=False,
        deepseek_max_retries=0,
        deepseek_max_output_tokens=4096,
        debug_model_io=False,
        deepseek_timeout_seconds=180,
        image_api_key=None,
    )


def identity(c, wid):
    return hashlib.sha256(
        encoded(
            [
                list(r)
                for r in c.execute(
                    "SELECT id,name,identity,skills_json,goals_json,traits_json FROM characters "
                    "WHERE world_id=? ORDER BY id",
                    (wid,),
                )
            ]
        ).encode()
    ).hexdigest()


def prepare(
    source,
    wid,
    output,
    *,
    max_calls,
    token_limit,
    budget_source=None,
    target_count=20,
    continue_world=False,
    property_kind=None,
):
    if budget_source:
        from scripts.demo_science import run_lock
        from world_engine.principle_budget import lineage_for, lock_path

        lineage = lineage_for(budget_source, ROOT / ".context/principle-budgets")
        lock_path(lineage).parent.mkdir(parents=True, exist_ok=True)
        with run_lock(lock_path(lineage)):
            return _prepare(
                source,
                wid,
                output,
                max_calls=max_calls,
                token_limit=token_limit,
                budget_source=budget_source,
                target_count=target_count,
                continue_world=continue_world,
                property_kind=property_kind,
            )
    return _prepare(
        source,
        wid,
        output,
        max_calls=max_calls,
        token_limit=token_limit,
        target_count=target_count,
        continue_world=continue_world,
        property_kind=property_kind,
    )


def _prepare(
    source,
    wid,
    output,
    *,
    max_calls,
    token_limit,
    budget_source=None,
    target_count=20,
    continue_world=False,
    property_kind=None,
):
    if output.exists():
        raise ValueError("使用新的独立目录，不覆盖旧科研存档")
    output.mkdir(parents=True)
    lineage = None
    if budget_source:
        from world_engine.principle_budget import lineage_for, usage

        lineage = lineage_for(budget_source, ROOT / ".context/principle-budgets")
        remaining = usage(lineage, allow_initial=True)
        max_calls = min(max_calls, remaining["max_calls"] - remaining["calls"])
        token_limit = min(token_limit, remaining["token_limit"] - remaining["accounted_tokens"])
        if max_calls <= 0 or token_limit <= 0 or remaining["reserved_tokens"]:
            raise ValueError("继承预算没有足够的可用额度")
    backup_world(source, output / "world.db")
    db = Database(output / "world.db", legacy_world_defaults=False)
    with db.read() as c:
        original = identity(c, wid)
    db.initialize()
    with db.write() as c:
        if original != identity(c, wid):
            raise ValueError("迁移改变了原人物身份，不能继续")
        initial = c.execute(
            "SELECT count(*) FROM technologies WHERE world_id=?", (wid,)
        ).fetchone()[0]
        initial += c.execute(
            "SELECT count(*) FROM principle_technologies WHERE world_id=?", (wid,)
        ).fetchone()[0]
        if initial and not continue_world:
            raise ValueError("来源世界已有科技，不能假装从零开始")
        from world_engine.principle_workshop import seed_inputs

        if not continue_world:
            seed_inputs(c, wid)
        at = from_iso(
            c.execute("SELECT w.current_time FROM worlds w WHERE id=?", (wid,)).fetchone()[0]
        )
        manifest = {
            "run_kind": "real",
            "initial_technologies": initial,
            "initial_conditions": "继承真实科研成果与剩余库存，不补给、不清空历史"
            if continue_world
            else "现有职业人员各有有限普通原料和手工器具，不预置任何科技或原型",
            "source_db": str(Path(source).resolve()),
            "identity_hash": original,
            "source_files": file_manifest(
                (ROOT / "world_engine", ROOT / "scripts/demo_principle.py")
            ),
            "world_files": file_manifest((ROOT / "docs/worldbuilding", ROOT / "docs/knowledge")),
            "multi_invention_reviews": True,
            "research_rounds_per_actor": 3,
            "research_cycle_version": 1,
            "acceptance_version": "continuation_properties_v1"
            if continue_world
            else "chain_inventions_v1",
            "research_focus": "manufactured_item_properties" if continue_world else None,
            "requested_property": property_kind,
            **({"budget_lineage": lineage} if lineage else {}),
        }
        sid = str(uuid4())
        c.execute(
            "INSERT INTO principle_sessions(id,world_id,target_count,max_calls,token_limit,"
            "created_at,manifest_json) VALUES (?,?,?,?,?,?,?)",
            (sid, wid, target_count, max_calls, token_limit, to_iso(at), encoded(manifest)),
        )
        c.execute("UPDATE world_clock SET time_scale=1 WHERE world_id=?", (wid,))
    (output / "run.json").write_text(
        encoded({"session_id": sid, "world_id": wid}), encoding="utf-8"
    )
    if lineage:
        from world_engine.principle_budget import register

        lineage["required_runs"] = register(lineage, output / "world.db", sid)
        with db.write() as c:
            c.execute(
                "UPDATE principle_sessions SET manifest_json=? WHERE id=?", (encoded(manifest), sid)
            )
    print(encoded(export_principle(db, sid, output / "reports/prepared")), flush=True)


def run(output):
    from scripts.demo_science import run_lock

    info = json.loads((output / "run.json").read_text(encoding="utf-8"))
    with Database(output / "world.db", legacy_world_defaults=False).read() as c:
        manifest = json.loads(
            c.execute(
                "SELECT manifest_json FROM principle_sessions WHERE id=?", (info["session_id"],)
            ).fetchone()[0]
        )
    with run_lock(output / "process.lock"):
        if manifest.get("budget_lineage"):
            from world_engine.principle_budget import lock_path

            with run_lock(lock_path(manifest["budget_lineage"])):
                _run(output)
        else:
            _run(output)


def _run(output):
    info = json.loads((output / "run.json").read_text(encoding="utf-8"))
    db, config = Database(output / "world.db", legacy_world_defaults=False), settings(output)
    with db.write() as c:
        session = c.execute(
            "SELECT * FROM principle_sessions WHERE id=?", (info["session_id"],)
        ).fetchone()
        manifest = json.loads(session["manifest_json"])
        if session["status"] != "prepared":
            raise ValueError("本轮已经启动，不清空账目或重新发送")
        c.execute(
            "UPDATE principle_sessions SET status='running' WHERE id=?", (info["session_id"],)
        )
    knowledge = WorldKnowledgeBase.from_paths(
        config.knowledge_paths,
        project_root=ROOT,
        excluded_filenames=config.knowledge_excluded_filenames,
    )
    backend = build_agent_model_backend(config)
    runner = PrincipleRunner(db, backend, info["session_id"], knowledge=knowledge)
    cycle_blocked = False
    try:
        while True:
            if (
                file_manifest((ROOT / "world_engine", ROOT / "scripts/demo_principle.py"))
                != manifest["source_files"]
            ):
                raise RuntimeError("源码已变动，不能把后续结果视为同一受控运行")
            if (
                file_manifest((ROOT / "docs/worldbuilding", ROOT / "docs/knowledge"))
                != manifest["world_files"]
            ):
                raise RuntimeError("世界设定已变动，停止本轮")
            with db.read() as c:
                at = from_iso(
                    c.execute(
                        "SELECT w.current_time FROM worlds w WHERE id=?", (info["world_id"],)
                    ).fetchone()[0]
                )
            if not runner.step(at):
                break
            if not settle_research(db, config, info):
                with db.write() as c:
                    runner.stop(c, "研发周期尚有阻碍，保留项目与已投入进度，停止新增调用")
                cycle_blocked = True
                break
            # 哥哥，先观察本人选择的应用，再将真实结果反馈给下一轮研究。
            settle_applications(db, config, info, attempted_once=True)
            with db.read() as c:
                count = c.execute(
                    "SELECT count(*) FROM principle_technologies WHERE session_id=?",
                    (info["session_id"],),
                ).fetchone()[0]
                print(encoded({"researched": count, "usage": runner.usage(c)}), flush=True)
        if not cycle_blocked:
            settle_research(db, config, info)
        settle_applications(db, config, info)
        report = export_principle(db, info["session_id"], output / "reports/final")
        with db.write() as c:
            c.execute(
                "UPDATE principle_sessions SET status=?,stop_reason=COALESCE(stop_reason,?) "
                "WHERE id=?",
                (
                    "completed" if report["goal_achieved"] else "stopped",
                    "研发与应用验收通过"
                    if report["goal_achieved"]
                    else "未满足全部研发与世界影响验收条件",
                    info["session_id"],
                ),
            )
    except BaseException as exc:
        with db.write() as c:
            runner.stop(c, "运行中断：" + str(exc))
        raise
    finally:
        if backend:
            backend.close()
        print(
            encoded(export_principle(db, info["session_id"], output / "reports/final")), flush=True
        )


def settle_research(db, config, info):
    """哥哥，只推进已提交研究与正常生活，有限观察后仍未完成就保留障碍。"""
    from world_engine.principle_cycles import tick
    from world_engine.principle_inventions import has_table

    applications = PrincipleApplications(db, config, info["world_id"])
    for _ in range(1024):
        with db.write() as c:
            if not has_table(c, "principle_projects"):
                return True
            at = from_iso(
                c.execute(
                    "SELECT w.current_time FROM worlds w WHERE w.id=?", (info["world_id"],)
                ).fetchone()[0]
            )
            tick(c, info["world_id"], at)
            pending = c.execute(
                "SELECT j.status FROM principle_projects j JOIN principle_proposals p "
                "ON p.id=j.proposal_id WHERE p.session_id=? AND j.status!='completed'",
                (info["session_id"],),
            ).fetchall()
            if not pending:
                return True
            if any(row["status"] == "blocked" for row in pending):
                return False
        applications.advance_to(at + timedelta(hours=1))
    return False


def settle_applications(db, config, info, attempted_once=False):
    applications = PrincipleApplications(db, config, info["world_id"])
    with db.read() as c:
        from world_engine.principle_inventions import candidates

        applications_to_try = []
        for tech in c.execute(
            "SELECT * FROM principle_technologies WHERE session_id=? ORDER BY rowid",
            (info["session_id"],),
        ):
            for option in candidates(c, tech):
                reference = option["invention_id"]
                tried = c.execute(
                    "SELECT 1 FROM principle_application_attempts WHERE technology_id=? "
                    "AND invention_id IS ?",
                    (tech["id"], reference),
                ).fetchone()
                completed = c.execute(
                    "SELECT 1 FROM principle_application_attempts WHERE technology_id=? "
                    "AND invention_id IS ? AND json_extract(result_json,'$.status')='applied'",
                    (tech["id"], reference),
                ).fetchone()
                if not completed and (not attempted_once or not tried):
                    applications_to_try.append((tech["id"], reference))
    for tid, invention_id in applications_to_try:
        try:
            result = applications.apply(tid, invention_id)
        except ValueError as exc:
            result = {"technology_id": tid, "status": "pending", "reason": str(exc)}
        with db.write() as c:
            at = c.execute(
                "SELECT w.current_time FROM worlds w WHERE id=?", (info["world_id"],)
            ).fetchone()[0]
            c.execute(
                "INSERT INTO principle_application_attempts(id,technology_id,result_json,"
                "occurred_at,invention_id) "
                "VALUES (?,?,?,?,?)",
                (str(uuid4()), tid, encoded(result), at, invention_id),
            )
        print(encoded(result), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-db", type=Path)
    parser.add_argument("--world-id")
    parser.add_argument("--max-calls", type=int, default=60)
    parser.add_argument("--token-limit", type=int, default=600000)
    parser.add_argument("--budget-source", type=Path)
    parser.add_argument("--target-count", type=int, default=20)
    parser.add_argument("--continue-world", action="store_true")
    parser.add_argument("--property-kind", choices=["material_strength", "craft_precision"])
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--prepare-only", action="store_true")
    action.add_argument("--run", action="store_true")
    action.add_argument("--export-only", action="store_true")
    action.add_argument("--settle-only", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    if args.prepare_only:
        if not args.source_db or not args.world_id:
            parser.error("准备需要来源数据库和世界编号")
        prepare(
            args.source_db,
            args.world_id,
            output,
            max_calls=args.max_calls,
            token_limit=args.token_limit,
            budget_source=args.budget_source,
            target_count=args.target_count,
            continue_world=args.continue_world,
            property_kind=args.property_kind,
        )
    elif args.run:
        run(output)
    elif args.settle_only:
        from scripts.demo_science import run_lock

        with run_lock(output / "process.lock"):
            info = json.loads((output / "run.json").read_text(encoding="utf-8"))
            config = replace(settings(output), deepseek_api_key=None)
            db = Database(output / "world.db", legacy_world_defaults=False)
            with db.read() as c:
                session = c.execute(
                    "SELECT * FROM principle_sessions WHERE id=?", (info["session_id"],)
                ).fetchone()
                manifest = json.loads(session["manifest_json"])
                if (
                    file_manifest((ROOT / "world_engine", ROOT / "scripts/demo_principle.py"))
                    != manifest["source_files"]
                ):
                    raise ValueError("源码已改变，不能用不同规则重试旧应用")
                if (
                    file_manifest((ROOT / "docs/worldbuilding", ROOT / "docs/knowledge"))
                    != manifest["world_files"]
                ):
                    raise ValueError("世界设定已改变，不能用不同规则重试旧应用")
            settle_research(db, config, info)
            settle_applications(db, config, info)
            export_principle(db, info["session_id"], output / "reports/settlement")
    else:
        info = json.loads((output / "run.json").read_text(encoding="utf-8"))
        export_principle(
            Database(output / "world.db", legacy_world_defaults=False),
            info["session_id"],
            output / "reports/export",
        )


if __name__ == "__main__":
    main()
