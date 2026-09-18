"""世界元素注册的模型层：枚举、候选 Spec、视图、异常与处理器协议。

这些定义原先内嵌在 `registration.py` 里。这里按「数据契約」搬出，
行为与之完全一致（字段、校验、错误文案一字未改）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from world_engine.activity_tasks import ActivityRecipeSpec
from world_engine.economy import CommoditySpec, WorkplaceBudgetSpec
from world_engine.interiors import InteriorRoomSpec
from world_engine.routines import RoutinePlanSpec


class ElementType(StrEnum):
    CHARACTER_BIRTH = "character_birth"
    CHARACTER_ARRIVAL = "character_arrival"
    SETTLEMENT = "settlement"
    BUILDING = "building"
    STRUCTURE = "structure"
    LORE = "lore"
    INTERIOR_ROOM = "interior_room"
    ACTIVITY_RECIPE = "activity_recipe"
    COMMODITY = "commodity"
    WORKPLACE_BUDGET = "workplace_budget"
    NPC_ROUTINE = "npc_routine"


class RegistrationStatus(StrEnum):
    PROPOSED = "proposed"
    VALIDATING = "validating"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLYING = "applying"
    APPLIED = "applied"
    FAILED = "failed"


class CharacterBirthSpec(BaseModel):
    """人物出生登记；planned 只登记计划，born 才创建人物。"""

    model_config = ConfigDict(extra="forbid")

    element_type: Literal["character_birth"] = "character_birth"
    name: str = Field(min_length=1, max_length=60)
    parent_character_ids: list[str] = Field(min_length=1, max_length=2)
    relation_type: Literal["biological", "adoptive", "guardian"] = "biological"
    birth_state: Literal["planned", "born"] = "planned"
    identity: str = Field(default="幼童", min_length=1, max_length=100)
    traits: list[str] = Field(default_factory=list, max_length=5)
    goals: list[str] = Field(default_factory=list, max_length=3)
    location_id: str | None = None
    parent_registration_id: str | None = None
    species: str | None = Field(default=None, min_length=1, max_length=40)
    gender: Literal["女性", "男性"] | None = None


class CharacterArrivalSpec(BaseModel):
    """独立人物抵达登记；适用于无既有血缘的新 NPC。"""

    model_config = ConfigDict(extra="forbid")

    element_type: Literal["character_arrival"] = "character_arrival"
    name: str = Field(min_length=1, max_length=60)
    identity: str = Field(min_length=1, max_length=100)
    location_id: str
    traits: list[str] = Field(default_factory=list, max_length=5)
    goals: list[str] = Field(default_factory=list, max_length=3)
    species: str = Field(default="human", min_length=1, max_length=40)
    gender: Literal["女性", "男性"] | None = None
    birth_world_time: datetime | None = None
    activation_policy: Literal["distance", "persistent"] = "distance"
    longitude: float | None = Field(default=None, ge=-180, le=180)
    latitude: float | None = Field(default=None, ge=-90, le=90)


class SettlementSpec(BaseModel):
    """聚落登记；玩家申请默认从规划阶段开始。"""

    model_config = ConfigDict(extra="forbid")

    element_type: Literal["settlement"] = "settlement"
    name: str = Field(min_length=1, max_length=100)
    settlement_kind: Literal["village", "town", "city"] = "village"
    stage: Literal["planned", "established"] = "planned"
    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-90, le=90)
    area_radius_km: float = Field(default=2.0, ge=0.1, le=500)
    resources: dict[str, int] = Field(default_factory=dict)
    requirements: dict[str, int] = Field(default_factory=dict)


class BuildingSpec(BaseModel):
    """建筑登记；独立记录施工状态，不把建筑伪装成普通地点。"""

    model_config = ConfigDict(extra="forbid")

    element_type: Literal["building"] = "building"
    name: str = Field(min_length=1, max_length=100)
    building_type: str = Field(min_length=1, max_length=80)
    stage: Literal["planned", "constructing", "completed"] = "planned"
    location_id: str
    longitude: float | None = Field(default=None, ge=-180, le=180)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    owner_entity_id: str | None = Field(default=None, max_length=100)
    requirements: dict[str, int] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class StructureSpec(BaseModel):
    """巨构/遗迹登记；发现事实与来源解释分开保存。"""

    model_config = ConfigDict(extra="forbid")

    element_type: Literal["structure"] = "structure"
    name: str = Field(min_length=1, max_length=120)
    structure_type: str = Field(min_length=1, max_length=80)
    origin_mode: Literal["constructed", "discovered"]
    status: Literal["planned", "constructing", "active", "dormant", "ruined", "discovered"]
    location_id: str | None = None
    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-90, le=90)
    provenance_claim: str = Field(default="来源未知", min_length=1, max_length=1000)
    requirements: dict[str, int] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class LoreSpec(BaseModel):
    """动态世界观登记；人物提交默认只能成为观点、地方说法或公开知识。"""

    model_config = ConfigDict(extra="forbid")

    element_type: Literal["lore"] = "lore"
    knowledge_level: Literal[
        "character_belief", "local_claim", "public_lore", "author_canon"
    ] = "character_belief"
    title: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1, max_length=8000)
    subject_entity_id: str | None = Field(default=None, max_length=100)
    confidence: float = Field(default=0.5, ge=0, le=1)
    tags: list[str] = Field(default_factory=list, max_length=20)


RegistrationPayload = Annotated[
    CharacterBirthSpec
    | CharacterArrivalSpec
    | SettlementSpec
    | BuildingSpec
    | StructureSpec
    | LoreSpec
    | InteriorRoomSpec
    | ActivityRecipeSpec
    | CommoditySpec
    | WorkplaceBudgetSpec
    | RoutinePlanSpec,
    Field(discriminator="element_type"),
]
REGISTRATION_PAYLOAD_ADAPTER = TypeAdapter(RegistrationPayload)


class ElementRegistrationSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requested_by_character_id: str | None = None
    source_event_id: str
    idempotency_key: str = Field(min_length=8, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    payload: RegistrationPayload


class RegistrarCandidateBatch(BaseModel):
    """RegistrarAgent 只允许返回严格元素候选列表。"""

    model_config = ConfigDict(extra="forbid")

    candidates: list[RegistrationPayload] = Field(default_factory=list, max_length=3)


class RegistrationEffectView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    effect_type: str
    entity_type: str | None = None
    entity_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ElementRegistrationView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    world_id: str
    element_type: ElementType
    requested_by_character_id: str | None = None
    source_event_id: str
    idempotency_key: str
    schema_version: int
    status: RegistrationStatus
    payload: dict[str, Any]
    result_entity_id: str | None = None
    rejection_reason: str | None = None
    input_world_version: int
    applied_world_version: int | None = None
    created_at: datetime
    updated_at: datetime
    effects: list[RegistrationEffectView] = Field(default_factory=list)


class RegistrationRejected(ValueError):
    """候选不满足世界规则；请求仍会以 rejected 状态保留。"""


class RegistrationConflict(ValueError):
    """幂等键已经用于不同请求。"""


class RegistrationNotFound(LookupError):
    pass


@dataclass(slots=True)
class Effect:
    effect_type: str
    entity_type: str | None = None
    entity_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class HandlerResult:
    status: RegistrationStatus
    summary: str
    entity_type: str | None = None
    entity_id: str | None = None
    entity_name: str | None = None
    location_id: str | None = None
    effects: list[Effect] = field(default_factory=list)


@dataclass(slots=True)
class RegistrationContext:
    connection: sqlite3.Connection
    registration_id: str
    world_id: str
    world_time: datetime
    requested_by_character_id: str | None
    source_event: sqlite3.Row
    now: datetime


class RegistrationHandler(Protocol):
    element_type: ElementType

    def apply(self, context: RegistrationContext, payload: RegistrationPayload) -> HandlerResult:
        ...


def _clean_nonnegative_values(values: dict[str, int], label: str) -> dict[str, int]:
    if any(not isinstance(value, int) or value < 0 for value in values.values()):
        raise RegistrationRejected(f"{label}只能包含非负整数")
    return {str(key): int(value) for key, value in values.items()}


def _require_location(
    connection: sqlite3.Connection, world_id: str, location_id: str
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM locations WHERE id = ? AND world_id = ? AND is_active = 1",
        (location_id, world_id),
    ).fetchone()
    if row is None:
        raise RegistrationRejected("目标地点不属于当前世界")
    return row


def _entity_exists(connection: sqlite3.Connection, world_id: str, entity_id: str) -> bool:
    for table in ("characters", "locations"):
        row = connection.execute(
            f"SELECT 1 FROM {table} WHERE id = ? AND world_id = ?",  # noqa: S608
            (entity_id, world_id),
        ).fetchone()
        if row is not None:
            return True
    return (
        connection.execute(
            "SELECT 1 FROM world_entities WHERE entity_id = ? AND world_id = ?",
            (entity_id, world_id),
        ).fetchone()
        is not None
    )
