"""通用活动、成果领取与主动等候的接口。"""

import json
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from world_engine.action_checks import CheckService
from world_engine.actions import ActionService
from world_engine.activity_tasks import ActivityRecipeSpec, TaskService
from world_engine.api_deps import require_player_and_time
from world_engine.domain import ActionProposal, ActionType
from world_engine.inventory import InventoryError, InventoryService
from world_engine.life import LifeActivityError, LifeActivityService, LifeSceneService
from world_engine.registration import ElementRegistrationSubmit, WorldElementRegistry
from world_engine.repository import WorldNotFoundError, to_iso, utc_now


class TaskStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipe_id: str | None = None
    target_item_id: str | None = None
    map_record_id: str | None = None
    request_id: str | None = Field(
        default=None, min_length=8, max_length=100, pattern=r"^[A-Za-z0-9._:-]+$"
    )


class WaitAdvance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0, strict=True)
    request_id: str = Field(min_length=8, max_length=100, pattern=r"^[A-Za-z0-9._:-]+$")


class ClaimOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quantity: int = Field(default=1, ge=1, le=10000, strict=True)


class RecipeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=8, max_length=100, pattern=r"^[A-Za-z0-9._:-]+$")
    recipe: ActivityRecipeSpec


def build_task_router(database, engine):
    router = APIRouter(prefix="/api/worlds/{world_id}", tags=["活动过程"])

    def player_and_time(c, wid):
        return require_player_and_time(c, wid)

    @router.get("/activity-recipes/catalog")
    def catalog(world_id: str):
        with database.read() as c:
            player, _ = player_and_time(c, world_id)
            return {
                "item_types": [
                    dict(row)
                    for row in c.execute("SELECT id,name,category FROM item_types ORDER BY name")
                ],
                "available_recipes": TaskService.available(c, player),
            }

    @router.post("/activity-recipes", status_code=201)
    def propose_recipe(world_id: str, payload: RecipeRequest):
        with database.write() as c:
            _, at = player_and_time(c, world_id)
            key = f"recipe:{payload.idempotency_key}"
            existing = c.execute(
                "SELECT source_event_id FROM element_registration_requests WHERE "
                "world_id=? AND idempotency_key=?",
                (world_id, key),
            ).fetchone()
            source = (
                existing["source_event_id"]
                if existing
                else ActionService._record_event(
                    c,
                    world_id=world_id,
                    tick_id=key,
                    occurred_at=at,
                    event_type="world.activity_recipe_proposed",
                    actor_id=None,
                    target_id=None,
                    location_id=None,
                    summary=f"编年者提交了{payload.recipe.name}的活动规则待审核。",
                    payload={},
                )
            )
            try:
                result = WorldElementRegistry().submit(
                    c,
                    world_id=world_id,
                    request=ElementRegistrationSubmit(
                        source_event_id=source, idempotency_key=key, payload=payload.recipe
                    ),
                    auto_apply=False,
                )
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            return {
                "summary": "配方已提交，请核对规则后确认登记。",
                "registration": result.model_dump(mode="json"),
            }

    @router.post("/player/life/tasks", status_code=201)
    def start(world_id: str, payload: TaskStart):
        with database.write() as c:
            player, at = player_and_time(c, world_id)
            canonical = json.dumps(payload.model_dump(exclude={"request_id"}), sort_keys=True)
            if payload.request_id:
                cached = c.execute(
                    "SELECT * FROM activity_start_requests WHERE world_id=? AND request_id=?",
                    (world_id, payload.request_id),
                ).fetchone()
                if cached:
                    if cached["payload_json"] != canonical:
                        raise HTTPException(409, "请求标识已用于不同的活动")
                    return json.loads(cached["response_json"])
            proposal = ActionProposal(
                actor_id=player["id"],
                action=ActionType.ACTIVITY,
                reason="开始已登记的活动",
                metadata=payload.model_dump(exclude={"request_id"}),
            )
            result = ActionService().execute(
                c, world_id=world_id, tick_id=str(uuid4()), occurred_at=at, proposal=proposal
            )
            if not result.accepted:
                raise HTTPException(409, result.rejection_reason)
            c.execute(
                "UPDATE worlds SET version=version+1,updated_at=? WHERE id=?",
                (to_iso(utc_now()), world_id),
            )
            response = {
                "summary": result.summary,
                "activity_id": proposal.metadata["life_activity_id"],
            }
            if payload.request_id:
                c.execute(
                    "INSERT INTO activity_start_requests VALUES (?,?,?,?)",
                    (
                        world_id,
                        payload.request_id,
                        canonical,
                        json.dumps(response, ensure_ascii=False),
                    ),
                )
            return response

    @router.post("/player/life/tasks/preview")
    def preview(world_id: str, payload: TaskStart):
        try:
            with database.read() as c:
                player, at = player_and_time(c, world_id)
                spec, _, _, _, plan = TaskService.prepare(
                    c,
                    player,
                    at,
                    payload.recipe_id,
                    payload.target_item_id,
                    payload.map_record_id,
                )
                return {
                    "name": spec["name"],
                    "duration_minutes": spec["duration_minutes"],
                    "energy_cost": spec["energy_cost"],
                    "check": CheckService.public_plan(plan) if plan else None,
                }
        except (LifeActivityError, InventoryError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get("/player/life/checks")
    def checks(world_id: str):
        with database.read() as c:
            player, _ = player_and_time(c, world_id)
            return CheckService.list_for(c, world_id, player["id"])

    @router.post("/player/life/activities/{activity_id}/advance")
    def advance(world_id: str, activity_id: str, payload: WaitAdvance):
        try:
            result = engine.heartbeat(
                world_id, activity_skip={"activity_id": activity_id, **payload.model_dump()}
            )
        except WorldNotFoundError as exc:
            raise HTTPException(404, "世界不存在") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        summary = (
            f"你等候了{result.world_delta_seconds / 60:.1f}个世界分钟，"
            f"停在：{result.time_skip_reason}。"
        )
        if result.adjudication_error:
            summary += " 世界裁判尚未完成，继续等候前需先处理。"
        return {"summary": summary, "heartbeat": result.model_dump(mode="json")}

    @router.post("/player/life/outputs/{item_id}/claim")
    def claim(world_id: str, item_id: str, payload: ClaimOutput):
        try:
            with database.write() as c:
                player, at = player_and_time(c, world_id)
                LifeActivityService.assert_available(c, player["id"])
                item = c.execute(
                    "SELECT i.*,t.name,t.stack_limit,t.slot_size,a.location_id,d.room_id "
                    "FROM item_instances i "
                    "JOIN item_types t ON t.id=i.item_type_id JOIN "
                    "character_life_activities a ON a.id=i.container_id "
                    "JOIN activity_task_details d ON d.activity_id=a.id WHERE i.id=? AND "
                    "i.world_id=? "
                    "AND i.owner_character_id=? AND i.container_type='activity_output'",
                    (item_id, world_id, player["id"]),
                ).fetchone()
                if item is None:
                    raise HTTPException(404, "成果或退还物品已不在领取列表中")
                location = LifeSceneService.ground_location(c, player)
                if (
                    location is None
                    or location["id"] != item["location_id"]
                    or player["current_room_id"] != item["room_id"]
                ):
                    raise LifeActivityError("请返回原活动地点和房间领取")
                before = InventoryService.snapshot(c, world_id)
                InventoryService.transfer(c, item, player["id"], payload.quantity)
                event_id = ActionService._record_event(
                    c,
                    world_id=world_id,
                    tick_id=str(uuid4()),
                    occurred_at=at,
                    event_type="action.activity_claimed",
                    actor_id=player["id"],
                    target_id=None,
                    location_id=location["id"],
                    summary=f"你领取了{payload.quantity}份{item['name']}。",
                    payload={"item_id": item_id},
                )
                InventoryService.audit(c, world_id, event_id, before)
                c.execute(
                    "UPDATE worlds SET version=version+1,updated_at=? WHERE id=?",
                    (to_iso(utc_now()), world_id),
                )
                return {"summary": f"你领取了{payload.quantity}份{item['name']}。"}
        except (InventoryError, LifeActivityError) as exc:
            raise HTTPException(409, str(exc)) from exc

    return router
