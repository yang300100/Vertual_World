"""世界的时钟、推进、事件与待办接口。

这些路由原先内嵌在 `api.create_app` 里，与元素登记、玩家视角等域混在同一个
1600 行的闭包中。这里按「世界推进」整体搬出，行为与之完全一致（路径、方法、
响应模型、状态码与错误文案一字未改）。

`_world_time` 是 `api_deps.require_world_time_text` 的别名：它返回原始 ISO
文本，供需要原样比较世界时间的端点使用；同文件里返回 datetime 的
`require_world_time` 不能替代它。
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from world_engine.api_deps import require_world_time_text as _world_time
from world_engine.domain import ClockUpdateResult, HeartbeatResult, TickResult
from world_engine.engine import ConcurrentWorldUpdateError
from world_engine.history import HistoryExportResult
from world_engine.repository import WorldNotFoundError


class ClockUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    time_scale: float = Field(ge=0, le=10080)
    operator: str = Field(default="main_view", min_length=1, max_length=100)


class AdjudicationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trigger: str = Field(default="manual", pattern="^(manual|player_intervention)$")
    character_ids: list[str] | None = Field(default=None, max_length=10)


def build_world_router(database, engine, repository) -> APIRouter:
    """世界的时钟、推进、事件与待办接口。"""

    router = APIRouter(prefix="/api/worlds/{world_id}", tags=["世界推进"])

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
