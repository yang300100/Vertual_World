"""Yuki 保存危机及各地设施的真实恢复证据。"""

INCIDENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS world_incidents (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 idempotency_key TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','resolved')),
 started_world_time TEXT NOT NULL, resolved_world_time TEXT,
 source_event_id TEXT NOT NULL REFERENCES world_events(id),
 resolution_event_id TEXT REFERENCES world_events(id), UNIQUE(world_id,idempotency_key)
);
CREATE TABLE IF NOT EXISTS incident_sites (
 id TEXT PRIMARY KEY, incident_id TEXT NOT NULL REFERENCES world_incidents(id) ON DELETE CASCADE,
 location_id TEXT NOT NULL REFERENCES locations(id),
 custodian_id TEXT NOT NULL REFERENCES characters(id), item_id TEXT NOT NULL REFERENCES item_instances(id),
 recipe_id TEXT NOT NULL REFERENCES activity_recipes(id), resource_keys_json TEXT NOT NULL,
 required_condition INTEGER NOT NULL CHECK(required_condition BETWEEN 1 AND 100),
 observation_event_id TEXT REFERENCES world_events(id), request_event_id TEXT REFERENCES world_events(id),
 resolution_event_id TEXT REFERENCES world_events(id), resolved_world_time TEXT,
 UNIQUE(incident_id,item_id)
);
CREATE INDEX IF NOT EXISTS idx_incident_sites_location ON incident_sites(location_id);
CREATE TABLE IF NOT EXISTS incident_disruptions (
 id TEXT PRIMARY KEY, site_id TEXT NOT NULL REFERENCES incident_sites(id) ON DELETE CASCADE,
 started_world_time TEXT NOT NULL, resolved_world_time TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_incident_disruptions_open
ON incident_disruptions(site_id) WHERE resolved_world_time IS NULL;
INSERT INTO incident_disruptions(id,site_id,started_world_time,resolved_world_time)
SELECT 'legacy:' || s.id,s.id,i.started_world_time,s.resolved_world_time
FROM incident_sites s JOIN world_incidents i ON i.id=s.incident_id
WHERE NOT EXISTS (SELECT 1 FROM incident_disruptions d WHERE d.site_id=s.id);
"""
