from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from world_engine.knowledge import KnowledgeHit


class ActionType(StrEnum):
    REST = "rest"
    EAT = "eat"
    WORK = "work"
    TRAVEL = "travel"
    SOCIALIZE = "socialize"
    IDLE = "idle"
    ATTACK = "attack"
    USE = "use"
    GATHER = "gather"
    ACTIVITY = "activity"


class LocationState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    world_id: str
    name: str
    kind: str
    resources: dict[str, int] = Field(default_factory=dict)
    longitude: float = Field(default=0.0, ge=-180, le=180)
    latitude: float = Field(default=0.0, ge=-90, le=90)
    area_radius_km: float = Field(default=1.0, ge=0)
    area_priority: int = 0
    parent_location_id: str | None = None


class WorldMapState(BaseModel):
    """一张地图的世界经纬度覆盖范围。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    world_id: str
    name: str
    kind: str
    asset_path: str
    min_longitude: float = Field(ge=-180, le=180)
    max_longitude: float = Field(ge=-180, le=180)
    min_latitude: float = Field(ge=-90, le=90)
    max_latitude: float = Field(ge=-90, le=90)
    width_pixels: int = Field(gt=0)
    height_pixels: int = Field(gt=0)
    zoom_level: int = Field(ge=0)
    tile_row: int | None = None
    tile_column: int | None = None
    parent_map_id: str | None = None
    location_id: str | None = None
    map_role: str = "world"
    review_status: str = "approved"


class MapFeatureState(BaseModel):
    """建筑、奇观、遗迹等未来空间实体的统一地图投影数据。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    world_id: str
    name: str
    feature_type: str
    longitude: float = Field(default=0.0, ge=-180, le=180)
    latitude: float = Field(default=0.0, ge=-90, le=90)
    location_id: str | None = None
    is_known: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class VehicleState(BaseModel):
    """人物拥有或可使用的交通工具。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    world_id: str
    name: str
    movement_type: str
    speed_kmh: float = Field(gt=0)
    owner_character_id: str | None = None
    is_available: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class MovementState(BaseModel):
    """一段由世界时钟持续推进的移动。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    world_id: str
    character_id: str
    status: str
    movement_type: str
    vehicle_id: str | None = None
    speed_kmh: float = Field(gt=0)
    origin_longitude: float = Field(ge=-180, le=180)
    origin_latitude: float = Field(ge=-90, le=90)
    destination_longitude: float = Field(ge=-180, le=180)
    destination_latitude: float = Field(ge=-90, le=90)
    total_distance_km: float = Field(ge=0)
    distance_travelled_km: float = Field(ge=0)
    destination_location_id: str | None = None
    route: dict[str, Any] | None = None
    route_index: int = Field(default=0, ge=0)
    route_distance_km: float = Field(default=0, ge=0)
    navigation_dataset_id: str | None = None
    replan_reason: str | None = None
    started_at_world: datetime
    updated_at_world: datetime
    estimated_arrival_world: datetime
    encountered_character_ids: list[str] = Field(default_factory=list)

    @property
    def progress(self) -> float:
        if self.total_distance_km <= 0:
            return 1.0
        return min(1.0, self.distance_travelled_km / self.total_distance_km)


class CharacterState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    world_id: str
    name: str
    location_id: str
    longitude: float = Field(default=0.0, ge=-180, le=180)
    latitude: float = Field(default=0.0, ge=-90, le=90)
    movement_type: str = "land"
    movement_speed_kmh: float = Field(default=5.0, gt=0)
    active_vehicle_id: str | None = None
    current_location_id: str | None = None
    current_room_id: str | None = None
    current_fixture_id: str | None = None
    activation_state: str = "background"
    activation_policy: str = "distance"
    activation_reason: str | None = None
    activation_until_world_time: datetime | None = None
    activation_radius_km: float = Field(default=35.0, ge=0)
    activation_probability: float = Field(default=0.85, ge=0, le=1)
    last_activation_check_world_time: datetime | None = None
    identity: str | None = None
    gender: str | None = None
    birth_world_time: datetime | None = None
    age_years: int | None = Field(default=None, ge=0)
    portrait_url: str | None = None
    is_player: bool = False
    is_pov: bool = False
    energy: int = Field(ge=0, le=100)
    satiety: int = Field(ge=0, le=100)
    money: int = Field(ge=0)
    health: int = Field(default=100, ge=0, le=100)
    skills: list[str] = Field(default_factory=list)
    traits: list[str] = Field(default_factory=list)
    inventory: list[dict[str, object]] = Field(default_factory=list)
    equipment: list[dict[str, object]] = Field(default_factory=list)
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
    heartbeat_interval_seconds: int = Field(ge=1)
    last_heartbeat_real_time: datetime | None = None
    last_worker_seen_at: datetime | None = None


class RelationshipView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_character_id: str
    target_character_id: str
    affinity: int
    trust: int


class WorldSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    world: WorldState
    locations: list[LocationState]
    maps: list[WorldMapState] = Field(default_factory=list)
    map_features: list[MapFeatureState] = Field(default_factory=list)
    characters: list[CharacterState]
    vehicles: list[VehicleState] = Field(default_factory=list)
    movements: list[MovementState] = Field(default_factory=list)
    relationships: list[RelationshipView] = Field(default_factory=list)

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
    dialogue: str | None = Field(default=None, max_length=500)
    reply: str | None = Field(default=None, max_length=500)


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
    narrative: str | None = None
    director_seeds: list[EventSeed] = Field(default_factory=list)
    agent_runs: list[AgentRunView] = Field(default_factory=list)


