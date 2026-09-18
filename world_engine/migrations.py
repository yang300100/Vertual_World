"""世界数据库的迁移、回填与修复。

从 `Database` 类里搬出来的无状态过程：它们只接收一个连接，对既有存档做
结构补齐、历史数据修复与默认值回填。`Database.initialize()` 在建立表结构后
按固定顺序调用它们。

顺序敏感：迁移之间可能互相依赖（例如先补列、再回填数据），不要重排。

注意：这些函数会被反复执行，必须保持幂等。
"""

from __future__ import annotations

import json
import re
import sqlite3

from world_engine.config import PROJECT_ROOT
from world_engine.demographics import stable_npc_demographics
from world_engine.time_utils import is_time_only, next_adjudication_boundary, parse_datetime


def migrate_life_kinds(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE name='character_life_activities'"
    ).fetchone()
    if row is None or "map_review" in row["sql"]:
        return
    connection.commit()
    connection.execute("PRAGMA foreign_keys=OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        ddl = row["sql"].replace(
            "character_life_activities", "character_life_activities_next", 1,
        )
        ddl = re.sub(
            r"kind IN \([^)]*\)",
            "kind IN ('rest','wait','work','craft','repair','map_review')", ddl, count=1,
        )
        connection.execute(ddl)
        connection.execute(
            "INSERT INTO character_life_activities_next SELECT * FROM character_life_activities"
        )
        connection.execute("DROP TABLE character_life_activities")
        connection.execute(
            "ALTER TABLE character_life_activities_next RENAME TO character_life_activities"
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys=ON")


def migrate_completion_columns(connection: sqlite3.Connection) -> None:
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


def migrate_social_operation_columns(connection: sqlite3.Connection) -> None:
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


def remove_legacy_player_controlled_social_records(connection: sqlite3.Connection) -> None:
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


def migrate_registration_columns(connection: sqlite3.Connection) -> None:
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


def migrate_registration_element_types(connection: sqlite3.Connection) -> None:
    """为旧存档扩展登记类型约束，保留全部申请与外键引用。"""
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' "
        "AND name = 'element_registration_requests'"
    ).fetchone()
    if row is None or "npc_routine" in str(row["sql"] or ""):
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


def migrate_element_lifecycle_columns(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(locations)")
    }
    if "is_active" not in columns:
        connection.execute(
            "ALTER TABLE locations ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1"
        )


def migrate_character_species_columns(connection: sqlite3.Connection) -> None:
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


def ensure_npc_demographics(connection: sqlite3.Connection) -> None:
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


def ensure_default_species_profiles(connection: sqlite3.Connection) -> None:
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


def repair_time_only_timestamps(connection: sqlite3.Connection) -> None:
    """把损坏的"仅含时间"的 worlds.current_time 修复为完整 datetime。

    来源可能来自外部/手工写入；若不修复，编排入口与裁判的
    datetime.fromisoformat 会因时间串直接崩溃。
    """
    for row in connection.execute("SELECT w.id, w.current_time FROM worlds w").fetchall():
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


def ensure_runtime_columns(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(world_runtime)").fetchall()
    }
    if "last_worker_seen_at" not in columns:
        connection.execute("ALTER TABLE world_runtime ADD COLUMN last_worker_seen_at TEXT")


def migrate_identity_column(connection: sqlite3.Connection) -> None:
    """为旧库 characters 表补 identity 列；新库由 SCHEMA 直接创建。"""
    character_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(characters)").fetchall()
    }
    if "identity" not in character_columns:
        connection.execute("ALTER TABLE characters ADD COLUMN identity TEXT")


def migrate_pov_column(connection: sqlite3.Connection) -> None:
    """为旧库 characters 表补 is_pov 列(玩家主控标记)；新库由 SCHEMA 直接创建。"""
    character_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(characters)").fetchall()
    }
    if "is_pov" not in character_columns:
        connection.execute(
            "ALTER TABLE characters ADD COLUMN is_pov INTEGER NOT NULL DEFAULT 0"
        )


def migrate_health_column(connection: sqlite3.Connection) -> None:
    """为旧库 characters 表补 health 列(生命值)；新库由 SCHEMA 直接创建。"""
    character_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(characters)").fetchall()
    }
    if "health" not in character_columns:
        connection.execute(
            "ALTER TABLE characters ADD COLUMN health INTEGER NOT NULL DEFAULT 100"
        )


def migrate_skills_column(connection: sqlite3.Connection) -> None:
    """为旧库 characters 表补 skills_json 列；新库由 SCHEMA 直接创建。"""
    character_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(characters)").fetchall()
    }
    if "skills_json" not in character_columns:
        connection.execute(
            "ALTER TABLE characters ADD COLUMN skills_json TEXT NOT NULL DEFAULT '[]'"
        )


def migrate_spatial_columns(connection: sqlite3.Connection) -> None:
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


def migrate_movement_columns(connection: sqlite3.Connection) -> None:
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


def migrate_area_and_activation_columns(
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


def ensure_detail_world_maps(connection: sqlite3.Connection) -> None:
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


def migrate_event_importance(connection: sqlite3.Connection) -> None:
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


def ensure_default_world_maps(connection: sqlite3.Connection) -> None:
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


def ensure_noryia_city_detail_maps(connection: sqlite3.Connection) -> None:
    """为已落盘的 Noryia 城镇图登记可自动切换的详细地图。"""

    index_path = PROJECT_ROOT / "docs/worldbuilding/maps/map_new/cities/index.json"
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
        asset = PROJECT_ROOT / "docs/worldbuilding/maps" / asset_path
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


def ensure_noryia_display_names(connection: sqlite3.Connection) -> None:
    """将 Noryia 中文展示名同步到运行时，保留原始名在来源索引中。"""

    path = PROJECT_ROOT / "docs/worldbuilding/maps/map_new/data/translations.json"
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


def migrate_navigation_columns(connection: sqlite3.Connection) -> None:
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


def ensure_known_map_features(connection: sqlite3.Connection) -> None:
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


def migrate_player_column(connection: sqlite3.Connection) -> None:
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


def migrate_satiety_columns(connection: sqlite3.Connection) -> None:
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


def ensure_clock_and_accumulator_rows(connection: sqlite3.Connection) -> None:
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
