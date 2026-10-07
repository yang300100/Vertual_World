"""哥哥，科研观察路由只读世界事实，不触发模型或恢复计账。"""

from fastapi import APIRouter, HTTPException

from world_engine.science_chronicle import technology_process
from world_engine.science_runner import ScienceRunner
from world_engine.technologies import TechnologyService


def build_science_router(database):
    router = APIRouter(prefix="/api/worlds/{world_id}", tags=["科研"])

    def check(c, world_id):
        if not c.execute("SELECT 1 FROM worlds WHERE id=?", (world_id,)).fetchone():
            raise HTTPException(404, "世界不存在")

    @router.get("/science")
    def summary(world_id: str):
        with database.read() as c:
            check(c, world_id)
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
            return TechnologyService.list(c, world_id)

    @router.get("/technologies/graph")
    def graph(world_id: str):
        with database.read() as c:
            check(c, world_id)
            return TechnologyService.graph(c, world_id)

    @router.get("/technologies/{technology_id}/process")
    def process(world_id: str, technology_id: str):
        with database.read() as c:
            check(c, world_id)
            try:
                return technology_process(c, world_id, technology_id)
            except ValueError as exc:
                raise HTTPException(404, str(exc)) from exc

    @router.get("/science/candidates")
    def candidates(world_id: str):
        with database.read() as c:
            check(c, world_id)
            return [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM technology_candidates WHERE world_id=? "
                    "ORDER BY created_at,rowid",
                    (world_id,),
                )
            ]

    return router
