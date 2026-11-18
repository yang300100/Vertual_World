"""哥哥可以在场邀请同行，也能随时结束共同旅程。"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from world_engine.api_deps import require_world_time
from world_engine.companions import CompanionService
from world_engine.companion_proposals import CompanionProposalService
from world_engine.repository import to_iso, utc_now


class CompanionInvite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipient_id: str = Field(min_length=1, max_length=100)
    minutes: int = Field(default=60, ge=5, le=240, strict=True)


def build_companion_router(database):
    router = APIRouter(prefix="/api/worlds/{world_id}", tags=["同行"])

    def player_at(c, wid):
        at = require_world_time(c, wid)
        player = c.execute(
            "SELECT * FROM characters WHERE world_id=? AND is_player=1 AND is_pov=1", (wid,)
        ).fetchone()
        if player is None:
            raise HTTPException(404, "当前世界尚无玩家")
        return player, at

    @router.get("/companions")
    def list_companions(world_id: str):
        with database.read() as c:
            player, _ = player_at(c, world_id)
            return [dict(row) for row in CompanionService.active(c, player["id"])]

    @router.get("/companion-proposals")
    def companion_proposals(world_id: str, character_id: str | None = None):
        with database.read() as c:
            player, at = player_at(c, world_id)
            if character_id and not c.execute(
                "SELECT 1 FROM characters WHERE id=? AND world_id=? AND is_player=0",(character_id,world_id)
            ).fetchone():
                raise HTTPException(404,"人物不属于当前世界")
            return CompanionProposalService.rows(c,world_id,player["id"],at,character_id)

    @router.post("/companions")
    def invite_companion(world_id: str, payload: CompanionInvite):
        with database.write() as c:
            player, at = player_at(c, world_id)
            npc = c.execute(
                "SELECT * FROM characters WHERE id=? AND world_id=?",
                (payload.recipient_id, world_id),
            ).fetchone()
            if npc is None:
                raise HTTPException(404, "人物不属于当前世界")
            try:
                CompanionService.maintain(c, world_id, at)
                result = CompanionService.invite(c, world_id, player, npc, at, payload.minutes)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            c.execute(
                "UPDATE worlds SET version=version+1,updated_at=? WHERE id=?",
                (to_iso(utc_now()), world_id),
            )
            return result

    @router.delete("/companions/{session_id}")
    def end_companion(world_id: str, session_id: str):
        with database.write() as c:
            player, at = player_at(c, world_id)
            session = c.execute(
                "SELECT * FROM companion_sessions WHERE id=? AND world_id=? AND leader_id=?",
                (session_id, world_id, player["id"]),
            ).fetchone()
            if session is None:
                raise HTTPException(404, "同行记录不存在")
            CompanionService.end(c, session, at, "你主动结束了同行，双方保留已经走过的路程")
            c.execute(
                "UPDATE worlds SET version=version+1,updated_at=? WHERE id=?",
                (to_iso(utc_now()), world_id),
            )
            return dict(
                c.execute("SELECT * FROM companion_sessions WHERE id=?", (session_id,)).fetchone()
            )

    return router
