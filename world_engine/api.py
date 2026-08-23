from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from world_engine.config import Settings
from world_engine.database import Database
from world_engine.domain import TickResult, WorldSnapshot, WorldState
from world_engine.engine import ConcurrentWorldUpdateError, WorldEngine
from world_engine.repository import WorldNotFoundError, WorldRepository


class CreateWorldRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    minutes_per_tick: int | None = Field(default=None, ge=1, le=24 * 60)
    seed_demo: bool = True


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or Settings.from_env()
    database = Database(resolved_settings.database_path)
    repository = WorldRepository()
    engine = WorldEngine(database, resolved_settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        database.initialize()
        yield

    application = FastAPI(
        title="Virtual World Core",
        version="0.1.0",
        description="自主世界的事实、人物、事件、记忆与推进接口",
        lifespan=lifespan,
    )

    @application.get("/")
    def root() -> dict[str, str]:
        return {
            "name": "Virtual World Core",
            "docs": "/docs",
            "health": "/api/health",
        }

    @application.get("/api/health")
    def health() -> dict[str, object]:
        try:
            with database.read() as connection:
                connection.execute("SELECT 1").fetchone()
            return {"status": "ok", "database": "ready"}
        except sqlite3.Error as exc:
            raise HTTPException(status_code=503, detail="世界数据库不可用") from exc

    @application.post("/api/worlds", response_model=WorldSnapshot, status_code=201)
    def create_world(payload: CreateWorldRequest) -> WorldSnapshot:
        minutes_per_tick = payload.minutes_per_tick or resolved_settings.minutes_per_tick
        try:
            with database.write() as connection:
                world_id = repository.create_world(
                    connection,
                    name=payload.name,
                    minutes_per_tick=minutes_per_tick,
                    seed_demo=payload.seed_demo,
                )
                return repository.get_snapshot(connection, world_id)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="世界初始数据存在冲突") from exc

    @application.get("/api/worlds", response_model=list[WorldState])
    def list_worlds() -> list[WorldState]:
        with database.read() as connection:
            return repository.list_worlds(connection)

    @application.get("/api/worlds/{world_id}", response_model=WorldSnapshot)
    def get_world(world_id: str) -> WorldSnapshot:
        try:
            with database.read() as connection:
                return repository.get_snapshot(connection, world_id)
        except WorldNotFoundError as exc:
            raise HTTPException(status_code=404, detail="世界不存在") from exc

    @application.post("/api/worlds/{world_id}/tick", response_model=TickResult)
    def tick_world(world_id: str) -> TickResult:
        try:
            return engine.tick(world_id)
        except WorldNotFoundError as exc:
            raise HTTPException(status_code=404, detail="世界不存在") from exc
        except ConcurrentWorldUpdateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except sqlite3.OperationalError as exc:
            raise HTTPException(status_code=503, detail="世界正在由另一个进程结算") from exc

    @application.get("/api/worlds/{world_id}/events")
    def list_events(
        world_id: str,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> list[dict[str, object]]:
        try:
            with database.read() as connection:
                return repository.list_events(connection, world_id, limit)
        except WorldNotFoundError as exc:
            raise HTTPException(status_code=404, detail="世界不存在") from exc

    @application.get("/api/worlds/{world_id}/characters/{character_id}/memories")
    def list_memories(
        world_id: str,
        character_id: str,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> list[dict[str, object]]:
        try:
            with database.read() as connection:
                return repository.list_memories(connection, world_id, character_id, limit)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="世界或人物不存在") from exc

    return application


app = create_app()

