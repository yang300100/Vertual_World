"""Yuki 保存版本化同意、实物交付与有运输时间的全球合作证据。"""

COOPERATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS cooperative_crises (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
 started_world_time TEXT NOT NULL, source_event_id TEXT NOT NULL REFERENCES world_events(id),
 coordinator_id TEXT NOT NULL REFERENCES characters(id), hub_id TEXT NOT NULL REFERENCES locations(id),
 catalogue_json TEXT NOT NULL, recipes_json TEXT NOT NULL, cure_type_id TEXT NOT NULL REFERENCES item_types(id),
 proposal_event_id TEXT REFERENCES world_events(id), revision INTEGER NOT NULL DEFAULT 0,
 terms_json TEXT NOT NULL DEFAULT '{}', resolution_event_id TEXT REFERENCES world_events(id),
 resolved_world_time TEXT, cure_origin_item_id TEXT
);
CREATE TABLE IF NOT EXISTS cooperation_members (
 crisis_id TEXT NOT NULL REFERENCES cooperative_crises(id) ON DELETE CASCADE, key TEXT NOT NULL,
 name TEXT NOT NULL, source TEXT NOT NULL, capital_id TEXT NOT NULL REFERENCES locations(id),
 delegate_id TEXT NOT NULL REFERENCES characters(id), local_actor_id TEXT NOT NULL REFERENCES characters(id),
 sample_type_id TEXT NOT NULL REFERENCES item_types(id), policy_json TEXT NOT NULL,
 observation_event_id TEXT REFERENCES world_events(id), report_delivery_event_id TEXT REFERENCES world_events(id),
 briefing_event_id TEXT REFERENCES world_events(id), consent_event_id TEXT REFERENCES world_events(id),
 consent_revision INTEGER, contribution_event_id TEXT REFERENCES world_events(id),
 application_event_id TEXT REFERENCES world_events(id), applied_world_time TEXT,
 PRIMARY KEY(crisis_id,key), UNIQUE(crisis_id,delegate_id), UNIQUE(crisis_id,local_actor_id),
 UNIQUE(crisis_id,sample_type_id)
);
CREATE TABLE IF NOT EXISTS cooperation_packets (
 id TEXT PRIMARY KEY, crisis_id TEXT NOT NULL REFERENCES cooperative_crises(id) ON DELETE CASCADE,
 member_key TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('report','remedy')),
 sender_id TEXT NOT NULL REFERENCES characters(id), recipient_id TEXT NOT NULL REFERENCES characters(id),
 destination_id TEXT NOT NULL REFERENCES locations(id), distance_km REAL NOT NULL, speed_kmh REAL NOT NULL,
 dispatched_world_time TEXT NOT NULL, arrival_world_time TEXT NOT NULL,
 dispatch_event_id TEXT NOT NULL REFERENCES world_events(id), source_event_id TEXT NOT NULL REFERENCES world_events(id),
 item_id TEXT, delivery_event_id TEXT REFERENCES world_events(id), delivered_world_time TEXT,
 UNIQUE(crisis_id,member_key,kind)
);
CREATE TABLE IF NOT EXISTS cooperation_production_blocks (
 crisis_id TEXT NOT NULL REFERENCES cooperative_crises(id) ON DELETE CASCADE, member_key TEXT NOT NULL,
 location_id TEXT NOT NULL REFERENCES locations(id), resource_keys_json TEXT NOT NULL,
 started_world_time TEXT NOT NULL, resolved_world_time TEXT,
 recovery_event_id TEXT REFERENCES world_events(id), PRIMARY KEY(crisis_id,member_key)
);
CREATE INDEX IF NOT EXISTS idx_cooperation_packets_due ON cooperation_packets(arrival_world_time);
"""
