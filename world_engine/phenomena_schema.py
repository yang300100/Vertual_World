"""哥哥，世界规则版本、现象来源与人物观察分别留档；迁移不生成发现。"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS phenomenon_rules (
 id TEXT PRIMARY KEY,world_id TEXT NOT NULL REFERENCES worlds(id),rule_key TEXT NOT NULL,
 version INTEGER NOT NULL CHECK(version>0),scope_key TEXT NOT NULL,definition_json TEXT NOT NULL,
 fingerprint TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(world_id,rule_key,version,scope_key)
);
CREATE TABLE IF NOT EXISTS phenomenon_events (
 id TEXT PRIMARY KEY,world_id TEXT NOT NULL REFERENCES worlds(id),
 rule_id TEXT NOT NULL REFERENCES phenomenon_rules(id),source_kind TEXT NOT NULL,
 source_id TEXT NOT NULL,operation TEXT NOT NULL,occurred_at TEXT NOT NULL,
 audit_json TEXT NOT NULL,audit_hash TEXT NOT NULL,public_facts_json TEXT NOT NULL,
 UNIQUE(world_id,source_kind,source_id)
);
CREATE TABLE IF NOT EXISTS phenomenon_observations (
 id TEXT PRIMARY KEY,event_id TEXT NOT NULL REFERENCES phenomenon_events(id),
 observer_id TEXT NOT NULL REFERENCES characters(id),world_id TEXT NOT NULL REFERENCES worlds(id),
 facts_json TEXT NOT NULL,observed_at TEXT NOT NULL,UNIQUE(event_id,observer_id)
);
CREATE INDEX IF NOT EXISTS phenomenon_actor_time
 ON phenomenon_observations(observer_id,observed_at);
CREATE INDEX IF NOT EXISTS phenomenon_world_time
 ON phenomenon_events(world_id,occurred_at);
CREATE TRIGGER IF NOT EXISTS phenomenon_rule_fixed BEFORE UPDATE ON phenomenon_rules
 BEGIN SELECT RAISE(ABORT,'phenomenon rule version is immutable'); END;
CREATE TRIGGER IF NOT EXISTS phenomenon_event_fixed BEFORE UPDATE ON phenomenon_events
 BEGIN SELECT RAISE(ABORT,'phenomenon event is immutable'); END;
CREATE TRIGGER IF NOT EXISTS phenomenon_observation_fixed
 BEFORE UPDATE ON phenomenon_observations
 BEGIN SELECT RAISE(ABORT,'phenomenon observation is immutable'); END;
CREATE TRIGGER IF NOT EXISTS phenomenon_rule_retained BEFORE DELETE ON phenomenon_rules
 BEGIN SELECT RAISE(ABORT,'phenomenon rule is retained'); END;
CREATE TRIGGER IF NOT EXISTS phenomenon_event_retained BEFORE DELETE ON phenomenon_events
 BEGIN SELECT RAISE(ABORT,'phenomenon event is retained'); END;
CREATE TRIGGER IF NOT EXISTS phenomenon_observation_retained
 BEFORE DELETE ON phenomenon_observations
 BEGIN SELECT RAISE(ABORT,'phenomenon observation is retained'); END;
"""


def install_phenomena_schema(c):
    c.executescript(SCHEMA)
