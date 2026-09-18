from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from world_engine.config import PROJECT_ROOT, Settings
from world_engine.conversations import ConversationService, NpcCharacterCard
from world_engine.database import Database
from world_engine.decisions import DecisionProviderError
from world_engine.domain import (
    MovementState,
    PlayerActionResult,
    WorldSnapshot,
    WorldState,
)
from world_engine.engine import ConcurrentWorldUpdateError, WorldEngine
from world_engine.food_supply import seed_food_supply
from world_engine.intent_parser import IntentPreview
from world_engine.navigation import TerrainService
from world_engine.photos import (
    PhotoCaptureRequest,
    PhotoCaptureView,
    PhotoGenerationError,
    PhotoService,
    PortraitUploadRequest,
    PortraitView,
)
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
from world_engine.removal import (
    ElementRemovalConflict,
    ElementRemovalNotFound,
    ElementRemovalSubmit,
    ElementRemovalView,
    WorldElementRemover,
)
from world_engine.repository import WorldNotFoundError, WorldRepository, to_iso, utc_now
from world_engine.routing import RoutePlanner

LOGGER = logging.getLogger("virtual-world.api")

WEB_DIRECTORY = Path(__file__).resolve().parent / "web"
WORLD_MAP_DIRECTORY = PROJECT_ROOT / "docs" / "worldbuilding" / "maps"

# 只有整张世界图能作为可交互底图：它们与世界坐标同为等距圆柱投影、覆盖
# [-180, 180] × [-90, 90]。区域旧图和城镇详图不能混入此列表，否则点击坐标会
# 被错误投影到另一片地理范围。navigation/noryia 是当前 Noryia 的同投影图层目录。
WORLD_MAP_LAYER_EXTENSIONS = {".svg", ".png", ".jpg", ".jpeg", ".webp"}


def _world_map_layer_label(path: Path) -> str:
    """为前端地图图层提供稳定、易读的中文名称。"""
    stem = path.stem
    if stem == "Noryia":
        return "Noryia 地形图"
    if stem == "Noryia_标注":
        return "Noryia 标注图"
    if stem.startswith("noryia-world-satellite-"):
        return f"Noryia 卫星图 {stem.removeprefix('noryia-world-satellite-').upper()}"
    return stem


def _world_map_layers() -> list[dict[str, str]]:
    """列出与世界坐标对齐的 SVG/位图底图，不暴露城镇或旧线区域图。"""
    navigation_directory = WORLD_MAP_DIRECTORY / "navigation" / "noryia"
    candidates = [
        path
        for path in sorted(navigation_directory.glob("*"))
        if path.is_file() and path.suffix.lower() in WORLD_MAP_LAYER_EXTENSIONS
    ]
    layers: list[dict[str, str]] = []
    for path in candidates:
        if not path.is_file():
            continue
        relative_path = path.relative_to(WORLD_MAP_DIRECTORY).as_posix()
        layers.append(
            {
                "asset_path": relative_path,
                "label": _world_map_layer_label(path),
                "format": path.suffix.removeprefix(".").lower(),
            }
        )
    return layers


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
    target_character_id: str | None = Field(default=None, min_length=1, max_length=100)
    delivery: Literal["normal","whisper","shout"] = "normal"


class GroupDialogueRequest(BaseModel):
    """玩家面向在场多人发言；发言人仍由本地调度器最终选择。"""

    model_config = ConfigDict(extra="forbid")

    intent: str = Field(min_length=1, max_length=1000)
    participant_ids: list[str] | None = Field(default=None, max_length=8)
    max_speakers: int = Field(default=2, ge=1, le=3)
    delivery: Literal["normal","whisper","shout"] = "normal"


class CharacterCardRequest(BaseModel):
    """创作侧可维护的 NPC 角色卡；不包含任何直接世界效果。"""

    model_config = ConfigDict(extra="forbid")

    public_role: str = Field(min_length=1, max_length=120)
    current_preoccupation: str = Field(min_length=1, max_length=240)
    private_tension: str = Field(min_length=1, max_length=240)
    social_boundary: str = Field(min_length=1, max_length=240)
    expression_notes: str = Field(min_length=1, max_length=240)
    speech_style: str = Field(
        default="使用符合身份的自然口语，避免重复套话",
        min_length=1,
        max_length=240,
    )
    initiative_notes: str = Field(
        default="必要时追问来意，并把话题带回自己关心的事务",
        min_length=1,
        max_length=240,
    )
    preferred_address: str = Field(
        default="根据关系和场合自然称呼对方",
        min_length=1,
        max_length=120,
    )
    dialogue_examples: list[str] = Field(default_factory=list, max_length=4)

    def to_card(self) -> NpcCharacterCard:
        return NpcCharacterCard.from_dict(self.model_dump())

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
    biome: str | None = None
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


