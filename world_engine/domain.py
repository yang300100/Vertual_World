from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ActionType(StrEnum):
    REST = "rest"
    EAT = "eat"
    WORK = "work"
    TRAVEL = "travel"
    SOCIALIZE = "socialize"
    IDLE = "idle"


class LocationState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    world_id: str
    name: str
    kind: str
    resources: dict[str, int] = Field(default_factory=dict)


class CharacterState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    world_id: str
    name: str
    location_id: str
    energy: int = Field(ge=0, le=100)
    hunger: int = Field(ge=0, le=100)
    money: int = Field(ge=0)
    traits: list[str] = Field(default_factory=list)
    goals: list[str] = Field(default_factory=list)
    is_core: bool = False


class WorldState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    current_time: datetime
    minutes_per_tick: int = Field(ge=1)
    status: str
    version: int = Field(ge=0)
    tick_count: int = Field(ge=0)
    time_scale: float = Field(ge=0, le=10080)
    clock_revision: int = Field(ge=0)
    offline_policy: str
    last_adjudication_time: datetime
    next_adjudication_time: datetime
    adjudication_interval_minutes: int = Field(ge=1)


class WorldSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    world: WorldState
    locations: list[LocationState]
    characters: list[CharacterState]

    def character_by_id(self, character_id: str) -> CharacterState | None:
        return next((item for item in self.characters if item.id == character_id), None)

    def location_by_id(self, location_id: str) -> LocationState | None:
        return next((item for item in self.locations if item.id == location_id), None)


class ActionProposal(BaseModel):
    """决策器唯一允许提交的动作格式。"""

    model_config = ConfigDict(extra="forbid")

    actor_id: str
    action: ActionType
    reason: str = Field(min_length=1, max_length=500)
    target_id: str | None = None
    destination_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ActionOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accepted: bool
    actor_id: str
    action: ActionType
    summary: str
    event_id: str | None = None
    rejection_reason: str | None = None


class TickResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    world_id: str
    tick_id: str
    started_at: datetime
    completed_at: datetime
    previous_time: datetime
    current_time: datetime
    previous_version: int
    current_version: int
    outcomes: list[ActionOutcome]
    trigger: str = "manual"
    provider: str = "rules"
    fallback_used: bool = False


class HeartbeatResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    world_id: str
    heartbeat_id: str
    real_time: datetime
    real_elapsed_seconds: float = Field(ge=0)
    time_scale: float = Field(ge=0, le=10080)
    world_delta_seconds: float = Field(ge=0)
    previous_time: datetime
    current_time: datetime
    clock_revision: int = Field(ge=0)
    characters_updated: int = Field(ge=0)
    state_update_count: int = Field(ge=0)
    adjudication_due: bool
    adjudication: TickResult | None = None
    adjudication_error: str | None = None


class ClockUpdateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    world_id: str
    old_time_scale: float
    new_time_scale: float
    world_time: datetime
    clock_revision: int
    event_id: str | None = None
