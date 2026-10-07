"""Yuki 保存通用实验、人物自拟程序、通信和模型开销的事实证据。"""

RESEARCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_networks (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id), name TEXT NOT NULL,
 forcing REAL NOT NULL DEFAULT 0.05, hazard_limit REAL NOT NULL DEFAULT 0.15,
 service_min REAL NOT NULL DEFAULT 0.65, stable_hours REAL NOT NULL DEFAULT 24,
 status TEXT NOT NULL DEFAULT 'quiet', started_at TEXT, safe_since TEXT, resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS research_devices (
 id TEXT PRIMARY KEY, network_id TEXT NOT NULL REFERENCES research_networks(id),
 location_id TEXT NOT NULL REFERENCES locations(id), polity_key TEXT NOT NULL,
 name TEXT NOT NULL, controls_json TEXT NOT NULL, physical_json TEXT NOT NULL,
 reserve REAL NOT NULL DEFAULT 200 CHECK(reserve>=0), updated_at TEXT,
 UNIQUE(network_id,polity_key)
);
CREATE TABLE IF NOT EXISTS research_people (
 character_id TEXT PRIMARY KEY REFERENCES characters(id),
 device_id TEXT NOT NULL REFERENCES research_devices(id), capability INTEGER NOT NULL,
 next_available TEXT, enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1))
);
CREATE TABLE IF NOT EXISTS research_contacts (
 sender_id TEXT NOT NULL REFERENCES characters(id),
 recipient_id TEXT NOT NULL REFERENCES characters(id),
 PRIMARY KEY(sender_id,recipient_id)
);
CREATE TABLE IF NOT EXISTS research_programs (
 id TEXT PRIMARY KEY, character_id TEXT NOT NULL REFERENCES characters(id),
 author_id TEXT NOT NULL REFERENCES characters(id), spec_json TEXT NOT NULL,
 protocol_hash TEXT NOT NULL, pc INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL DEFAULT 'active', results_json TEXT NOT NULL DEFAULT '[]',
 created_at TEXT NOT NULL, source_message_id TEXT, call_id TEXT,
 activity_id TEXT REFERENCES character_life_activities(id), work_json TEXT NOT NULL DEFAULT '{}',
 updated_at TEXT,
 UNIQUE(character_id,protocol_hash)
);
CREATE TABLE IF NOT EXISTS research_steps (
 id TEXT PRIMARY KEY, program_id TEXT NOT NULL REFERENCES research_programs(id),
 operation TEXT NOT NULL, command_index INTEGER NOT NULL, arguments_json TEXT NOT NULL,
 result_json TEXT NOT NULL, event_id TEXT NOT NULL REFERENCES world_events(id),
 world_time TEXT NOT NULL, UNIQUE(program_id,command_index)
);
CREATE TABLE IF NOT EXISTS research_messages (
 id TEXT PRIMARY KEY, network_id TEXT NOT NULL REFERENCES research_networks(id),
 sender_id TEXT NOT NULL REFERENCES characters(id),
 recipient_id TEXT NOT NULL REFERENCES characters(id), body TEXT NOT NULL,
 program_json TEXT, protocol_hash TEXT, evidence_json TEXT NOT NULL,
 sent_at TEXT NOT NULL, due_at TEXT NOT NULL, destination_id TEXT NOT NULL REFERENCES locations(id),
 sent_event_id TEXT NOT NULL REFERENCES world_events(id),
 delivered_event_id TEXT REFERENCES world_events(id)
 ,failed_event_id TEXT REFERENCES world_events(id)
);
CREATE INDEX IF NOT EXISTS research_mail_due ON research_messages(due_at)
 WHERE delivered_event_id IS NULL;
CREATE TABLE IF NOT EXISTS research_calls (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id),
 character_id TEXT NOT NULL REFERENCES characters(id), fingerprint TEXT NOT NULL,
 status TEXT NOT NULL, reason TEXT NOT NULL, context_json TEXT NOT NULL,
 response_json TEXT, input_tokens INTEGER, output_tokens INTEGER,
 reserved_tokens INTEGER NOT NULL, error TEXT, created_at TEXT NOT NULL,
 diagnostics_json TEXT NOT NULL DEFAULT '{}',
 UNIQUE(character_id,fingerprint)
);
"""


def install_research_schema(connection):
    connection.executescript(RESEARCH_SCHEMA)
    for table, fields in {
        "research_programs": {
            "activity_id": "TEXT REFERENCES character_life_activities(id)",
            "work_json": "TEXT NOT NULL DEFAULT '{}'",
            "updated_at": "TEXT",
        },
        "research_messages": {"failed_event_id": "TEXT REFERENCES world_events(id)"},
        "research_calls": {"diagnostics_json": "TEXT NOT NULL DEFAULT '{}'"},
    }.items():
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        for name, declaration in fields.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
