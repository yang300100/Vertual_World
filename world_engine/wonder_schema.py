"""哥哥的奇观存档：默认地面，旧世界不会被挪到空中。"""


def initialize(connection):
    for table in ("locations", "characters"):
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        for name, ddl in (
            ("space_layer", "TEXT NOT NULL DEFAULT 'ground'"),
            ("altitude_m", "REAL NOT NULL DEFAULT 0"),
        ):
            if name not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS wonder_intents (
            id TEXT PRIMARY KEY REFERENCES world_events(id),
            world_id TEXT NOT NULL REFERENCES worlds(id),
            spec_json TEXT NOT NULL, representatives_json TEXT NOT NULL,
            registration_id TEXT REFERENCES element_registration_requests(id)
        );
        CREATE TABLE IF NOT EXISTS wonder_decisions (
            id TEXT PRIMARY KEY REFERENCES world_events(id),
            world_id TEXT NOT NULL REFERENCES worlds(id),
            intent_id TEXT NOT NULL REFERENCES wonder_intents(id),
            character_id TEXT NOT NULL REFERENCES characters(id),
            polity TEXT NOT NULL, decision TEXT NOT NULL CHECK(decision IN ('join','decline')),
            reason TEXT NOT NULL, UNIQUE(intent_id,character_id)
        );
        CREATE TABLE IF NOT EXISTS world_wonders (
            id TEXT PRIMARY KEY REFERENCES world_structures(id),
            world_id TEXT NOT NULL REFERENCES worlds(id),
            registration_id TEXT NOT NULL UNIQUE REFERENCES element_registration_requests(id),
            location_id TEXT UNIQUE REFERENCES locations(id),
            ground_location_id TEXT NOT NULL REFERENCES locations(id),
            space_layer TEXT NOT NULL UNIQUE, altitude_m REAL NOT NULL,
            radius_km REAL NOT NULL, sponsors_json TEXT NOT NULL,
            completed_event_id TEXT REFERENCES world_events(id)
        );
    """)
