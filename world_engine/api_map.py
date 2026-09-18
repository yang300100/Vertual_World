"""地图底图、样式偏好与地形通行性接口。

这些路由与它们专用的辅助函数原先内嵌在 `api.create_app` 里。这里按
「地图与地形」整体搬出，行为与之完全一致（路径、方法、响应模型、状态码
与错误文案一字未改）。

本模块自行维护两份只被它使用的模块级常量与四个辅助函数：
- `WORLD_MAP_DIRECTORY` / `WORLD_MAP_LAYER_EXTENSIONS`：底图目录与允许的图片后缀；
- `_world_map_layer_label` / `_world_map_layers`：列出可交互底图；
- `_terrain_service_for_world`：加载审核通过的导航数据集；
- `_approved_dataset_meta`：读取数据集元信息。

`WORLD_MAP_DIRECTORY` 同时被 `api.create_app` 用来挂载 `/world-assets`
静态目录，因此 `api.py` 仍从本模块导入该常量，避免两处各写一份路径。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from world_engine.config import PROJECT_ROOT, Settings  # noqa: F401 - Settings 供类型注解使用
from world_engine.database import Database
from world_engine.navigation import TerrainService
from world_engine.repository import WorldNotFoundError, to_iso, utc_now
from world_engine.routing import RoutePlanner

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


def build_map_router(database) -> APIRouter:
    """地图底图、样式偏好与地形通行性接口。"""

    router = APIRouter(prefix="/api/worlds/{world_id}", tags=["地图与地形"])

    @router.get(
        "/terrain",
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

    @router.get("/map-styles")
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

    @router.put("/map-styles", response_model=dict[str, Any])
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

    @router.get("/navigation-dataset")
    def navigation_dataset(world_id: str) -> dict[str, Any]:
        dataset = _approved_dataset_meta(database, world_id)
        if dataset is None:
            raise HTTPException(status_code=404, detail="该世界没有导航数据集")
        return dataset

    @router.get("/passability")
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

    return router
