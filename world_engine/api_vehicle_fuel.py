"""哥哥，燃料装卸只接受当前玩家本人明确提交的实际物品。"""

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from world_engine.api_deps import require_world_time
from world_engine.vehicle_fuel_inventory import VehicleFuelInventoryService


class FuelTransfer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["load", "unload"]
    vehicle_id: str = Field(min_length=1, max_length=100)
    item_id: str = Field(min_length=1, max_length=100)
    quantity: int = Field(strict=True, ge=1, le=1000000000)
    idempotency_key: str = Field(min_length=1, max_length=128)


def build_vehicle_fuel_router(database):
    router = APIRouter(prefix="/api/worlds/{world_id}/vehicle-fuel", tags=["载具燃料"])

    def player_at(c, world_id):
        at = require_world_time(c, world_id)
        player = c.execute("SELECT id FROM characters WHERE world_id=? AND is_player=1 AND is_pov=1", (world_id,)).fetchone()
        if player is None:
            raise HTTPException(404, "当前世界尚无玩家")
        return player["id"], at

    @router.get("/options")
    def options(world_id: str):
        with database.read() as c:
            actor_id, at = player_at(c, world_id)
            return {"vehicles": VehicleFuelInventoryService.options(c, world_id=world_id, actor_id=actor_id, at=at)}

    @router.post("/transfer")
    def transfer(world_id: str, payload: FuelTransfer):
        try:
            with database.write() as c:
                actor_id, at = player_at(c, world_id)
                method = VehicleFuelInventoryService.load if payload.operation == "load" else VehicleFuelInventoryService.unload
                return method(c, world_id=world_id, actor_id=actor_id, vehicle_id=payload.vehicle_id,
                              item_id=payload.item_id, quantity=payload.quantity,
                              idempotency_key=payload.idempotency_key, at=at)
        except (ValueError, LookupError) as exc:
            raise HTTPException(409, str(exc)) from exc

    return router
