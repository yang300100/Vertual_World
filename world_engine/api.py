from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from world_engine.config import Settings
from world_engine.database import Database
from world_engine.domain import (
    ClockUpdateResult,
    HeartbeatResult,
    MovementState,
    PlayerActionResult,
    TickResult,
    WorldSnapshot,
    WorldState,
)
from world_engine.engine import ConcurrentWorldUpdateError, WorldEngine
from world_engine.history import HistoryExportResult
from world_engine.navigation import TerrainService
from world_engine.registration import (
    ConstructionProjectService,
    ElementRegistrationSubmit,
    ElementRegistrationView,
    ElementType,
    RegistrationConflict,
    RegistrationNotFound,
    RegistrationStatus,
    WorldElementRegistry,
)
from world_engine.repository import WorldNotFoundError, WorldRepository, to_iso, utc_now
from world_engine.routing import RoutePlanner

WEB_DIRECTORY = Path(__file__).resolve().parent / "web"
WORLD_MAP_DIRECTORY = Path(__file__).resolve().parent.parent / "docs" / "worldbuilding" / "maps"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _terrain_service_for_world(database: Database, world_id: str) -> TerrainService:
    """加载世界审核通过的导航数据集；缺失时抛出 LookupError。"""
    with database.read() as connection:
        row = connection.execute(
            """
            SELECT asset_root FROM navigation_datasets
            WHERE world_id = ? AND review_status = 'approved'
            ORDER BY created_at DESC LIMIT 1
            """,
            (world_id,),
        ).fetchone()
    if row is None:
        raise LookupError("没有审核通过的导航数据集")
    return TerrainService(PROJECT_ROOT / row["asset_root"])


def _approved_dataset_meta(database: Database, world_id: str) -> dict[str, Any] | None:
    """返回世界审核通过/候选数据集的元信息；无数据集返回 None。"""
    with database.read() as connection:
        row = connection.execute(
            """
            SELECT id, name, asset_root, source_sha256, review_status, bounds_json, approved_at
            FROM navigation_datasets
            WHERE world_id = ?
            ORDER BY approved_at DESC, created_at DESC LIMIT 1
            """,
            (world_id,),
        ).fetchone()
    if row is None:
        return None
    item = dict(row)
    try:
        item["bounds"] = json.loads(item.pop("bounds_json"))
    except (json.JSONDecodeError, TypeError):
        item["bounds"] = None
    asset_root = PROJECT_ROOT / item["asset_root"]
    version = None
    feature_counts = None
    try:
        metadata = json.loads((asset_root / "metadata.json").read_text(encoding="utf-8"))
        version = metadata.get("azgaar_version")
        feature_counts = metadata.get("feature_counts")
    except (OSError, json.JSONDecodeError, TypeError):
        version = None
        feature_counts = None
    item["azgaar_version"] = version
    item["feature_counts"] = feature_counts
    return item


class CreateWorldRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    minutes_per_tick: int | None = Field(default=None, ge=1, le=24 * 60)
    time_scale: float | None = Field(default=None, ge=0, le=10080)
    seed_demo: bool = True


class ClockUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    time_scale: float = Field(ge=0, le=10080)
    operator: str = Field(default="main_view", min_length=1, max_length=100)


class AdjudicationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trigger: str = Field(default="manual", pattern="^(manual|player_intervention)$")
    character_ids: list[str] | None = Field(default=None, max_length=10)


class CreatePlayerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=60)
    identity: str = Field(default="旅人", min_length=1, max_length=100)
    location_id: str = Field(min_length=1, max_length=100)
    traits: list[str] = Field(default_factory=list, max_length=5)
    goal: str = Field(default="", max_length=200)


class PlayerIntentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: str = Field(min_length=1, max_length=1000)


class PlayerMoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    destination_longitude: float = Field(ge=-180, le=180)
    destination_latitude: float = Field(ge=-90, le=90)
    vehicle_id: str | None = Field(default=None, max_length=100)


class PlayerMoveResponse(BaseModel):
    """开始移动的返回：移动主体 + 规划出的可通行路线。"""

    model_config = ConfigDict(extra="forbid")

    movement: MovementState
    route: dict[str, Any] | None = None


