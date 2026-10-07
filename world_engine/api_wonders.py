"""哥哥可以直接从地图访问奇观，也可以免费检查拍摄场景。"""

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from world_engine.game_scene import GameSceneService
from world_engine.photos import PhotoCaptureRequest
from world_engine.repository import from_iso
from world_engine.wonders import WonderService


class TraverseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    direction: Literal["enter", "exit"]


def build_wonders_router(database, photo_service):
    router = APIRouter(prefix="/api/worlds/{world_id}", tags=["奇观"])

    @router.get("/wonders")
    def wonders(world_id: str):
        with database.read() as c:
            if not c.execute("SELECT 1 FROM worlds WHERE id=?", (world_id,)).fetchone():
                raise HTTPException(404, "世界不存在")
            return WonderService.view(c, world_id)

    @router.post("/wonders/{wonder_id}/traverse")
    def traverse(world_id: str, wonder_id: str, request: TraverseRequest):
        with database.write() as c:
            player = c.execute(
                "SELECT id FROM characters WHERE world_id=? AND is_player=1 AND is_pov=1",
                (world_id,),
            ).fetchone()
            if not player:
                raise HTTPException(404, "当前世界没有玩家视角")
            at = from_iso(
                c.execute("SELECT w.current_time FROM worlds w WHERE id=?", (world_id,)).fetchone()[
                    0
                ]
            )
            try:
                result = WonderService.traverse(
                    c, world_id, player["id"], wonder_id, request.direction, at
                )
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            result["scene"] = GameSceneService.snapshot(c, world_id)
            return result

    @router.post("/photos/preview")
    def preview(world_id: str, request: PhotoCaptureRequest):
        with database.read() as c:
            try:
                prepared = photo_service.prepare_capture(c, world_id=world_id, request=request)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            return {
                "world_time": prepared.world_time,
                "context": prepared.context,
                "prompt": prepared.prompt,
                "generation_requested": False,
            }

    return router
