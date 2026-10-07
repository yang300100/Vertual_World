"""玩家视角与人物档案接口。

这些路由原先内嵌在 `api.create_app` 里。这里按「玩家／人物」搬出，行为与
之完全一致（路径、方法、响应模型、状态码与错误文案一字未改）。

覆盖三块内容：
- 玩家角色本身的创建、意图提交/预览、群体对话与行动反应重试；
- 玩家移动的开始、取消与载具选择；
- 人物档案的读取与改写：记忆、角色卡、肖像。

「人物」路由虽然带 `{character_id}`，但读写契约是玩家视角（角色卡额外
禁止作用于玩家角色），因此随本模块归入玩家域而非世界级端点。
"""

from __future__ import annotations

import base64
import binascii
import logging
import sqlite3
from dataclasses import replace
from typing import Annotated, Any, Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from world_engine.bounded_calls import submit_call
from world_engine.conversations import ConversationService, NpcCharacterCard
from world_engine.decisions import DecisionProviderError
from world_engine.domain import MovementState, PlayerActionResult, WorldSnapshot
from world_engine.engine import ConcurrentWorldUpdateError
from world_engine.event_log import record_event
from world_engine.intent_parser import IntentPreview
from world_engine.photos import PortraitUploadRequest, PortraitView
from world_engine.registration import (
    CharacterArrivalSpec,
    ElementRegistrationSubmit,
    RegistrationStatus,
    WorldElementRegistry,
)
from world_engine.repository import WorldNotFoundError, from_iso, to_iso, utc_now
from world_engine.sillytavern_cards import (
    CardImportError,
    analyze_character_book,
    apply_sillytavern_edits,
    character_book_review_text,
    extract_character_book_fragments,
    parse_sillytavern_card,
    read_sillytavern_source,
    select_character_book_person,
)

LOGGER = logging.getLogger("virtual-world.card-import")


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
    participant_ids: list[str] | None = Field(default=None, max_length=100)
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


class SillyTavernCardImportRequest(BaseModel):
    """从浏览器读取的 PNG/JSON 文件；Yuki 只把角色资料交给 NPC。"""

    model_config = ConfigDict(extra="forbid")

    file_name: str = Field(min_length=5, max_length=255)
    data_url: str = Field(min_length=20, max_length=12 * 1024 * 1024)
    card_overrides: dict[str, str] = Field(default_factory=dict)
    person_name: str | None = Field(default=None, max_length=120)
    include_entry_indices: list[int] = Field(default_factory=list, max_length=20)


class SillyTavernNpcCreateRequest(SillyTavernCardImportRequest):
    """Yuki 让导入卡的新人物拥有明确的世界身份和落脚地点。"""

    name: str | None = Field(default=None, max_length=60)
    identity: str = Field(min_length=1, max_length=100)
    location_id: str = Field(min_length=1, max_length=100)


def _uploaded_card_bytes(payload: SillyTavernCardImportRequest) -> bytes:
    header, comma, encoded = payload.data_url.partition(",")
    if not comma or not header.startswith("data:") or not header.endswith(";base64"):
        raise HTTPException(status_code=422, detail="角色卡文件编码无效")
    try:
        return base64.b64decode(encoded, validate=True)
    except binascii.Error as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _parse_uploaded_sillytavern_card(
    payload: SillyTavernCardImportRequest, *, preview: bool = False,
    backend: object | None = None,
) -> dict[str, str]:
    raw = _uploaded_card_bytes(payload)
    try:
        source, spec = read_sillytavern_source(raw, payload.file_name)
        people = analyze_character_book(source)
        if payload.person_name:
            extractor = None
            if backend is not None and not any(
                payload.card_overrides.get(key)
                for key in ("description", "personality", "scenario")
            ):
                def extractor(person_name: str, content: str) -> dict[str, str]:
                    try:
                        return submit_call(
                            extract_character_book_fragments, backend,
                            person_name=person_name, content=content,
                        ).result(timeout=20)
                    except Exception:
                        LOGGER.info("自由格式人物条目模型提取失败，保留本地解析结果", exc_info=True)
                        return {}
            parsed, _ = select_character_book_person(
                source, spec, payload.person_name, payload.include_entry_indices,
                extractor=extractor,
            )
        elif len(people) > 1:
            raise CardImportError("此角色卡含有多个人物，请先解析并选择一位")
        else:
            parsed = parse_sillytavern_card(raw, payload.file_name)
        edited = apply_sillytavern_edits(
            parsed, payload.card_overrides,
            allow_empty_profile=preview and bool(payload.person_name),
        )
        if preview and payload.person_name:
            person = next(item for item in people if item["name"] == payload.person_name)
            indices = [int(person["entry_index"]), *payload.include_entry_indices]
            entries = source["character_book"]["entries"]
            edited["source_text"] = "\n\n".join(
                f"条目 {index}：{entries[index].get('comment') or ''}\n"
                f"{character_book_review_text(str(entries[index].get('content') or ''))}"
                for index in indices
            )
        return edited
    except CardImportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _attach_card_avatar(
    card: dict[str, str], payload: SillyTavernCardImportRequest,
    photo_service, world_id: str, character_id: str,
) -> None:
    """PNG 卡面只作为头像资源保存，不混进 NPC 的世界事实。"""
    if payload.file_name.casefold().endswith(".png") and not payload.person_name:
        png_data = base64.b64decode(payload.data_url.partition(",")[2], validate=True)
        card["avatar_media_path"] = photo_service.store_card_avatar(
            world_id=world_id, character_id=character_id, png_data=png_data,
        )


class PlayerMoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    destination_longitude: float = Field(ge=-180, le=180)
    destination_latitude: float = Field(ge=-90, le=90)
    vehicle_id: str | None = Field(default=None, max_length=100)


class PlayerSteerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dx: float = Field(ge=-1, le=1)
    dy: float = Field(ge=-1, le=1)
    sequence: int = Field(ge=0)


class PlayerSteerStopRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=0)


class PlayerMoveResponse(BaseModel):
    """开始移动的返回：移动主体 + 规划出的可通行路线。"""

    model_config = ConfigDict(extra="forbid")

    movement: MovementState
    route: dict[str, Any] | None = None


class PlayerTransportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vehicle_id: str | None = Field(default=None, max_length=100)


def build_player_router(database, engine, repository, photo_service, world_pack=None) -> APIRouter:
    """玩家视角与人物档案接口。"""

    router = APIRouter(prefix="/api/worlds/{world_id}", tags=["玩家"])

    @router.post(
        "/player",
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
                    player_defaults=world_pack.player if world_pack is not None else None,
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

    @router.post(
        "/player/act",
        response_model=PlayerActionResult,
        status_code=201,
    )
    def player_act(world_id: str, payload: PlayerIntentRequest) -> PlayerActionResult:
        try:
            for attempt in range(3):
                try:
                    return engine.submit_player_intent(
                        world_id,
                        payload.intent,
                        target_character_id=payload.target_character_id,
                        delivery=payload.delivery,
                        defer_action_reaction=True,
                    )
                except ConcurrentWorldUpdateError:
                    if attempt == 2:
                        raise
            raise AssertionError("重试次数应在循环内结束")
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

    @router.get("/player/activities")
    def player_activities(world_id: str) -> list[dict[str, object]]:
        from world_engine.player_activities import PlayerActivityService

        with database.read() as connection:
            snapshot = repository.get_snapshot(connection, world_id)
            player = next((item for item in snapshot.characters if item.is_player), None)
            if player is None:
                raise HTTPException(status_code=404, detail="玩家角色不存在")
            return PlayerActivityService.recent_records(connection, world_id, player.id)

    @router.post("/player/actions/{event_id}/reaction")
    def retry_action_reaction(world_id: str, event_id: str) -> dict[str, object]:
        from world_engine.player_action_flow import react_to_action

        result = react_to_action(engine, world_id, event_id)
        engine._sync_history_safely(world_id, background=True)
        return result

    @router.get("/player/actions/{event_id}/reaction")
    def get_action_reaction(world_id: str, event_id: str) -> dict[str, object]:
        from world_engine.player_action_flow import action_reaction_status

        return action_reaction_status(engine, world_id, event_id)

    @router.post(
        "/player/group-dialogue",
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

    @router.post(
        "/player/intents/preview",
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

    @router.post("/player/steer")
    def steer_player(world_id: str, payload: PlayerSteerRequest) -> dict[str, object]:
        try:
            return engine.steer_player(world_id, dx=payload.dx, dy=payload.dy,
                                       sequence=payload.sequence)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ValueError, LookupError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/player/steer/stop")
    def stop_steering(world_id: str, payload: PlayerSteerStopRequest) -> dict[str, object]:
        try:
            return engine.steer_player(world_id, dx=0, dy=0,
                                       sequence=payload.sequence, stop=True)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ValueError, LookupError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post(
        "/player/move",
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

    @router.post(
        "/player/move/cancel",
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

    @router.patch(
        "/player/transport",
        response_model=WorldSnapshot,
    )
    def select_player_transport(world_id: str, payload: PlayerTransportRequest) -> WorldSnapshot:
        try:
            return engine.select_player_transport(world_id, payload.vehicle_id)
        except WorldNotFoundError as exc:
            raise HTTPException(status_code=404, detail="世界不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/characters/{character_id}/memories")
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

    @router.get("/characters/{character_id}/character-card")
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

    @router.put("/characters/{character_id}/character-card")
    def update_character_card(
        world_id: str,
        character_id: str,
        payload: CharacterCardRequest,
    ) -> dict[str, object]:
        """更新角色卡，不改变角色属性、关系、记忆或任何世界事实。"""
        with database.write() as connection:
            snapshot = repository.get_snapshot(connection, world_id)
            character = snapshot.character_by_id(character_id)
            if character is None or character.is_player:
                raise HTTPException(status_code=404, detail="NPC不存在")
            existing = engine.conversations.get_card(
                connection, world_id=world_id, npc=character,
            )
            updated = replace(
                payload.to_card(), sillytavern_card=existing.sillytavern_card,
            )
            ConversationService.save_card(
                connection, world_id=world_id, npc_id=character_id, card=updated,
            )
        return updated.to_dict()

    @router.post("/characters/{character_id}/sillytavern-card")
    def import_sillytavern_card(
        world_id: str, character_id: str, payload: SillyTavernCardImportRequest,
    ) -> dict[str, object]:
        """为现有 NPC 导入外部扮演资料；世界中的身份和事实保持原样。"""
        with database.write() as connection:
            snapshot = repository.get_snapshot(connection, world_id)
            character = snapshot.character_by_id(character_id)
            if character is None or character.is_player:
                raise HTTPException(status_code=404, detail="NPC不存在")
            imported = _parse_uploaded_sillytavern_card(
                payload, backend=engine.agent_model_backend,
            )
            _attach_card_avatar(imported, payload, photo_service, world_id, character_id)
            existing = engine.conversations.get_card(
                connection, world_id=world_id, npc=character,
            )
            updated = replace(existing, sillytavern_card=imported)
            ConversationService.save_card(
                connection, world_id=world_id, npc_id=character_id, card=updated,
            )
            return updated.to_dict()

    @router.post("/characters/sillytavern-preview")
    def preview_sillytavern_card(
        world_id: str, payload: SillyTavernCardImportRequest,
    ) -> dict[str, str]:
        """只解析并展示卡片内容，等待玩家改写后才写入世界。"""
        with database.read() as connection:
            exists = connection.execute("SELECT 1 FROM worlds WHERE id=?", (world_id,)).fetchone()
            if exists is None:
                raise HTTPException(status_code=404, detail="世界不存在")
        return _parse_uploaded_sillytavern_card(
            payload, preview=True, backend=engine.agent_model_backend,
        )

    @router.post("/characters/sillytavern-analyze")
    def analyze_sillytavern_upload(
        world_id: str, payload: SillyTavernCardImportRequest,
    ) -> dict[str, object]:
        """返回候选人物与补充条目编号，供哥哥确认后再绑定。"""
        with database.read() as connection:
            exists = connection.execute("SELECT 1 FROM worlds WHERE id=?", (world_id,)).fetchone()
            if exists is None:
                raise HTTPException(status_code=404, detail="世界不存在")
        try:
            source, spec = read_sillytavern_source(
                _uploaded_card_bytes(payload), payload.file_name,
            )
            people = analyze_character_book(source)
            entries = (source.get("character_book") or {}).get("entries") or []
            return {
                "spec": spec, "people": people,
                "entries": [{"index": index, "title": str(item.get("comment") or f"条目 {index}"),
                             "enabled": item.get("enabled", True) is True,
                             "requires_manual_review": (
                                 "# SFW" not in str(item.get("content") or "")
                             )}
                            for index, item in enumerate(entries) if isinstance(item, dict)],
            }
        except CardImportError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/characters/from-sillytavern", status_code=201)
    def create_npc_from_sillytavern(
        world_id: str, payload: SillyTavernNpcCreateRequest,
    ) -> dict[str, str]:
        """Yuki 将卡片扮演资料与人物到来登记一起原子写入世界。"""
        imported = _parse_uploaded_sillytavern_card(
            payload, backend=engine.agent_model_backend,
        )
        name = (payload.name or imported["name"]).strip()
        if not name or len(name) > 60:
            raise HTTPException(status_code=422, detail="NPC 名字须为 1 到 60 字")
        identity = payload.identity.strip()
        if not identity:
            raise HTTPException(status_code=422, detail="请填写 NPC 身份")
        with database.write() as connection:
            world = connection.execute(
                "SELECT current_time FROM worlds WHERE id = ?", (world_id,)
            ).fetchone()
            if world is None:
                raise HTTPException(status_code=404, detail="世界不存在")
            location = connection.execute(
                "SELECT id FROM locations WHERE id = ? AND world_id = ?",
                (payload.location_id, world_id),
            ).fetchone()
            if location is None:
                raise HTTPException(status_code=404, detail="所选地点不存在")
            duplicate = connection.execute(
                "SELECT 1 FROM characters WHERE world_id = ? AND name = ?",
                (world_id, name),
            ).fetchone()
            if duplicate is not None:
                raise HTTPException(status_code=409, detail="当前世界已有同名人物")
            source_id = record_event(
                connection, world_id=world_id, tick_id=f"card-import:{uuid4()}",
                occurred_at=from_iso(world["current_time"]),
                event_type="world.character_card_imported",
                summary=f"编年者登记了{name}的到来。", location_id=payload.location_id,
                payload={"name": name, "location_id": payload.location_id},
            )
            registration = WorldElementRegistry().submit(
                connection, world_id=world_id,
                request=ElementRegistrationSubmit(
                    source_event_id=source_id, idempotency_key=f"card-import:{uuid4()}",
                    payload=CharacterArrivalSpec(
                        name=name, identity=identity,
                        location_id=payload.location_id, activation_policy="persistent",
                    ),
                ),
            )
            if (
                registration.status is not RegistrationStatus.APPLIED
                or not registration.result_entity_id
            ):
                raise HTTPException(
                    status_code=422,
                    detail=registration.rejection_reason or "NPC 登记失败",
                )
            character_id = registration.result_entity_id
            _attach_card_avatar(imported, payload, photo_service, world_id, character_id)
            connection.execute(
                """UPDATE characters SET is_core = 1, activation_state = 'active',
                   activation_policy = 'persistent', activation_reason = 'sillytavern_import',
                   updated_at = ? WHERE id = ? AND world_id = ?""",
                (to_iso(utc_now()), character_id, world_id),
            )
            npc = repository.get_snapshot(connection, world_id).character_by_id(character_id)
            base_card = engine.conversations.get_card(
                connection, world_id=world_id, npc=npc,
            )
            ConversationService.save_card(
                connection, world_id=world_id, npc_id=character_id,
                card=replace(base_card, sillytavern_card=imported),
            )
        return {"character_id": character_id, "name": name}

    @router.delete("/characters/{character_id}/sillytavern-card")
    def remove_sillytavern_card(world_id: str, character_id: str) -> dict[str, object]:
        """Yuki 移除外部扮演资料时，会留下本地角色卡和世界身份。"""
        with database.write() as connection:
            snapshot = repository.get_snapshot(connection, world_id)
            character = snapshot.character_by_id(character_id)
            if character is None or character.is_player:
                raise HTTPException(status_code=404, detail="NPC不存在")
            existing = engine.conversations.get_card(
                connection, world_id=world_id, npc=character,
            )
            updated = replace(existing, sillytavern_card=None)
            ConversationService.save_card(
                connection, world_id=world_id, npc_id=character_id, card=updated,
            )
            return updated.to_dict()

    @router.put(
        "/characters/{character_id}/portrait",
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

    return router
