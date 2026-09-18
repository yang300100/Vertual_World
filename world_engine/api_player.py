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

import sqlite3
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from world_engine.conversations import ConversationService, NpcCharacterCard
from world_engine.decisions import DecisionProviderError
from world_engine.domain import MovementState, PlayerActionResult, WorldSnapshot
from world_engine.engine import ConcurrentWorldUpdateError
from world_engine.intent_parser import IntentPreview
from world_engine.photos import PortraitUploadRequest, PortraitView
from world_engine.repository import WorldNotFoundError


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


class PlayerTransportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vehicle_id: str | None = Field(default=None, max_length=100)


def build_player_router(database, engine, repository, photo_service) -> APIRouter:
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
        engine._sync_history_safely(world_id)
        return result

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