class PlayerActionResult(BaseModel):
    """玩家提交一次自然语言意图并结算为一次行动的返回。"""

    model_config = ConfigDict(extra="forbid")

    world_id: str
    action_id: str
    started_at: datetime
    completed_at: datetime
    previous_version: int
    current_version: int
    outcome: ActionOutcome
    provider: str = "rules"
    fallback_used: bool = False
    registration_ids: list[str] = Field(default_factory=list)
    npc_reply: str | None = None
    npc_reply_error: str | None = None
    activity_progress: list[dict[str, object]] = Field(default_factory=list)


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
    movements_updated: int = Field(default=0, ge=0)
    construction_updates: int = Field(default=0, ge=0)
    activation_updates: int = Field(default=0, ge=0)
    adjudication_due: bool
    adjudication: TickResult | None = None
    adjudication_error: str | None = None
    time_skip_reason: str | None = None
    time_skip_replayed: bool = False


class ClockUpdateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    world_id: str
    old_time_scale: float
    new_time_scale: float
    previous_world_time: datetime
    world_time: datetime
    clock_revision: int
    world_version: int
    settled_world_seconds: float = Field(ge=0)
    state_update_count: int = Field(ge=0)
    movements_updated: int = Field(default=0, ge=0)
    construction_updates: int = Field(default=0, ge=0)
    activation_updates: int = Field(default=0, ge=0)
    adjudication_due: bool = False
    adjudication_triggered: bool = False
    no_op: bool = False
    event_id: str | None = None


class TerrainContext(BaseModel):
    """来自正式导航数据集的只读地形采样，供场景文字与战斗判断共用。"""

    model_config = ConfigDict(extra="forbid")

    dataset_id: str
    location_id: str | None = None
    surface: str | None = None
    speed_multiplier: float = Field(default=1.0, ge=0)
    terrain_features: list[str] = Field(default_factory=list)
    reachable: bool = True


class EventView(BaseModel):
    """已结算事件的只读投影，供Agent上下文使用。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    event_type: str
    occurred_at: datetime
    summary: str
    actor_id: str | None = None
    target_id: str | None = None
    location_id: str | None = None
    importance: str = "routine"


class SceneContext(BaseModel):
    """一个版本冻结、只读的世界场景快照，是Agent的唯一输入边界。"""

    model_config = ConfigDict(extra="forbid")

    world_id: str
    world_time: datetime
    trigger: Literal["heartbeat", "player_intent", "combat", "event_followup"]
    pov_character_id: str | None = None
    location: LocationState | None = None
    terrain: TerrainContext | None = None
    visible_characters: list[CharacterState] = Field(default_factory=list)
    recent_events: list[EventView] = Field(default_factory=list)
    available_actions: list[ActionType] = Field(default_factory=list)
    knowledge: list[KnowledgeHit] = Field(default_factory=list)
    token_budget: int = 0

    def knowledge_to_payload(self) -> list[dict[str, object]]:
        return [hit.to_dict() for hit in self.knowledge]


class EventSeed(BaseModel):
    """事件导演Agent的候选事件种子；被协调器选中并由规则执行后才成为正式事件。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    category: Literal["social", "economic", "political", "travel", "hazard", "combat"]
    priority: int = Field(default=50, ge=0, le=100)
    participant_ids: list[str] = Field(default_factory=list)
    location_id: str | None = None
    premise: str = Field(min_length=1, max_length=1000)
    proposed_consequences: list[str] = Field(default_factory=list)
    expires_at: datetime


class CombatIntent(BaseModel):
    """战斗战术Agent的战术偏好；不决定命中、伤害或掉落。"""

    model_config = ConfigDict(extra="forbid")

    actor_id: str
    intent: Literal["attack", "defend", "withdraw", "use_item", "move"]
    target_id: str | None = None
    preferred_position: str | None = None
    reason: str = Field(min_length=1, max_length=500)


class MemoryCandidate(BaseModel):
    """记忆/关系Agent产出的记忆候选；必须引用已存在的事件并满足身份可见性。"""

    model_config = ConfigDict(extra="forbid")

    character_id: str
    event_id: str
    summary: str = Field(min_length=1, max_length=1000)
    importance: int = Field(ge=1, le=5)
    confidence: float = Field(ge=0, le=1)
    memory_type: Literal["experienced", "heard", "inferred"]


class SceneNarrativeResult(BaseModel):
    """场景叙事Agent的展示文本包装，用于模型结构化输出。"""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=3000)


class MemoryCandidateBatch(BaseModel):
    """记忆Agent表格式输出的包装。"""

    model_config = ConfigDict(extra="forbid")

    candidates: list[MemoryCandidate] = Field(default_factory=list)


class CombatIntentBatch(BaseModel):
    """战斗战术Agent表格式输出的包装。"""

    model_config = ConfigDict(extra="forbid")

    intents: list[CombatIntent] = Field(default_factory=list)


class CombatEncounterState(BaseModel):
    """战斗遭遇只读视图，供CombatResolver与战斗Agent使用。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    world_id: str
    status: str
    random_seed: int
    started_at_world: datetime
    resolved_at_world: datetime | None = None
    participants: list[str] = Field(default_factory=list)
    terrain_id: str | None = None
    location_id: str | None = None
    summary: str | None = None


class AgentRunView(BaseModel):
    """Agent单次调用的审计记录(agent_runs行的只读投影)。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    world_id: str
    trigger: str
    agent_name: str
    input_snapshot_version: int
    status: str
    model_name: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None
    error_text: str | None = None
    created_at: datetime
    completed_at: datetime | None = None


class AgentProposalView(BaseModel):
    """Agent提案的审计记录(agent_proposals行的只读投影)。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    run_id: str
    world_id: str
    actor_id: str | None = None
    proposal_type: str
    payload: dict[str, object]
    validation_status: str
    rejection_reason: str | None = None
    created_at: datetime
