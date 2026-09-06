from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from world_engine.demographics import stable_npc_demographics
from world_engine.time_utils import is_time_only, next_adjudication_boundary, parse_datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS worlds (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    current_time TEXT NOT NULL,
    minutes_per_tick INTEGER NOT NULL CHECK (minutes_per_tick > 0),
    status TEXT NOT NULL DEFAULT 'running',
    version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS world_runtime (
    world_id TEXT PRIMARY KEY REFERENCES worlds(id) ON DELETE CASCADE,
    tick_count INTEGER NOT NULL DEFAULT 0,
    last_tick_started_at TEXT,
    last_tick_finished_at TEXT,
    last_tick_status TEXT,
    last_worker_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS locations (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    resources_json TEXT NOT NULL DEFAULT '{}',
    longitude REAL CHECK (longitude BETWEEN -180 AND 180),
    latitude REAL CHECK (latitude BETWEEN -90 AND 90),
    area_radius_km REAL NOT NULL DEFAULT 1 CHECK (area_radius_km >= 0),
    area_priority INTEGER NOT NULL DEFAULT 0,
    parent_location_id TEXT REFERENCES locations(id) ON DELETE SET NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    UNIQUE(world_id, name)
);

CREATE TABLE IF NOT EXISTS world_maps (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'terrain',
    asset_path TEXT NOT NULL,
    min_longitude REAL NOT NULL CHECK (min_longitude BETWEEN -180 AND 180),
    max_longitude REAL NOT NULL CHECK (max_longitude BETWEEN -180 AND 180),
    min_latitude REAL NOT NULL CHECK (min_latitude BETWEEN -90 AND 90),
    max_latitude REAL NOT NULL CHECK (max_latitude BETWEEN -90 AND 90),
    width_pixels INTEGER NOT NULL CHECK (width_pixels > 0),
    height_pixels INTEGER NOT NULL CHECK (height_pixels > 0),
    zoom_level INTEGER NOT NULL DEFAULT 0 CHECK (zoom_level >= 0),
    tile_row INTEGER,
    tile_column INTEGER,
    parent_map_id TEXT REFERENCES world_maps(id) ON DELETE SET NULL,
    location_id TEXT REFERENCES locations(id) ON DELETE CASCADE,
    map_role TEXT NOT NULL DEFAULT 'world',
    review_status TEXT NOT NULL DEFAULT 'approved',
    CHECK (min_longitude < max_longitude),
    CHECK (min_latitude < max_latitude),
    UNIQUE(world_id, asset_path)
);

CREATE INDEX IF NOT EXISTS idx_world_maps_world_zoom
ON world_maps(world_id, zoom_level, tile_row, tile_column);

CREATE TABLE IF NOT EXISTS characters (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    species TEXT NOT NULL DEFAULT 'human',
    birth_world_time TEXT,
    gender TEXT,
    location_id TEXT NOT NULL REFERENCES locations(id),
    energy INTEGER NOT NULL CHECK (energy BETWEEN 0 AND 100),
    satiety INTEGER NOT NULL CHECK (satiety BETWEEN 0 AND 100),
    money INTEGER NOT NULL CHECK (money >= 0),
    health INTEGER NOT NULL DEFAULT 100,
    traits_json TEXT NOT NULL DEFAULT '[]',
    goals_json TEXT NOT NULL DEFAULT '[]',
    skills_json TEXT NOT NULL DEFAULT '[]',
    identity TEXT,
    is_player INTEGER NOT NULL DEFAULT 0,
    is_pov INTEGER NOT NULL DEFAULT 0,
    is_core INTEGER NOT NULL DEFAULT 0,
    longitude REAL CHECK (longitude BETWEEN -180 AND 180),
    latitude REAL CHECK (latitude BETWEEN -90 AND 90),
    movement_type TEXT NOT NULL DEFAULT 'land'
        CHECK (movement_type IN ('land', 'flight', 'ship', 'underground', 'water')),
    movement_speed_kmh REAL NOT NULL DEFAULT 5 CHECK (movement_speed_kmh > 0),
    active_vehicle_id TEXT,
    current_location_id TEXT REFERENCES locations(id) ON DELETE SET NULL,
    activation_state TEXT NOT NULL DEFAULT 'background',
    activation_policy TEXT NOT NULL DEFAULT 'distance',
    activation_reason TEXT,
    activation_until_world_time TEXT,
    activation_radius_km REAL NOT NULL DEFAULT 35 CHECK (activation_radius_km >= 0),
    activation_probability REAL NOT NULL DEFAULT 0.85
        CHECK (activation_probability BETWEEN 0 AND 1),
    last_activation_check_world_time TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(world_id, name)
);

CREATE TABLE IF NOT EXISTS map_features (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    feature_type TEXT NOT NULL,
    longitude REAL NOT NULL CHECK (longitude BETWEEN -180 AND 180),
    latitude REAL NOT NULL CHECK (latitude BETWEEN -90 AND 90),
    location_id TEXT REFERENCES locations(id) ON DELETE SET NULL,
    is_known INTEGER NOT NULL DEFAULT 1,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_map_features_world_position
ON map_features(world_id, longitude, latitude);

CREATE TABLE IF NOT EXISTS vehicles (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    movement_type TEXT NOT NULL
        CHECK (movement_type IN ('land', 'flight', 'ship', 'underground', 'water')),
    speed_kmh REAL NOT NULL CHECK (speed_kmh > 0),
    owner_character_id TEXT REFERENCES characters(id) ON DELETE SET NULL,
    is_available INTEGER NOT NULL DEFAULT 1,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_vehicles_world_owner
ON vehicles(world_id, owner_character_id, is_available);

CREATE TABLE IF NOT EXISTS character_movements (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'moving',
    movement_type TEXT NOT NULL
        CHECK (movement_type IN ('land', 'flight', 'ship', 'underground', 'water')),
    vehicle_id TEXT REFERENCES vehicles(id) ON DELETE SET NULL,
    speed_kmh REAL NOT NULL CHECK (speed_kmh > 0),
    origin_longitude REAL NOT NULL CHECK (origin_longitude BETWEEN -180 AND 180),
    origin_latitude REAL NOT NULL CHECK (origin_latitude BETWEEN -90 AND 90),
    destination_longitude REAL NOT NULL CHECK (destination_longitude BETWEEN -180 AND 180),
    destination_latitude REAL NOT NULL CHECK (destination_latitude BETWEEN -90 AND 90),
    total_distance_km REAL NOT NULL CHECK (total_distance_km >= 0),
    distance_travelled_km REAL NOT NULL DEFAULT 0 CHECK (distance_travelled_km >= 0),
    destination_location_id TEXT REFERENCES locations(id) ON DELETE SET NULL,
    route_json TEXT NOT NULL DEFAULT '[]',
    route_index INTEGER NOT NULL DEFAULT 0,
    route_distance_km REAL NOT NULL DEFAULT 0,
    navigation_dataset_id TEXT,
    replan_reason TEXT,
    started_at_world TEXT NOT NULL,
    updated_at_world TEXT NOT NULL,
    estimated_arrival_world TEXT NOT NULL,
    encountered_character_ids_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_character_movements_one_active
ON character_movements(character_id) WHERE status = 'moving';

CREATE INDEX IF NOT EXISTS idx_character_movements_world_status
ON character_movements(world_id, status, updated_at_world);

CREATE TABLE IF NOT EXISTS navigation_datasets (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    asset_root TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    review_status TEXT NOT NULL CHECK(review_status IN ('candidate','approved','retired')),
    bounds_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    approved_at TEXT
);

CREATE TABLE IF NOT EXISTS navigation_nodes (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES navigation_datasets(id) ON DELETE CASCADE,
    node_type TEXT NOT NULL
        CHECK(node_type IN ('road','bridge','ford','ferry','port','pass','tunnel')),
    longitude REAL NOT NULL CHECK (longitude BETWEEN -180 AND 180),
    latitude REAL NOT NULL CHECK (latitude BETWEEN -90 AND 90),
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_navigation_nodes_dataset
ON navigation_nodes(dataset_id);

CREATE TABLE IF NOT EXISTS navigation_edges (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES navigation_datasets(id) ON DELETE CASCADE,
    from_node_id TEXT NOT NULL REFERENCES navigation_nodes(id) ON DELETE CASCADE,
    to_node_id TEXT NOT NULL REFERENCES navigation_nodes(id) ON DELETE CASCADE,
    edge_type TEXT NOT NULL,
    distance_km REAL NOT NULL,
    speed_multiplier REAL NOT NULL,
    allowed_modes_json TEXT NOT NULL,
    polyline_json TEXT NOT NULL,
    requirements_json TEXT NOT NULL DEFAULT '{}',
    is_open INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_navigation_edges_dataset
ON navigation_edges(dataset_id);

CREATE INDEX IF NOT EXISTS idx_navigation_edges_nodes
ON navigation_edges(from_node_id, to_node_id);

CREATE TABLE IF NOT EXISTS world_map_preferences (
    world_id TEXT PRIMARY KEY REFERENCES worlds(id) ON DELETE CASCADE,
    style TEXT NOT NULL CHECK(style IN ('political','terrain','elevation','passability')),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS item_types (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    stack_limit INTEGER NOT NULL DEFAULT 1,
    slot_size INTEGER NOT NULL DEFAULT 1,
    usable INTEGER NOT NULL DEFAULT 0,
    attack_bonus INTEGER NOT NULL DEFAULT 0,
    defense_bonus INTEGER NOT NULL DEFAULT 0,
    heal INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS item_instances (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    item_type_id TEXT NOT NULL REFERENCES item_types(id),
    container_id TEXT NOT NULL,
    container_type TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    condition INTEGER NOT NULL DEFAULT 100
);

CREATE TABLE IF NOT EXISTS relationships (
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    source_character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    target_character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    affinity INTEGER NOT NULL DEFAULT 0 CHECK (affinity BETWEEN -100 AND 100),
    trust INTEGER NOT NULL DEFAULT 0 CHECK (trust BETWEEN -100 AND 100),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(world_id, source_character_id, target_character_id),
    CHECK (source_character_id <> target_character_id)
);

CREATE TABLE IF NOT EXISTS world_events (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    tick_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT REFERENCES characters(id) ON DELETE SET NULL,
    target_id TEXT REFERENCES characters(id) ON DELETE SET NULL,
    location_id TEXT REFERENCES locations(id) ON DELETE SET NULL,
    summary TEXT NOT NULL,
    importance TEXT NOT NULL DEFAULT 'routine',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_world_events_world_time
ON world_events(world_id, occurred_at DESC, created_at DESC);

CREATE TABLE IF NOT EXISTS character_memories (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    event_id TEXT NOT NULL REFERENCES world_events(id) ON DELETE CASCADE,
    memory_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    importance INTEGER NOT NULL CHECK (importance BETWEEN 1 AND 10),
    confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    created_at TEXT NOT NULL,
    UNIQUE(character_id, event_id, memory_type)
);

CREATE INDEX IF NOT EXISTS idx_character_memories_character_time
ON character_memories(character_id, created_at DESC);

CREATE TABLE IF NOT EXISTS character_skill_proficiencies (
    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    skill_name TEXT NOT NULL,
    proficiency INTEGER NOT NULL DEFAULT 0 CHECK(proficiency BETWEEN 0 AND 100),
    source_event_id TEXT REFERENCES world_events(id) ON DELETE SET NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(character_id, skill_name)
);

CREATE TABLE IF NOT EXISTS action_effects (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    source_event_id TEXT NOT NULL REFERENCES world_events(id) ON DELETE CASCADE,
    effect_type TEXT NOT NULL,
    actor_character_id TEXT REFERENCES characters(id) ON DELETE SET NULL,
    target_character_id TEXT REFERENCES characters(id) ON DELETE SET NULL,
    status TEXT NOT NULL CHECK(status IN ('applied','rejected')),
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS character_contacts (
    id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    requester_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    recipient_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK(status IN ('pending','accepted','rejected')),
    source_event_id TEXT REFERENCES world_events(id) ON DELETE SET NULL,
    response_reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(world_id, requester_id, recipient_id), CHECK(requester_id <> recipient_id)
);
CREATE TABLE IF NOT EXISTS character_messages (
    id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    contact_id TEXT NOT NULL REFERENCES character_contacts(id) ON DELETE CASCADE,
    sender_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    recipient_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    content TEXT NOT NULL, world_time TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS npc_todos (
    id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    title TEXT NOT NULL, details TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','doing','done','cancelled')),
    due_world_time TEXT, source_event_id TEXT REFERENCES world_events(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS long_term_operation_requests (
    id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    requester_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    recipient_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    operation_type TEXT NOT NULL, terms_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL CHECK(status IN ('submitted','npc_accepted','npc_rejected','npc_countered','player_confirmed','applied','cancelled')),
    npc_response TEXT, system_notice TEXT, counter_terms_json TEXT,
    source_event_id TEXT REFERENCES world_events(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

-- NPC 的可读取对话属性：会话按 NPC 与对话对象分组，原话按世界时间保存。
CREATE TABLE IF NOT EXISTS npc_conversation_sessions (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    npc_character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    counterpart_character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','closed')),
    last_world_time TEXT NOT NULL,
    last_event_id TEXT REFERENCES world_events(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(world_id, npc_character_id, counterpart_character_id),
    CHECK(npc_character_id <> counterpart_character_id)
);

CREATE INDEX IF NOT EXISTS idx_npc_conversation_sessions_recent
ON npc_conversation_sessions(world_id, counterpart_character_id, last_world_time DESC);

CREATE TABLE IF NOT EXISTS npc_conversation_turns (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES npc_conversation_sessions(id) ON DELETE CASCADE,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    event_id TEXT NOT NULL REFERENCES world_events(id) ON DELETE CASCADE,
    turn_index INTEGER NOT NULL CHECK(turn_index > 0),
    speaker_character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    listener_character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    content TEXT NOT NULL CHECK(length(content) BETWEEN 1 AND 1000),
    world_time TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(session_id, turn_index)
);

CREATE INDEX IF NOT EXISTS idx_npc_conversation_turns_window
ON npc_conversation_turns(session_id, world_time, turn_index);

-- 长对话的可追溯分段摘要。原始回合始终保留，摘要只引用固定回合区间。
CREATE TABLE IF NOT EXISTS npc_conversation_episodes (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES npc_conversation_sessions(id) ON DELETE CASCADE,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    first_turn_index INTEGER NOT NULL CHECK(first_turn_index > 0),
    last_turn_index INTEGER NOT NULL CHECK(last_turn_index >= first_turn_index),
    summary TEXT NOT NULL,
    topic_terms_json TEXT NOT NULL DEFAULT '[]',
    open_questions_json TEXT NOT NULL DEFAULT '[]',
    open_commitments_json TEXT NOT NULL DEFAULT '[]',
    source_event_ids_json TEXT NOT NULL DEFAULT '[]',
    source_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(session_id, first_turn_index, last_turn_index)
);

CREATE INDEX IF NOT EXISTS idx_npc_conversation_episodes_session
ON npc_conversation_episodes(session_id, last_turn_index DESC);

-- 角色卡不是世界设定百科，而是 NPC 在对话中可持续使用的主观底色。
-- 它与角色一对一绑定，避免把职业模板误当成性格。
CREATE TABLE IF NOT EXISTS npc_character_cards (
    character_id TEXT PRIMARY KEY REFERENCES characters(id) ON DELETE CASCADE,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    card_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_npc_character_cards_world
ON npc_character_cards(world_id, character_id);

CREATE TABLE IF NOT EXISTS world_clock (
    world_id TEXT PRIMARY KEY REFERENCES worlds(id) ON DELETE CASCADE,
    time_scale REAL NOT NULL DEFAULT 1.0 CHECK (time_scale BETWEEN 0 AND 10080),
    heartbeat_interval_seconds INTEGER NOT NULL DEFAULT 60 CHECK (heartbeat_interval_seconds > 0),
    last_heartbeat_real_time TEXT,
    clock_revision INTEGER NOT NULL DEFAULT 0,
    offline_policy TEXT NOT NULL DEFAULT 'pause' CHECK (offline_policy = 'pause'),
    last_adjudication_world_time TEXT NOT NULL,
    next_adjudication_world_time TEXT NOT NULL,
    adjudication_interval_minutes INTEGER NOT NULL DEFAULT 720,
    last_player_intervention_world_time TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS world_heartbeats (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    real_time TEXT NOT NULL,
    real_elapsed_seconds REAL NOT NULL CHECK (real_elapsed_seconds >= 0),
    time_scale REAL NOT NULL CHECK (time_scale >= 0),
    world_delta_seconds REAL NOT NULL CHECK (world_delta_seconds >= 0),
    world_time_before TEXT NOT NULL,
    world_time_after TEXT NOT NULL,
    characters_updated INTEGER NOT NULL DEFAULT 0,
    adjudication_due INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_world_heartbeats_world_time
ON world_heartbeats(world_id, created_at ASC);

CREATE TABLE IF NOT EXISTS character_state_accumulators (
    character_id TEXT PRIMARY KEY REFERENCES characters(id) ON DELETE CASCADE,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    satiety_residual REAL NOT NULL DEFAULT 0,
    energy_residual REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS character_state_updates (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    heartbeat_id TEXT NOT NULL REFERENCES world_heartbeats(id) ON DELETE CASCADE,
    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    world_time_before TEXT NOT NULL,
    world_time_after TEXT NOT NULL,
    changes_json TEXT NOT NULL,
    cause TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_character_state_updates_character_time
ON character_state_updates(character_id, created_at ASC);

CREATE TABLE IF NOT EXISTS adjudication_runs (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    trigger_type TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    provider TEXT NOT NULL,
    selected_character_ids_json TEXT NOT NULL,
    proposals_json TEXT NOT NULL,
    rule_rejections_json TEXT NOT NULL,
    final_event_ids_json TEXT NOT NULL,
    fallback_used INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    error_text TEXT
);

CREATE INDEX IF NOT EXISTS idx_adjudication_runs_world_time
ON adjudication_runs(world_id, completed_at ASC);

-- 多Agent编排的审计与异步工作表（世界状态仍以现有表为唯一权威）。
CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    trigger TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    input_snapshot_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed','timed_out','skipped')),
    model_name TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    latency_ms INTEGER,
    error_text TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_world_trigger
ON agent_runs(world_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_agent_runs_world_agent
ON agent_runs(world_id, agent_name, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_proposals (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    actor_id TEXT REFERENCES characters(id) ON DELETE SET NULL,
    proposal_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    validation_status TEXT NOT NULL CHECK(validation_status IN('accepted','rejected','superseded')),
    rejection_reason TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_proposals_world_time
ON agent_proposals(world_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_agent_proposals_run
ON agent_proposals(run_id);

CREATE TABLE IF NOT EXISTS memory_jobs (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    event_id TEXT NOT NULL REFERENCES world_events(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK(status IN ('pending','processing','done','failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_jobs_world_status
ON memory_jobs(world_id, status, created_at);

CREATE TABLE IF NOT EXISTS combat_encounters (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK(status IN ('active','resolved','withdrawn')),
    random_seed INTEGER NOT NULL,
    started_at_world TEXT NOT NULL,
    resolved_at_world TEXT,
    participants_json TEXT NOT NULL,
    terrain_id TEXT,
    location_id TEXT REFERENCES locations(id) ON DELETE SET NULL,
    summary TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_combat_encounters_world_status
ON combat_encounters(world_id, status);

-- 世界元素注册器：统一保存申请、结果和来源，但实际实体仍进入各自专用表。
CREATE TABLE IF NOT EXISTS element_registration_requests (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    element_type TEXT NOT NULL CHECK(element_type IN (
        'character_birth','character_arrival','settlement','building','structure','lore'
    )),
    requested_by_character_id TEXT REFERENCES characters(id) ON DELETE SET NULL,
    source_event_id TEXT NOT NULL REFERENCES world_events(id) ON DELETE RESTRICT,
    idempotency_key TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL CHECK(status IN (
        'proposed','validating','approved','rejected','applying','applied','failed'
    )),
    payload_json TEXT NOT NULL,
    result_entity_id TEXT,
    rejection_reason TEXT,
    input_world_version INTEGER NOT NULL,
    applied_world_version INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(world_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_element_registrations_world_status
ON element_registration_requests(world_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_element_registrations_source_event
ON element_registration_requests(source_event_id);

CREATE TABLE IF NOT EXISTS world_entities (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    name TEXT NOT NULL,
    registration_id TEXT NOT NULL
        REFERENCES element_registration_requests(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    UNIQUE(world_id, entity_type, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_world_entities_world_type
ON world_entities(world_id, entity_type, name);

CREATE TABLE IF NOT EXISTS element_registration_effects (
    id TEXT PRIMARY KEY,
    registration_id TEXT NOT NULL
        REFERENCES element_registration_requests(id) ON DELETE CASCADE,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    effect_type TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_registration_effects_request
ON element_registration_effects(registration_id, created_at);

CREATE TABLE IF NOT EXISTS character_lineages (
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    parent_character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    child_character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL DEFAULT 'biological'
        CHECK(relation_type IN ('biological','adoptive','guardian')),
    registration_id TEXT NOT NULL
        REFERENCES element_registration_requests(id) ON DELETE RESTRICT,
    established_at_world TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(parent_character_id, child_character_id, relation_type),
    CHECK(parent_character_id <> child_character_id)
);

CREATE INDEX IF NOT EXISTS idx_character_lineages_child
ON character_lineages(world_id, child_character_id);

CREATE TABLE IF NOT EXISTS species_profiles (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    gestation_world_days INTEGER NOT NULL CHECK(gestation_world_days > 0),
    adult_age_world_years INTEGER NOT NULL CHECK(adult_age_world_years > 0),
    compatible_species_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS family_plans (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    registration_id TEXT NOT NULL UNIQUE
        REFERENCES element_registration_requests(id) ON DELETE RESTRICT,
    planned_child_name TEXT NOT NULL,
    parent_character_ids_json TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('planned','due','completed','cancelled')),
    due_at_world TEXT NOT NULL,
    completed_registration_id TEXT
        REFERENCES element_registration_requests(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_family_plans_world_due
ON family_plans(world_id, status, due_at_world);

CREATE TABLE IF NOT EXISTS land_claims (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    registration_id TEXT NOT NULL UNIQUE
        REFERENCES element_registration_requests(id) ON DELETE RESTRICT,
    claimant_character_id TEXT REFERENCES characters(id) ON DELETE SET NULL,
    longitude REAL NOT NULL CHECK(longitude BETWEEN -180 AND 180),
    latitude REAL NOT NULL CHECK(latitude BETWEEN -90 AND 90),
    radius_km REAL NOT NULL CHECK(radius_km > 0),
    status TEXT NOT NULL CHECK(status IN ('reserved','active','released','disputed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_land_claims_world_status
ON land_claims(world_id, status, longitude, latitude);

CREATE TABLE IF NOT EXISTS construction_projects (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    registration_id TEXT NOT NULL UNIQUE
        REFERENCES element_registration_requests(id) ON DELETE RESTRICT,
    project_type TEXT NOT NULL CHECK(project_type IN ('settlement','building','structure')),
    target_name TEXT NOT NULL,
    target_entity_id TEXT,
    status TEXT NOT NULL CHECK(status IN (
        'planned','surveyed','constructing','completed','cancelled'
    )),
    location_id TEXT REFERENCES locations(id) ON DELETE SET NULL,
    longitude REAL CHECK(longitude BETWEEN -180 AND 180),
    latitude REAL CHECK(latitude BETWEEN -90 AND 90),
    progress REAL NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 1),
    requirements_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_construction_projects_world_status
ON construction_projects(world_id, status, created_at);

CREATE TABLE IF NOT EXISTS construction_escrow (
    project_id TEXT PRIMARY KEY
        REFERENCES construction_projects(id) ON DELETE CASCADE,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    requester_character_id TEXT REFERENCES characters(id) ON DELETE SET NULL,
    source_location_id TEXT REFERENCES locations(id) ON DELETE SET NULL,
    committed_resources_json TEXT NOT NULL DEFAULT '{}',
    committed_money INTEGER NOT NULL DEFAULT 0,
    refunded_resources_json TEXT NOT NULL DEFAULT '{}',
    refunded_money INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS buildings (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    registration_id TEXT NOT NULL UNIQUE
        REFERENCES element_registration_requests(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    building_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('planned','constructing','completed','damaged','ruined')),
    location_id TEXT NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
    owner_entity_id TEXT,
    longitude REAL NOT NULL CHECK(longitude BETWEEN -180 AND 180),
    latitude REAL NOT NULL CHECK(latitude BETWEEN -90 AND 90),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(world_id, name)
);

CREATE INDEX IF NOT EXISTS idx_buildings_world_location
ON buildings(world_id, location_id, status);

CREATE TABLE IF NOT EXISTS world_structures (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    registration_id TEXT NOT NULL UNIQUE
        REFERENCES element_registration_requests(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    structure_type TEXT NOT NULL,
    origin_mode TEXT NOT NULL CHECK(origin_mode IN ('constructed','discovered')),
    status TEXT NOT NULL CHECK(status IN (
        'planned','constructing','active','dormant','ruined','discovered'
    )),
    location_id TEXT REFERENCES locations(id) ON DELETE SET NULL,
    longitude REAL NOT NULL CHECK(longitude BETWEEN -180 AND 180),
    latitude REAL NOT NULL CHECK(latitude BETWEEN -90 AND 90),
    provenance_status TEXT NOT NULL CHECK(provenance_status IN ('claimed','verified','canonical')),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(world_id, name)
);

CREATE INDEX IF NOT EXISTS idx_world_structures_world_location
ON world_structures(world_id, location_id, status);

CREATE TABLE IF NOT EXISTS knowledge_entries (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    registration_id TEXT NOT NULL UNIQUE
        REFERENCES element_registration_requests(id) ON DELETE RESTRICT,
    knowledge_level TEXT NOT NULL CHECK(knowledge_level IN (
        'character_belief','local_claim','public_lore','author_canon'
    )),
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    subject_entity_id TEXT,
    author_character_id TEXT REFERENCES characters(id) ON DELETE SET NULL,
    source_event_id TEXT NOT NULL REFERENCES world_events(id) ON DELETE RESTRICT,
    confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
    tags_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disputed','retired')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_knowledge_entries_world_level
ON knowledge_entries(world_id, knowledge_level, status, created_at DESC);

-- 统一元素目录只管理身份、生命周期和来源；各类元素的专有属性仍放在专用事实表中。
CREATE TABLE IF NOT EXISTS world_element_catalog (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    name TEXT NOT NULL,
    lifecycle_state TEXT NOT NULL DEFAULT 'active'
        CHECK(lifecycle_state IN ('active','destroyed','retired')),
    source_kind TEXT NOT NULL DEFAULT 'system'
        CHECK(source_kind IN ('seed','system','registration')),
    source_registration_id TEXT
        REFERENCES element_registration_requests(id) ON DELETE SET NULL,
    source_event_id TEXT REFERENCES world_events(id) ON DELETE SET NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    destroyed_at TEXT,
    destroyed_by_event_id TEXT REFERENCES world_events(id) ON DELETE SET NULL,
    UNIQUE(world_id, entity_type, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_world_element_catalog_world_state
ON world_element_catalog(world_id, lifecycle_state, entity_type, name);

-- 删除器使用墓碑而不是物理 DELETE，确保事件、谱系和历史引用永远可复核。
CREATE TABLE IF NOT EXISTS element_removal_requests (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    target_element_type TEXT NOT NULL,
    target_entity_id TEXT NOT NULL,
    requested_by_character_id TEXT REFERENCES characters(id) ON DELETE SET NULL,
    source_event_id TEXT NOT NULL REFERENCES world_events(id) ON DELETE RESTRICT,
    idempotency_key TEXT NOT NULL,
    reason TEXT NOT NULL CHECK(reason IN ('destroyed','retired','removed')),
    details TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('validating','applied','rejected','failed')),
    rejection_reason TEXT,
    input_world_version INTEGER NOT NULL,
    applied_world_version INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(world_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_element_removals_world_status
ON element_removal_requests(world_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS element_removal_effects (
    id TEXT PRIMARY KEY,
    removal_id TEXT NOT NULL REFERENCES element_removal_requests(id) ON DELETE CASCADE,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    effect_type TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_element_removal_effects_request
ON element_removal_effects(removal_id, created_at);

CREATE TABLE IF NOT EXISTS character_portraits (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    media_path TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_character_portraits_active
ON character_portraits(world_id, character_id, is_active, created_at DESC);

CREATE TABLE IF NOT EXISTS photo_captures (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    photographer_character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE RESTRICT,
    direction TEXT NOT NULL,
    include_self INTEGER NOT NULL,
    included_character_ids_json TEXT NOT NULL DEFAULT '[]',
    prompt TEXT NOT NULL,
    context_json TEXT NOT NULL,
    media_path TEXT NOT NULL,
    model_name TEXT NOT NULL,
    source_event_id TEXT REFERENCES world_events(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_photo_captures_world_time
ON photo_captures(world_id, created_at DESC);
"""


class Database:
    """负责SQLite连接、WAL设置与显式写事务。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            self._ensure_runtime_columns(connection)
            self._migrate_satiety_columns(connection)
            self._migrate_identity_column(connection)
            self._migrate_player_column(connection)
            self._migrate_pov_column(connection)
            self._migrate_health_column(connection)
            self._migrate_skills_column(connection)
            self._migrate_spatial_columns(connection)
            self._migrate_movement_columns(connection)
            self._migrate_area_and_activation_columns(connection)
            self._migrate_event_importance(connection)
            self._migrate_navigation_columns(connection)
            self._migrate_registration_columns(connection)
            self._migrate_registration_element_types(connection)
            self._migrate_element_lifecycle_columns(connection)
            self._migrate_character_species_columns(connection)
            self._migrate_social_operation_columns(connection)
            self._migrate_completion_columns(connection)
            self._remove_legacy_player_controlled_social_records(connection)
            self._ensure_default_species_profiles(connection)
            self._ensure_npc_demographics(connection)
            self._ensure_known_map_features(connection)
            self._ensure_default_world_maps(connection)
            self._ensure_noryia_city_detail_maps(connection)
            self._ensure_noryia_display_names(connection)
            self._ensure_detail_world_maps(connection)
            self._ensure_clock_and_accumulator_rows(connection)
            self._repair_time_only_timestamps(connection)
            self._synchronize_world_element_catalog(connection)

    @staticmethod
    def _migrate_completion_columns(connection: sqlite3.Connection) -> None:
        """只新增缺失字段；既有物品的数量、状态和已知所有权均保留。"""
        additions = {
            "characters": {"inventory_capacity": "INTEGER NOT NULL DEFAULT 2"},
            "item_instances": {"owner_character_id": "TEXT REFERENCES characters(id)"},
            "memory_jobs": {"last_error": "TEXT", "retry_at": "TEXT"},
            "npc_todos": {"contract_id": "TEXT"},
            "npc_conversation_turns": {"message_kind": "TEXT NOT NULL DEFAULT 'speech'"},
        }
        for table, fields in additions.items():
            columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
            for name, declaration in fields.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
                    if table == "item_instances" and name == "owner_character_id":
                        connection.execute(
                            """UPDATE item_instances SET owner_character_id=container_id
                               WHERE container_type IN ('character_inventory','character_equipment')
                               AND container_id IN (SELECT id FROM characters)"""
                        )
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS player_activity_records (
                id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id),
                player_id TEXT NOT NULL REFERENCES characters(id),
                npc_id TEXT REFERENCES characters(id),
                source_event_id TEXT NOT NULL REFERENCES world_events(id) ON DELETE CASCADE,
                request_event_id TEXT REFERENCES world_events(id) ON DELETE SET NULL,
                step_key TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL,
                content_json TEXT NOT NULL, location_id TEXT, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_player_activity_records_player
                ON player_activity_records(world_id,player_id,created_at);
            CREATE TABLE IF NOT EXISTS player_action_reactions (
                source_event_id TEXT PRIMARY KEY REFERENCES world_events(id) ON DELETE CASCADE,
                world_id TEXT NOT NULL REFERENCES worlds(id), npc_id TEXT NOT NULL REFERENCES characters(id),
                reaction_event_id TEXT REFERENCES world_events(id) ON DELETE SET NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','ready','failed')),
                error_text TEXT
            );
            CREATE TABLE IF NOT EXISTS contract_fulfillments (
                request_id TEXT PRIMARY KEY REFERENCES long_term_operation_requests(id) ON DELETE CASCADE,
                world_id TEXT NOT NULL REFERENCES worlds(id), kind TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active','completed','overdue','expired','cancelled')),
                requester_id TEXT NOT NULL REFERENCES characters(id),
                recipient_id TEXT NOT NULL REFERENCES characters(id),
                lender_id TEXT, borrower_id TEXT, principal INTEGER NOT NULL DEFAULT 0,
                escrow INTEGER NOT NULL DEFAULT 0, asset_id TEXT, location_id TEXT,
                required_units INTEGER NOT NULL DEFAULT 1, started_world_time TEXT NOT NULL,
                due_world_time TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_active_vehicle_lease
                ON contract_fulfillments(asset_id) WHERE kind='lease' AND status='active';
            CREATE TABLE IF NOT EXISTS contract_receipts (
                event_id TEXT PRIMARY KEY REFERENCES world_events(id) ON DELETE CASCADE,
                request_id TEXT NOT NULL REFERENCES contract_fulfillments(request_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS inventory_changes (
                id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id),
                source_event_id TEXT NOT NULL REFERENCES world_events(id) ON DELETE CASCADE,
                item_instance_id TEXT NOT NULL, before_json TEXT, after_json TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_inventory_changes_event
                ON inventory_changes(world_id, source_event_id);
            CREATE TRIGGER IF NOT EXISTS item_initial_owner AFTER INSERT ON item_instances
            WHEN NEW.owner_character_id IS NULL
             AND NEW.container_type IN ('character_inventory','character_equipment')
            BEGIN
                UPDATE item_instances SET owner_character_id=NEW.container_id WHERE id=NEW.id;
            END;
            CREATE TRIGGER IF NOT EXISTS enqueue_event_memory AFTER INSERT ON world_events
            BEGIN
                INSERT OR IGNORE INTO memory_jobs(id,world_id,event_id,status,created_at,updated_at)
                VALUES ('memjob:'||NEW.id, NEW.world_id, NEW.id, 'pending',
                        NEW.created_at, NEW.created_at);
            END;
        """)
        connection.execute("""
            INSERT OR IGNORE INTO memory_jobs(id,world_id,event_id,status,created_at,updated_at)
            SELECT 'memjob:'||id, world_id, id, 'pending', created_at, created_at FROM world_events
        """)

    @staticmethod
    def _migrate_social_operation_columns(connection: sqlite3.Connection) -> None:
        """为已有世界补长期事务的反提案字段；不改写任何既有事务。"""
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "long_term_operation_requests" not in tables:
            return
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(long_term_operation_requests)"
            ).fetchall()
        }
        if "counter_terms_json" not in columns:
            connection.execute(
                "ALTER TABLE long_term_operation_requests ADD COLUMN counter_terms_json TEXT"
            )
        if "system_notice" not in columns:
            connection.execute(
                "ALTER TABLE long_term_operation_requests ADD COLUMN system_notice TEXT"
            )

    @staticmethod
    def _remove_legacy_player_controlled_social_records(connection: sqlite3.Connection) -> None:
        """清理旧版取消事务和玩家代写的 NPC 待办，避免它们在升级后继续生效。"""
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        required_tables = {"long_term_operation_requests", "npc_todos", "world_events"}
        if not required_tables.issubset(tables):
            return

        cancelled_request_ids = {
            str(row["id"])
            for row in connection.execute(
                "SELECT id FROM long_term_operation_requests WHERE status = 'cancelled'"
            ).fetchall()
        }
        player_todo_event_ids = {
            str(row["id"])
            for row in connection.execute(
                "SELECT id FROM world_events WHERE event_type = 'social.todo_created'"
            ).fetchall()
        }
        request_event_ids: set[str] = set()
        if cancelled_request_ids:
            for event in connection.execute(
                "SELECT id, payload_json FROM world_events"
            ).fetchall():
                try:
                    payload = json.loads(event["payload_json"])
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(payload, dict) and payload.get("request_id") in cancelled_request_ids:
                    request_event_ids.add(str(event["id"]))

        event_ids = request_event_ids | player_todo_event_ids
        if event_ids:
            placeholders = ", ".join("?" for _ in event_ids)
            connection.execute(
                f"DELETE FROM npc_todos WHERE source_event_id IN ({placeholders})",
                tuple(event_ids),
            )
            # 关联记忆随 world_events 的外键级联删除。
            connection.execute(
                f"DELETE FROM world_events WHERE id IN ({placeholders})", tuple(event_ids)
            )
        if cancelled_request_ids:
            placeholders = ", ".join("?" for _ in cancelled_request_ids)
            connection.execute(
                f"DELETE FROM long_term_operation_requests WHERE id IN ({placeholders})",
                tuple(cancelled_request_ids),
            )

    @staticmethod
    def _migrate_registration_columns(connection: sqlite3.Connection) -> None:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "construction_projects" not in tables:
            return
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(construction_projects)"
            ).fetchall()
        }
        if "target_entity_id" not in columns:
            connection.execute(
                "ALTER TABLE construction_projects ADD COLUMN target_entity_id TEXT"
            )

    @staticmethod
    def _migrate_registration_element_types(connection: sqlite3.Connection) -> None:
        """为旧存档扩展登记类型约束，保留全部申请与外键引用。"""
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'element_registration_requests'"
        ).fetchone()
        if row is None or "character_arrival" in str(row["sql"] or ""):
            return
        connection.commit()
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.execute(
                """
                CREATE TABLE element_registration_requests_next (
                    id TEXT PRIMARY KEY,
                    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
                    element_type TEXT NOT NULL CHECK(element_type IN (
                        'character_birth','character_arrival','settlement','building','structure','lore'
                    )),
                    requested_by_character_id TEXT REFERENCES characters(id) ON DELETE SET NULL,
                    source_event_id TEXT NOT NULL REFERENCES world_events(id) ON DELETE RESTRICT,
                    idempotency_key TEXT NOT NULL,
                    schema_version INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN (
                        'proposed','validating','approved','rejected','applying','applied','failed'
                    )),
                    payload_json TEXT NOT NULL,
                    result_entity_id TEXT,
                    rejection_reason TEXT,
                    input_world_version INTEGER NOT NULL,
                    applied_world_version INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(world_id, idempotency_key)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO element_registration_requests_next
                SELECT * FROM element_registration_requests
                """
            )
            connection.execute("DROP TABLE element_registration_requests")
            connection.execute(
                "ALTER TABLE element_registration_requests_next "
                "RENAME TO element_registration_requests"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_element_registrations_world_status "
                "ON element_registration_requests(world_id, status, created_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_element_registrations_source_event "
                "ON element_registration_requests(source_event_id)"
            )
            connection.commit()
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _migrate_element_lifecycle_columns(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(locations)")
        }
        if "is_active" not in columns:
            connection.execute(
                "ALTER TABLE locations ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1"
            )

    @staticmethod
    def _synchronize_world_element_catalog(connection: sqlite3.Connection) -> None:
        """为旧存档补全目录；目录不复制领域字段，也不改变既有事实。"""
        # 延迟导入避免数据库初始化阶段产生循环依赖。
        from world_engine.elements import WorldElementCatalog

        catalog = WorldElementCatalog()
        world_ids = connection.execute("SELECT id FROM worlds").fetchall()
        for world in world_ids:
            catalog.synchronize_world(connection, world_id=world["id"])

    @staticmethod
    def _migrate_character_species_columns(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(characters)")
        }
        if "species" not in columns:
            connection.execute(
                "ALTER TABLE characters ADD COLUMN species TEXT NOT NULL DEFAULT 'human'"
            )
        if "birth_world_time" not in columns:
            connection.execute("ALTER TABLE characters ADD COLUMN birth_world_time TEXT")
        if "gender" not in columns:
            connection.execute("ALTER TABLE characters ADD COLUMN gender TEXT")

    @staticmethod
    def _ensure_npc_demographics(connection: sqlite3.Connection) -> None:
        """仅补全缺失的 NPC 性别/生日，绝不覆盖已经写入的角色属性。"""
        rows = connection.execute(
            """
            SELECT c.id, c.name, c.species, c.gender, c.birth_world_time,
                   w.current_time, COALESCE(s.adult_age_world_years, 16) AS adult_age
            FROM characters c
            JOIN worlds w ON w.id = c.world_id
            LEFT JOIN species_profiles s ON s.id = c.species
            WHERE c.is_player = 0 AND (c.gender IS NULL OR c.gender = '' OR c.birth_world_time IS NULL OR c.birth_world_time = '')
            """
        ).fetchall()
        for row in rows:
            demographic = stable_npc_demographics(
                identity_key=f"{row['id']}|{row['name']}|{row['species']}",
                world_time=parse_datetime(row["current_time"]),
                adult_age_world_years=int(row["adult_age"]),
            )
            gender = row["gender"] or demographic.gender
            birth = row["birth_world_time"] or demographic.birth_world_time.isoformat()
            connection.execute(
                "UPDATE characters SET gender = ?, birth_world_time = ? WHERE id = ?",
                (gender, birth, row["id"]),
            )

    @staticmethod
    def _ensure_default_species_profiles(connection: sqlite3.Connection) -> None:
        now = "1970-01-01T00:00:00+00:00"
        profiles = (
            ("human", "人类", 270, 16, ["human"]),
            ("elf", "精灵", 360, 60, ["elf"]),
            ("dwarf", "矮人", 300, 18, ["dwarf"]),
            ("goblin", "地精", 180, 10, ["goblin"]),
            ("orc", "兽人", 240, 16, ["orc"]),
        )
        for identifier, name, gestation, adult, compatible in profiles:
            connection.execute(
                """
                INSERT OR IGNORE INTO species_profiles(
                    id, display_name, gestation_world_days, adult_age_world_years,
                    compatible_species_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    name,
                    gestation,
                    adult,
                    json.dumps(compatible, ensure_ascii=False),
                    now,
                    now,
                ),
            )

    @staticmethod
    def _repair_time_only_timestamps(connection: sqlite3.Connection) -> None:
        """把损坏的"仅含时间"的 worlds.current_time 修复为完整 datetime。

        来源可能来自外部/手工写入；若不修复，编排入口与裁判的
        datetime.fromisoformat 会因时间串直接崩溃。
        """
        for row in connection.execute("SELECT id, current_time FROM worlds").fetchall():
            current = str(row["current_time"] or "")
            if not is_time_only(current):
                continue
            clock_row = connection.execute(
                "SELECT last_adjudication_world_time FROM world_clock WHERE world_id = ?",
                (row["id"],),
            ).fetchone()
            base_date = None
            if clock_row and clock_row["last_adjudication_world_time"]:
                try:
                    base_date = parse_datetime(
                        clock_row["last_adjudication_world_time"]
                    ).date()
                except (TypeError, ValueError):
                    base_date = None
            repaired = parse_datetime(current, base_date=base_date)
            connection.execute(
                "UPDATE worlds SET current_time = ? WHERE id = ?",
                (repaired.isoformat(), row["id"]),
            )

    @staticmethod
    def _ensure_runtime_columns(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(world_runtime)").fetchall()
        }
        if "last_worker_seen_at" not in columns:
            connection.execute("ALTER TABLE world_runtime ADD COLUMN last_worker_seen_at TEXT")

    @staticmethod
    def _migrate_identity_column(connection: sqlite3.Connection) -> None:
        """为旧库 characters 表补 identity 列；新库由 SCHEMA 直接创建。"""
        character_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(characters)").fetchall()
        }
        if "identity" not in character_columns:
            connection.execute("ALTER TABLE characters ADD COLUMN identity TEXT")

    @staticmethod
    def _migrate_pov_column(connection: sqlite3.Connection) -> None:
        """为旧库 characters 表补 is_pov 列(玩家主控标记)；新库由 SCHEMA 直接创建。"""
        character_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(characters)").fetchall()
        }
        if "is_pov" not in character_columns:
            connection.execute(
                "ALTER TABLE characters ADD COLUMN is_pov INTEGER NOT NULL DEFAULT 0"
            )

    @staticmethod
    def _migrate_health_column(connection: sqlite3.Connection) -> None:
        """为旧库 characters 表补 health 列(生命值)；新库由 SCHEMA 直接创建。"""
        character_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(characters)").fetchall()
        }
        if "health" not in character_columns:
            connection.execute(
                "ALTER TABLE characters ADD COLUMN health INTEGER NOT NULL DEFAULT 100"
            )

    @staticmethod
    def _migrate_skills_column(connection: sqlite3.Connection) -> None:
        """为旧库 characters 表补 skills_json 列；新库由 SCHEMA 直接创建。"""
        character_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(characters)").fetchall()
        }
        if "skills_json" not in character_columns:
            connection.execute(
                "ALTER TABLE characters ADD COLUMN skills_json TEXT NOT NULL DEFAULT '[]'"
            )

    @staticmethod
    def _migrate_spatial_columns(connection: sqlite3.Connection) -> None:
        """为旧库补世界经纬度，并让人物位置跟随其地点中心。"""
        location_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(locations)").fetchall()
        }
        if "longitude" not in location_columns:
            connection.execute("ALTER TABLE locations ADD COLUMN longitude REAL")
        if "latitude" not in location_columns:
            connection.execute("ALTER TABLE locations ADD COLUMN latitude REAL")

        character_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(characters)").fetchall()
        }
        if "longitude" not in character_columns:
            connection.execute("ALTER TABLE characters ADD COLUMN longitude REAL")
        if "latitude" not in character_columns:
            connection.execute("ALTER TABLE characters ADD COLUMN latitude REAL")

        # 旧地点按稳定顺序散布在北方主大陆；该回填可重复执行且不会改写已有坐标。
        missing_locations = connection.execute(
            """
            SELECT id, world_id, name
            FROM locations
            WHERE longitude IS NULL OR latitude IS NULL
            ORDER BY world_id, name, id
            """
        ).fetchall()
        world_indexes: dict[str, int] = {}
        known_coordinates = {
            "澜誓城": (-71.0, 28.0),
            "锻谷城": (-132.0, 31.0),
            "望镜湖庭": (-62.0, 50.0),
            "东澜港": (-22.0, 30.0),
            "赭泉关": (-67.0, 12.0),
            "冠枝河庭": (58.0, 14.0),
            "阶泉城": (42.0, -28.0),
            "九泉驿城": (78.0, -18.0),
            "南潮门港": (52.0, -48.0),
            "烬湾": (112.0, 26.0),
            "灯庭": (132.0, 20.0),
            "东门城": (148.0, 8.0),
            "避潮岛": (161.0, -8.0),
            "河务档案区": (-71.08, 28.04),
            "王室堤岸": (-70.94, 27.98),
            "河畔大粮仓": (-71.03, 27.92),
            "法师家族宅区": (-71.12, 28.10),
        }
        for row in missing_locations:
            index = world_indexes.get(row["world_id"], 0)
            column = index % 6
            band = index // 6
            longitude, latitude = known_coordinates.get(
                row["name"], (-138.0 + column * 15.0, 58.0 - band * 11.0)
            )
            connection.execute(
                "UPDATE locations SET longitude = ?, latitude = ? WHERE id = ?",
                (longitude, latitude, row["id"]),
            )
            world_indexes[row["world_id"]] = index + 1

        # 用人物 ID 生成稳定的小偏移，避免同地点多人完全重叠，同时保证位于地点附近。
        missing_characters = connection.execute(
            """
            SELECT c.id, l.longitude, l.latitude
            FROM characters c
            JOIN locations l ON l.id = c.location_id
            WHERE c.longitude IS NULL OR c.latitude IS NULL
            ORDER BY c.id
            """
        ).fetchall()
        for row in missing_characters:
            checksum = sum(ord(character) for character in row["id"])
            longitude_offset = ((checksum % 17) - 8) * 0.002
            latitude_offset = (((checksum // 17) % 17) - 8) * 0.002
            connection.execute(
                """
                UPDATE characters
                SET longitude = ?, latitude = ?
                WHERE id = ?
                """,
                (
                    max(-180.0, min(180.0, row["longitude"] + longitude_offset)),
                    max(-90.0, min(90.0, row["latitude"] + latitude_offset)),
                    row["id"],
                ),
            )

    @staticmethod
    def _migrate_movement_columns(connection: sqlite3.Connection) -> None:
        """为旧库人物补当前移动能力；进行中路程另存于 character_movements。"""
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(characters)").fetchall()
        }
        if "movement_type" not in columns:
            connection.execute(
                "ALTER TABLE characters ADD COLUMN movement_type TEXT NOT NULL DEFAULT 'land'"
            )
        if "movement_speed_kmh" not in columns:
            connection.execute(
                """
                ALTER TABLE characters
                ADD COLUMN movement_speed_kmh REAL NOT NULL DEFAULT 5
                """
            )
        if "active_vehicle_id" not in columns:
            connection.execute("ALTER TABLE characters ADD COLUMN active_vehicle_id TEXT")

    @staticmethod
    def _migrate_area_and_activation_columns(
        connection: sqlite3.Connection,
    ) -> None:
        """为旧库补分层区域、详细地图绑定和NPC激活生命周期。"""
        location_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(locations)").fetchall()
        }
        area_columns_added = False
        if "area_radius_km" not in location_columns:
            connection.execute(
                "ALTER TABLE locations ADD COLUMN area_radius_km REAL NOT NULL DEFAULT 1"
            )
            area_columns_added = True
        if "area_priority" not in location_columns:
            connection.execute(
                "ALTER TABLE locations ADD COLUMN area_priority INTEGER NOT NULL DEFAULT 0"
            )
            area_columns_added = True
        if "parent_location_id" not in location_columns:
            connection.execute("ALTER TABLE locations ADD COLUMN parent_location_id TEXT")
        if area_columns_added:
            connection.execute(
                """
                UPDATE locations
                SET area_radius_km = CASE kind
                        WHEN 'city' THEN 20
                        WHEN 'ruin' THEN 8
                        WHEN 'public' THEN 2
                        WHEN 'workplace' THEN 2
                        WHEN 'home' THEN 1.5
                        ELSE 3
                    END,
                    area_priority = CASE kind
                        WHEN 'home' THEN 40
                        WHEN 'workplace' THEN 35
                        WHEN 'public' THEN 30
                        WHEN 'ruin' THEN 20
                        WHEN 'city' THEN 10
                        ELSE 5
                    END
                """
            )
        connection.execute(
            """
            UPDATE locations AS child
            SET parent_location_id = (
                SELECT city.id FROM locations AS city
                WHERE city.world_id = child.world_id AND city.name = '澜誓城'
            )
            WHERE child.name IN (
                '河务档案区', '王室堤岸', '河畔大粮仓', '法师家族宅区'
            ) AND child.parent_location_id IS NULL
            """
        )

        map_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(world_maps)").fetchall()
        }
        if "location_id" not in map_columns:
            connection.execute("ALTER TABLE world_maps ADD COLUMN location_id TEXT")
        if "map_role" not in map_columns:
            connection.execute(
                "ALTER TABLE world_maps ADD COLUMN map_role TEXT NOT NULL DEFAULT 'world'"
            )
        if "review_status" not in map_columns:
            connection.execute(
                """
                ALTER TABLE world_maps
                ADD COLUMN review_status TEXT NOT NULL DEFAULT 'approved'
                """
            )
        connection.execute(
            """
            UPDATE world_maps SET map_role = CASE
                WHEN location_id IS NOT NULL THEN 'detail'
                WHEN zoom_level > 0 THEN 'tile'
                ELSE 'world'
            END
            """
        )

        character_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(characters)").fetchall()
        }
        additions = {
            "current_location_id": "TEXT",
            "activation_state": "TEXT NOT NULL DEFAULT 'background'",
            "activation_policy": "TEXT NOT NULL DEFAULT 'distance'",
            "activation_reason": "TEXT",
            "activation_until_world_time": "TEXT",
            "activation_radius_km": "REAL NOT NULL DEFAULT 35",
            "activation_probability": "REAL NOT NULL DEFAULT 0.85",
            "last_activation_check_world_time": "TEXT",
        }
        activation_columns_added = False
        for name, definition in additions.items():
            if name not in character_columns:
                connection.execute(
                    f"ALTER TABLE characters ADD COLUMN {name} {definition}"  # noqa: S608
                )
                activation_columns_added = True
        if activation_columns_added:
            connection.execute(
                """
                UPDATE characters
                SET current_location_id = location_id
                WHERE current_location_id IS NULL
                """
            )
        persistent_keywords = (
            "女王",
            "代表",
            "议长",
            "召集",
            "主持",
            "行誓者",
            "记录官",
            "书记官",
            "管事",
            "工长",
            "领航",
            "传声",
            "记录者",
            "调度官",
            "首席",
            "总监",
            "守潮",
            "井见",
            "灯判",
            "港守",
            "祭官",
        )
        identity_clause = " OR ".join("COALESCE(identity, '') LIKE ?" for _ in persistent_keywords)
        identity_values = tuple(f"%{keyword}%" for keyword in persistent_keywords)
        connection.execute(
            f"""
            UPDATE characters
            SET activation_policy = 'persistent',
                activation_state = 'active',
                activation_reason = CASE
                    WHEN is_player = 1 THEN 'player'
                    WHEN is_core = 1 THEN 'core'
                    ELSE 'special'
                END
            WHERE is_player = 1 OR is_core = 1 OR {identity_clause}
            """,  # noqa: S608 - 条件只由上方固定关键词生成
            identity_values,
        )
        if activation_columns_added:
            connection.execute(
                f"""
                UPDATE characters
                SET activation_policy = 'distance',
                    activation_state = 'background',
                    activation_reason = 'background'
                WHERE is_player = 0 AND is_core = 0
                  AND NOT ({identity_clause})
                """,  # noqa: S608 - 条件只由上方固定关键词生成
                identity_values,
            )

    @staticmethod
    def _ensure_detail_world_maps(connection: sqlite3.Connection) -> None:
        """为已存在的澜誓城登记详细地图；其他地点可沿同一模型扩展。"""
        rows = connection.execute(
            """
            SELECT id, world_id, longitude, latitude
            FROM locations WHERE name = '澜誓城'
            """
        ).fetchall()
        for row in rows:
            latitude_delta = 0.18
            longitude_delta = 0.21
            connection.execute(
                """
                INSERT OR IGNORE INTO world_maps(
                    id, world_id, name, kind, asset_path,
                    min_longitude, max_longitude, min_latitude, max_latitude,
                    width_pixels, height_pixels, zoom_level,
                    location_id, map_role, review_status
                ) VALUES (?, ?, '澜誓城详细地图', 'settlement', ?, ?, ?, ?, ?,
                          1600, 1600, 2, ?, 'detail', 'candidate')
                """,
                (
                    f"{row['world_id']}:map:detail:oathflow",
                    row["world_id"],
                    "navigation/settlements/oathflow/detail-map.svg",
                    row["longitude"] - longitude_delta,
                    row["longitude"] + longitude_delta,
                    row["latitude"] - latitude_delta,
                    row["latitude"] + latitude_delta,
                    row["id"],
                ),
            )
            connection.execute(
                """
                UPDATE world_maps
                SET asset_path = ?, name = '澜誓城详细地图', kind = 'settlement',
                    min_longitude = ?, max_longitude = ?,
                    min_latitude = ?, max_latitude = ?,
                    width_pixels = 1600, height_pixels = 1600,
                    zoom_level = 2, location_id = ?, map_role = 'detail',
                    review_status = 'candidate'
                WHERE id = ?
                """,
                (
                    "navigation/settlements/oathflow/detail-map.svg",
                    row["longitude"] - longitude_delta,
                    row["longitude"] + longitude_delta,
                    row["latitude"] - latitude_delta,
                    row["latitude"] + latitude_delta,
                    row["id"],
                    f"{row['world_id']}:map:detail:oathflow",
                ),
            )

    @staticmethod
    def _migrate_event_importance(connection: sqlite3.Connection) -> None:
        """旧动作归入日志；只有明确的世界级事实进入重大编年。"""
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(world_events)").fetchall()
        }
        if "importance" not in columns:
            connection.execute(
                """
                ALTER TABLE world_events
                ADD COLUMN importance TEXT NOT NULL DEFAULT 'routine'
                """
            )
        connection.execute(
            """
            UPDATE world_events
            SET importance = CASE
                WHEN event_type IN (
                    'world.initial', 'world.major_death', 'world.catastrophe',
                    'world.treaty', 'world.regime_change'
                ) THEN 'major'
                ELSE 'routine'
            END
            WHERE importance NOT IN ('major', 'routine')
               OR importance IS NULL
               OR (importance = 'routine' AND event_type IN (
                    'world.initial', 'world.major_death', 'world.catastrophe',
                    'world.treaty', 'world.regime_change'
               ))
            """
        )
        connection.execute(
            """
            UPDATE world_events
            SET importance = 'routine'
            WHERE event_type IN (
                'world.player_defeat', 'world.warden_intervention',
                'world.adjudication', 'world.clock_rate_changed'
            ) OR event_type LIKE 'action.%'
            """
        )

    @staticmethod
    def _ensure_default_world_maps(connection: sqlite3.Connection) -> None:
        """登记 Noryia 全球等距圆柱总览；并清除历史遗留的 8×8 瓦片登记。

        Noryia.svg 由 Azgaar 奇幻地图生成器导出，坐标为等距圆柱投影：
        mapCoordinates lonW=-180, lonE=180, latS=-90, latN=90，画布 10015×5008。
        旧版 Gogenia 总览与 map_new/A1.png…H8.png 瓦片资源已不存在，故一并移除，
        前端因此回落到单张主图并沿用现有的滚轮缩放 / 拖拽平移逻辑。
        """
        worlds = connection.execute("SELECT id FROM worlds").fetchall()
        for world in worlds:
            world_id = world["id"]
            master_id = f"{world_id}:map:z0"
            connection.execute(
                """
                INSERT INTO world_maps(
                    id, world_id, name, kind, asset_path,
                    min_longitude, max_longitude, min_latitude, max_latitude,
                    width_pixels, height_pixels, zoom_level
                ) VALUES (?, ?, 'Noryia 世界地图', 'terrain', 'map_new/Noryia.svg',
                          -180, 180, -90, 90, 10015, 5008, 0)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    kind = excluded.kind,
                    asset_path = excluded.asset_path,
                    min_longitude = excluded.min_longitude,
                    max_longitude = excluded.max_longitude,
                    min_latitude = excluded.min_latitude,
                    max_latitude = excluded.max_latitude,
                    width_pixels = excluded.width_pixels,
                    height_pixels = excluded.height_pixels,
                    zoom_level = excluded.zoom_level
                """,
                (master_id, world_id),
            )
            # 历史 8×8 瓦片资源(map_new/A1.png…H8.png)不存在，删除以免前端渲染空图。
            connection.execute(
                "DELETE FROM world_maps WHERE world_id = ? AND map_role = 'tile'",
                (world_id,),
            )

    @staticmethod
    def _ensure_noryia_city_detail_maps(connection: sqlite3.Connection) -> None:
        """为已落盘的 Noryia 城镇图登记可自动切换的详细地图。"""

        root = Path(__file__).resolve().parents[1]
        index_path = root / "docs/worldbuilding/maps/map_new/cities/index.json"
        if not index_path.is_file():
            return
        cities = json.loads(index_path.read_text(encoding="utf-8")).get("cities", [])
        by_source_id = {int(city["id"]): city for city in cities if city.get("downloaded")}
        for location in connection.execute("SELECT * FROM locations").fetchall():
            try:
                source_id = json.loads(location["resources_json"]).get("source_id")
            except json.JSONDecodeError:
                continue
            city = by_source_id.get(source_id)
            if city is None:
                continue
            asset_path = str(city["asset_path"])
            asset = root / "docs/worldbuilding/maps" / asset_path
            if not asset.is_file() or asset.stat().st_size < 20_000:
                continue
            radius_degrees = max(0.025, float(location["area_radius_km"]) / 111.0)
            connection.execute(
                """
                INSERT OR IGNORE INTO world_maps(
                    id, world_id, name, kind, asset_path,
                    min_longitude, max_longitude, min_latitude, max_latitude,
                    width_pixels, height_pixels, zoom_level,
                    location_id, map_role, review_status
                ) VALUES (?, ?, ?, 'settlement', ?, ?, ?, ?, ?,
                          1280, 720, 2, ?, 'detail', 'approved')
                """,
                (
                    f"{location['world_id']}:map:city:{source_id}",
                    location["world_id"],
                    f"{location['name']}详细地图",
                    asset_path,
                    max(-180.0, location["longitude"] - radius_degrees),
                    min(180.0, location["longitude"] + radius_degrees),
                    max(-90.0, location["latitude"] - radius_degrees),
                    min(90.0, location["latitude"] + radius_degrees),
                    location["id"],
                ),
            )

    @staticmethod
    def _ensure_noryia_display_names(connection: sqlite3.Connection) -> None:
        """将 Noryia 中文展示名同步到运行时，保留原始名在来源索引中。"""

        root = Path(__file__).resolve().parents[1]
        path = root / "docs/worldbuilding/maps/map_new/data/translations.json"
        if not path.is_file():
            return
        translations = json.loads(path.read_text(encoding="utf-8"))
        city_names = translations.get("cities", {})
        used: set[tuple[str, str]] = set()
        for location in connection.execute(
            "SELECT id, world_id, resources_json FROM locations"
        ).fetchall():
            try:
                source_id = str(json.loads(location["resources_json"]).get("source_id"))
            except (TypeError, json.JSONDecodeError):
                continue
            translated = city_names.get(source_id, {}).get("display_name")
            if not translated:
                continue
            key = (location["world_id"], translated)
            if key in used:
                translated = f"{translated}·{source_id}"
            used.add(key)
            connection.execute(
                "UPDATE locations SET name = ? WHERE id = ?", (translated, location["id"])
            )
        state_names = translations.get("states", {})
        for feature in connection.execute(
            "SELECT id, name, metadata_json FROM map_features WHERE feature_type = 'capital'"
        ).fetchall():
            try:
                source_state = json.loads(feature["metadata_json"]).get("state")
            except json.JSONDecodeError:
                continue
            translated = state_names.get(source_state, {}).get("full_name_display")
            if translated:
                connection.execute(
                    "UPDATE map_features SET name = ? WHERE id = ?", (translated, feature["id"])
                )
        province_names = translations.get("provinces", {})
        for feature in connection.execute(
            "SELECT id, name FROM map_features WHERE feature_type = 'province'"
        ).fetchall():
            translated = next(
                (
                    item.get("display_name")
                    for item in province_names.values()
                    if feature["name"].startswith(str(item.get("source_name", "")))
                ),
                None,
            )
            if translated:
                connection.execute(
                    "UPDATE map_features SET name = ? WHERE id = ?",
                    (translated, feature["id"]),
                )

    @staticmethod
    def _migrate_navigation_columns(connection: sqlite3.Connection) -> None:
        """为旧 character_movements 表补路线折线相关字段；新库由 SCHEMA 直接创建。"""
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(character_movements)").fetchall()
        }
        additions = {
            "route_json": "TEXT NOT NULL DEFAULT '[]'",
            "route_index": "INTEGER NOT NULL DEFAULT 0",
            "route_distance_km": "REAL NOT NULL DEFAULT 0",
            "navigation_dataset_id": "TEXT",
            "replan_reason": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE character_movements ADD COLUMN {name} {definition}"  # noqa: S608
                )

    @staticmethod
    def _ensure_known_map_features(connection: sqlite3.Connection) -> None:
        """把旧世界中已有的建筑/地标地点登记为空间要素，不创造新设定。"""
        feature_types = {
            "河务档案区": "building",
            "王室堤岸": "landmark",
            "河畔大粮仓": "building",
            "法师家族宅区": "district",
        }
        for name, feature_type in feature_types.items():
            rows = connection.execute(
                """
                SELECT l.id, l.world_id, l.name, l.longitude, l.latitude,
                       w.updated_at
                FROM locations l
                JOIN worlds w ON w.id = l.world_id
                WHERE l.name = ?
                """,
                (name,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO map_features(
                        id, world_id, name, feature_type, longitude, latitude,
                        location_id, is_known, metadata_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, '{}', ?, ?)
                    """,
                    (
                        f"{row['world_id']}:feature:{row['id']}",
                        row["world_id"],
                        row["name"],
                        feature_type,
                        row["longitude"],
                        row["latitude"],
                        row["id"],
                        row["updated_at"],
                        row["updated_at"],
                    ),
                )

    @staticmethod
    def _migrate_player_column(connection: sqlite3.Connection) -> None:
        """补充玩家归属标记，并清除旧版本误设给 NPC 的主视角。"""
        character_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(characters)").fetchall()
        }
        if "is_player" not in character_columns:
            connection.execute(
                "ALTER TABLE characters ADD COLUMN is_player INTEGER NOT NULL DEFAULT 0"
            )
            if "is_pov" in character_columns:
                connection.execute("UPDATE characters SET is_pov = 0")

    @staticmethod
    def _migrate_satiety_columns(connection: sqlite3.Connection) -> None:
        character_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(characters)").fetchall()
        }
        if "hunger" in character_columns and "satiety" not in character_columns:
            connection.execute("ALTER TABLE characters RENAME COLUMN hunger TO satiety")
            connection.execute("UPDATE characters SET satiety = 100 - satiety")

        accumulator_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(character_state_accumulators)"
            ).fetchall()
        }
        if (
            "hunger_residual" in accumulator_columns
            and "satiety_residual" not in accumulator_columns
        ):
            connection.execute(
                """
                ALTER TABLE character_state_accumulators
                RENAME COLUMN hunger_residual TO satiety_residual
                """
            )

    @staticmethod
    def _ensure_clock_and_accumulator_rows(connection: sqlite3.Connection) -> None:
        world_rows = connection.execute(
            """
            SELECT worlds.id,
                   worlds."current_time" AS current_time,
                   worlds.updated_at
            FROM worlds
            """
        ).fetchall()
        for row in world_rows:
            current_time = parse_datetime(row["current_time"])
            next_boundary = next_adjudication_boundary(current_time)
            connection.execute(
                """
                INSERT OR IGNORE INTO world_clock(
                    world_id, time_scale, heartbeat_interval_seconds,
                    last_heartbeat_real_time, clock_revision, offline_policy,
                    last_adjudication_world_time, next_adjudication_world_time,
                    adjudication_interval_minutes, updated_at
                ) VALUES (?, 1.0, 60, NULL, 0, 'pause', ?, ?, 720, ?)
                """,
                (
                    row["id"],
                    row["current_time"],
                    next_boundary.isoformat(),
                    row["updated_at"],
                ),
            )
        connection.execute(
            """
            INSERT OR IGNORE INTO character_state_accumulators(
                character_id, world_id, satiety_residual, energy_residual, updated_at
            )
            SELECT id, world_id, 0, 0, updated_at FROM characters
            """
        )

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
