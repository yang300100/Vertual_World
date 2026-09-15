"""生活场景接口：读取真实物品、开始持续活动及主动结束。"""

from __future__ import annotations

import json
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from world_engine.actions import ActionService
from world_engine.domain import ActionProposal, ActionType
from world_engine.food import FoodService
from world_engine.inventory import InventoryError, InventoryService
from world_engine.life import LifeActivityError, LifeActivityService, LifeSceneService
from world_engine.repository import from_iso, to_iso, utc_now


class LifeStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["rest", "wait"]
    duration_minutes: int = Field(ge=1, le=720, strict=True)


class SceneItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["pickup", "drop"]


class EatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=8, max_length=100)
    accept_spoiled: bool = Field(default=False, strict=True)


def build_life_router(database) -> APIRouter:
    router = APIRouter(prefix="/api/worlds/{world_id}/player", tags=["生活"])

    def player_and_time(connection, world_id):
        world = connection.execute(
            'SELECT "current_time" FROM worlds WHERE id=?',
            (world_id,),
        ).fetchone()
        if world is None:
            raise HTTPException(404, "世界不存在")
        player = connection.execute(
            "SELECT * FROM characters WHERE world_id=? AND is_player=1",
            (world_id,),
        ).fetchone()
        if player is None:
            raise HTTPException(404, "当前世界尚无玩家")
        return player, from_iso(world["current_time"])

    def bump(connection, world_id):
        connection.execute(
            "UPDATE worlds SET version=version+1,updated_at=? WHERE id=?",
            (to_iso(utc_now()), world_id),
        )

    @router.get("/life")
    def scene(world_id: str):
        with database.read() as connection:
            player, at = player_and_time(connection, world_id)
            return LifeSceneService.scene(connection, player, at)

    @router.get("/life/items/{item_id}")
    def inspect_item(world_id: str, item_id: str):
        with database.read() as connection:
            player, at = player_and_time(connection, world_id)
            items = LifeSceneService.scene(connection, player, at)["items"]
            item = next((item for item in items if item["id"] == item_id), None)
            if item is None:
                raise HTTPException(404, "这件物品不在当前可查看范围内")
            return item

    @router.post("/life/items/{item_id}/eat")
    def eat_item(world_id: str, item_id: str, payload: EatRequest):
        try:
            with database.write() as c:
                player, at = player_and_time(c, world_id)
                canonical = json.dumps({"item_id": item_id, "accept_spoiled": payload.accept_spoiled}, sort_keys=True)
                prior = c.execute("SELECT * FROM food_eat_requests WHERE world_id=? AND request_id=?",
                                  (world_id, payload.request_id)).fetchone()
                if prior:
                    if prior["payload_json"] != canonical:
                        raise HTTPException(409, "请求标识已用于另一次进食")
                    return json.loads(prior["response_json"])
                LifeActivityService.assert_available(c, player["id"])
                if player["health"] <= 0 or c.execute("SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'", (player["id"],)).fetchone():
                    raise HTTPException(409, "请先结束行程并确认身体状态")
                item = c.execute(
                    "SELECT i.*,t.name,p.nutrition FROM item_instances i JOIN item_types t ON t.id=i.item_type_id "
                    "JOIN world_item_profiles p ON p.item_type_id=i.item_type_id WHERE i.id=? AND i.world_id=? "
                    "AND p.world_id=? AND p.nutrition>0 AND i.quantity>0", (item_id, world_id, world_id)
                ).fetchone()
                if not item:
                    raise HTTPException(409, "这件食物已不存在或没有可食用规则")
                before = InventoryService.snapshot(c, world_id)
                effect = FoodService.eat(c, player, item, at, accept_spoiled=payload.accept_spoiled)
                summary = f"你吃了一份{item['name']}，恢复{effect['nutrition']}点饱食。"
                if effect["spoiled"]:
                    summary += f"变质食物造成6世界小时的肠胃不适，消耗{effect['energy_loss']}点精力，期间新检定条件修正-10。"
                event = ActionService._record_event(c, world_id=world_id, tick_id=payload.request_id,
                    occurred_at=at, event_type="action.eat", actor_id=player["id"], target_id=None,
                    location_id=player["current_location_id"] or player["location_id"], summary=summary,
                    payload={"item_id": item_id, "food_effect": effect})
                if effect["spoiled"]:
                    FoodService.record_discomfort(c, player, event, at)
                InventoryService.audit(c, world_id, event, before)
                ActionService._record_memory(c,world_id=world_id,character_id=player["id"],event_id=event,
                    memory_type="experienced",summary=summary,importance=2)
                bump(c, world_id)
                response = {"summary": summary, "event_id": event, "effect": effect}
                c.execute("INSERT INTO food_eat_requests VALUES (?,?,?,?)",
                          (world_id, payload.request_id, canonical, json.dumps(response, ensure_ascii=False)))
                return response
        except (LifeActivityError, InventoryError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/life/items/{item_id}")
    def interact_item(world_id: str, item_id: str, payload: SceneItemRequest):
        with database.write() as connection:
            player, at = player_and_time(connection, world_id)
            item = next(
                (
                    row
                    for row in LifeSceneService.accessible_items(connection, player)
                    if row["id"] == item_id
                ),
                None,
            )
            if item is None:
                raise HTTPException(404, "这件物品已经不在当前可交互范围内")
            expected = ("room_ground" if player["current_room_id"] else "location_ground") if (
                payload.operation == "pickup"
            ) else "character_inventory"
            if item["container_type"] != expected:
                raise HTTPException(409, "物品位置已经改变，请刷新后再试")
            if LifeSceneService.ground_location(connection, player) is None:
                raise HTTPException(409, "请先走近该地点，再拾取或放下物品")
            outcome = ActionService().execute(
                connection,
                world_id=world_id,
                tick_id=str(uuid4()),
                occurred_at=at,
                proposal=ActionProposal(
                    actor_id=player["id"],
                    action=ActionType.GATHER if payload.operation == "pickup" else ActionType.USE,
                    reason=f"{'拾取' if payload.operation == 'pickup' else '放下'}{item['name']}",
                    metadata={"item": item["name"], "item_id": item_id, "operation": "drop"},
                ),
            )
            if not outcome.accepted:
                raise HTTPException(409, outcome.rejection_reason)
            bump(connection, world_id)
            return {"summary": outcome.summary, "event_id": outcome.event_id}

    @router.post("/life/activities", status_code=201)
    def start_activity(world_id: str, payload: LifeStartRequest):
        with database.write() as connection:
            player, at = player_and_time(connection, world_id)
            outcome = ActionService().execute(
                connection,
                world_id=world_id,
                tick_id=str(uuid4()),
                occurred_at=at,
                proposal=ActionProposal(
                    actor_id=player["id"],
                    action=ActionType.REST if payload.kind == "rest" else ActionType.IDLE,
                    reason=f"开始{'休息' if payload.kind == 'rest' else '等待'}",
                    metadata={
                        "life_activity": payload.kind,
                        "duration_minutes": payload.duration_minutes,
                    },
                ),
            )
            if not outcome.accepted:
                raise HTTPException(409, outcome.rejection_reason)
            bump(connection, world_id)
            active = LifeActivityService.running(connection, player["id"])
            return {"summary": outcome.summary, "activity": LifeActivityService.view(active, at)}

    @router.post("/life/activities/{activity_id}/stop")
    def stop_activity(world_id: str, activity_id: str):
        with database.write() as connection:
            player, at = player_and_time(connection, world_id)
            row = connection.execute(
                "SELECT * FROM character_life_activities "
                "WHERE id=? AND world_id=? AND character_id=?",
                (activity_id, world_id, player["id"]),
            ).fetchone()
            if row is None:
                raise HTTPException(404, "这项活动不存在或不属于当前玩家")
            if row["status"] == "running":
                LifeActivityService.finish(
                    connection, row, at, "cancelled", "你主动结束了这项活动。"
                )
                bump(connection, world_id)
            row = connection.execute(
                "SELECT * FROM character_life_activities WHERE id=?",
                (activity_id,),
            ).fetchone()
            return {
                "summary": "这项活动已结束，已经经过的时间与恢复的精力会保留。",
                "activity": LifeActivityService.view(row, at),
            }

    return router
