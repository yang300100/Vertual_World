"""世界数据库的建表语句集中定义。

历史上每张表的 DDL 分散在各自的业务模块中，`database.py` 为了聚合它们
不得不反向 import 19 个业务模块（其中还包括 API 层的 `living_api`），
成为全项目循环依赖的主要来源。这里集中所有建表语句后，数据库层只依赖
本模块，业务模块也不再持有 DDL。

注意：这里的顺序即建表顺序，表间外键依赖它，不要随意调整。
"""

from __future__ import annotations

# 原定义于 world_engine/action_checks.py
CHECK_SCHEMA = """
CREATE TABLE IF NOT EXISTS action_check_attempts (
 id TEXT PRIMARY KEY,
 world_id TEXT NOT NULL REFERENCES worlds(id),
 character_id TEXT NOT NULL REFERENCES characters(id),
 fingerprint TEXT NOT NULL,
 kind TEXT NOT NULL,
 snapshot_json TEXT NOT NULL,
 roll INTEGER CHECK(roll BETWEEN 1 AND 100),
 outcome TEXT CHECK(outcome IN ('success','partial','failure')),
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','resolved')),
 activity_id TEXT REFERENCES character_life_activities(id),
 source_event_id TEXT REFERENCES world_events(id),
 result_event_id TEXT REFERENCES world_events(id),
 created_at TEXT NOT NULL,
 UNIQUE(world_id,character_id,fingerprint)
);
CREATE TABLE IF NOT EXISTS activity_start_requests (
 world_id TEXT NOT NULL REFERENCES worlds(id),
 request_id TEXT NOT NULL,
 payload_json TEXT NOT NULL,
 response_json TEXT NOT NULL,
 PRIMARY KEY(world_id,request_id)
);
CREATE TABLE IF NOT EXISTS skill_practice_awards (
 attempt_id TEXT PRIMARY KEY REFERENCES action_check_attempts(id),world_id TEXT NOT NULL,
 character_id TEXT NOT NULL,skill_name TEXT NOT NULL,amount INTEGER NOT NULL,world_day TEXT NOT NULL
);
"""


# 原定义于 world_engine/activity_tasks.py
TASK_SCHEMA = """
CREATE TABLE IF NOT EXISTS activity_recipes (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id),
 registration_id TEXT NOT NULL REFERENCES element_registration_requests(id),
 name TEXT NOT NULL, spec_json TEXT NOT NULL, UNIQUE(world_id,name)
);
CREATE TABLE IF NOT EXISTS activity_task_details (
 activity_id TEXT PRIMARY KEY REFERENCES character_life_activities(id),
 room_id TEXT REFERENCES life_rooms(id), spec_json TEXT NOT NULL,
 before_json TEXT NOT NULL DEFAULT '{}', energy_paid INTEGER NOT NULL DEFAULT 0,
 satiety_paid INTEGER NOT NULL DEFAULT 0, result_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS activity_time_skips (
 request_id TEXT NOT NULL, world_id TEXT NOT NULL REFERENCES worlds(id),
 activity_id TEXT NOT NULL REFERENCES character_life_activities(id),
 input_version INTEGER NOT NULL, response_json TEXT NOT NULL,
 PRIMARY KEY(world_id,request_id)
);
"""


