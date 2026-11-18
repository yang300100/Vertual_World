"""哥哥，本人跨层入口只提交自己的选择，NPC 选择来自其本地对话。"""

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from world_engine.api_deps import require_world_time
from world_engine.cross_layer import CrossLayerPassageService


class PassageProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    npc_id: str = Field(min_length=1, max_length=100)
    wonder_id: str = Field(min_length=1, max_length=100)
    direction: Literal["enter", "exit"]
    idempotency_key: str = Field(min_length=1, max_length=128)


class PassagePersonalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PassageApproachRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=128)


def build_cross_layer_router(database):
    router = APIRouter(prefix="/api/worlds/{world_id}/cross-layer", tags=["共同跨层"])

    def player_at(c, world_id):
        at = require_world_time(c, world_id)
        player = c.execute(
            "SELECT id FROM characters WHERE world_id=? AND is_player=1 AND is_pov=1",
            (world_id,),
        ).fetchone()
        if player is None:
            raise HTTPException(404, "当前世界没有玩家视角")
        return player["id"], at

    @router.get("/options")
    def options(world_id: str):
        with database.read() as c:
            player_id, at = player_at(c, world_id)
            return CrossLayerPassageService.options(c, world_id, player_id, at)

    @router.get("")
    def rows(world_id: str):
        with database.read() as c:
            player_id, at = player_at(c, world_id)
            return {"passages": CrossLayerPassageService.rows(c, world_id, player_id, at)}

    @router.post("/proposals")
    def propose(world_id: str, request: PassageProposalRequest):
        try:
            with database.write() as c:
                player_id, at = player_at(c, world_id)
                return CrossLayerPassageService.propose(c, world_id, player_id, request.npc_id,
                    request.wonder_id, request.direction, request.idempotency_key, at)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/{proposal_id}/traverse")
    def player_traverse(world_id: str, proposal_id: str, request: PassagePersonalRequest | None = None):
        try:
            with database.write() as c:
                player_id, at = player_at(c, world_id)
                return CrossLayerPassageService.player_traverse(c, world_id, player_id, proposal_id, at)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/{proposal_id}/approach")
    def player_approach(world_id: str, proposal_id: str, request: PassageApproachRequest):
        try:
            with database.write() as c:
                player_id, at = player_at(c, world_id)
                return CrossLayerPassageService.player_approach(
                    c, world_id, player_id, proposal_id, request.idempotency_key, at)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/{proposal_id}/cancel")
    def cancel(world_id: str, proposal_id: str, request: PassagePersonalRequest | None = None):
        try:
            with database.write() as c:
                player_id, at = player_at(c, world_id)
                return CrossLayerPassageService.cancel(c, world_id, player_id, proposal_id, at)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    return router
