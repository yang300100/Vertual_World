from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

from world_engine.domain import (
    CharacterState,
    LocationState,
    MapFeatureState,
    MovementState,
    RelationshipView,
    VehicleState,
    WorldMapState,
    WorldSnapshot,
    WorldState,
)
from world_engine.demographics import age_years_at
from world_engine.elements import WorldElementCatalog
from world_engine.time_utils import next_adjudication_boundary, parse_datetime


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def from_iso(value: str) -> datetime:
    return parse_datetime(value)


def _movement_route_or_none(row: sqlite3.Row) -> dict[str, object] | None:
    """route_json 可能是空折线(合法)，此时解析为空列表，应视为无路线。"""
    raw = row["route_json"] if "route_json" in row.keys() else None
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


class WorldNotFoundError(LookupError):
    pass


class WorldRepository:
    """所有世界事实的持久化入口。"""

    def create_world(
        self,
        connection: sqlite3.Connection,
        *,
        name: str,
        minutes_per_tick: int,
        time_scale: float = 1.0,
        adjudication_interval_minutes: int = 720,
        seed_demo: bool = True,
    ) -> str:
        world_id = str(uuid4())
        now = utc_now()
        world_time = datetime(2040, 4, 1, 8, 0, tzinfo=UTC)
        connection.execute(
            """
            INSERT INTO worlds(
                id, name, current_time, minutes_per_tick, status, version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'running', 0, ?, ?)
            """,
            (
                world_id,
                name.strip(),
                to_iso(world_time),
                minutes_per_tick,
                to_iso(now),
                to_iso(now),
            ),
        )
        connection.execute(
            """
            INSERT INTO world_runtime(world_id, tick_count, last_tick_status)
            VALUES (?, 0, 'never')
            """,
            (world_id,),
        )
        connection.execute(
            """
            INSERT INTO world_clock(
                world_id, time_scale, heartbeat_interval_seconds,
                last_heartbeat_real_time, clock_revision, offline_policy,
                last_adjudication_world_time, next_adjudication_world_time,
                adjudication_interval_minutes, updated_at
            ) VALUES (?, ?, 60, NULL, 0, 'pause', ?, ?, ?, ?)
            """,
            (
                world_id,
                time_scale,
                to_iso(world_time),
                to_iso(next_adjudication_boundary(world_time, adjudication_interval_minutes)),
                adjudication_interval_minutes,
                to_iso(now),
            ),
        )
        if seed_demo:
            self._seed_demo(connection, world_id, now)
        self._seed_world_maps(connection, world_id)
        connection.execute(
            """
            INSERT OR IGNORE INTO character_state_accumulators(
                character_id, world_id, satiety_residual, energy_residual, updated_at
            )
            SELECT id, world_id, 0, 0, updated_at
            FROM characters WHERE world_id = ?
            """,
            (world_id,),
        )
        WorldElementCatalog().synchronize_world(connection, world_id=world_id)
        return world_id

    def _seed_demo(
        self, connection: sqlite3.Connection, world_id: str, created_at: datetime
    ) -> None:
        square_id = str(uuid4())
        home_id = str(uuid4())
        workshop_id = str(uuid4())
        locations = [
            (square_id, "晨星广场", "public", {"food": 20}, -72.4, 34.8, 2.0, 30),
            (home_id, "河畔住宅", "home", {"food": 8}, -72.1, 34.6, 1.5, 40),
            (
                workshop_id,
                "旧钟表工坊",
                "workplace",
                {"materials": 15},
                -71.8,
                34.9,
                2.0,
                35,
            ),
        ]
        connection.executemany(
            """
            INSERT INTO locations(
                id, world_id, name, kind, resources_json, longitude, latitude,
                area_radius_km, area_priority
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    location_id,
                    world_id,
                    name,
                    kind,
                    json.dumps(resources, ensure_ascii=False),
                    longitude,
                    latitude,
                    radius,
                    priority,
                )
                for (
                    location_id,
                    name,
                    kind,
                    resources,
                    longitude,
                    latitude,
                    radius,
                    priority,
                ) in locations
            ],
        )

        characters = [
            (
                str(uuid4()),
                "林澈",
                square_id,
                65,
                22,
                12,
                ["谨慎", "善良"],
                ["照顾家人", "在城镇站稳脚跟"],
                1,
            ),
            (
                str(uuid4()),
                "白露",
                home_id,
                24,
                58,
                18,
                ["敏感", "好奇"],
                ["寻找失踪的旧友"],
                1,
            ),
            (
                str(uuid4()),
                "瑞恩",
                workshop_id,
                76,
                65,
                5,
                ["勤奋", "固执"],
                ["修复工坊的大钟"],
                0,
            ),
        ]
        connection.executemany(
            """
            INSERT INTO characters(
                id, world_id, name, location_id, energy, satiety, money,
                traits_json, goals_json, is_core, longitude, latitude,
                created_at, updated_at
            )
            SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, l.longitude, l.latitude, ?, ?
            FROM locations l WHERE l.id = ?
            """,
            [
                (
                    character_id,
                    world_id,
                    name,
                    location_id,
                    energy,
                    satiety,
                    money,
                    json.dumps(traits, ensure_ascii=False),
                    json.dumps(goals, ensure_ascii=False),
                    is_core,
                    to_iso(created_at),
                    to_iso(created_at),
                    location_id,
                )
                for (
                    character_id,
                    name,
                    location_id,
                    energy,
                    satiety,
                    money,
                    traits,
                    goals,
                    is_core,
                ) in characters
            ],
        )
        connection.execute(
            """
            UPDATE characters
            SET current_location_id = location_id,
                activation_policy = CASE WHEN is_core = 1 THEN 'persistent' ELSE 'distance' END,
                activation_state = CASE WHEN is_core = 1 THEN 'active' ELSE 'background' END,
                activation_reason = CASE WHEN is_core = 1 THEN 'core' ELSE 'background' END
            WHERE world_id = ?
            """,
            (world_id,),
        )

    @staticmethod
    def _seed_world_maps(connection: sqlite3.Connection, world_id: str) -> None:
        """登记 Noryia 全球等距圆柱总览作为默认世界地图。

        Noryia.svg 为 Azgaar 生成的等距圆柱世界地图（10015×5008，
        lon∈[-180,180]，lat∈[-90,90]），前端据此渲染单张主图并支持缩放/平移。
        """
        master_id = f"{world_id}:map:z0"
        connection.execute(
            """
            INSERT OR IGNORE INTO world_maps(
                id, world_id, name, kind, asset_path,
                min_longitude, max_longitude, min_latitude, max_latitude,
                width_pixels, height_pixels, zoom_level, map_role
            ) VALUES (?, ?, 'Noryia 世界地图', 'terrain', 'map_new/Noryia.svg',
                      -180, 180, -90, 90, 10015, 5008, 0, 'world')
            """,
            (master_id, world_id),
        )

    def create_player_character(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        name: str,
        identity: str,
        location_id: str,
        traits: list[str],
        goal: str,
    ) -> CharacterState:
        """创建由用户控制的唯一玩家人物，并将其设为世界主视角。"""
        self._ensure_world(connection, world_id)
        location = connection.execute(
            """
            SELECT id, longitude, latitude FROM locations
            WHERE id = ? AND world_id = ? AND is_active = 1
            """,
            (location_id, world_id),
        ).fetchone()
        if location is None:
            raise LookupError(location_id)
        existing_player = connection.execute(
            "SELECT id FROM characters WHERE world_id = ? AND is_player = 1",
            (world_id,),
        ).fetchone()
        if existing_player is not None:
            raise ValueError("这个世界已经创建了玩家角色")

        character_id = str(uuid4())
        now = utc_now()
        clean_name = name.strip()
        clean_identity = identity.strip() or "旅人"
        clean_traits = [item.strip() for item in traits if item.strip()][:5]
        clean_goals = [goal.strip()] if goal.strip() else []
        connection.execute(
            "UPDATE characters SET is_pov = 0 WHERE world_id = ?",
            (world_id,),
        )
        connection.execute(
            """
            INSERT INTO characters(
                id, world_id, name, location_id, energy, satiety, money, health,
                traits_json, goals_json, identity, is_player, is_pov, is_core,
                longitude, latitude, current_location_id,
                activation_state, activation_policy, activation_reason,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 100, 100, 30, 100, ?, ?, ?, 1, 1, 0,
                      ?, ?, ?, 'active', 'persistent', 'player', ?, ?)
            """,
            (
                character_id,
                world_id,
                clean_name,
                location_id,
                json.dumps(clean_traits, ensure_ascii=False),
                json.dumps(clean_goals, ensure_ascii=False),
                clean_identity,
                location["longitude"],
                location["latitude"],
                location_id,
                to_iso(now),
                to_iso(now),
            ),
        )
        connection.execute(
            """
            INSERT INTO character_state_accumulators(
                character_id, world_id, satiety_residual, energy_residual, updated_at
            ) VALUES (?, ?, 0, 0, ?)
            """,
            (character_id, world_id, to_iso(now)),
        )
        # 初始技能与随身物品：玩家自带"剑术"，装备一把铁剑，背包两剂疗伤药。
        connection.execute(
            "UPDATE characters SET skills_json = ? WHERE id = ?",
            (json.dumps(["剑术"], ensure_ascii=False), character_id),
        )
        for type_id, name, category, stack, usable, attack, defense, heal in (
            ("iron_sword", "铁剑", "weapon", 1, 0, 8, 0, 0),
            ("healing_potion", "疗伤药", "consumable", 3, 1, 0, 0, 30),
            ("wooden_charm", "硬木护符", "charm", 1, 0, 0, 4, 0),
        ):
            connection.execute(
                """
                INSERT OR IGNORE INTO item_types(
                    id, name, category, stack_limit, usable, attack_bonus, defense_bonus, heal
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (type_id, name, category, stack, usable, attack, defense, heal),
            )
        connection.execute(
            """
            INSERT INTO item_instances(
                id, world_id, item_type_id, container_id, container_type, quantity, condition
            ) VALUES (?, ?, ?, ?, 'character_equipment', 1, 100)
            """,
            (str(uuid4()), world_id, "iron_sword", character_id),
        )
        for _ in range(2):
            connection.execute(
                """
                INSERT INTO item_instances(
                    id, world_id, item_type_id, container_id, container_type, quantity, condition
                ) VALUES (?, ?, ?, ?, 'character_inventory', 1, 100)
                """,
                (str(uuid4()), world_id, "healing_potion", character_id),
            )
        row = connection.execute(
            "SELECT * FROM characters WHERE id = ?",
            (character_id,),
        ).fetchone()
        WorldElementCatalog().upsert(
            connection,
            world_id=world_id,
            entity_type="character",
            entity_id=character_id,
            name=clean_name,
            source_kind="system",
        )
        return self._character_from_row(row)

    def list_worlds(self, connection: sqlite3.Connection) -> list[WorldState]:
        rows = connection.execute(
            """
            SELECT w.*, r.tick_count, r.last_worker_seen_at,
                   c.time_scale, c.clock_revision, c.offline_policy,
                   c.last_adjudication_world_time, c.next_adjudication_world_time,
                   c.adjudication_interval_minutes, c.heartbeat_interval_seconds,
                   c.last_heartbeat_real_time
            FROM worlds w
            JOIN world_runtime r ON r.world_id = w.id
            JOIN world_clock c ON c.world_id = w.id
            ORDER BY w.created_at DESC
            """
        ).fetchall()
        return [self._world_from_row(row) for row in rows]

    def get_snapshot(self, connection: sqlite3.Connection, world_id: str) -> WorldSnapshot:
        world_row = connection.execute(
            """
            SELECT w.*, r.tick_count, r.last_worker_seen_at,
                   c.time_scale, c.clock_revision, c.offline_policy,
                   c.last_adjudication_world_time, c.next_adjudication_world_time,
                   c.adjudication_interval_minutes, c.heartbeat_interval_seconds,
                   c.last_heartbeat_real_time
            FROM worlds w
            JOIN world_runtime r ON r.world_id = w.id
            JOIN world_clock c ON c.world_id = w.id
            WHERE w.id = ?
            """,
            (world_id,),
        ).fetchone()
        if world_row is None:
            raise WorldNotFoundError(world_id)

        location_rows = connection.execute(
            "SELECT * FROM locations WHERE world_id = ? AND is_active = 1 ORDER BY name",
            (world_id,),
        ).fetchall()
        character_rows = connection.execute(
            """
            SELECT c.*, (
                SELECT media_path FROM character_portraits p
                WHERE p.world_id = c.world_id AND p.character_id = c.id AND p.is_active = 1
                ORDER BY p.created_at DESC LIMIT 1
            ) AS portrait_media_path
            FROM characters c
            WHERE c.world_id = ? AND (c.health > 0 OR c.is_player = 1)
            ORDER BY c.is_core DESC, c.name
            """,
            (world_id,),
        ).fetchall()
        world = self._world_from_row(world_row)
        characters = [self._character_from_row(row, world.current_time) for row in character_rows]
        item_rows = connection.execute(
            """
            SELECT ii.container_id, ii.container_type, it.name, ii.quantity,
                   it.attack_bonus, it.defense_bonus, it.heal
            FROM item_instances ii JOIN item_types it ON it.id = ii.item_type_id
            WHERE ii.world_id = ?
            """,
            (world_id,),
        ).fetchall()
        items_by_char: dict[str, dict[str, list[dict[str, object]]]] = {}
        for item in item_rows:
            char_id = item["container_id"]
            bucket = items_by_char.setdefault(char_id, {"inventory": [], "equipment": []})
            record = {
                "name": item["name"],
                "quantity": item["quantity"],
                "attack_bonus": item["attack_bonus"],
                "defense_bonus": item["defense_bonus"],
                "heal": item["heal"],
            }
            key = "inventory" if item["container_type"] == "character_inventory" else "equipment"
            bucket[key].append(record)
        for character in characters:
            if character.id in items_by_char:
                character.inventory = items_by_char[character.id]["inventory"]
                character.equipment = items_by_char[character.id]["equipment"]
        relationship_rows = connection.execute(
            """
            SELECT source_character_id, target_character_id, affinity, trust
            FROM relationships WHERE world_id = ?
            """,
            (world_id,),
        ).fetchall()
        relationships = [
            RelationshipView(
                source_character_id=row["source_character_id"],
                target_character_id=row["target_character_id"],
                affinity=row["affinity"],
                trust=row["trust"],
            )
            for row in relationship_rows
        ]
        map_rows = connection.execute(
            """
            SELECT * FROM world_maps
            WHERE world_id = ?
            ORDER BY zoom_level, tile_row, tile_column, name
            """,
            (world_id,),
        ).fetchall()
        feature_rows = connection.execute(
            """
            SELECT * FROM map_features
            WHERE world_id = ? AND is_known = 1
            ORDER BY feature_type, name
            """,
            (world_id,),
        ).fetchall()
        vehicle_rows = connection.execute(
            """
            SELECT * FROM vehicles
            WHERE world_id = ?
            ORDER BY owner_character_id, name
            """,
            (world_id,),
        ).fetchall()
        movement_rows = connection.execute(
            """
            SELECT * FROM character_movements
            WHERE world_id = ? AND status = 'moving'
            ORDER BY started_at_world, id
            """,
            (world_id,),
        ).fetchall()
        return WorldSnapshot(
            world=world,
            locations=[self._location_from_row(row) for row in location_rows],
            maps=[self._map_from_row(row) for row in map_rows],
            map_features=[self._map_feature_from_row(row) for row in feature_rows],
            characters=characters,
            vehicles=[self._vehicle_from_row(row) for row in vehicle_rows],
            movements=[self._movement_from_row(row) for row in movement_rows],
            relationships=relationships,
        )

    def list_events(
        self,
        connection: sqlite3.Connection,
        world_id: str,
        limit: int = 100,
        scope: str = "all",
        participant_id: str | None = None,
    ) -> list[dict[str, object]]:
        self._ensure_world(connection, world_id)
        if scope not in {"all", "chronicle", "log"}:
            raise ValueError("事件范围必须是 all、chronicle 或 log")
        importance = {"chronicle": "major", "log": "routine"}.get(scope)
        clauses = ["world_id = ?"]
        parameters: list[object] = [world_id]
        if importance is not None:
            clauses.append("importance = ?")
            parameters.append(importance)
        if participant_id:
            clauses.append("(actor_id = ? OR target_id = ?)")
            parameters.extend((participant_id, participant_id))
        parameters.append(max(1, min(limit, 500)))
        rows = connection.execute(
            f"""
            SELECT * FROM world_events
            WHERE {' AND '.join(clauses)}
            ORDER BY occurred_at DESC, created_at DESC
            LIMIT ?
            """,  # noqa: S608 - 查询片段只来自上方固定白名单
            parameters,
        ).fetchall()
        return [self._event_dict(row) for row in rows]

    def list_all_events_ascending(
        self, connection: sqlite3.Connection, world_id: str
    ) -> list[dict[str, object]]:
        """按实际写入顺序读取完整客观历史，用于生成可重复的日志。"""

        self._ensure_world(connection, world_id)
        rows = connection.execute(
            """
            SELECT * FROM world_events
            WHERE world_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (world_id,),
        ).fetchall()
        return [self._event_dict(row) for row in rows]

    def list_heartbeats_ascending(
        self, connection: sqlite3.Connection, world_id: str
    ) -> list[dict[str, object]]:
        self._ensure_world(connection, world_id)
        rows = connection.execute(
            """
            SELECT * FROM world_heartbeats
            WHERE world_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (world_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_state_updates_ascending(
        self, connection: sqlite3.Connection, world_id: str
    ) -> list[dict[str, object]]:
        self._ensure_world(connection, world_id)
        rows = connection.execute(
            """
            SELECT * FROM character_state_updates
            WHERE world_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (world_id,),
        ).fetchall()
        items: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            item["changes"] = json.loads(item.pop("changes_json"))
            items.append(item)
        return items

    def list_adjudication_runs(
        self, connection: sqlite3.Connection, world_id: str, limit: int = 100
    ) -> list[dict[str, object]]:
        self._ensure_world(connection, world_id)
        rows = connection.execute(
            """
            SELECT * FROM adjudication_runs
            WHERE world_id = ?
            ORDER BY completed_at DESC, id DESC
            LIMIT ?
            """,
            (world_id, max(1, min(limit, 500))),
        ).fetchall()
        json_fields = {
            "selected_character_ids_json": "selected_character_ids",
            "proposals_json": "proposals",
            "rule_rejections_json": "rule_rejections",
            "final_event_ids_json": "final_event_ids",
        }
        items: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            for source, target in json_fields.items():
                item[target] = json.loads(item.pop(source))
            item["fallback_used"] = bool(item["fallback_used"])
            items.append(item)
        return items

    def list_agent_runs(
        self, connection: sqlite3.Connection, world_id: str, limit: int = 100
    ) -> list[dict[str, object]]:
        self._ensure_world(connection, world_id)
        rows = connection.execute(
            """
            SELECT * FROM agent_runs
            WHERE world_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (world_id, max(1, min(limit, 500))),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_agent_proposals(
        self, connection: sqlite3.Connection, world_id: str, limit: int = 200
    ) -> list[dict[str, object]]:
        self._ensure_world(connection, world_id)
        rows = connection.execute(
            """
            SELECT * FROM agent_proposals
            WHERE world_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (world_id, max(1, min(limit, 500))),
        ).fetchall()
        items: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            items.append(item)
        return items

    def list_memory_jobs(
        self, connection: sqlite3.Connection, world_id: str, limit: int = 100
    ) -> list[dict[str, object]]:
        self._ensure_world(connection, world_id)
        rows = connection.execute(
            """
            SELECT * FROM memory_jobs
            WHERE world_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (world_id, max(1, min(limit, 500))),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_combat_encounters(
        self, connection: sqlite3.Connection, world_id: str, limit: int = 100
    ) -> list[dict[str, object]]:
        self._ensure_world(connection, world_id)
        rows = connection.execute(
            """
            SELECT * FROM combat_encounters
            WHERE world_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (world_id, max(1, min(limit, 500))),
        ).fetchall()
        items: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            item["participants"] = json.loads(item.pop("participants_json"))
            items.append(item)
        return items

    def list_memories(
        self,
        connection: sqlite3.Connection,
        world_id: str,
        character_id: str,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        character = connection.execute(
            "SELECT id FROM characters WHERE id = ? AND world_id = ?",
            (character_id, world_id),
        ).fetchone()
        if character is None:
            raise LookupError(character_id)
        rows = connection.execute(
            """
            SELECT * FROM character_memories
            WHERE world_id = ? AND character_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (world_id, character_id, max(1, min(limit, 500))),
        ).fetchall()
        return [dict(row) for row in rows]

    def _ensure_world(self, connection: sqlite3.Connection, world_id: str) -> None:
        if connection.execute("SELECT 1 FROM worlds WHERE id = ?", (world_id,)).fetchone() is None:
            raise WorldNotFoundError(world_id)

    @staticmethod
    def _world_from_row(row: sqlite3.Row) -> WorldState:
        return WorldState(
            id=row["id"],
            name=row["name"],
            current_time=from_iso(row["current_time"]),
            minutes_per_tick=row["minutes_per_tick"],
            status=row["status"],
            version=row["version"],
            tick_count=row["tick_count"],
            time_scale=row["time_scale"],
            clock_revision=row["clock_revision"],
            offline_policy=row["offline_policy"],
            last_adjudication_time=from_iso(row["last_adjudication_world_time"]),
            next_adjudication_time=from_iso(row["next_adjudication_world_time"]),
            adjudication_interval_minutes=row["adjudication_interval_minutes"],
            heartbeat_interval_seconds=row["heartbeat_interval_seconds"],
            last_heartbeat_real_time=(
                from_iso(row["last_heartbeat_real_time"])
                if row["last_heartbeat_real_time"]
                else None
            ),
            last_worker_seen_at=(
                from_iso(row["last_worker_seen_at"]) if row["last_worker_seen_at"] else None
            ),
        )

    @staticmethod
    def _location_from_row(row: sqlite3.Row) -> LocationState:
        return LocationState(
            id=row["id"],
            world_id=row["world_id"],
            name=row["name"],
            kind=row["kind"],
            resources=json.loads(row["resources_json"]),
            longitude=row["longitude"],
            latitude=row["latitude"],
            area_radius_km=(row["area_radius_km"] if "area_radius_km" in row.keys() else 1.0),
            area_priority=(row["area_priority"] if "area_priority" in row.keys() else 0),
            parent_location_id=(
                row["parent_location_id"] if "parent_location_id" in row.keys() else None
            ),
        )

    @staticmethod
    def _map_from_row(row: sqlite3.Row) -> WorldMapState:
        return WorldMapState(**dict(row))

    @staticmethod
    def _map_feature_from_row(row: sqlite3.Row) -> MapFeatureState:
        return MapFeatureState(
            id=row["id"],
            world_id=row["world_id"],
            name=row["name"],
            feature_type=row["feature_type"],
            longitude=row["longitude"],
            latitude=row["latitude"],
            location_id=row["location_id"],
            is_known=bool(row["is_known"]),
            metadata=json.loads(row["metadata_json"]),
        )

    @staticmethod
    def _vehicle_from_row(row: sqlite3.Row) -> VehicleState:
        return VehicleState(
            id=row["id"],
            world_id=row["world_id"],
            name=row["name"],
            movement_type=row["movement_type"],
            speed_kmh=row["speed_kmh"],
            owner_character_id=row["owner_character_id"],
            is_available=bool(row["is_available"]),
            metadata=json.loads(row["metadata_json"]),
        )

    @staticmethod
    def _movement_from_row(row: sqlite3.Row) -> MovementState:
        return MovementState(
            id=row["id"],
            world_id=row["world_id"],
            character_id=row["character_id"],
            status=row["status"],
            movement_type=row["movement_type"],
            vehicle_id=row["vehicle_id"],
            speed_kmh=row["speed_kmh"],
            origin_longitude=row["origin_longitude"],
            origin_latitude=row["origin_latitude"],
            destination_longitude=row["destination_longitude"],
            destination_latitude=row["destination_latitude"],
            total_distance_km=row["total_distance_km"],
            distance_travelled_km=row["distance_travelled_km"],
            destination_location_id=row["destination_location_id"],
            route=_movement_route_or_none(row),
            route_index=(row["route_index"] if "route_index" in row.keys() else 0),
            route_distance_km=(
                row["route_distance_km"] if "route_distance_km" in row.keys() else 0
            ),
            navigation_dataset_id=(
                row["navigation_dataset_id"] if "navigation_dataset_id" in row.keys() else None
            ),
            replan_reason=(row["replan_reason"] if "replan_reason" in row.keys() else None),
            started_at_world=from_iso(row["started_at_world"]),
            updated_at_world=from_iso(row["updated_at_world"]),
            estimated_arrival_world=from_iso(row["estimated_arrival_world"]),
            encountered_character_ids=json.loads(row["encountered_character_ids_json"]),
        )

    @staticmethod
    def _character_from_row(row: sqlite3.Row, world_time: datetime | None = None) -> CharacterState:
        birth_world_time = (
            from_iso(row["birth_world_time"])
            if "birth_world_time" in row.keys() and row["birth_world_time"]
            else None
        )
        return CharacterState(
            id=row["id"],
            world_id=row["world_id"],
            name=row["name"],
            location_id=row["location_id"],
            longitude=row["longitude"],
            latitude=row["latitude"],
            movement_type=(row["movement_type"] if "movement_type" in row.keys() else "land"),
            movement_speed_kmh=(
                row["movement_speed_kmh"] if "movement_speed_kmh" in row.keys() else 5.0
            ),
            active_vehicle_id=(
                row["active_vehicle_id"] if "active_vehicle_id" in row.keys() else None
            ),
            current_location_id=(
                row["current_location_id"]
                if "current_location_id" in row.keys()
                else row["location_id"]
            ),
            activation_state=(
                row["activation_state"]
                if "activation_state" in row.keys()
                else ("active" if bool(row["is_core"]) else "background")
            ),
            activation_policy=(
                row["activation_policy"]
                if "activation_policy" in row.keys()
                else ("persistent" if bool(row["is_core"]) else "distance")
            ),
            activation_reason=(
                row["activation_reason"] if "activation_reason" in row.keys() else None
            ),
            activation_until_world_time=(
                from_iso(row["activation_until_world_time"])
                if "activation_until_world_time" in row.keys()
                and row["activation_until_world_time"]
                else None
            ),
            activation_radius_km=(
                row["activation_radius_km"] if "activation_radius_km" in row.keys() else 35.0
            ),
            activation_probability=(
                row["activation_probability"] if "activation_probability" in row.keys() else 0.85
            ),
            last_activation_check_world_time=(
                from_iso(row["last_activation_check_world_time"])
                if "last_activation_check_world_time" in row.keys()
                and row["last_activation_check_world_time"]
                else None
            ),
            energy=row["energy"],
            satiety=row["satiety"],
            money=row["money"],
            health=row["health"] if "health" in row.keys() else 100,
            skills=json.loads(row["skills_json"]) if "skills_json" in row.keys() else [],
            traits=json.loads(row["traits_json"]),
            goals=json.loads(row["goals_json"]),
            identity=row["identity"] if "identity" in row.keys() else None,
            gender=row["gender"] if "gender" in row.keys() else None,
            birth_world_time=birth_world_time,
            age_years=age_years_at(birth_world_time, world_time) if world_time else None,
            portrait_url=(
                f"/world-media/{row['portrait_media_path']}"
                if "portrait_media_path" in row.keys() and row["portrait_media_path"]
                else None
            ),
            is_player=(bool(row["is_player"]) if "is_player" in row.keys() else False),
            is_pov=bool(row["is_pov"]) if "is_pov" in row.keys() else False,
            is_core=bool(row["is_core"]),
        )

    @staticmethod
    def _event_dict(row: sqlite3.Row) -> dict[str, object]:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        return item