def create_app(
    settings: Settings | None = None,
    *,
    photo_service_override: PhotoService | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings.from_env()
    database = Database(resolved_settings.database_path)
    repository = WorldRepository()
    engine = WorldEngine(database, resolved_settings)
    element_registry = WorldElementRegistry()
    element_remover = WorldElementRemover()
    construction_projects = ConstructionProjectService()
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

    @application.post(
        "/api/worlds/{world_id}/removals",
        response_model=ElementRemovalView,
        status_code=201,
    )
    def submit_element_removal(
        world_id: str, payload: ElementRemovalSubmit
    ) -> ElementRemovalView:
        """按来源事件执行可审计墓碑删除，永不绕过领域规则物理删行。"""

        try:
            with database.write() as connection:
                return element_remover.submit(
                    connection,
                    world_id=world_id,
                    request=payload,
                )
        except ElementRemovalNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ElementRemovalConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except sqlite3.OperationalError as exc:
            raise HTTPException(status_code=503, detail="世界正在由另一个进程更新") from exc

    @application.get(
        "/api/worlds/{world_id}/removals",
        response_model=list[ElementRemovalView],
    )
    def list_element_removals(
        world_id: str,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> list[ElementRemovalView]:
        with database.read() as connection:
            exists = connection.execute(
                "SELECT 1 FROM worlds WHERE id = ?", (world_id,)
            ).fetchone()
            if exists is None:
                raise HTTPException(status_code=404, detail="世界不存在")
            return element_remover.list(connection, world_id=world_id, limit=limit)

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
            return engine.submit_player_intent(
                world_id,
                payload.intent,
                target_character_id=payload.target_character_id,
                delivery=payload.delivery,
            )
        except WorldNotFoundError as exc:
            raise HTTPException(status_code=404, detail="世界不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ConcurrentWorldUpdateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except DecisionProviderError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except sqlite3.OperationalError as exc:
            raise HTTPException(status_code=503, detail="世界正在由另一个进程结算") from exc

    @application.get("/api/worlds/{world_id}/player/activities")
    def player_activities(world_id: str) -> list[dict[str, object]]:
        from world_engine.player_activities import PlayerActivityService

        with database.read() as connection:
            snapshot = repository.get_snapshot(connection, world_id)
            player = next((item for item in snapshot.characters if item.is_player), None)
            if player is None:
                raise HTTPException(status_code=404, detail="玩家角色不存在")
            return PlayerActivityService.recent_records(connection, world_id, player.id)

    @application.post("/api/worlds/{world_id}/player/actions/{event_id}/reaction")
    def retry_action_reaction(world_id: str, event_id: str) -> dict[str, object]:
        from world_engine.player_action_flow import react_to_action

        result = react_to_action(engine, world_id, event_id)
        engine._sync_history_safely(world_id)
        return result

    @application.post(
        "/api/worlds/{world_id}/player/group-dialogue",
        status_code=201,
    )
    def player_group_dialogue(
        world_id: str, payload: GroupDialogueRequest
    ) -> dict[str, object]:
        try:
            return engine.submit_group_dialogue(
                world_id,
                payload.intent,
                participant_ids=payload.participant_ids,
                max_speakers=payload.max_speakers,
                delivery=payload.delivery,
            )
        except WorldNotFoundError as exc:
            raise HTTPException(status_code=404, detail="世界不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ConcurrentWorldUpdateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except DecisionProviderError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except sqlite3.OperationalError as exc:
            raise HTTPException(status_code=503, detail="世界正在由另一个进程结算") from exc

    @application.post(
        "/api/worlds/{world_id}/player/intents/preview",
        response_model=IntentPreview,
    )
    def preview_player_intent(world_id: str, payload: PlayerIntentRequest) -> IntentPreview:
        try:
            return engine.preview_player_intent(
                world_id,
                payload.intent,
                target_character_id=payload.target_character_id,
            )
        except WorldNotFoundError as exc:
            raise HTTPException(status_code=404, detail="世界不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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
                biome=sample["biome"],
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
        """返回可交互的世界底图；PNG 与 SVG 使用同一套世界坐标。"""
        layers = _world_map_layers()
        # files 是仅含安全相对路径的简化列表；前端显示名称和格式时使用 layers。
        return {"layers": layers, "files": [item["asset_path"] for item in layers]}

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

    @application.get("/api/worlds/{world_id}/characters/{character_id}/character-card")
    def get_character_card(world_id: str, character_id: str) -> dict[str, object]:
        """读取 NPC 角色卡；旧存档首次读取会安全补齐确定性默认卡。"""
        with database.write() as connection:
            snapshot = repository.get_snapshot(connection, world_id)
            character = snapshot.character_by_id(character_id)
            if character is None or character.is_player:
                raise HTTPException(status_code=404, detail="NPC不存在")
            card = engine.conversations.get_card(
                connection, world_id=world_id, npc=character, persist_default=True
            )
            return card.to_dict()

    @application.put("/api/worlds/{world_id}/characters/{character_id}/character-card")
    def update_character_card(
        world_id: str,
        character_id: str,
        payload: CharacterCardRequest,
    ) -> dict[str, object]:
        """更新角色卡，不改变角色属性、关系、记忆或任何世界事实。"""
        with database.write() as connection:
            row = connection.execute(
                "SELECT is_player FROM characters WHERE id = ? AND world_id = ?",
                (character_id, world_id),
            ).fetchone()
            if row is None or row["is_player"]:
                raise HTTPException(status_code=404, detail="NPC不存在")
            ConversationService.save_card(
                connection, world_id=world_id, npc_id=character_id, card=payload.to_card()
            )
        return payload.to_card().to_dict()

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
    from world_engine.api_world import build_world_router
    from world_engine.interior_api import build_interior_router
    from world_engine.life_api import build_life_router
    from world_engine.living_api import build_living_router
    from world_engine.task_api import build_task_router

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