# 原定义于 world_engine/character_growth.py
CHARACTER_GROWTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS character_traits (
 character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
 world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 trait TEXT NOT NULL, intensity INTEGER NOT NULL CHECK(intensity BETWEEN 0 AND 100),
 origin_type TEXT NOT NULL CHECK(origin_type IN ('initial','acquired')),
 origin_event_id TEXT REFERENCES world_events(id) ON DELETE SET NULL,
 updated_world_time TEXT NOT NULL, PRIMARY KEY(character_id,trait)
);
CREATE TABLE IF NOT EXISTS character_trait_changes (
 id TEXT PRIMARY KEY,character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
 world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 trait TEXT NOT NULL,event_id TEXT REFERENCES world_events(id) ON DELETE SET NULL,
 before_value INTEGER NOT NULL,after_value INTEGER NOT NULL,world_day TEXT NOT NULL,
 occurred_at TEXT NOT NULL,reason TEXT NOT NULL,UNIQUE(character_id,trait,event_id)
);
"""


# 原定义于 world_engine/economy.py
ECONOMY_SCHEMA = """
CREATE TABLE IF NOT EXISTS world_item_profiles (
 world_id TEXT NOT NULL REFERENCES worlds(id),item_type_id TEXT PRIMARY KEY REFERENCES item_types(id),
 registration_id TEXT NOT NULL REFERENCES element_registration_requests(id),
 price INTEGER NOT NULL,nutrition INTEGER NOT NULL DEFAULT 0,
 resource_location_id TEXT REFERENCES locations(id),resource_key TEXT,resource_owner_id TEXT REFERENCES characters(id),
 daily_growth INTEGER NOT NULL DEFAULT 0,resource_capacity INTEGER NOT NULL DEFAULT 0,last_growth_world_time TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workplace_accounts (
 world_id TEXT NOT NULL REFERENCES worlds(id),location_id TEXT PRIMARY KEY REFERENCES locations(id),
 registration_id TEXT NOT NULL REFERENCES element_registration_requests(id),balance INTEGER NOT NULL,
 wage INTEGER NOT NULL DEFAULT 9
);
CREATE TABLE IF NOT EXISTS room_rental_rates (
 room_id TEXT PRIMARY KEY REFERENCES life_rooms(id),nightly_rate INTEGER NOT NULL CHECK(nightly_rate>0)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_active_room_lease ON contract_fulfillments(asset_id)
 WHERE kind='lodging' AND status='active';
"""


# 原定义于 world_engine/epistemics.py
EPISTEMIC_SCHEMA = """
CREATE TABLE IF NOT EXISTS observer_knowledge_nodes (
 observer_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
 subject_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
 world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 first_learned_at TEXT NOT NULL,last_learned_at TEXT NOT NULL,
 PRIMARY KEY(observer_id,subject_id)
);
CREATE TABLE IF NOT EXISTS observer_knowledge_edges (
 id TEXT PRIMARY KEY,world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 observer_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
 subject_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
 predicate TEXT NOT NULL CHECK(predicate IN ('name','location')),
 value TEXT NOT NULL,status TEXT NOT NULL,confidence REAL NOT NULL,
 first_learned_at TEXT NOT NULL,last_learned_at TEXT NOT NULL,last_verified_at TEXT,
 UNIQUE(observer_id,subject_id,predicate,value)
);
CREATE TABLE IF NOT EXISTS knowledge_evidence (
 id TEXT PRIMARY KEY,edge_id TEXT NOT NULL REFERENCES observer_knowledge_edges(id) ON DELETE CASCADE,
 source_event_id TEXT NOT NULL REFERENCES world_events(id) ON DELETE CASCADE,
 source_character_id TEXT REFERENCES characters(id) ON DELETE SET NULL,
 origin_event_id TEXT NOT NULL REFERENCES world_events(id) ON DELETE CASCADE,
 method TEXT NOT NULL CHECK(method IN ('observed','reported','document','heard')),
 quote TEXT NOT NULL,confidence REAL NOT NULL,hops INTEGER NOT NULL,
 learned_at TEXT NOT NULL,valid_until TEXT,fact_world_time TEXT NOT NULL,
 UNIQUE(edge_id,source_event_id,origin_event_id,method)
);
CREATE INDEX IF NOT EXISTS idx_epistemic_observer ON observer_knowledge_edges(world_id,observer_id,subject_id);
"""


# 原定义于 world_engine/event_history.py
EVENT_HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_profiles (
 event_id TEXT PRIMARY KEY REFERENCES world_events(id) ON DELETE CASCADE,
 world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 scope_type TEXT NOT NULL CHECK(scope_type IN ('global','regional','local','interpersonal')),
 scope_id TEXT, impact_level INTEGER NOT NULL CHECK(impact_level BETWEEN 1 AND 5),
 status TEXT NOT NULL CHECK(status IN ('confirmed','ongoing','resolved','archived')),
 classification_basis TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_causes (
 event_id TEXT NOT NULL REFERENCES world_events(id) ON DELETE CASCADE,
 cause_event_id TEXT NOT NULL REFERENCES world_events(id) ON DELETE CASCADE,
 relation TEXT NOT NULL, PRIMARY KEY(event_id,cause_event_id), CHECK(event_id!=cause_event_id)
);
CREATE TABLE IF NOT EXISTS event_affected_entities (
 event_id TEXT NOT NULL REFERENCES world_events(id) ON DELETE CASCADE,
 entity_id TEXT NOT NULL, entity_type TEXT NOT NULL,
 PRIMARY KEY(event_id,entity_id,entity_type)
);
CREATE INDEX IF NOT EXISTS idx_event_profile_scope ON event_profiles(world_id,scope_type,scope_id);
"""


# 原定义于 world_engine/food.py
FOOD_SCHEMA = """
CREATE TABLE IF NOT EXISTS food_storage_rules (
 item_type_id TEXT PRIMARY KEY REFERENCES item_types(id),
 world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 shelf_life_hours INTEGER NOT NULL CHECK(shelf_life_hours BETWEEN 1 AND 8760)
);
CREATE TABLE IF NOT EXISTS character_food_discomfort (
 character_id TEXT PRIMARY KEY REFERENCES characters(id) ON DELETE CASCADE,
 world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 source_event_id TEXT NOT NULL REFERENCES world_events(id) ON DELETE CASCADE,
 expires_world_time TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS food_eat_requests (
 world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,request_id TEXT NOT NULL,
 payload_json TEXT NOT NULL,response_json TEXT NOT NULL,PRIMARY KEY(world_id,request_id)
);
"""


# 原定义于 world_engine/interiors.py
INTERIOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS life_rooms (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    location_id TEXT NOT NULL REFERENCES locations(id) ON DELETE RESTRICT,
    parent_room_id TEXT REFERENCES life_rooms(id) ON DELETE RESTRICT,
    registration_id TEXT NOT NULL REFERENCES element_registration_requests(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    owner_character_id TEXT REFERENCES characters(id) ON DELETE RESTRICT,
    access_policy TEXT NOT NULL CHECK(access_policy IN ('public','private')),
    door_open INTEGER NOT NULL DEFAULT 0 CHECK(door_open IN (0,1)),
    door_locked INTEGER NOT NULL DEFAULT 0 CHECK(door_locked IN (0,1)),
    CHECK(NOT (door_open=1 AND door_locked=1))
);
CREATE INDEX IF NOT EXISTS idx_life_rooms_location
    ON life_rooms(world_id,location_id,parent_room_id);
CREATE TABLE IF NOT EXISTS life_fixtures (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    room_id TEXT NOT NULL REFERENCES life_rooms(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('container','seat')),
    capacity INTEGER NOT NULL DEFAULT 8 CHECK(capacity BETWEEN 1 AND 64),
    is_open INTEGER NOT NULL DEFAULT 0 CHECK(is_open IN (0,1)),
    is_locked INTEGER NOT NULL DEFAULT 0 CHECK(is_locked IN (0,1)),
    CHECK(NOT (is_open=1 AND is_locked=1))
);
CREATE TABLE IF NOT EXISTS life_room_access (
    room_id TEXT NOT NULL REFERENCES life_rooms(id) ON DELETE CASCADE,
    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    PRIMARY KEY(room_id,character_id)
);
"""


# 原定义于 world_engine/life.py
LIFE_SCHEMA = """
CREATE TABLE IF NOT EXISTS character_life_activities (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('rest','wait','work','craft','repair','map_review')),
    status TEXT NOT NULL CHECK(status IN ('running','completed','cancelled','interrupted')),
    location_id TEXT REFERENCES locations(id) ON DELETE SET NULL,
    longitude REAL NOT NULL,
    latitude REAL NOT NULL,
    initial_health INTEGER NOT NULL,
    started_world_time TEXT NOT NULL,
    ends_world_time TEXT NOT NULL,
    last_processed_world_time TEXT NOT NULL,
    finished_world_time TEXT,
    energy_steps INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    source_event_id TEXT REFERENCES world_events(id) ON DELETE SET NULL,
    finish_event_id TEXT REFERENCES world_events(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_life_one_running
    ON character_life_activities(character_id) WHERE status='running';
CREATE INDEX IF NOT EXISTS idx_life_world_status
    ON character_life_activities(world_id,status);
CREATE INDEX IF NOT EXISTS idx_state_updates_heartbeat
    ON character_state_updates(heartbeat_id);
"""


# 原定义于 world_engine/living_api.py
LIVING_SCHEMA = """
CREATE TABLE IF NOT EXISTS player_life_goals (
 id TEXT PRIMARY KEY,world_id TEXT NOT NULL REFERENCES worlds(id),player_id TEXT NOT NULL REFERENCES characters(id),
 kind TEXT NOT NULL,title TEXT NOT NULL,target_id TEXT,quantity INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'active',created_world_time TEXT NOT NULL,completed_world_time TEXT
);
CREATE TABLE IF NOT EXISTS player_trade_requests (
 world_id TEXT NOT NULL,request_id TEXT NOT NULL,payload_json TEXT NOT NULL,response_json TEXT NOT NULL,
 PRIMARY KEY(world_id,request_id)
);
"""


# 原定义于 world_engine/npc_goals.py
GOAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS npc_life_goals (
 id TEXT PRIMARY KEY,world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
 plan_id TEXT NOT NULL REFERENCES npc_routine_plans(id) ON DELETE CASCADE,
 revision INTEGER NOT NULL,goal_key TEXT NOT NULL,spec_json TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'waiting',progress INTEGER NOT NULL DEFAULT 0,
 reason TEXT NOT NULL DEFAULT '',next_step_json TEXT NOT NULL DEFAULT '{}',
 retry_world_time TEXT,failures INTEGER NOT NULL DEFAULT 0,spent INTEGER NOT NULL DEFAULT 0,
 source_event_id TEXT REFERENCES world_events(id) ON DELETE SET NULL,
 UNIQUE(plan_id,revision,goal_key)
);
CREATE TABLE IF NOT EXISTS npc_goal_steps (
 id TEXT PRIMARY KEY,goal_id TEXT NOT NULL REFERENCES npc_life_goals(id) ON DELETE CASCADE,
 world_time TEXT NOT NULL,kind TEXT NOT NULL,
 activity_id TEXT REFERENCES character_life_activities(id) ON DELETE SET NULL,
 source_event_id TEXT REFERENCES world_events(id) ON DELETE SET NULL,
 summary TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_npc_life_goals_actor ON npc_life_goals(character_id,status);
"""


# 原定义于 world_engine/routines.py
ROUTINE_SCHEMA = """
CREATE TABLE IF NOT EXISTS npc_routine_plans (
 id TEXT PRIMARY KEY,world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 character_id TEXT NOT NULL UNIQUE REFERENCES characters(id) ON DELETE CASCADE,
 registration_id TEXT NOT NULL REFERENCES element_registration_requests(id),
 revision INTEGER NOT NULL,spec_json TEXT NOT NULL,effective_world_time TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS npc_routine_occurrences (
 id TEXT PRIMARY KEY,plan_id TEXT NOT NULL REFERENCES npc_routine_plans(id),
 world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 character_id TEXT NOT NULL REFERENCES characters(id),revision INTEGER NOT NULL,
 slot_key TEXT NOT NULL,starts_world_time TEXT NOT NULL,ends_world_time TEXT NOT NULL,
 slot_json TEXT NOT NULL,required_minutes INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'planned',reason TEXT NOT NULL DEFAULT '',
 next_attempt_world_time TEXT,source_event_id TEXT REFERENCES world_events(id) ON DELETE SET NULL,
 UNIQUE(plan_id,revision,slot_key,starts_world_time)
);
CREATE TABLE IF NOT EXISTS npc_routine_activities (
 occurrence_id TEXT NOT NULL REFERENCES npc_routine_occurrences(id) ON DELETE CASCADE,
 activity_id TEXT NOT NULL UNIQUE REFERENCES character_life_activities(id),
 PRIMARY KEY(occurrence_id,activity_id)
);
CREATE TABLE IF NOT EXISTS npc_work_preferences (
 character_id TEXT PRIMARY KEY REFERENCES characters(id),plan_id TEXT NOT NULL REFERENCES npc_routine_plans(id),
 scope_key TEXT NOT NULL,location_id TEXT NOT NULL REFERENCES locations(id),
 selected_world_day TEXT NOT NULL,reason TEXT NOT NULL,
 source_event_id TEXT REFERENCES world_events(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS npc_routine_cursors (
 plan_id TEXT PRIMARY KEY REFERENCES npc_routine_plans(id),
 revision INTEGER NOT NULL,last_world_time TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS npc_routine_supplies (
 occurrence_id TEXT NOT NULL REFERENCES npc_routine_occurrences(id) ON DELETE CASCADE,
 world_time TEXT NOT NULL,item_type_id TEXT NOT NULL REFERENCES item_types(id),
 method TEXT NOT NULL CHECK(method IN ('harvest','purchase')),spent INTEGER NOT NULL CHECK(spent>=0),
 source_event_id TEXT REFERENCES world_events(id),
 PRIMARY KEY(occurrence_id,world_time)
);
"""


# 原定义于 world_engine/schedules.py
SCHEDULE_SCHEMA = """
CREATE TABLE IF NOT EXISTS appointment_windows (
 contract_id TEXT PRIMARY KEY REFERENCES contract_fulfillments(request_id) ON DELETE CASCADE,
 world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 starts_world_time TEXT NOT NULL, ends_world_time TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS appointment_revisions (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 contract_id TEXT NOT NULL REFERENCES contract_fulfillments(request_id) ON DELETE CASCADE,
 amendment_id TEXT REFERENCES long_term_operation_requests(id) ON DELETE SET NULL,
 revision INTEGER NOT NULL, old_start TEXT, old_end TEXT NOT NULL,
 new_start TEXT NOT NULL,new_end TEXT NOT NULL, changed_world_time TEXT NOT NULL,
 UNIQUE(contract_id,revision)
);
CREATE TABLE IF NOT EXISTS npc_schedule_assessments (
 character_id TEXT PRIMARY KEY REFERENCES characters(id) ON DELETE CASCADE,
 world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 signature TEXT NOT NULL, contract_id TEXT REFERENCES contract_fulfillments(request_id) ON DELETE SET NULL,
 reason TEXT NOT NULL, checked_world_time TEXT NOT NULL,
 source_event_id TEXT REFERENCES world_events(id) ON DELETE SET NULL
);
"""


# 原定义于 world_engine/sequences.py
SEQUENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS player_action_sequences (
 id TEXT PRIMARY KEY,world_id TEXT NOT NULL REFERENCES worlds(id),player_id TEXT NOT NULL REFERENCES characters(id),
 request_key TEXT NOT NULL,payload_json TEXT NOT NULL,status TEXT NOT NULL,next_index INTEGER NOT NULL DEFAULT 0,
 results_json TEXT NOT NULL DEFAULT '[]',claim_token TEXT,claim_time TEXT,error TEXT,
 UNIQUE(world_id,request_key)
);
"""


# 原定义于 world_engine/society.py
SOCIETY_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_observers (
 event_id TEXT NOT NULL REFERENCES world_events(id) ON DELETE CASCADE,
 character_id TEXT NOT NULL REFERENCES characters(id),
 channel TEXT NOT NULL, PRIMARY KEY(event_id,character_id)
);
CREATE TABLE IF NOT EXISTS character_acquaintances (
 observer_id TEXT NOT NULL REFERENCES characters(id), subject_id TEXT NOT NULL REFERENCES characters(id),
 world_id TEXT NOT NULL REFERENCES worlds(id), known_name TEXT,
 encounters INTEGER NOT NULL DEFAULT 0, shared_experiences INTEGER NOT NULL DEFAULT 0,
 respect INTEGER NOT NULL DEFAULT 0, conflict INTEGER NOT NULL DEFAULT 0,
 first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, last_social_gain TEXT,
 source_event_id TEXT REFERENCES world_events(id) ON DELETE SET NULL, PRIMARY KEY(observer_id,subject_id)
);
CREATE TABLE IF NOT EXISTS character_emotions (
 character_id TEXT PRIMARY KEY REFERENCES characters(id), world_id TEXT NOT NULL REFERENCES worlds(id),
 emotion TEXT NOT NULL, intensity INTEGER NOT NULL, stress INTEGER NOT NULL DEFAULT 0,
 expires_world_time TEXT NOT NULL, source_event_id TEXT REFERENCES world_events(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS character_event_knowledge (
 character_id TEXT NOT NULL REFERENCES characters(id), event_id TEXT NOT NULL REFERENCES world_events(id) ON DELETE CASCADE,
 world_id TEXT NOT NULL REFERENCES worlds(id), source_kind TEXT NOT NULL,
 source_character_id TEXT REFERENCES characters(id), confidence REAL NOT NULL,
 hops INTEGER NOT NULL DEFAULT 0, learned_world_time TEXT NOT NULL,
 PRIMARY KEY(character_id,event_id)
);
CREATE TABLE IF NOT EXISTS player_notifications (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id), recipient_id TEXT NOT NULL REFERENCES characters(id),
 event_id TEXT REFERENCES world_events(id) ON DELETE CASCADE, title TEXT NOT NULL, read_at TEXT, created_at TEXT NOT NULL,
 UNIQUE(recipient_id,event_id)
);
CREATE TABLE IF NOT EXISTS npc_daily_states (
 character_id TEXT PRIMARY KEY REFERENCES characters(id), world_id TEXT NOT NULL REFERENCES worlds(id),
 last_slot TEXT, intention TEXT NOT NULL DEFAULT '', next_world_time TEXT,
 last_outreach_day TEXT, last_error TEXT
);
CREATE TABLE IF NOT EXISTS npc_outreach_jobs (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id), npc_id TEXT NOT NULL REFERENCES characters(id),
 player_id TEXT NOT NULL REFERENCES characters(id), contact_id TEXT REFERENCES character_contacts(id),
 channel TEXT NOT NULL, reason TEXT NOT NULL, source_event_id TEXT REFERENCES world_events(id) ON DELETE SET NULL,
 status TEXT NOT NULL DEFAULT 'pending', claim_time TEXT, attempts INTEGER NOT NULL DEFAULT 0,
 world_day TEXT NOT NULL, result_event_id TEXT REFERENCES world_events(id) ON DELETE SET NULL, error TEXT,
 UNIQUE(world_id,npc_id,player_id,world_day)
);
CREATE INDEX IF NOT EXISTS idx_knowledge_observer ON character_event_knowledge(world_id,character_id,learned_world_time);
CREATE INDEX IF NOT EXISTS idx_outreach_jobs_world_status ON npc_outreach_jobs(world_id,status,world_day);
CREATE INDEX IF NOT EXISTS idx_player_notifications_inbox ON player_notifications(world_id,recipient_id,created_at DESC);
"""


# 原定义于 world_engine/visits.py
VISIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS life_door_policies (
 room_id TEXT PRIMARY KEY REFERENCES life_rooms(id) ON DELETE CASCADE,
 key_item_type_id TEXT REFERENCES item_types(id),
 visitor_policy TEXT NOT NULL CHECK(visitor_policy IN ('manual','authorized_only','trusted_contacts'))
);
CREATE TABLE IF NOT EXISTS life_visits (
 id TEXT PRIMARY KEY,world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 room_id TEXT NOT NULL REFERENCES life_rooms(id) ON DELETE CASCADE,
 visitor_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
 host_id TEXT REFERENCES characters(id) ON DELETE SET NULL,request_id TEXT NOT NULL,
 created_world_time TEXT NOT NULL,expires_world_time TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','invited','entered','declined','cancelled','expired')),
 source_event_id TEXT REFERENCES world_events(id) ON DELETE SET NULL,
 response_event_id TEXT REFERENCES world_events(id) ON DELETE SET NULL,
 UNIQUE(world_id,visitor_id,request_id)
);
CREATE INDEX IF NOT EXISTS idx_life_visits_pending ON life_visits(world_id,status,expires_world_time);
"""


