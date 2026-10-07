"""哥哥，一次冻结科研只准备条件，由NPC自己选择全部研究行动。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import contextmanager
from dataclasses import fields, replace
from datetime import timedelta
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from world_engine.config import Settings  # noqa: E402
from world_engine.database import Database  # noqa: E402
from world_engine.engine import WorldEngine  # noqa: E402
from world_engine.materials import encoded  # noqa: E402
from world_engine.repository import from_iso  # noqa: E402
from world_engine.science_budget import budget_lineage  # noqa: E402
from world_engine.science_exports import export_science  # noqa: E402
from world_engine.science_freeze import (  # noqa: E402
    FrozenScienceDatabase,
    backup_world,
    file_manifest,
    freeze_session,
    verify_freeze,
)
from world_engine.science_schema import create_session  # noqa: E402
from world_engine.science_workshop import seed_workshop  # noqa: E402


def load_settings(output):
    # 仅读取既有密钥，不复制或覆盖.env，也不将它写入报告。
    from dotenv import load_dotenv

    load_dotenv(Path(r"C:\Users\14261\Desktop\Vertual_World-main\.env"), override=False)
    base = Settings.from_env()
    return replace(
        base,
        database_path=output / "world.db",
        science_api_enabled=True,
        world_pack_directory=None,
        history_logging_enabled=False,
        deepseek_max_retries=0,
        deepseek_max_output_tokens=8192,
        deepseek_timeout_seconds=max(180, base.deepseek_timeout_seconds),
        research_api_enabled=False,
        collective_api_enabled=False,
        world_agent_enabled=False,
        decision_provider="rules",
        image_api_key=None,
    )


def science_model_config(settings):
    # 哥哥，准备和启动核验共用实际请求设置，推理模式也不能暗中改变。
    return {
        "model": settings.deepseek_model,
        "base_url": settings.deepseek_base_url,
        "max_retries": 0,
        "timeout_seconds": settings.deepseek_timeout_seconds,
        "max_output_tokens": 8192,
        "thinking": {"type": "disabled"},
    }


def runtime_config(settings):
    # 哥哥，运行配置与实际知识源一起冻结，密钥永不进入审计资料。
    omitted = {
        "deepseek_api_key",
        "image_api_key",
        "database_path",
        "history_directory",
        "media_directory",
    }

    def plain(value):
        if isinstance(value, Path):
            return str(value.resolve())
        if isinstance(value, (tuple, list)):
            return [plain(v) for v in value]
        return value

    return {
        "settings": {
            f.name: plain(getattr(settings, f.name))
            for f in fields(settings)
            if f.name not in omitted
        },
        "knowledge_files": file_manifest(settings.knowledge_paths)
        if settings.knowledge_enabled
        else {},
    }


def preserved_identity(c, wid):
    return {
        "characters": [
            dict(r)
            for r in c.execute(
                (
                    "SELECT id,name,identity,skills_json,goals_json,traits_json FROM "
                    "characters WHERE world_id=? ORDER BY id"
                ),
                (wid,),
            )
        ],
        "skills": [
            dict(r)
            for r in c.execute(
                (
                    "SELECT * FROM character_skill_proficiencies WHERE world_id=? ORDER BY "
                    "character_id,skill_name"
                ),
                (wid,),
            )
        ],
        "memories": [
            dict(r)
            for r in c.execute(
                "SELECT * FROM character_memories WHERE world_id=? ORDER BY id", (wid,)
            )
        ],
        "relationships": [
            dict(r)
            for r in c.execute(
                (
                    "SELECT * FROM relationships WHERE world_id=? ORDER BY "
                    "source_character_id,target_character_id"
                ),
                (wid,),
            )
        ],
    }


def known_postal_channels(c, wid, assets):
    from world_engine.spatial import great_circle_distance_km

    actors = {
        r["id"]: dict(r)
        for r in c.execute(
            (
                "SELECT id,location_id,current_location_id,longitude,latitude FROM "
                "characters WHERE world_id=? AND is_player=0"
            ),
            (wid,),
        )
    }
    existing = {
        (r[0], r[1])
        for r in c.execute(
            "SELECT source_character_id,target_character_id FROM relationships WHERE world_id=?",
            (wid,),
        )
    }
    existing.update(
        tuple(r)
        for r in c.execute(
            (
                "SELECT requester_id,recipient_id FROM character_contacts WHERE "
                "world_id=? AND status='accepted'"
            ),
            (wid,),
        )
    )
    for aid, a in actors.items():
        for bid, b in actors.items():
            if aid == bid or aid not in assets["actors"] or bid not in assets["actors"]:
                continue
            local = (a["current_location_id"] or a["location_id"]) == (
                b["current_location_id"] or b["location_id"]
            )
            if not local and (aid, bid) not in existing and (bid, aid) not in existing:
                continue
            if local:
                hours = 0.25
            else:
                if any(
                    p is None
                    for p in (a["longitude"], a["latitude"], b["longitude"], b["latitude"])
                ):
                    continue
                hours = max(
                    0.25,
                    great_circle_distance_km(
                        a["longitude"], a["latitude"], b["longitude"], b["latitude"]
                    )
                    / 8,
                )
            c.execute(
                (
                    "INSERT OR IGNORE INTO "
                    "collective_contacts(sender_id,recipient_id,transit_hours) VALUES (?,?,?)"
                ),
                (aid, bid, hours),
            )


def prepare(source, wid, output, settings, verification, *, prior_runs=()):
    if output.exists():
        raise ValueError("准备须使用新的独立输出目录，不能覆盖旧运行")
    lineage = budget_lineage(prior_runs, wid)
    if lineage["remaining"]["token_limit"] < 8192:
        raise ValueError("跨轮预算不足以保留冻结的单次输出上限，禁止准备新实验")
    output.mkdir(parents=True)
    target = output / "world.db"
    backup_world(source, target)
    db = Database(target, legacy_world_defaults=False)
    with db.read() as c:
        before = preserved_identity(c, wid)
        world = c.execute("SELECT w.current_time FROM worlds w WHERE w.id=?", (wid,)).fetchone()
        if world is None:
            raise ValueError("来源世界不存在")
        if len([a for a in before["characters"]]) < 1:
            raise ValueError("来源世界没有人物")
    db.initialize()
    with db.write() as c:
        after = preserved_identity(c, wid)
        if before != after:
            raise ValueError("正常迁移改变了原身份、技能、目标、关系或记忆，禁止冻结")
        at = from_iso(world["current_time"])
        sid = create_session(c, wid, at, **lineage["remaining"], max_output_tokens=8192)
        assets = seed_workshop(c, wid, at, seed=20261005)
        c.execute(
            "UPDATE material_modes SET policy_json=? WHERE world_id=?",
            (encoded({"mode": "science"}), wid),
        )
        known_postal_channels(c, wid, assets)
        c.execute("UPDATE world_clock SET time_scale=1 WHERE world_id=?", (wid,))
        manifest = freeze_session(
            c,
            sid,
            source_paths=(PROJECT / "world_engine", PROJECT / "scripts/demo_science.py"),
            world_paths=(PROJECT / "docs/worldbuilding", PROJECT / "docs/knowledge"),
            model_config=science_model_config(settings),
            verification={
                **verification,
                "run_kind": "real",
                "max_world_hours": 240,
                "max_heartbeats": 4800,
                "source_identity_preserved": before == after,
                "runtime_config": runtime_config(settings),
                "budget_lineage": lineage,
            },
        )
    (output / "run.json").write_text(
        encoded({"session_id": sid, "world_id": wid, "source_db": str(Path(source).resolve())}),
        encoding="utf-8",
    )
    (output / "source-identity.json").write_text(
        json.dumps(before, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report = export_science(db, sid, output / "reports/prepared")
    print(
        encoded(
            {
                "prepared": True,
                "session_id": sid,
                "npc_count": len(assets["actors"]),
                "calls": 0,
                "goal_achieved": report["goal_achieved"],
            }
        ),
        flush=True,
    )
    return manifest


@contextmanager
def run_lock(path):
    # 哥哥，锁由操作系统释放；崩溃遗留的文件不会阻止同账恢复，也不能允许并行。
    handle = path.open("a+b")
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        def acquire():
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

        def release():
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        def acquire():
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        def release():
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    try:
        try:
            acquire()
        except OSError as exc:
            raise ValueError("该输出已有运行锁，不能启动并行模拟") from exc
        try:
            yield
        finally:
            handle.seek(0)
            release()
    finally:
        handle.close()


def run(output, settings, *, resume=False):
    with run_lock(output / "process.lock"):
        _run(output, settings, resume=resume)


def _run(output, settings, *, resume=False):
    info = json.loads((output / "run.json").read_text(encoding="utf-8"))
    sid, wid = info["session_id"], info["world_id"]
    raw = Database(output / "world.db", legacy_world_defaults=False)
    with raw.read() as c:
        s = c.execute(
            "SELECT * FROM science_sessions WHERE id=? AND world_id=?", (sid, wid)
        ).fetchone()
        if s is None or not verify_freeze(c, sid):
            raise ValueError("冻结核验未通过，禁止运行")
        manifest = json.loads(s["freeze_json"])
        verification = manifest["verification"]
        lineage = verification.get("budget_lineage")
        if not lineage or lineage != budget_lineage(
            tuple(r["output_path"] for r in lineage["prior_runs"]), wid
        ):
            raise ValueError("跨轮预算来源不同于冻结清单，禁止继续本轮")
        if {k: s[k] for k in lineage["remaining"]} != lineage["remaining"]:
            raise ValueError("跨轮预算会话额度不同于剩余总授权")
        if verification.get("runtime_config") != runtime_config(settings):
            raise ValueError("运行配置或实际知识源不同于冻结清单，禁止继续本轮")
        if not (
            verification.get("offline_passed") and verification.get("independent_review_passed")
        ):
            raise ValueError("离线验收或独立整体审查未完成，禁止付费运行")
        config = science_model_config(settings)
        if config != manifest["model_config"]:
            raise ValueError("模型配置不同于冻结清单，禁止重新指纹继续运行")
        if not settings.deepseek_api_key:
            raise ValueError("未配置既有API密钥，不请求模型")
        if s["status"] == "stopped" or (s["status"] == "stopping" and not resume):
            raise ValueError("本轮已停止新增判断，不能恢复付费调用")
        if not resume and s["status"] != "prepared":
            raise ValueError("会话已启动，须用同一会话resume，不得另开本轮")
        started = from_iso(s["created_at"])
    db = FrozenScienceDatabase(output / "world.db", session_id=sid)
    engine = None
    try:
        with db.write() as c:
            c.execute(
                "UPDATE science_sessions SET status='running' WHERE id=? AND status='prepared'",
                (sid,),
            )
        engine = WorldEngine(db, settings)
        engine.heartbeat(wid, elapsed_seconds=0)
        from world_engine.collective_clock import next_boundary

        for _ in range(verification["max_heartbeats"]):
            with db.read() as c:
                s = c.execute("SELECT * FROM science_sessions WHERE id=?", (sid,)).fetchone()
                at = from_iso(
                    c.execute(
                        "SELECT w.current_time FROM worlds w WHERE w.id=?", (wid,)
                    ).fetchone()[0]
                )
                if s["status"] == "stopped":
                    break
                boundary = next_boundary(c, wid, at)
            if at >= started + timedelta(hours=verification["max_world_hours"]):
                engine.science_runners[sid].stop("达到冻结的世界时间观察边界，未完成计划仍保留", at)
                break
            engine.heartbeat(wid, elapsed_seconds=(boundary - at).total_seconds())
            print(
                encoded(
                    {"world_time": at.isoformat(), "usage": engine.science_runners[sid].usage()}
                ),
                flush=True,
            )
        else:
            with db.read() as c:
                at = from_iso(
                    c.execute(
                        "SELECT w.current_time FROM worlds w WHERE w.id=?", (wid,)
                    ).fetchone()[0]
                )
            engine.science_runners[sid].stop("达到冻结的心跳观察边界", at)
    finally:
        if engine is not None:
            engine.close()
        report = export_science(raw, sid, output / "reports/final")
        print(encoded(report), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-db", type=Path)
    parser.add_argument("--world-id")
    parser.add_argument("--prior-run", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    actions = parser.add_mutually_exclusive_group(required=True)
    for flag in ("prepare-only", "run", "resume", "export-only"):
        actions.add_argument("--" + flag, action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    settings = load_settings(output)
    if args.prepare_only:
        if args.source_db is None or not args.world_id:
            parser.error("准备需要source-db与world-id")
        verification_path = (
            PROJECT / ".superpowers/sdd/2026-10-05-emergent-science/verification.json"
        )
        verification = (
            json.loads(verification_path.read_text(encoding="utf-8"))
            if verification_path.exists()
            else {}
        )
        prepare(
            args.source_db,
            args.world_id,
            output,
            settings,
            verification,
            prior_runs=tuple(args.prior_run),
        )
    elif args.export_only:
        info = json.loads((output / "run.json").read_text(encoding="utf-8"))
        print(
            encoded(
                export_science(
                    Database(output / "world.db", legacy_world_defaults=False),
                    info["session_id"],
                    output / "reports/export",
                )
            )
        )
    else:
        run(output, settings, resume=args.resume)


if __name__ == "__main__":
    main()
