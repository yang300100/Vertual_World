from __future__ import annotations

import logging
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from world_engine.api_map import WORLD_MAP_DIRECTORY, _world_map_layers
from world_engine.config import PROJECT_ROOT, Settings
from world_engine.database import Database
from world_engine.domain import WorldSnapshot, WorldState
from world_engine.engine import WorldEngine
from world_engine.food_supply import seed_food_supply
from world_engine.photos import (
    PhotoCaptureRequest,
    PhotoCaptureView,
    PhotoGenerationError,
    PhotoService,
    PortraitUploadRequest,
    PortraitView,
)
from world_engine.repository import WorldNotFoundError, WorldRepository, to_iso, utc_now

LOGGER = logging.getLogger("virtual-world.api")

WEB_DIRECTORY = Path(__file__).resolve().parent / "web"


class CreateWorldRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    minutes_per_tick: int | None = Field(default=None, ge=1, le=24 * 60)
    time_scale: float | None = Field(default=None, ge=0, le=10080)
    seed_demo: bool = True


def create_app(
    settings: Settings | None = None,
    *,
    photo_service_override: PhotoService | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings.from_env()
    database = Database(resolved_settings.database_path)
    repository = WorldRepository()
    engine = WorldEngine(database, resolved_settings)
    photo_service = photo_service_override or PhotoService(resolved_settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        database.initialize()
        engine.reset_offline_baseline()
        try:
            yield
        finally:
            engine.close()

    application = FastAPI(
        title="Virtual World Core",
        version="0.1.0",
        description="自主世界的事实、人物、事件、记忆与推进接口",
        lifespan=lifespan,
    )
    application.mount(
        "/ui",
        StaticFiles(directory=WEB_DIRECTORY, html=True),
        name="world-ui",
    )
    application.mount(
        "/world-assets",
        StaticFiles(directory=WORLD_MAP_DIRECTORY),
        name="world-assets",
    )
    media_directory = resolved_settings.media_directory or (PROJECT_ROOT / "data" / "world-media")
    media_directory.mkdir(parents=True, exist_ok=True)
    application.mount(
        "/world-media",
        StaticFiles(directory=media_directory),
        name="world-media",
    )

    @application.middleware("http")
    async def _no_cache_frontend(request, call_next):
        """前端资源不缓存：改样式/HTML 后用户刷新即拿到新版，避免旧缓存导致白底裸文本。"""
        response = await call_next(request)
        if request.url.path.startswith("/ui/"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response

    @application.exception_handler(WorldNotFoundError)
    async def _world_not_found_handler(_request, exc):
        """把领域层的世界不存在统一映射为 404，路由内不必各自写 try/except。"""
        return JSONResponse(
            status_code=404, content={"detail": str(exc) or "世界不存在"}
        )

    @application.exception_handler(Exception)
    async def _unhandled_exception_handler(request, exc):
        """兜底处理器：记录完整堆栈，但只向客户端返回统一错误体，不泄露内部细节。"""
        LOGGER.exception("处理 %s %s 时发生未捕获异常", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "服务器内部错误"})

    @application.get("/")
    def root() -> RedirectResponse:
        return RedirectResponse(url="/ui/", status_code=307)

    @application.get("/api/health")
    def health() -> dict[str, object]:
        try:
            with database.read() as connection:
                connection.execute("SELECT 1").fetchone()
            return {
                "service": "virtual-world-core",
                "status": "ok",
                "server_time": to_iso(utc_now()),
                "database": "ready",
                "decision_provider": engine.decision_provider.name,
                "knowledge": {
                    "enabled": resolved_settings.knowledge_enabled,
                    "loaded_chunks": getattr(
                        getattr(engine.decision_provider, "knowledge_base", None),
                        "chunk_count",
                        0,
                    ),
                },
                "heartbeat_interval_seconds": resolved_settings.worker_interval_seconds,
                "image_generation": {
                    "provider": resolved_settings.image_generation_provider,
                    "model": resolved_settings.image_model,
                    "configured": bool(resolved_settings.image_api_key),
                },
            }
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
                    time_scale=(
                        payload.time_scale
                        if payload.time_scale is not None
                        else resolved_settings.default_time_scale
                    ),
                    adjudication_interval_minutes=(resolved_settings.adjudication_interval_minutes),
                    seed_demo=payload.seed_demo,
                )
                # 新建世界必须即刻播种食物供给，否则 API 进程长驻期间新建的世界
                # 会一直停留在「无任何可采集资源」的饥饿死锁状态，直到进程重启
                # 触发 Database.initialize() 的补种。播种放在调用方而非 repository，
                # 是为了不让数据访问层依赖业务层的 food_supply。
                seed_food_supply(connection, world_id)
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

    @application.get("/api/world-map-layers")
    def world_map_layers() -> dict[str, object]:
        """返回可交互的世界底图；PNG 与 SVG 使用同一套世界坐标。

        这条路由不带 world_id，属于世界级端点而非某个世界内的地图接口，
        因此留在 api.py；图层扫描实现随地图域放在 api_map。
        """
        layers = _world_map_layers()
        # files 是仅含安全相对路径的简化列表；前端显示名称和格式时使用 layers。
        return {"layers": layers, "files": [item["asset_path"] for item in layers]}

    @application.put(
        "/api/worlds/{world_id}/characters/{character_id}/portrait",
        response_model=PortraitView,
    )
    def upload_character_portrait(
        world_id: str,
        character_id: str,
        payload: PortraitUploadRequest,
    ) -> PortraitView:
        try:
            with database.write() as connection:
                return photo_service.upload_portrait(
                    connection,
                    world_id=world_id,
                    character_id=character_id,
                    payload=payload,
                )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.post(
        "/api/worlds/{world_id}/photos",
        response_model=PhotoCaptureView,
        status_code=201,
    )
    def capture_photo(world_id: str, payload: PhotoCaptureRequest) -> PhotoCaptureView:
        try:
            with database.read() as connection:
                prepared = photo_service.prepare_capture(
                    connection,
                    world_id=world_id,
                    request=payload,
                )
            generated = photo_service.generate_capture(prepared)
            with database.write() as connection:
                return photo_service.finalize_capture(
                    connection,
                    prepared=prepared,
                    generated=generated,
                )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except PhotoGenerationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @application.get(
        "/api/worlds/{world_id}/photos",
        response_model=list[PhotoCaptureView],
    )
    def list_photos(
        world_id: str,
        limit: Annotated[int, Query(ge=1, le=100)] = 30,
    ) -> list[PhotoCaptureView]:
        with database.read() as connection:
            exists = connection.execute(
                "SELECT 1 FROM worlds WHERE id = ?", (world_id,)
            ).fetchone()
            if exists is None:
                raise HTTPException(status_code=404, detail="世界不存在")
            return photo_service.list_captures(connection, world_id=world_id, limit=limit)

    @application.post("/api/worlds/{world_id}/agents/memory-jobs/{job_id}/retry")
    def retry_memory_job(world_id: str, job_id: str) -> dict[str, object]:
        with database.write() as connection:
            changed = connection.execute(
                """UPDATE memory_jobs SET status='pending', attempt_count=0,
                   retry_at=NULL,last_error=NULL,updated_at=?
                   WHERE id=? AND world_id=? AND status='failed'""",
                (to_iso(utc_now()), job_id, world_id),
            ).rowcount
            if changed != 1:
                raise HTTPException(status_code=409, detail="只能重试当前世界中失败的记忆任务")
        return {"status": "pending"}

    @application.get("/api/worlds/{world_id}/agents/runs")
    def list_agent_runs(
        world_id: str,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> list[dict[str, object]]:
        try:
            with database.read() as connection:
                return repository.list_agent_runs(connection, world_id, limit)
        except WorldNotFoundError as exc:
            raise HTTPException(status_code=404, detail="世界不存在") from exc

    @application.get("/api/worlds/{world_id}/agents/proposals")
    def list_agent_proposals(
        world_id: str,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
    ) -> list[dict[str, object]]:
        try:
            with database.read() as connection:
                return repository.list_agent_proposals(connection, world_id, limit)
        except WorldNotFoundError as exc:
            raise HTTPException(status_code=404, detail="世界不存在") from exc

    @application.get("/api/worlds/{world_id}/memory-jobs")
    def list_memory_jobs(
        world_id: str,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> list[dict[str, object]]:
        try:
            with database.read() as connection:
                return repository.list_memory_jobs(connection, world_id, limit)
        except WorldNotFoundError as exc:
            raise HTTPException(status_code=404, detail="世界不存在") from exc

    from world_engine.api_dialogue import (
        DialogueContextServices,
        build_dialogue_router,
    )
    from world_engine.api_elements import build_elements_router
    from world_engine.api_map import build_map_router
    from world_engine.api_player import build_player_router
    from world_engine.api_world import build_world_router
    from world_engine.interior_api import build_interior_router
    from world_engine.life_api import build_life_router
    from world_engine.living_api import build_living_router
    from world_engine.task_api import build_task_router

    application.include_router(build_elements_router(database))
    application.include_router(build_map_router(database))
    application.include_router(build_player_router(database, engine, repository, photo_service))
    application.include_router(build_world_router(database, engine, repository))
    application.include_router(build_life_router(database))
    application.include_router(build_interior_router(database))
    application.include_router(build_task_router(database, engine))
    application.include_router(build_living_router(database,engine))
    application.include_router(
        build_dialogue_router(
            database,
            engine,
            repository,
            # 对话域只读「模型调用超时秒数」一项设置，用适配对象隔离 Settings 类型。
            DialogueContextServices(resolved_settings),
        )
    )
    return application


app = create_app()
