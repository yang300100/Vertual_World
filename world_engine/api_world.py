"""世界的时钟、推进、事件与待办接口。

这些路由原先内嵌在 `api.create_app` 里，与元素登记、玩家视角等域混在同一个
1600 行的闭包中。这里按「世界推进」整体搬出，行为与之完全一致（路径、方法、
响应模型、状态码与错误文案一字未改）。

`_world_time` 是 `api_deps.require_world_time_text` 的别名：它返回原始 ISO
文本，供需要原样比较世界时间的端点使用；同文件里返回 datetime 的
`require_world_time` 不能替代它。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from world_engine.api_deps import require_world_time_text as _world_time
from world_engine.domain import ClockUpdateResult, HeartbeatResult, TickResult
from world_engine.engine import ConcurrentWorldUpdateError
from world_engine.history import HistoryExportResult
from world_engine.repository import WorldNotFoundError, from_iso


class ClockUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    time_scale: float = Field(ge=0, le=10080)
    operator: str = Field(default="main_view", min_length=1, max_length=100)


class AdjudicationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trigger: str = Field(default="manual", pattern="^(manual|player_intervention)$")
    character_ids: list[str] | None = Field(default=None, max_length=10)


class OpportunityPublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issuer_id: str
    issuer_location_id: str
    objective_location_id: str
    condition: dict[str, object]
    reward_money: int = Field(ge=0)


class IncidentSiteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    location_id: str
    custodian_id: str
    item_id: str
    recipe_id: str
    resource_keys: list[str] = Field(min_length=1, max_length=20)
    damaged_condition: int = Field(ge=1, le=99)
    required_condition: int = Field(ge=2, le=100)


class IncidentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=2000)
    idempotency_key: str = Field(min_length=1, max_length=100)
    sites: list[IncidentSiteRequest] = Field(min_length=1, max_length=1000)


def build_world_router(database, engine, repository) -> APIRouter:
    """世界的时钟、推进、事件与待办接口。"""

    router = APIRouter(prefix="/api/worlds/{world_id}", tags=["世界推进"])

    @router.get("/incidents")
    def list_incidents(world_id: str) -> list[dict[str, object]]:
        from world_engine.incidents import IncidentService

        with database.read() as connection:
            _world_time(connection, world_id)
            return IncidentService.view(connection, world_id)

    @router.post("/incidents", status_code=201)
    def create_incident(world_id: str, payload: IncidentCreateRequest) -> dict[str, str]:
        from world_engine.incidents import IncidentService

        with database.write() as connection:
            try:
                at = from_iso(_world_time(connection, world_id))
                incident = IncidentService.create(
                    connection, world_id, payload.title, payload.description,
                    [site.model_dump() for site in payload.sites], at,
                    idempotency_key=payload.idempotency_key,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except LookupError as exc:
                raise HTTPException(status_code=404, detail="世界不存在") from exc
        return {"id": incident}

    @router.get("/opportunities")
    def list_opportunities(world_id: str) -> list[dict[str, object]]:
        with database.read() as connection:
            _world_time(connection, world_id)
            return [dict(row) for row in connection.execute(
                "SELECT * FROM npc_opportunities WHERE world_id=? "
                "ORDER BY created_world_time DESC LIMIT 100", (world_id,),
            )]

    @router.post("/opportunities", status_code=201)
    def publish_opportunity(world_id: str, payload: OpportunityPublishRequest) -> dict[str, str]:
        from world_engine.opportunities import OpportunityService

        with database.write() as connection:
            try:
                at = from_iso(_world_time(connection, world_id))
                opportunity_id = OpportunityService.publish(
                    connection, world_id, payload.issuer_id,
                    payload.issuer_location_id, payload.objective_location_id,
                    payload.condition, payload.reward_money, at,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except LookupError as exc:
                raise HTTPException(status_code=404, detail="世界不存在") from exc
        return {"id": opportunity_id}

    @router.get("/game-scene")
    def game_scene(world_id: str) -> dict[str, object]:
        from world_engine.game_scene import GameSceneService

        with database.read() as connection:
            try:
                return GameSceneService.snapshot(connection, world_id)
            except LookupError as exc:
                raise HTTPException(status_code=404, detail="世界不存在") from exc

    @router.get("/npc-agency/{character_id}")
    def npc_agency(world_id: str, character_id: str) -> dict[str, object]:
        from world_engine.npc_agency import NpcAgencyService

        with database.read() as connection:
            character = connection.execute(
                "SELECT id,name FROM characters WHERE id=? AND world_id=? AND is_player=0",
                (character_id, world_id),
            ).fetchone()
            if character is None:
                raise HTTPException(status_code=404, detail="人物不存在")
            goals = NpcAgencyService.view(connection, character_id)
            for goal in goals:
                adopted = connection.execute(
                    "SELECT id,payload_json FROM world_events WHERE world_id=? AND actor_id=? "
                    "AND event_type='agency.goal_adopted' "
                    "AND json_extract(payload_json,'$.goal_id')=? LIMIT 1",
                    (world_id, character_id, goal["id"]),
                ).fetchone()
                goal["adoption_event_id"] = adopted["id"] if adopted else None
                goal["trigger_state"] = json.loads(adopted["payload_json"]).get("trigger_state", {}) if adopted else {}
                completed = connection.execute(
                    "SELECT id FROM world_events WHERE world_id=? AND actor_id=? "
                    "AND event_type='agency.goal_completed' AND json_extract(payload_json,'$.goal_id')=?",
                    (world_id, character_id, goal["id"]),
                ).fetchone()
                goal["completion_event_id"] = completed["id"] if completed else None
                source = connection.execute(
                    "SELECT summary FROM world_events WHERE id=?", (goal["source_event_id"],),
                ).fetchone() if goal["source_event_id"] else None
                goal["source_summary"] = source["summary"] if source else None
                steps = [dict(row) for row in connection.execute(
                    "SELECT * FROM npc_agency_steps WHERE goal_id=? ORDER BY world_time,id",
                    (goal["id"],),
                )]
                for step in steps:
                    step["expected"] = json.loads(step["expected_json"])
                    step["observed"] = json.loads(step["observed_json"])
                    event = connection.execute(
                        "SELECT summary FROM world_events WHERE id=?", (step["event_id"],),
                    ).fetchone()
                    step["event_summary"] = event["summary"] if event else None
                    step["causes"] = [dict(row) for row in connection.execute(
                        "SELECT r.cause_event_id,r.relation,e.summary FROM event_causes r "
                        "JOIN world_events e ON e.id=r.cause_event_id WHERE r.event_id=?",
                        (step["event_id"],),
                    )]
                    effects = connection.execute(
                        "SELECT effect_type,status,payload_json FROM action_effects "
                        "WHERE source_event_id=?", (step["event_id"],),
                    ).fetchall()
                    step["effects"] = [{**dict(row), "payload": json.loads(row["payload_json"])}
                                       for row in effects]
                    result = connection.execute(
                        "SELECT l.result_event_id,l.result_status,e.summary "
                        "FROM action_execution_links l LEFT JOIN world_events e ON e.id=l.result_event_id "
                        "WHERE l.source_event_id=? LIMIT 1", (step["event_id"],),
                    ).fetchone()
                    step["result_event_id"] = result["result_event_id"] if result else None
                    step["result_summary"] = result["summary"] if result else None
                    step["execution_status"] = (result["result_status"] or "running") if result else step["status"]
                    step["result_causes"] = [dict(row) for row in connection.execute(
                        "SELECT r.cause_event_id,r.relation,e.summary FROM event_causes r "
                        "JOIN world_events e ON e.id=r.cause_event_id WHERE r.event_id=?",
                        (step["result_event_id"],),
                    )]
                    step["result_effects"] = [
                        {**dict(row), "payload": json.loads(row["payload_json"])}
                        for row in connection.execute(
                            "SELECT effect_type,status,payload_json FROM action_effects "
                            "WHERE source_event_id=?", (step["result_event_id"],),
                        )
                    ]
                goal["steps"] = steps
            return {"character": dict(character), "goals": goals}

    @router.post(
        "/tick",
        response_model=TickResult,
        deprecated=True,
    )
    def tick_world(world_id: str) -> TickResult:
        try:
            return engine.tick(world_id)
        except WorldNotFoundError as exc:
            raise HTTPException(status_code=404, detail="世界不存在") from exc
        except ConcurrentWorldUpdateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except sqlite3.OperationalError as exc:
            raise HTTPException(status_code=503, detail="世界正在由另一个进程结算") from exc

    @router.post(
        "/heartbeat",
        response_model=HeartbeatResult,
    )
    def heartbeat_world(world_id: str) -> HeartbeatResult:
        try:
            return engine.heartbeat(world_id)
        except WorldNotFoundError as exc:
            raise HTTPException(status_code=404, detail="世界不存在") from exc
        except sqlite3.OperationalError as exc:
            raise HTTPException(status_code=503, detail="世界正在由另一个进程更新") from exc

    @router.patch(
        "/clock",
        response_model=ClockUpdateResult,
    )
    def update_clock(world_id: str, payload: ClockUpdateRequest) -> ClockUpdateResult:
        try:
            return engine.set_time_scale(
                world_id,
                payload.time_scale,
                operator=payload.operator,
            )
        except WorldNotFoundError as exc:
            raise HTTPException(status_code=404, detail="世界不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post(
        "/adjudicate",
        response_model=TickResult,
    )
    def adjudicate_world(world_id: str, payload: AdjudicationRequest) -> TickResult:
        try:
            return engine.adjudicate(
                world_id,
                trigger=payload.trigger,
                character_ids=payload.character_ids,
            )
        except WorldNotFoundError as exc:
            raise HTTPException(status_code=404, detail="世界不存在") from exc
        except ConcurrentWorldUpdateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/events")
    def list_events(
        world_id: str,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        scope: Annotated[str, Query(pattern="^(all|chronicle|log)$")] = "all",
        participant_id: str | None = Query(default=None, min_length=1, max_length=100),
    ) -> list[dict[str, object]]:
        try:
            with database.read() as connection:
                return repository.list_events(
                    connection, world_id, limit, scope=scope, participant_id=participant_id
                )
        except WorldNotFoundError as exc:
            raise HTTPException(status_code=404, detail="世界不存在") from exc

    @router.post(
        "/history/sync",
        response_model=HistoryExportResult,
    )
    def sync_history(world_id: str) -> HistoryExportResult:
        try:
            return engine.sync_history(world_id)
        except WorldNotFoundError as exc:
            raise HTTPException(status_code=404, detail="世界不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.get("/adjudications")
    def list_adjudications(
        world_id: str,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> list[dict[str, object]]:
        try:
            with database.read() as connection:
                return repository.list_adjudication_runs(connection, world_id, limit)
        except WorldNotFoundError as exc:
            raise HTTPException(status_code=404, detail="世界不存在") from exc

    @router.get("/combat/encounters")
    def list_combat_encounters(
        world_id: str,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> list[dict[str, object]]:
        try:
            with database.read() as connection:
                return repository.list_combat_encounters(connection, world_id, limit)
        except WorldNotFoundError as exc:
            raise HTTPException(status_code=404, detail="世界不存在") from exc

    @router.get("/todos")
    def list_todos(
        world_id: str,
        character_id: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict[str, object]]:
        with database.read() as connection:
            _world_time(connection, world_id)
            sql = "SELECT t.*, c.name AS character_name FROM npc_todos t JOIN characters c ON c.id = t.character_id WHERE t.world_id = ?"
            args: list[object] = [world_id]
            if character_id:
                sql += " AND t.character_id = ?"
                args.append(character_id)
            args.extend((limit, offset))
            return [
                dict(row)
                for row in connection.execute(
                    sql + " ORDER BY t.status, t.due_world_time, t.created_at LIMIT ? OFFSET ?",
                    args,
                ).fetchall()
            ]

    return router
