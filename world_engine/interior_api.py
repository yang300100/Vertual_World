"""室内空间的玩家交互及编年者布局提案入口。"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from world_engine.actions import ActionService
from world_engine.interiors import InteriorError, InteriorRoomSpec, InteriorService
from world_engine.inventory import InventoryError
from world_engine.life import LifeActivityError
from world_engine.registration import ElementRegistrationSubmit, WorldElementRegistry
from world_engine.repository import from_iso


class DoorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["open", "close", "lock", "unlock", "enter", "exit"]


class FixtureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["open", "close", "lock", "unlock", "sit", "stand"]


class KnockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=8, max_length=100, pattern=r"^[A-Za-z0-9._:-]+$")


class VisitResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accept: bool = Field(strict=True)


class StorageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: str
    quantity: int = Field(ge=1, le=10000, strict=True)
    operation: Literal["deposit", "withdraw"]


class AccessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    character_id: str
    allow: bool = Field(strict=True)


class LayoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=8, max_length=100, pattern=r"^[A-Za-z0-9._:-]+$")
    room: InteriorRoomSpec


def build_interior_router(database) -> APIRouter:
    router = APIRouter(prefix="/api/worlds/{world_id}", tags=["室内空间"])

    def world_time(connection, world_id):
        row = connection.execute(
            'SELECT "current_time" FROM worlds WHERE id=?', (world_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "世界不存在")
        return from_iso(row["current_time"])

    def apply(world_id, function):
        try:
            with database.write() as connection:
                at = world_time(connection, world_id)
                player = connection.execute(
                    "SELECT * FROM characters WHERE world_id=? AND is_player=1",
                    (world_id,),
                ).fetchone()
                if player is None:
                    raise HTTPException(404, "当前世界尚无玩家")
                if player["health"] <= 0:
                    raise HTTPException(409, "当前身体状态无法进行室内操作")
                return function(connection, player, at)
        except (InteriorError, InventoryError, LifeActivityError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/player/life/doors/{room_id}")
    def door(world_id: str, room_id: str, payload: DoorRequest):
        return apply(
            world_id,
            lambda connection, player, at: InteriorService.door(
                connection,
                player,
                room_id,
                payload.operation,
                at,
            ),
        )

    @router.post("/player/life/fixtures/{fixture_id}")
    def fixture(world_id: str, fixture_id: str, payload: FixtureRequest):
        return apply(
            world_id,
            lambda connection, player, at: InteriorService.operate_fixture(
                connection,
                player,
                fixture_id,
                payload.operation,
                at,
            ),
        )

    @router.post("/player/life/doors/{room_id}/knock")
    def knock(world_id: str, room_id: str, payload: KnockRequest):
        from world_engine.visits import VisitService

        return apply(world_id, lambda c, player, at: VisitService.knock(c, player, room_id, payload.request_id, at))

    @router.post("/player/life/visits/{visit_id}/respond")
    def respond(world_id: str, visit_id: str, payload: VisitResponse):
        from world_engine.visits import VisitService

        return apply(world_id, lambda c, player, at: VisitService.respond(c, player, visit_id, payload.accept, at))

    @router.post("/player/life/visits/{visit_id}/withdraw")
    def withdraw_visit(world_id: str, visit_id: str):
        from world_engine.visits import VisitService

        return apply(world_id, lambda c, player, at: VisitService.withdraw(c, player, visit_id, at))

    @router.post("/player/life/fixtures/{fixture_id}/items")
    def transfer(world_id: str, fixture_id: str, payload: StorageRequest):
        return apply(
            world_id,
            lambda connection, player, at: InteriorService.transfer(
                connection,
                player,
                fixture_id,
                payload.item_id,
                payload.quantity,
                payload.operation == "deposit",
                at,
            ),
        )

    @router.post("/player/life/rooms/{room_id}/access")
    def access(world_id: str, room_id: str, payload: AccessRequest):
        def update(connection, player, at):
            room = InteriorService.room(connection, world_id, room_id)
            if room["owner_character_id"] != player["id"]:
                raise HTTPException(403, "只有房间所有者可以调整许可")
            if payload.character_id == player["id"]:
                raise HTTPException(409, "所有者无需为自己设置许可")
            target = connection.execute(
                "SELECT name FROM characters WHERE id=? AND world_id=?",
                (payload.character_id, world_id),
            ).fetchone()
            if target is None:
                raise HTTPException(404, "被授权人物不存在于当前世界")
            if payload.allow:
                connection.execute(
                    "INSERT OR IGNORE INTO life_room_access VALUES (?,?)",
                    (room_id, payload.character_id),
                )
            else:
                connection.execute(
                    "DELETE FROM life_room_access WHERE room_id=? AND character_id=?",
                    (room_id, payload.character_id),
                )
            verb = "授予" if payload.allow else "撤回"
            summary = f"你{verb}了{target['name']}使用{room['name']}及储物容器的许可。"
            event = InteriorService.record(
                connection,
                player,
                at,
                "access",
                summary,
                {"room_id": room_id, "character_id": payload.character_id, "allow": payload.allow},
            )
            return {"summary": summary, "event_id": event}

        return apply(world_id, update)

    @router.post("/interior-layouts", status_code=201)
    def propose_layout(world_id: str, payload: LayoutRequest):
        """编年者核实已有空间后提交，复用元素注册页的确认/拒绝；不开放给人物造物。"""
        with database.write() as connection:
            at = world_time(connection, world_id)
            key = f"interior:{payload.idempotency_key}"
            existing = connection.execute(
                "SELECT source_event_id FROM element_registration_requests WHERE "
                "world_id=? AND idempotency_key=?",
                (world_id, key),
            ).fetchone()
            source = (
                existing["source_event_id"]
                if existing
                else ActionService._record_event(
                    connection,
                    world_id=world_id,
                    tick_id=key,
                    occurred_at=at,
                    event_type="world.interior_layout_proposed",
                    actor_id=None,
                    target_id=None,
                    location_id=None,
                    summary=f"编年者提交了{payload.room.name}的室内布局待审核。",
                    payload={"name": payload.room.name},
                )
            )
            try:
                result = WorldElementRegistry().submit(
                    connection,
                    world_id=world_id,
                    request=ElementRegistrationSubmit(
                        source_event_id=source,
                        requested_by_character_id=None,
                        idempotency_key=key,
                        payload=payload.room,
                    ),
                    auto_apply=False,
                )
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            return {
                "summary": "室内布局已进入元素注册，请核对完整内容后确认登记。",
                "registration": result.model_dump(mode="json"),
            }

    return router
