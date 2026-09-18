from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from world_engine import migrations
from world_engine import schema as world_schema

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
        'character_birth','character_arrival','settlement','building','structure','lore','interior_room','activity_recipe','commodity','workplace_budget','npc_routine'
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

-- 以下索引服务于 adjudication 与列表接口的高频过滤条件。
-- 当前这些表数据量很小，收益有限；它们是为世界长期运行后的增长预置的。
CREATE INDEX IF NOT EXISTS idx_npc_todos_owner
ON npc_todos(world_id, character_id, status);

CREATE INDEX IF NOT EXISTS idx_item_instances_world_container
ON item_instances(world_id, container_type, container_id);

CREATE INDEX IF NOT EXISTS idx_item_instances_container
ON item_instances(container_type, container_id);

CREATE INDEX IF NOT EXISTS idx_character_messages_contact
ON character_messages(contact_id, world_time, created_at);
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
        # WAL 下 NORMAL 已能保证崩溃不损坏数据库，只可能丢失最后一个未落盘事务，
        # 相比默认的 FULL 可省去每次 commit 的 WAL fsync。心跳循环写事务密集，收益明显。
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            migrations.ensure_runtime_columns(connection)
            migrations.migrate_satiety_columns(connection)
            migrations.migrate_identity_column(connection)
            migrations.migrate_player_column(connection)
            migrations.migrate_pov_column(connection)
            migrations.migrate_health_column(connection)
            migrations.migrate_skills_column(connection)
            migrations.migrate_spatial_columns(connection)
            migrations.migrate_movement_columns(connection)
            migrations.migrate_area_and_activation_columns(connection)
            migrations.migrate_event_importance(connection)
            migrations.migrate_navigation_columns(connection)
            migrations.migrate_registration_columns(connection)
            migrations.migrate_registration_element_types(connection)
            migrations.migrate_element_lifecycle_columns(connection)
            migrations.migrate_character_species_columns(connection)
            migrations.migrate_social_operation_columns(connection)
            migrations.migrate_completion_columns(connection)
            migrations.migrate_life_kinds(connection)
            connection.executescript(world_schema.LIFE_SCHEMA)
            connection.executescript(world_schema.TASK_SCHEMA)
            connection.executescript(world_schema.CHECK_SCHEMA)
            connection.executescript(world_schema.SOCIETY_SCHEMA)
            connection.executescript(world_schema.ECONOMY_SCHEMA)
            connection.executescript(world_schema.LIVING_SCHEMA)
            connection.executescript(world_schema.SEQUENCE_SCHEMA)
            connection.executescript(world_schema.SCHEDULE_SCHEMA)
            connection.executescript(world_schema.ROUTINE_SCHEMA)
            connection.executescript(world_schema.GOAL_SCHEMA)
            connection.executescript(world_schema.EVENT_HISTORY_SCHEMA)
            connection.executescript(world_schema.EPISTEMIC_SCHEMA)
            connection.executescript(world_schema.CHARACTER_GROWTH_SCHEMA)
            connection.executescript(world_schema.INTERIOR_SCHEMA)
            connection.executescript(world_schema.FOOD_SCHEMA)
            item_columns = {row["name"] for row in connection.execute("PRAGMA table_info(item_instances)")}
            for column in ("fresh_until_world_time", "spoils_world_time"):
                if column not in item_columns:
                    connection.execute(f"ALTER TABLE item_instances ADD COLUMN {column} TEXT")
            connection.executescript(world_schema.VISIT_SCHEMA)
            character_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(characters)")
            }
            if "current_room_id" not in character_columns:
                connection.execute(
                    "ALTER TABLE characters ADD COLUMN current_room_id TEXT "
                    "REFERENCES life_rooms(id) ON DELETE RESTRICT"
                )
            if "current_fixture_id" not in character_columns:
                connection.execute(
                    "ALTER TABLE characters ADD COLUMN current_fixture_id TEXT "
                    "REFERENCES life_fixtures(id) ON DELETE RESTRICT"
                )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_occupied_fixture "
                "ON characters(current_fixture_id) "
                "WHERE current_fixture_id IS NOT NULL"
            )
            migrations.remove_legacy_player_controlled_social_records(connection)
            migrations.ensure_default_species_profiles(connection)
            migrations.ensure_npc_demographics(connection)
            migrations.ensure_known_map_features(connection)
            migrations.ensure_default_world_maps(connection)
            migrations.ensure_noryia_city_detail_maps(connection)
            migrations.ensure_noryia_display_names(connection)
            migrations.ensure_detail_world_maps(connection)
            migrations.ensure_clock_and_accumulator_rows(connection)
            migrations.repair_time_only_timestamps(connection)
            self._synchronize_world_element_catalog(connection)
            from world_engine.society import SocietyService
            SocietyService.bootstrap(connection)
            from world_engine.character_growth import CharacterGrowthService
            from world_engine.event_history import EventHistoryService
            EventHistoryService.bootstrap(connection)
            CharacterGrowthService.bootstrap(connection)
            from world_engine.retention import initialize as initialize_retention

            initialize_retention(connection)
            migrations.backfill_food_supply(connection)

    @staticmethod
    def _synchronize_world_element_catalog(connection: sqlite3.Connection) -> None:
        """为旧存档补全目录；目录不复制领域字段，也不改变既有事实。"""
        # 延迟导入避免数据库初始化阶段产生循环依赖。
        from world_engine.elements import WorldElementCatalog

        catalog = WorldElementCatalog()
        world_ids = connection.execute("SELECT id FROM worlds").fetchall()
        for world in world_ids:
            catalog.synchronize_world(connection, world_id=world["id"])

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