class TerrainSampleResponse(BaseModel):
    """任意经纬度的只读地形上下文；前端不做通行性判断。"""

    model_config = ConfigDict(extra="forbid")

    longitude: float
    latitude: float
    elevation_m: float
    surface_type: str
    slope_degrees: float
    water_kind: str | None
    road_ids: list[str] = Field(default_factory=list)
    road_type: str | None = None
    road_speed_multiplier: float = 1.0
    river_ids: list[int] = Field(default_factory=list)
    crossing_type: str | None = None
    crossing_name: str | None = None
    state_id: int | None = None
    province_id: int | None = None
    dataset_status: str = "candidate"
    passability: dict[str, Any] = Field(default_factory=dict)


class MapStyleRequest(BaseModel):
    """用户界面地图样式偏好；仅影响展示，不影响规则。"""

    model_config = ConfigDict(extra="forbid")

    style: str = Field(pattern="^(political|terrain|elevation|passability)$")


class PlayerTransportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vehicle_id: str | None = Field(default=None, max_length=100)


class ConstructionProjectStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["surveyed", "constructing", "cancelled"]


class RegistrationReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="", max_length=500)


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or Settings.from_env()
    database = Database(resolved_settings.database_path)
    repository = WorldRepository()
    engine = WorldEngine(database, resolved_settings)
    element_registry = WorldElementRegistry()
    construction_projects = ConstructionProjectService()

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

    @application.middleware("http")
    async def _no_cache_frontend(request, call_next):
        """前端资源不缓存：改样式/HTML 后用户刷新即拿到新版，避免旧缓存导致白底裸文本。"""
        response = await call_next(request)
        if request.url.path.startswith("/ui/"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response

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

    @application.post(
        "/api/worlds/{world_id}/registrations",
        response_model=ElementRegistrationView,
        status_code=201,
    )
    def submit_element_registration(
        world_id: str, payload: ElementRegistrationSubmit
    ) -> ElementRegistrationView:
        """提交严格类型的世界元素注册请求；重复幂等键不会重复应用。"""

        try:
            with database.write() as connection:
                return element_registry.submit(
                    connection,
                    world_id=world_id,
                    request=payload,
                )
        except RegistrationNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RegistrationConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except sqlite3.OperationalError as exc:
            raise HTTPException(status_code=503, detail="世界正在由另一个进程更新") from exc

    @application.get(
        "/api/worlds/{world_id}/registrations",
        response_model=list[ElementRegistrationView],
    )
    def list_element_registrations(
        world_id: str,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        element_type: ElementType | None = None,
        status: RegistrationStatus | None = None,
    ) -> list[ElementRegistrationView]:
        try:
            with database.read() as connection:
                return element_registry.list(
                    connection,
                    world_id=world_id,
                    limit=limit,
                    element_type=element_type,
                    status=status,
                )
        except RegistrationNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @application.get(
        "/api/worlds/{world_id}/registrations/{registration_id}",
        response_model=ElementRegistrationView,
    )
    def get_element_registration(
        world_id: str, registration_id: str
    ) -> ElementRegistrationView:
        try:
            with database.read() as connection:
                return element_registry.get(
                    connection,
                    world_id=world_id,
                    registration_id=registration_id,
                )
        except RegistrationNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @application.post(
        "/api/worlds/{world_id}/registrations/{registration_id}/confirm",
        response_model=ElementRegistrationView,
    )
    def confirm_element_registration(
        world_id: str, registration_id: str
    ) -> ElementRegistrationView:
        try:
            with database.write() as connection:
                return element_registry.confirm(
                    connection,
                    world_id=world_id,
                    registration_id=registration_id,
                )
        except RegistrationNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RegistrationConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.post(
        "/api/worlds/{world_id}/registrations/{registration_id}/reject",
        response_model=ElementRegistrationView,
    )
    def reject_element_registration(
        world_id: str,
        registration_id: str,
        payload: RegistrationReviewRequest,
    ) -> ElementRegistrationView:
        try:
            with database.write() as connection:
                return element_registry.reject(
                    connection,
                    world_id=world_id,
                    registration_id=registration_id,
                    reason=payload.reason,
                )
        except RegistrationNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RegistrationConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.get("/api/worlds/{world_id}/construction-projects")
    def list_construction_projects(world_id: str) -> list[dict[str, object]]:
        with database.read() as connection:
            exists = connection.execute(
                "SELECT 1 FROM worlds WHERE id = ?", (world_id,)
            ).fetchone()
            if exists is None:
                raise HTTPException(status_code=404, detail="世界不存在")
            return construction_projects.list(connection, world_id=world_id)

    @application.patch(
        "/api/worlds/{world_id}/registrations/{registration_id}/construction"
    )
    def update_construction_project(
        world_id: str,
        registration_id: str,
        payload: ConstructionProjectStatusUpdate,
    ) -> dict[str, object]:
        try:
            with database.write() as connection:
                return construction_projects.set_status(
                    connection,
                    world_id=world_id,
                    registration_id=registration_id,
                    status=payload.status,
                )
        except RegistrationNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RegistrationConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.post(
        "/api/worlds/{world_id}/player",
        response_model=WorldSnapshot,
        status_code=201,
    )
    def create_player(world_id: str, payload: CreatePlayerRequest) -> WorldSnapshot:
        try:
            with database.write() as connection:
                repository.create_player_character(
                    connection,
                    world_id=world_id,
                    name=payload.name,
                    identity=payload.identity,
                    location_id=payload.location_id,
                    traits=payload.traits,
                    goal=payload.goal,
                )
                return repository.get_snapshot(connection, world_id)
        except WorldNotFoundError as exc:
            raise HTTPException(status_code=404, detail="世界不存在") from exc
        except LookupError as exc:
            raise HTTPException(status_code=400, detail="起始地点不属于当前世界") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="角色名称已存在") from exc

    @application.post(
        "/api/worlds/{world_id}/player/act",
        response_model=PlayerActionResult,
        status_code=201,
    )
    def player_act(world_id: str, payload: PlayerIntentRequest) -> PlayerActionResult:
        try:
            return engine.submit_player_intent(world_id, payload.intent)
        except WorldNotFoundError as exc:
            raise HTTPException(status_code=404, detail="世界不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ConcurrentWorldUpdateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except sqlite3.OperationalError as exc:
            raise HTTPException(status_code=503, detail="世界正在由另一个进程结算") from exc

    @application.post(
        "/api/worlds/{world_id}/player/move",
        response_model=PlayerMoveResponse,
        status_code=201,
    )
    def start_player_movement(world_id: str, payload: PlayerMoveRequest) -> PlayerMoveResponse:
        try:
            movement = engine.start_player_movement(
                world_id,
                destination_longitude=payload.destination_longitude,
                destination_latitude=payload.destination_latitude,
                vehicle_id=payload.vehicle_id,
            )
            return PlayerMoveResponse(
                movement=movement,
                route=movement.route,
            )
        except WorldNotFoundError as exc:
            raise HTTPException(status_code=404, detail="世界不存在") from exc
        except (LookupError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except sqlite3.OperationalError as exc:
            raise HTTPException(status_code=503, detail="世界正在由另一个进程更新") from exc

    @application.get(
        "/api/worlds/{world_id}/terrain",
        response_model=TerrainSampleResponse,
    )
    def sample_terrain(
        world_id: str,
        longitude: Annotated[float, Query(ge=-180, le=180)],
        latitude: Annotated[float, Query(ge=-90, le=90)],
        movement_type: Annotated[
            str | None, Query(pattern="^(land|flight|ship|water|underground)$")
        ] = None,
    ) -> TerrainSampleResponse:
        try:
            terrain = _terrain_service_for_world(database, world_id)
            sample = terrain.sample(longitude, latitude)
            if sample is None:
                raise HTTPException(status_code=404, detail="当前位置没有地形数据")
            planner = RoutePlanner()
            description = planner.describe(
                terrain, longitude, latitude, movement_type=movement_type or "land"
            )
            return TerrainSampleResponse(
                longitude=longitude,
                latitude=latitude,
                elevation_m=float(sample["elevation_m"]),
                surface_type=str(sample["surface_type"]),
                slope_degrees=float(sample["slope_degrees"]),
                water_kind=sample["water_kind"],
                road_ids=sample["road_ids"],
                road_type=sample["road_type"],
                road_speed_multiplier=float(sample["road_speed_multiplier"]),
                river_ids=sample["river_ids"],
                crossing_type=sample["crossing_type"],
                crossing_name=sample["crossing_name"],
                state_id=sample["state_id"],
                province_id=sample["province_id"],
                dataset_status="approved",
                passability=description,
            )
        except WorldNotFoundError as exc:
            raise HTTPException(status_code=404, detail="世界不存在") from exc
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="没有审核通过的导航数据集") from exc

    @application.get("/api/worlds/{world_id}/map-styles")
    def map_styles(world_id: str) -> dict[str, Any]:
        with database.read() as connection:
            world = connection.execute("SELECT 1 FROM worlds WHERE id = ?", (world_id,)).fetchone()
            preference = connection.execute(
                "SELECT style FROM world_map_preferences WHERE world_id = ?", (world_id,)
            ).fetchone()
        if world is None:
            raise HTTPException(status_code=404, detail="世界不存在")
        dataset = _approved_dataset_meta(database, world_id)
        style = preference["style"] if preference else "political"
        return {
            "available_styles": ["political", "terrain", "elevation", "passability"],
            "style": style,
            "default": "political",
            "dataset_status": dataset["review_status"] if dataset else None,
            "passability_available": bool(dataset and dataset["review_status"] == "approved"),
        }

    @application.put("/api/worlds/{world_id}/map-styles", response_model=dict[str, Any])
    def map_styles_update(world_id: str, payload: MapStyleRequest) -> dict[str, Any]:
        now = to_iso(utc_now())
        try:
            with database.write() as connection:
                exists = connection.execute(
                    "SELECT 1 FROM worlds WHERE id = ?", (world_id,)
                ).fetchone()
                if exists is None:
                    raise WorldNotFoundError(world_id)
                connection.execute(
                    """
                    INSERT INTO world_map_preferences(world_id, style, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(world_id) DO UPDATE SET
                        style = excluded.style, updated_at = excluded.updated_at
                    """,
                    (world_id, payload.style, now),
                )
                preference = connection.execute(
                    "SELECT style, updated_at FROM world_map_preferences WHERE world_id = ?",
                    (world_id,),
                ).fetchone()
        except WorldNotFoundError as exc:
            raise HTTPException(status_code=404, detail="世界不存在") from exc
        return {
            "world_id": world_id,
            "style": preference["style"],
            "updated_at": preference["updated_at"],
        }

    @application.get("/api/worlds/{world_id}/navigation-dataset")
    def navigation_dataset(world_id: str) -> dict[str, Any]:
        dataset = _approved_dataset_meta(database, world_id)
        if dataset is None:
            raise HTTPException(status_code=404, detail="该世界没有导航数据集")
        return dataset

    @application.get("/api/world-map-layers")
    def world_map_layers() -> dict[str, Any]:
        """返回存放地图文件夹(navigation/noryia)下的图片文件名，供前端底图下拉选项。"""
        nav_dir = PROJECT_ROOT / "docs" / "worldbuilding" / "maps" / "navigation" / "noryia"
        files: list[str] = []
        if nav_dir.is_dir():
            for path in sorted(nav_dir.glob("*")):
                if not path.is_file():
                    continue
                if path.suffix.lower() not in (".svg", ".png", ".jpg", ".jpeg", ".webp"):
                    continue
                if path.name == "audit-overlay.svg":
                    continue
                files.append(path.name)
        return {"files": files}

    @application.get("/api/worlds/{world_id}/passability")
    def passability_grid(
        world_id: str,
        min_longitude: Annotated[float, Query(ge=-180, le=180)],
        max_longitude: Annotated[float, Query(ge=-180, le=180)],
        min_latitude: Annotated[float, Query(ge=-90, le=90)],
        max_latitude: Annotated[float, Query(ge=-90, le=90)],
        columns: Annotated[int, Query(ge=2, le=16)] = 12,
        rows: Annotated[int, Query(ge=2, le=8)] = 6,
        movement_type: Annotated[
            str | None, Query(pattern="^(land|flight|ship|water|underground)$")
        ] = None,
    ) -> dict[str, Any]:
        """对给定经纬度范围做粗采样通行图；规则由后端决定，前端只负责着色。"""
        if max_longitude <= min_longitude or max_latitude <= min_latitude:
            raise HTTPException(status_code=400, detail="经纬度范围无效")
        try:
            terrain = _terrain_service_for_world(database, world_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="没有审核通过的导航数据集") from exc
        planner = RoutePlanner()
        cells: list[dict[str, Any]] = []
        for row in range(rows):
            ratio_y = row / (rows - 1) if rows > 1 else 0.5
            latitude = max_latitude - (max_latitude - min_latitude) * ratio_y
            for column in range(columns):
                ratio_x = column / (columns - 1) if columns > 1 else 0.5
                longitude = min_longitude + (max_longitude - min_longitude) * ratio_x
                cells.append(
                    planner.describe(
                        terrain, longitude, latitude, movement_type=movement_type or "land"
                    )
                )
        return {
            "movement_type": movement_type or "land",
            "column_count": columns,
            "row_count": rows,
            "min_longitude": min_longitude,
            "max_longitude": max_longitude,
            "min_latitude": min_latitude,
            "max_latitude": max_latitude,
            "cells": cells,
            "dataset_status": "approved",
        }

    @application.post(
        "/api/worlds/{world_id}/player/move/cancel",
        response_model=MovementState,
    )
    def cancel_player_movement(world_id: str) -> MovementState:
        try:
            return engine.cancel_player_movement(world_id)
        except WorldNotFoundError as exc:
            raise HTTPException(status_code=404, detail="世界不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except sqlite3.OperationalError as exc:
            raise HTTPException(status_code=503, detail="世界正在由另一个进程更新") from exc

    @application.patch(
        "/api/worlds/{world_id}/player/transport",
        response_model=WorldSnapshot,
    )
    def select_player_transport(world_id: str, payload: PlayerTransportRequest) -> WorldSnapshot:
        try:
            return engine.select_player_transport(world_id, payload.vehicle_id)
        except WorldNotFoundError as exc:
            raise HTTPException(status_code=404, detail="世界不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @application.post(
        "/api/worlds/{world_id}/tick",
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

    @application.post(
        "/api/worlds/{world_id}/heartbeat",
        response_model=HeartbeatResult,
    )
    def heartbeat_world(world_id: str) -> HeartbeatResult:
        try:
            return engine.heartbeat(world_id)
        except WorldNotFoundError as exc:
            raise HTTPException(status_code=404, detail="世界不存在") from exc
        except sqlite3.OperationalError as exc:
            raise HTTPException(status_code=503, detail="世界正在由另一个进程更新") from exc

    @application.patch(
        "/api/worlds/{world_id}/clock",
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

    @application.post(
        "/api/worlds/{world_id}/adjudicate",
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

    @application.get("/api/worlds/{world_id}/events")
    def list_events(
        world_id: str,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        scope: Annotated[str, Query(pattern="^(all|chronicle|log)$")] = "all",
    ) -> list[dict[str, object]]:
        try:
            with database.read() as connection:
                return repository.list_events(connection, world_id, limit, scope=scope)
        except WorldNotFoundError as exc:
            raise HTTPException(status_code=404, detail="世界不存在") from exc

    @application.post(
        "/api/worlds/{world_id}/history/sync",
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

    @application.get("/api/worlds/{world_id}/adjudications")
    def list_adjudications(
        world_id: str,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> list[dict[str, object]]:
        try:
            with database.read() as connection:
                return repository.list_adjudication_runs(connection, world_id, limit)
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

    @application.get("/api/worlds/{world_id}/combat/encounters")
    def list_combat_encounters(
        world_id: str,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> list[dict[str, object]]:
        try:
            with database.read() as connection:
                return repository.list_combat_encounters(connection, world_id, limit)
        except WorldNotFoundError as exc:
            raise HTTPException(status_code=404, detail="世界不存在") from exc

    return application


app = create_app()
