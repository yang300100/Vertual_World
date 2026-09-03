from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field

from world_engine.config import PROJECT_ROOT, Settings
from world_engine.geo import great_circle_distance_km
from world_engine.iserra_time import to_iserra_calendar
from world_engine.navigation import TerrainService
from world_engine.repository import from_iso, to_iso, utc_now

_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_GENERATED_IMAGE_BYTES = 20 * 1024 * 1024
_DIRECTION_BEARINGS = {"north": 0.0, "east": 90.0, "south": 180.0, "west": 270.0}
_DIRECTION_LABELS = {"north": "北方", "east": "东方", "south": "南方", "west": "西方"}
_MIME_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


class PortraitUploadRequest(BaseModel):
    """浏览器将文件转成 data URL 上传，避免为单一接口增加 multipart 运行依赖。"""

    model_config = ConfigDict(extra="forbid")

    data_url: str = Field(min_length=32, max_length=12_000_000)


class PortraitView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    character_id: str
    url: str
    mime_type: str
    created_at: object


class PhotoCaptureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    photographer_character_id: str = Field(min_length=1, max_length=100)
    direction: Literal["north", "east", "south", "west"] = "north"
    include_self: bool = True
    include_nearby_npcs: bool = True
    included_character_ids: list[str] = Field(default_factory=list, max_length=12)
    style_hint: str = Field(default="", max_length=500)


class PhotoCaptureView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    world_id: str
    photographer_character_id: str
    direction: str
    included_character_ids: list[str]
    prompt: str
    context: dict[str, Any]
    image_url: str
    model_name: str
    created_at: object


class PhotoGenerationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class GeneratedImage:
    content: bytes
    mime_type: str


@dataclass(frozen=True, slots=True)
class PreparedPhotoCapture:
    """在只读快照中冻结的拍照事实；外部生图不应占用世界写锁。"""

    world_id: str
    photographer_character_id: str
    photographer_name: str
    direction: str
    include_self: bool
    included_character_ids: tuple[str, ...]
    world_time: str
    location_id: str | None
    prompt: str
    context: dict[str, Any]
    reference_images: tuple[str, ...]


class ImageGenerationClient(Protocol):
    def generate(self, *, prompt: str, reference_images: list[str]) -> GeneratedImage: ...


