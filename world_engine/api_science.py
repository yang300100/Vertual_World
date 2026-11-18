"""哥哥，科研观察路由只读世界事实，不触发模型或恢复计账。"""

import json

from fastapi import APIRouter, HTTPException

from world_engine.principle_service import PrincipleTechnologyService, table_exists
from world_engine.principle_tree import technology_tree
from world_engine.science_chronicle import technology_process
from world_engine.science_runner import ScienceRunner
from world_engine.science_views import physical_impacts, physical_projects
from world_engine.technologies import TechnologyService


def build_science_router(database):
    router = APIRouter(prefix="/api/worlds/{world_id}", tags=["科研"])

    def check(c, world_id):
        if not c.in_transaction:
            c.execute("BEGIN")
        if not c.execute("SELECT 1 FROM worlds WHERE id=?", (world_id,)).fetchone():
            raise HTTPException(404, "世界不存在")

    @router.get("/science")
    def summary(world_id: str):
        with database.read() as c:
            check(c, world_id)
            if table_exists(c):
                principle = c.execute(
                    "SELECT * FROM principle_sessions WHERE world_id=? ORDER BY rowid DESC LIMIT 1",
                    (world_id,),
                ).fetchone()
                if principle:
                    calls = c.execute(
                        "SELECT * FROM principle_calls WHERE session_id=?", (principle["id"],)
                    ).fetchall()
                    manifest = json.loads(principle["manifest_json"])
                    cumulative = None
                    if manifest.get("budget_lineage"):
                        from world_engine.principle_budget import usage

                        cumulative = usage(
                            manifest["budget_lineage"],
                            expected_run=(str(database.path.resolve()), principle["id"]),
                        )
                    return {
                        "mode": "principle_review",
                        "session": {
                            k: principle[k]
                            for k in (
                                "id",
                                "world_id",
                                "status",
                                "created_at",
                                "stop_reason",
                                "target_count",
                            )
                        },
                        "usage": {
                            "calls": len(calls),
                            "max_calls": principle["max_calls"],
                            "accounted_tokens": sum(r["accounted_tokens"] or 0 for r in calls),
                            "token_limit": principle["token_limit"],
                            "reserved_tokens": sum(
                                r["reserved_tokens"] for r in calls if r["status"] == "reserved"
                            ),
                            "stop_reason": principle["stop_reason"],
                            "cumulative": cumulative,
                        },
                        "technology_count": len(
                            PrincipleTechnologyService.graph(c, world_id)["nodes"]
                        ),
                        "impact_count": c.execute(
                            "SELECT count(*) FROM principle_impacts WHERE world_id=?", (world_id,)
                        ).fetchone()[0],
                    }
            s = c.execute("SELECT * FROM science_sessions WHERE world_id=?", (world_id,)).fetchone()
            if not s:
                return {"session": None, "usage": {"calls": 0}, "technology_count": 0}
            # 仅复用只读聚合函数，不运行构造器的崩溃恢复。
            view = object.__new__(ScienceRunner)
            view.session_id = s["id"]
            return {
                "session": {
                    k: s[k]
                    for k in ("id", "world_id", "status", "created_at", "stopped_at", "stop_reason")
                },
                "usage": view._usage(c),
                "technology_count": len(TechnologyService.list(c, world_id)),
            }

    @router.get("/technologies")
    def technologies(world_id: str):
        with database.read() as c:
            check(c, world_id)
            return (
                TechnologyService.list(c, world_id)
                + PrincipleTechnologyService.graph(c, world_id)["nodes"]
            )

    @router.get("/technologies/graph")
    def graph(world_id: str):
        with database.read() as c:
            check(c, world_id)
            legacy = TechnologyService.graph(c, world_id)
            principle = PrincipleTechnologyService.graph(c, world_id)
            return {
                "nodes": legacy["nodes"] + principle["nodes"],
                "edges": legacy["edges"] + principle["edges"],
            }

    @router.get("/technologies/{technology_id}/process")
    def process(world_id: str, technology_id: str):
        with database.read() as c:
            check(c, world_id)
            try:
                if (
                    table_exists(c)
                    and c.execute(
                        "SELECT 1 FROM principle_technologies WHERE id=? AND world_id=?",
                        (technology_id, world_id),
                    ).fetchone()
                ):
                    return PrincipleTechnologyService.process(c, world_id, technology_id)
                return technology_process(c, world_id, technology_id)
            except ValueError as exc:
                raise HTTPException(404, str(exc)) from exc

    @router.get("/science/tree")
    def tree(world_id: str):
        with database.read() as c:
            check(c, world_id)
            return technology_tree(c, world_id)

    @router.get("/science/magic")
    def magic_overview(world_id: str):
        from world_engine.magic_research import PUBLIC_RULES
        from world_engine.principle_inventions import has_table

        with database.read() as c:
            check(c, world_id)
            return {
                "rules": PUBLIC_RULES,
                "basis": "public_world_rules",
                "registered_patterns": c.execute(
                    "SELECT count(*) FROM magic_patterns WHERE world_id=?", (world_id,)
                ).fetchone()[0]
                if has_table(c, "magic_patterns")
                else 0,
                "registered_devices": c.execute(
                    "SELECT count(*) FROM magic_energy_devices WHERE world_id=?", (world_id,)
                ).fetchone()[0]
                if has_table(c, "magic_energy_devices")
                else 0,
                "notice": "世界规则与研究领域不计作已研发科技；魔法分支由NPC实际提案和评审产生。",
            }

    @router.get("/science/projects")
    def research_projects(world_id: str):
        from world_engine.principle_cycles import projects

        with database.read() as c:
            check(c, world_id)
            return projects(c, world_id) + physical_projects(c, world_id)

    @router.get("/science/mechanisms")
    def phenomenon_mechanisms(world_id: str):
        from world_engine.phenomena_rules import REGISTRY

        with database.read() as c:
            check(c, world_id)
            return {
                "basis": "world_rules",
                "mechanisms": REGISTRY.public_capabilities(),
                "notice": "这些是试验执行能力，不计作科技或发现；未覆盖机制不产生猜测结果。",
            }

    @router.get("/science/phenomena")
    def phenomenon_observations(world_id: str, observer_id: str):
        from world_engine.phenomena import observations
        from world_engine.repository import from_iso

        with database.read() as c:
            check(c, world_id)
            if not c.execute(
                "SELECT 1 FROM characters WHERE id=? AND world_id=?", (observer_id, world_id)
            ).fetchone():
                raise HTTPException(404, "观察者不属于这个世界")
            at = from_iso(
                c.execute(
                    "SELECT w.current_time FROM worlds w WHERE w.id=?", (world_id,)
                ).fetchone()[0]
            )
            return observations(c, observer_id, at)

    @router.get("/science/candidates")
    def candidates(world_id: str):
        with database.read() as c:
            check(c, world_id)
            legacy = [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM technology_candidates WHERE world_id=? "
                    "ORDER BY created_at,rowid",
                    (world_id,),
                )
            ]
            if table_exists(c):
                legacy += [
                    {
                        "id": r["id"],
                        "name": json.loads(r["spec_json"])["name"],
                        "problem": json.loads(r["spec_json"])["problem"],
                        "status": r["status"],
                        "gaps_json": r["review_json"] or "等待原理评审",
                    }
                    for r in c.execute(
                        "SELECT * FROM principle_proposals WHERE world_id=? "
                        "AND status!='researched' ORDER BY rowid",
                        (world_id,),
                    )
                ]
            return legacy

    @router.get("/science/impacts")
    def impacts(world_id: str):
        with database.read() as c:
            check(c, world_id)
            actual = physical_impacts(c, world_id)
            if not table_exists(c):
                return actual
            from world_engine.principle_inventions import has_table

            modern = has_table(c, "principle_inventions")
            title = "COALESCE(json_extract(v.spec_json,'$.name'),t.name)" if modern else "t.name"
            join = "LEFT JOIN principle_inventions v ON v.id=i.invention_id " if modern else ""
            principle = [
                dict(r)
                for r in c.execute(
                    f"SELECT i.*,{title} technology_name,a.name actor_name, "
                    "CASE WHEN i.source_kind='life_activity' THEN l.status "
                    "WHEN m.quantity_before>m.quantity_after AND m.satiety_after>m.satiety_before "
                    "THEN 'completed' ELSE 'unproven' END "
                    "source_status FROM principle_impacts i "
                    f"JOIN principle_technologies t ON t.id=i.technology_id {join}"
                    "JOIN characters a ON a.id=i.actor_id "
                    "LEFT JOIN character_life_activities l ON l.id=i.source_id "
                    "AND i.source_kind='life_activity' LEFT JOIN principle_meals m "
                    "ON m.id=i.source_id AND i.source_kind='meal' WHERE i.world_id=? "
                    "ORDER BY i.occurred_at DESC,i.rowid DESC",
                    (world_id,),
                )
            ]
            from world_engine.repository import from_iso

            return sorted(principle + actual, key=lambda i: from_iso(i["occurred_at"]), reverse=True)

    return router