class SeedreamImageClient:
    """兼容 Ark/Seedream images/generations 协议的最小客户端。

    模型、地址、响应格式全部来自 .env，故也可接入兼容该协议的自建网关。
    """

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self._client = client

    def generate(self, *, prompt: str, reference_images: list[str]) -> GeneratedImage:
        if not self.settings.image_api_key:
            raise PhotoGenerationError("尚未配置 IMAGE_API_KEY，不能调用生图模型")
        if not self.settings.image_model:
            raise PhotoGenerationError("尚未配置 IMAGE_MODEL，不能调用生图模型")
        payload: dict[str, Any] = {
            "model": self.settings.image_model,
            "prompt": prompt,
            "size": self.settings.image_size,
            "response_format": self.settings.image_response_format,
            "n": 1,
        }
        if reference_images:
            # Seedream 兼容接口以 image 数组接收参考图；data URL 可避免将本地人设图暴露为公网 URL。
            payload["image"] = reference_images[:4]
        own_client = self._client is None
        client = self._client or httpx.Client(timeout=self.settings.image_timeout_seconds)
        try:
            response = client.post(
                f"{self.settings.image_base_url}/images/generations",
                headers={"Authorization": f"Bearer {self.settings.image_api_key}"},
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
            item = self._first_image_item(body)
            if "b64_json" in item:
                return GeneratedImage(
                    content=self._decode_generated_base64(str(item["b64_json"])),
                    mime_type="image/png",
                )
            url = item.get("url") or item.get("image_url")
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                raise PhotoGenerationError("生图接口没有返回可保存的图片数据")
            downloaded = client.get(url)
            downloaded.raise_for_status()
            if len(downloaded.content) > _MAX_GENERATED_IMAGE_BYTES:
                raise PhotoGenerationError("生成图片超过允许的本地保存上限")
            mime_type = downloaded.headers.get("content-type", "image/png").split(";", 1)[0]
            if mime_type not in _MIME_EXTENSIONS:
                mime_type = "image/png"
            return GeneratedImage(content=downloaded.content, mime_type=mime_type)
        except httpx.HTTPError as exc:
            raise PhotoGenerationError(
                "生图服务请求失败，请检查 IMAGE_BASE_URL、模型名和网络"
            ) from exc
        except (ValueError, TypeError, KeyError) as exc:
            raise PhotoGenerationError("生图服务返回了无法解析的结果") from exc
        finally:
            if own_client:
                client.close()

    @staticmethod
    def _first_image_item(body: Any) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise PhotoGenerationError("生图服务返回格式错误")
        candidates = body.get("data") or body.get("images")
        if (
            not isinstance(candidates, list)
            or not candidates
            or not isinstance(candidates[0], dict)
        ):
            raise PhotoGenerationError("生图服务没有返回图片")
        return candidates[0]

    @staticmethod
    def _decode_generated_base64(value: str) -> bytes:
        try:
            content = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise PhotoGenerationError("生图服务返回的图片编码无效") from exc
        if not content or len(content) > _MAX_GENERATED_IMAGE_BYTES:
            raise PhotoGenerationError("生成图片为空或超过允许的本地保存上限")
        return content


class PhotoService:
    """把世界事实编译为照片提示词，再持久化照片与其当时的上下文。"""

    def __init__(
        self,
        settings: Settings,
        image_client: ImageGenerationClient | None = None,
    ) -> None:
        self.settings = settings
        self.image_client = image_client or SeedreamImageClient(settings)
        self.media_directory = settings.media_directory or (PROJECT_ROOT / "data" / "world-media")
        self.media_directory.mkdir(parents=True, exist_ok=True)

    def upload_portrait(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        character_id: str,
        payload: PortraitUploadRequest,
    ) -> PortraitView:
        character = connection.execute(
            "SELECT id FROM characters WHERE id = ? AND world_id = ?",
            (character_id, world_id),
        ).fetchone()
        if character is None:
            raise LookupError("人物不存在或不属于当前世界")
        content, mime_type = self._decode_data_url(payload.data_url)
        digest = hashlib.sha256(content).hexdigest()
        relative_path = (
            Path(world_id) / "portraits" / character_id / f"{digest}.{_MIME_EXTENSIONS[mime_type]}"
        )
        destination = self.media_directory / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            destination.write_bytes(content)
        now = utc_now()
        connection.execute(
            """
            UPDATE character_portraits SET is_active = 0
            WHERE world_id = ? AND character_id = ? AND is_active = 1
            """,
            (world_id, character_id),
        )
        connection.execute(
            """
            INSERT INTO character_portraits(
                id, world_id, character_id, media_path, mime_type, sha256, is_active, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                str(uuid4()),
                world_id,
                character_id,
                relative_path.as_posix(),
                mime_type,
                digest,
                to_iso(now),
            ),
        )
        return PortraitView(
            character_id=character_id,
            url=self._media_url(relative_path),
            mime_type=mime_type,
            created_at=now,
        )

    def prepare_capture(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        request: PhotoCaptureRequest,
    ) -> PreparedPhotoCapture:
        photographer = connection.execute(
            "SELECT * FROM characters WHERE id = ? AND world_id = ?",
            (request.photographer_character_id, world_id),
        ).fetchone()
        if photographer is None:
            raise LookupError("拍照人物不存在或不属于当前世界")
        if not bool(photographer["is_player"]) or not bool(photographer["is_pov"]):
            raise ValueError("只能由当前玩家视角拍照")
        world = connection.execute("SELECT * FROM worlds WHERE id = ?", (world_id,)).fetchone()
        if world is None:
            raise LookupError("世界不存在")
        context = self._build_context(
            connection, world=world, photographer=photographer, request=request
        )
        prompt = self._build_prompt(context, request.style_hint)
        references = self._reference_images(
            connection,
            world_id=world_id,
            character_ids=context["included_character_ids"],
        )
        return PreparedPhotoCapture(
            world_id=world_id,
            photographer_character_id=photographer["id"],
            photographer_name=photographer["name"],
            direction=request.direction,
            include_self=request.include_self,
            included_character_ids=tuple(context["included_character_ids"]),
            world_time=world["current_time"],
            location_id=photographer["current_location_id"] or photographer["location_id"],
            prompt=prompt,
            context=context,
            reference_images=tuple(references),
        )

    def generate_capture(self, prepared: PreparedPhotoCapture) -> GeneratedImage:
        """网络调用必须发生在数据库事务之外。"""

        return self.image_client.generate(
            prompt=prepared.prompt,
            reference_images=list(prepared.reference_images),
        )

    def finalize_capture(
        self,
        connection: sqlite3.Connection,
        *,
        prepared: PreparedPhotoCapture,
        generated: GeneratedImage,
    ) -> PhotoCaptureView:
        """短事务：只写最终媒体路径、事件和照片审计。"""

        world = connection.execute(
            "SELECT id FROM worlds WHERE id = ?", (prepared.world_id,)
        ).fetchone()
        photographer = connection.execute(
            """
            SELECT id FROM characters
            WHERE id = ? AND world_id = ? AND is_player = 1 AND is_pov = 1
            """,
            (prepared.photographer_character_id, prepared.world_id),
        ).fetchone()
        if world is None or photographer is None:
            raise LookupError("拍照期间世界或玩家视角已变化，请重新取景")
        image_path = self._persist_generated_image(
            world_id=prepared.world_id,
            generated=generated,
            capture_id=str(uuid4()),
        )
        now = utc_now()
        capture_id = image_path.stem
        event_id = str(uuid4())
        connection.execute(
            """
            INSERT INTO world_events(
                id, world_id, tick_id, occurred_at, event_type, actor_id,
                location_id, summary, importance, payload_json, created_at
            ) VALUES (?, ?, ?, ?, 'world.photo_captured', ?, ?, ?, 'routine', ?, ?)
            """,
            (
                event_id,
                prepared.world_id,
                capture_id,
                prepared.world_time,
                prepared.photographer_character_id,
                prepared.location_id,
                f"{prepared.photographer_name}面向{_DIRECTION_LABELS[prepared.direction]}留下了一张照片。",
                json.dumps(
                    {"capture_id": capture_id, "direction": prepared.direction},
                    ensure_ascii=False,
                ),
                to_iso(now),
            ),
        )
        connection.execute(
            """
            INSERT INTO photo_captures(
                id, world_id, photographer_character_id, direction, include_self,
                included_character_ids_json, prompt, context_json, media_path,
                model_name, source_event_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                capture_id,
                prepared.world_id,
                prepared.photographer_character_id,
                prepared.direction,
                int(prepared.include_self),
                json.dumps(prepared.included_character_ids, ensure_ascii=False),
                prepared.prompt,
                json.dumps(prepared.context, ensure_ascii=False),
                image_path.as_posix(),
                self.settings.image_model,
                event_id,
                to_iso(now),
            ),
        )
        return PhotoCaptureView(
            id=capture_id,
            world_id=prepared.world_id,
            photographer_character_id=prepared.photographer_character_id,
            direction=prepared.direction,
            included_character_ids=list(prepared.included_character_ids),
            prompt=prepared.prompt,
            context=prepared.context,
            image_url=self._media_url(image_path),
            model_name=self.settings.image_model,
            created_at=now,
        )

    def list_captures(
        self, connection: sqlite3.Connection, *, world_id: str, limit: int = 30
    ) -> list[PhotoCaptureView]:
        rows = connection.execute(
            "SELECT * FROM photo_captures WHERE world_id = ? ORDER BY created_at DESC LIMIT ?",
            (world_id, max(1, min(limit, 100))),
        ).fetchall()
        return [
            PhotoCaptureView(
                id=row["id"],
                world_id=row["world_id"],
                photographer_character_id=row["photographer_character_id"],
                direction=row["direction"],
                included_character_ids=json.loads(row["included_character_ids_json"]),
                prompt=row["prompt"],
                context=json.loads(row["context_json"]),
                image_url=self._media_url(Path(row["media_path"])),
                model_name=row["model_name"],
                created_at=from_iso(row["created_at"]),
            )
            for row in rows
        ]

    def _build_context(
        self,
        connection: sqlite3.Connection,
        *,
        world: sqlite3.Row,
        photographer: sqlite3.Row,
        request: PhotoCaptureRequest,
    ) -> dict[str, Any]:
        longitude, latitude = float(photographer["longitude"]), float(photographer["latitude"])
        location = connection.execute(
            "SELECT * FROM locations WHERE id = ? AND world_id = ? AND is_active = 1",
            (photographer["current_location_id"] or photographer["location_id"], world["id"]),
        ).fetchone()
        visible = self._visible_characters(
            connection,
            world_id=world["id"],
            photographer=photographer,
            request=request,
        )
        landmarks = self._visible_landmarks(
            connection,
            world_id=world["id"],
            longitude=longitude,
            latitude=latitude,
            direction=request.direction,
        )
        terrain = self._terrain_context(
            connection, world_id=world["id"], longitude=longitude, latitude=latitude
        )
        time_context = self._time_context(world["current_time"])
        return {
            "world_name": world["name"],
            "world_time": world["current_time"],
            "time": time_context,
            "camera": {
                "direction": request.direction,
                "direction_label": _DIRECTION_LABELS[request.direction],
                "longitude": longitude,
                "latitude": latitude,
                "location_name": location["name"] if location is not None else "未命名地域",
                "location_kind": location["kind"] if location is not None else "荒野",
            },
            "terrain": terrain,
            "landmarks": landmarks,
            "characters": visible,
            "included_character_ids": [item["id"] for item in visible],
        }

    def _visible_characters(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        photographer: sqlite3.Row,
        request: PhotoCaptureRequest,
    ) -> list[dict[str, Any]]:
        nearby = connection.execute(
            "SELECT * FROM characters WHERE world_id = ? AND health > 0 ORDER BY name",
            (world_id,),
        ).fetchall()
        nearby_by_id: dict[str, sqlite3.Row] = {}
        for character in nearby:
            distance = great_circle_distance_km(
                float(photographer["longitude"]),
                float(photographer["latitude"]),
                float(character["longitude"]),
                float(character["latitude"]),
            )
            same_location = character["current_location_id"] and character[
                "current_location_id"
            ] == (photographer["current_location_id"] or photographer["location_id"])
            if distance <= 1.5 or same_location:
                nearby_by_id[character["id"]] = character
        requested_ids = set(request.included_character_ids)
        invalid = requested_ids - set(nearby_by_id)
        invalid.discard(photographer["id"])
        if invalid:
            raise ValueError("只能让当前视野内的人物出现在照片中")
        selected_ids: set[str] = set(requested_ids)
        if request.include_self:
            selected_ids.add(photographer["id"])
        if request.include_nearby_npcs:
            selected_ids.update(
                char_id for char_id in nearby_by_id if char_id != photographer["id"]
            )
        result: list[dict[str, Any]] = []
        for character_id in sorted(selected_ids):
            character = (
                photographer
                if character_id == photographer["id"]
                else nearby_by_id.get(character_id)
            )
            if character is None:
                continue
            result.append(
                {
                    "id": character["id"],
                    "name": character["name"],
                    "identity": character["identity"] or "身份未明",
                    "traits": json.loads(character["traits_json"]),
                    "is_self": character["id"] == photographer["id"],
                }
            )
        return result

    def _visible_landmarks(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        longitude: float,
        latitude: float,
        direction: str,
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        sources = (
            ("buildings", "building", "name", "building_type", "status NOT IN ('ruined')"),
            ("world_structures", "structure", "name", "structure_type", "status NOT IN ('ruined')"),
            ("map_features", "map_feature", "name", "feature_type", "is_known = 1"),
        )
        for table, entity_type, name_col, kind_col, state_clause in sources:
            rows = connection.execute(
                f"""
                SELECT id, {name_col} AS name, {kind_col} AS kind, longitude, latitude
                FROM {table} WHERE world_id = ? AND {state_clause}
                """,  # noqa: S608 - 表/列/状态均是固定映射。
                (world_id,),
            ).fetchall()
            for row in rows:
                distance = great_circle_distance_km(
                    longitude, latitude, row["longitude"], row["latitude"]
                )
                bearing = self._bearing(longitude, latitude, row["longitude"], row["latitude"])
                if (
                    distance <= 12
                    and self._direction_difference(_DIRECTION_BEARINGS[direction], bearing) <= 62
                ):
                    candidates.append(
                        {
                            "id": row["id"],
                            "type": entity_type,
                            "name": row["name"],
                            "kind": row["kind"],
                            "distance_km": round(distance, 2),
                            "bearing": round(bearing, 1),
                        }
                    )
        return sorted(candidates, key=lambda item: (item["distance_km"], item["name"]))[:8]

    @staticmethod
    def _terrain_context(
        connection: sqlite3.Connection,
        *,
        world_id: str,
        longitude: float,
        latitude: float,
    ) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT asset_root FROM navigation_datasets
            WHERE world_id = ? AND review_status = 'approved'
            ORDER BY approved_at DESC, created_at DESC LIMIT 1
            """,
            (world_id,),
        ).fetchone()
        if row is None:
            return {"available": False, "description": "未加载可用的高程地形数据"}
        try:
            sample = TerrainService(PROJECT_ROOT / row["asset_root"]).sample(longitude, latitude)
        except (OSError, ValueError, KeyError, TypeError):
            sample = None
        if sample is None:
            return {"available": False, "description": "当前位置不在已审核地形数据范围内"}
        return {
            "available": True,
            **sample,
            "landform": PhotoService._landform_from_terrain(sample),
        }

    @staticmethod
    def _landform_from_terrain(terrain: dict[str, Any]) -> str:
        """只根据已审核的地表、水系、高程和坡度推导地貌，不虚构局部景观。"""

        surface = str(terrain.get("surface_type", ""))
        water_kind = terrain.get("water_kind")
        elevation = float(terrain.get("elevation_m", 0))
        slope = float(terrain.get("slope_degrees", 0))
        if surface == "marine" or water_kind == "ocean":
            return "近海海域"
        if water_kind == "lake":
            return "湖泊水域"
        if water_kind == "river":
            return "河道与河谷"
        if surface == "wetland":
            return "湿地低地"
        if surface == "glacier" or elevation >= 3200:
            return "冰川高山"
        if elevation >= 1800:
            return "高山高原"
        if elevation >= 800:
            return "高地丘陵"
        if slope >= 2.5:
            return "起伏丘陵"
        if surface == "desert":
            return "干旱荒漠平原"
        return "缓坡平原"

    @staticmethod
    def _time_context(raw_world_time: str) -> dict[str, str | int]:
        calendar = to_iserra_calendar(from_iso(raw_world_time))
        season_index = (calendar.month - 1) // 3
        season = ("初春", "盛夏", "秋季", "隆冬")[season_index]
        hour = calendar.hour
        if 5 <= hour < 8:
            period, lighting = "黎明", "低角度晨光与长阴影"
        elif 8 <= hour < 11:
            period, lighting = "上午", "清亮的自然日光"
        elif 11 <= hour < 16:
            period, lighting = "正午", "明亮直射日光"
        elif 16 <= hour < 19:
            period, lighting = "黄昏", "暖色余晖与拉长的阴影"
        elif 19 <= hour < 22:
            period, lighting = "夜晚", "暮色、月光和人造光源"
        else:
            period, lighting = "深夜", "低照度夜色与局部光源"
        return {
            "iserra_calendar": calendar.display(include_seconds=False),
            "season": season,
            "day_period": period,
            "lighting": lighting,
            "hour": hour,
        }

    @staticmethod
    def _build_prompt(context: dict[str, Any], style_hint: str) -> str:
        camera = context["camera"]
        terrain = context["terrain"]
        time_context = context["time"]
        terrain_text = (
            f"{terrain['landform']}地貌，{terrain.get('biome', terrain['surface_type'])}生物群系，"
            f"{terrain['surface_type']}地表，海拔{terrain['elevation_m']}米，"
            f"坡度{terrain['slope_degrees']}度"
            if terrain.get("available")
            else terrain["description"]
        )
        landmarks = context["landmarks"]
        landmark_text = (
            "；".join(
                f"{item['distance_km']}公里处的{item['kind']}“{item['name']}”" for item in landmarks
            )
            if landmarks
            else "视野内没有已登记的建筑、遗迹或奇观"
        )
        characters = context["characters"]
        character_text = (
            "；".join(
                f"{'自拍主体' if item['is_self'] else '同行人物'}："
                f"{item['name']}（{item['identity']}，"
                f"性格：{'、'.join(item['traits']) or '未记录'}）"
                for item in characters
            )
            if characters
            else "画面中不出现人物"
        )
        hint = style_hint.strip()
        return "\n".join(
            (
                "生成一张无文字、无界面、无水印的沉浸式奇幻世界现场照片。",
                f"世界：{context['world_name']}；世界时间：{time_context['iserra_calendar']}。",
                f"季节与光照：{time_context['season']}的{time_context['day_period']}，"
                f"{time_context['lighting']}。",
                f"相机位于{camera['location_name']}（{camera['location_kind']}），坐标"
                f"({camera['longitude']:.5f}, {camera['latitude']:.5f}），"
                f"镜头明确朝向{camera['direction_label']}。",
                f"高程与地形：{terrain_text}。",
                f"同一方向可见地标：{landmark_text}。",
                f"画面人物：{character_text}。上传的人设图只用于保持对应人物外观一致。",
                "画面必须遵循上述空间方向、地形、建筑/遗迹与人物在场信息，避免凭空加入相互矛盾的地标。",
                f"附加摄影偏好：{hint}" if hint else "附加摄影偏好：自然光影、纪实构图。",
            )
        )

    def _reference_images(
        self, connection: sqlite3.Connection, *, world_id: str, character_ids: list[str]
    ) -> list[str]:
        if not character_ids:
            return []
        placeholders = ",".join("?" for _ in character_ids)
        rows = connection.execute(
            f"""
            SELECT character_id, media_path, mime_type FROM character_portraits
            WHERE world_id = ? AND is_active = 1 AND character_id IN ({placeholders})
            ORDER BY created_at DESC
            """,  # noqa: S608 - 占位符只由角色数量生成。
            (world_id, *character_ids),
        ).fetchall()
        result: list[str] = []
        seen: set[str] = set()
        for row in rows:
            if row["character_id"] in seen:
                continue
            path = self.media_directory / row["media_path"]
            try:
                content = path.read_bytes()
            except OSError:
                continue
            if not content or len(content) > _MAX_IMAGE_BYTES:
                continue
            seen.add(row["character_id"])
            result.append(
                f"data:{row['mime_type']};base64,{base64.b64encode(content).decode('ascii')}"
            )
        return result

    def _persist_generated_image(
        self, *, world_id: str, generated: GeneratedImage, capture_id: str
    ) -> Path:
        if not generated.content or len(generated.content) > _MAX_GENERATED_IMAGE_BYTES:
            raise PhotoGenerationError("生成图片为空或超过允许的本地保存上限")
        mime_type = generated.mime_type if generated.mime_type in _MIME_EXTENSIONS else "image/png"
        relative_path = Path(world_id) / "photos" / f"{capture_id}.{_MIME_EXTENSIONS[mime_type]}"
        destination = self.media_directory / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(generated.content)
        return relative_path

    @staticmethod
    def _decode_data_url(data_url: str) -> tuple[bytes, str]:
        try:
            header, encoded = data_url.split(",", 1)
        except ValueError as exc:
            raise ValueError("人设图必须是浏览器上传的 data URL") from exc
        if not header.startswith("data:") or ";base64" not in header:
            raise ValueError("人设图必须使用 base64 data URL")
        mime_type = header[5:].split(";", 1)[0].lower()
        if mime_type not in _MIME_EXTENSIONS:
            raise ValueError("仅支持 PNG、JPEG 或 WebP 人设图")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("人设图编码无效") from exc
        if not content or len(content) > _MAX_IMAGE_BYTES:
            raise ValueError("人设图为空或超过 8MiB 上限")
        signatures = {
            "image/png": b"\x89PNG\r\n\x1a\n",
            "image/jpeg": b"\xff\xd8\xff",
            "image/webp": b"RIFF",
        }
        if not content.startswith(signatures[mime_type]):
            raise ValueError("人设图内容与声明格式不一致")
        if mime_type == "image/webp" and content[8:12] != b"WEBP":
            raise ValueError("人设图内容与声明格式不一致")
        return content, mime_type

    @staticmethod
    def _bearing(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
        lon1_r, lat1_r, lon2_r, lat2_r = map(math.radians, (lon1, lat1, lon2, lat2))
        delta = lon2_r - lon1_r
        x = math.sin(delta) * math.cos(lat2_r)
        y = math.cos(lat1_r) * math.sin(lat2_r) - math.sin(lat1_r) * math.cos(lat2_r) * math.cos(
            delta
        )
        return (math.degrees(math.atan2(x, y)) + 360) % 360

    @staticmethod
    def _direction_difference(left: float, right: float) -> float:
        return abs((left - right + 180) % 360 - 180)

    @staticmethod
    def _media_url(relative_path: Path) -> str:
        return f"/world-media/{relative_path.as_posix()}"
