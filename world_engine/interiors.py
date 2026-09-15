"""房间、门与固定家具：持久化空间规则，不依赖模型叙述创建事实。"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from world_engine.geo import great_circle_distance_km
from world_engine.inventory import InventoryError, InventoryService
from world_engine.life import LifeActivityService
from world_engine.proximity import VISIBLE_PERSON_RADIUS_KM
from world_engine.repository import to_iso, utc_now

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


class FixtureSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["container", "seat"]
    capacity: int = Field(default=8, ge=1, le=64, strict=True)


class InteriorRoomSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    element_type: Literal["interior_room"] = "interior_room"
    name: str = Field(min_length=1, max_length=80)
    location_id: str
    parent_room_id: str | None = None
    owner_character_id: str | None = None
    access_policy: Literal["public", "private"] = "public"
    fixtures: list[FixtureSpec] = Field(default_factory=list, max_length=16)
    nightly_rate: int = Field(default=0, ge=0, le=100000, strict=True)
    key_item_type_id: str | None = None
    visitor_policy: Literal["manual", "authorized_only", "trusted_contacts"] = "authorized_only"


class InteriorError(ValueError):
    pass


class InteriorService:
    @staticmethod
    def room(connection, world_id: str, room_id: str):
        row = connection.execute(
            "SELECT r.* FROM life_rooms r JOIN locations l ON l.id=r.location_id "
            "WHERE r.id=? AND r.world_id=? AND l.is_active=1",
            (room_id, world_id),
        ).fetchone()
        if row is None:
            raise InteriorError("房间不存在或所在地点已不可用")
        return row

    @staticmethod
    def keyed(connection, room, character_id: str) -> bool:
        leased = connection.execute(
            'SELECT 1 FROM contract_fulfillments f JOIN worlds w ON w.id=f.world_id '
            "WHERE f.kind='lodging' AND f.status='active' AND f.asset_id=? AND f.requester_id=? "
            "AND f.due_world_time>w.current_time", (room["id"], character_id),
        ).fetchone()
        return bool(leased) or (
            room["owner_character_id"] == character_id
            or connection.execute(
                "SELECT 1 FROM life_room_access WHERE room_id=? AND character_id=?",
                (room["id"], character_id),
            ).fetchone()
            is not None
        )

    @classmethod
    def allowed(cls, connection, room, character_id: str) -> bool:
        from world_engine.repository import from_iso
        from world_engine.visits import VisitService

        at = from_iso(connection.execute("SELECT w.current_time FROM worlds w WHERE id=?", (room["world_id"],)).fetchone()[0])
        return bool(room["access_policy"] == "public" or cls.keyed(connection, room, character_id)
                    or VisitService.invitation(connection, room, character_id, at))

    @classmethod
    def can_unlock(cls, connection, room, character_id):
        policy = connection.execute(
            "SELECT key_item_type_id FROM life_door_policies WHERE room_id=?", (room["id"],)
        ).fetchone()
        if not policy or not policy[0]:
            return cls.keyed(connection, room, character_id)
        return bool(cls.allowed(connection, room, character_id) and connection.execute(
            "SELECT 1 FROM item_instances WHERE world_id=? AND item_type_id=? "
            "AND container_type='character_inventory' AND container_id=? AND owner_character_id=? "
            "AND condition>0 AND quantity>0", (room["world_id"], policy[0], character_id, character_id)
        ).fetchone())

    @staticmethod
    def near_location(connection, actor, location_id: str) -> bool:
        location = connection.execute(
            "SELECT * FROM locations WHERE id=? AND world_id=? AND is_active=1",
            (location_id, actor["world_id"]),
        ).fetchone()
        return bool(
            location
            and great_circle_distance_km(
                actor["longitude"],
                actor["latitude"],
                location["longitude"],
                location["latitude"],
            )
            <= VISIBLE_PERSON_RADIUS_KM
        )

    @classmethod
    def reachable_door(cls, connection, actor, room) -> bool:
        current = actor["current_room_id"]
        if current == room["id"] or (current and current == room["parent_room_id"]):
            return cls.near_location(connection, actor, room["location_id"])
        return (
            current is None
            and room["parent_room_id"] is None
            and cls.near_location(
                connection,
                actor,
                room["location_id"],
            )
        )

    @classmethod
    def register(cls, connection, world_id: str, registration_id: str, spec: InteriorRoomSpec):
        if spec.key_item_type_id:
            key = connection.execute(
                "SELECT t.category,t.stack_limit FROM item_types t JOIN world_item_profiles p ON p.item_type_id=t.id "
                "WHERE t.id=? AND p.world_id=?", (spec.key_item_type_id, world_id)
            ).fetchone()
            if not key or key["category"] != "tool" or key["stack_limit"] != 1:
                raise InteriorError("实体钥匙须引用本世界已登记、不可堆叠的工具类型")
            if not spec.owner_character_id:
                raise InteriorError("配置实体钥匙需要明确房间所有者")
        if (
            connection.execute(
                "SELECT 1 FROM locations WHERE id=? AND world_id=? AND is_active=1",
                (spec.location_id, world_id),
            ).fetchone()
            is None
        ):
            raise InteriorError("所属地点不存在于当前世界")
        if spec.access_policy == "private" and not spec.owner_character_id:
            raise InteriorError("私人房间需要明确所有者")
        if spec.nightly_rate and not spec.owner_character_id:
            raise InteriorError("出租房间需要明确所有者")
        if (
            spec.owner_character_id
            and connection.execute(
                "SELECT 1 FROM characters WHERE id=? AND world_id=?",
                (spec.owner_character_id, world_id),
            ).fetchone()
            is None
        ):
            raise InteriorError("所有者不属于当前世界")
        if not spec.owner_character_id and any(item.kind == "container" for item in spec.fixtures):
            raise InteriorError("登记储物容器时需要明确房间所有者")
        parent_id, depth = spec.parent_room_id, 0
        while parent_id:
            parent = cls.room(connection, world_id, parent_id)
            if parent["location_id"] != spec.location_id:
                raise InteriorError("内外房间必须属于同一地点")
            depth += 1
            if depth >= 4:
                raise InteriorError("室内空间最多嵌套四层")
            parent_id = parent["parent_room_id"]
        if connection.execute(
            "SELECT 1 FROM life_rooms WHERE world_id=? AND location_id=? "
            "AND parent_room_id IS ? AND name=?",
            (world_id, spec.location_id, spec.parent_room_id, spec.name),
        ).fetchone():
            raise InteriorError("同一入口下已经有这个名称的房间")
        if len({item.name for item in spec.fixtures}) != len(spec.fixtures):
            raise InteriorError("同一房间的家具名称不能重复")
        room_id = str(uuid4())
        connection.execute(
            "INSERT INTO life_rooms(id,world_id,location_id,parent_room_id,registration_id,"
            "name,owner_character_id,access_policy) VALUES (?,?,?,?,?,?,?,?)",
            (
                room_id,
                world_id,
                spec.location_id,
                spec.parent_room_id,
                registration_id,
                spec.name,
                spec.owner_character_id,
                spec.access_policy,
            ),
        )
        for fixture in spec.fixtures:
            connection.execute(
                "INSERT INTO "
                "life_fixtures(id,world_id,room_id,name,kind,capacity) VALUES "
                "(?,?,?,?,?,?)",
                (str(uuid4()), world_id, room_id, fixture.name, fixture.kind, fixture.capacity),
            )
        if spec.nightly_rate:
            connection.execute("INSERT INTO room_rental_rates VALUES (?,?)", (room_id,spec.nightly_rate))
        connection.execute("INSERT INTO life_door_policies VALUES (?,?,?)",
                           (room_id, spec.key_item_type_id, spec.visitor_policy))
        return room_id

    @staticmethod
    def record(connection, actor, at: datetime, kind: str, summary: str, payload: dict) -> str:
        from world_engine.actions import ActionService

        event = ActionService._record_event(
            connection,
            world_id=actor["world_id"],
            tick_id=str(uuid4()),
            occurred_at=at,
            event_type=f"action.interior_{kind}",
            actor_id=actor["id"],
            target_id=None,
            location_id=actor["current_location_id"] or actor["location_id"],
            summary=summary,
            payload=payload,
        )
        ActionService._record_memory(
            connection,
            world_id=actor["world_id"],
            character_id=actor["id"],
            event_id=event,
            memory_type="experienced",
            summary=summary,
            importance=2,
        )
        connection.execute(
            "UPDATE worlds SET version=version+1,updated_at=? WHERE id=?",
            (to_iso(utc_now()), actor["world_id"]),
        )
        return event

    @classmethod
    def door(cls, connection, actor, room_id: str, operation: str, at: datetime):
        LifeActivityService.assert_available(connection, actor["id"])
        room = cls.room(connection, actor["world_id"], room_id)
        if not cls.reachable_door(connection, actor, room):
            raise InteriorError("请先走到这扇门旁边")
        inside = actor["current_room_id"] == room_id
        if operation in {"enter", "exit"}:
            if actor["current_fixture_id"]:
                raise InteriorError("请先从座位起身")
            if connection.execute(
                "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",
                (actor["id"],),
            ).fetchone():
                raise InteriorError("请先结束当前行程")
            if operation == "enter" and inside or operation == "exit" and not inside:
                raise InteriorError("当前位置已经改变，请刷新后再试")
            if not room["door_open"] or room["door_locked"]:
                raise InteriorError("门尚未打开")
            if not inside and not cls.allowed(connection, room, actor["id"]):
                raise InteriorError("没有进入这间私人房间的许可")
            if not inside:
                from world_engine.visits import VisitService

                invitation = VisitService.invitation(connection, room, actor["id"], at)
                if invitation:
                    connection.execute("UPDATE life_visits SET status='entered' WHERE id=?", (invitation[0],))
            destination = room["parent_room_id"] if inside else room_id
            connection.execute(
                "UPDATE characters SET current_room_id=?,current_fixture_id=NULL,"
                "current_location_id=?,updated_at=? WHERE id=?",
                (destination, room["location_id"], to_iso(utc_now()), actor["id"]),
            )
            summary = f"你{'离开' if inside else '进入'}了{room['name']}。"
        else:
            if operation not in {"open", "close", "lock", "unlock"}:
                raise InteriorError("未知的门操作")
            if operation == "lock" and room["owner_character_id"] != actor["id"]:
                raise InteriorError("只有房间所有者可以上锁")
            # 住在里面的人始终可以开门离开，不因权限撤销被锁死在房间里。
            if (
                operation == "unlock"
                and not inside
                and not cls.can_unlock(connection, room, actor["id"])
            ):
                raise InteriorError("需要解锁权限；使用实体锁的房间还需本人随身携带可用钥匙")
            if operation == "lock" and not inside and not cls.can_unlock(connection, room, actor["id"]):
                raise InteriorError("在门外上锁需要本人随身携带可用钥匙")
            if (
                operation in {"open", "close"}
                and not inside
                and not cls.allowed(connection, room, actor["id"])
            ):
                raise InteriorError("没有操作这扇门的许可")
            opened, locked = room["door_open"], room["door_locked"]
            if operation == "open":
                if locked:
                    raise InteriorError("门已上锁，请先解锁")
                opened = 1
            elif operation == "close":
                opened = 0
            elif operation == "lock":
                if opened:
                    raise InteriorError("请先关门再上锁")
                locked = 1
            else:
                locked = 0
            if (opened, locked) == (room["door_open"], room["door_locked"]):
                return {"summary": "门已经处于这个状态。"}
            connection.execute(
                "UPDATE life_rooms SET door_open=?,door_locked=? WHERE id=?",
                (opened, locked, room_id),
            )
            verb = {"open": "打开", "close": "关上", "lock": "锁上", "unlock": "解锁"}[operation]
            summary = f"你{verb}了{room['name']}的门。"
        event = cls.record(connection, actor, at, operation, summary, {"room_id": room_id})
        return {"summary": summary, "event_id": event}

    @classmethod
    def fixture(cls, connection, actor, fixture_id: str):
        fixture = connection.execute(
            "SELECT * FROM life_fixtures WHERE id=? AND world_id=? AND room_id=?",
            (fixture_id, actor["world_id"], actor["current_room_id"]),
        ).fetchone()
        if fixture is None or not cls.near_location(
            connection,
            actor,
            cls.room(connection, actor["world_id"], fixture["room_id"])["location_id"],
        ):
            raise InteriorError("这件家具不在你所在的房间内")
        return fixture

    @classmethod
    def operate_fixture(cls, connection, actor, fixture_id: str, operation: str, at: datetime):
        LifeActivityService.assert_available(connection, actor["id"])
        fixture = cls.fixture(connection, actor, fixture_id)
        room = cls.room(connection, actor["world_id"], fixture["room_id"])
        if fixture["kind"] == "seat":
            if operation == "sit":
                if actor["current_fixture_id"]:
                    raise InteriorError("请先从当前座位起身")
                if connection.execute(
                    "SELECT 1 FROM characters WHERE current_fixture_id=?", (fixture_id,)
                ).fetchone():
                    raise InteriorError("座位已经有人使用")
                connection.execute(
                    "UPDATE characters SET current_fixture_id=? WHERE id=?",
                    (fixture_id, actor["id"]),
                )
                summary = f"你在{fixture['name']}坐下。"
            elif operation == "stand" and actor["current_fixture_id"] == fixture_id:
                connection.execute(
                    "UPDATE characters SET current_fixture_id=NULL WHERE id=?", (actor["id"],)
                )
                summary = f"你从{fixture['name']}起身。"
            else:
                raise InteriorError("当前不能执行这个座位操作")
        else:
            if not cls.keyed(connection, room, actor["id"]):
                raise InteriorError("你没有使用这个储物容器的许可")
            opened, locked = fixture["is_open"], fixture["is_locked"]
            if operation == "open":
                if locked:
                    raise InteriorError("容器已上锁，请先解锁")
                opened = 1
            elif operation == "close":
                opened = 0
            elif operation == "lock":
                if opened:
                    raise InteriorError("请先关闭容器再上锁")
                locked = 1
            elif operation == "unlock":
                locked = 0
            else:
                raise InteriorError("未知的容器操作")
            if (opened, locked) == (fixture["is_open"], fixture["is_locked"]):
                return {"summary": "容器已经处于这个状态。"}
            connection.execute(
                "UPDATE life_fixtures SET is_open=?,is_locked=? WHERE id=?",
                (opened, locked, fixture_id),
            )
            verb = {"open": "打开", "close": "关闭", "lock": "锁上", "unlock": "解锁"}[operation]
            summary = f"你{verb}了{fixture['name']}。"
        event = cls.record(connection, actor, at, operation, summary, {"fixture_id": fixture_id})
        return {"summary": summary, "event_id": event}

    @classmethod
    def transfer(
        cls,
        connection,
        actor,
        fixture_id: str,
        item_id: str,
        quantity: int,
        deposit: bool,
        at: datetime,
    ):
        LifeActivityService.assert_available(connection, actor["id"])
        fixture = cls.fixture(connection, actor, fixture_id)
        room = cls.room(connection, actor["world_id"], fixture["room_id"])
        if fixture["kind"] != "container" or not cls.keyed(connection, room, actor["id"]):
            raise InteriorError("没有使用该容器的许可")
        if not fixture["is_open"] or fixture["is_locked"]:
            raise InteriorError("请先打开容器")
        item = connection.execute(
            "SELECT i.*,t.name,t.stack_limit,t.slot_size FROM item_instances i "
            "JOIN item_types t ON t.id=i.item_type_id WHERE i.id=? AND i.world_id=? "
            "AND i.container_type=? AND i.container_id=? AND i.owner_character_id=?",
            (
                item_id,
                actor["world_id"],
                "character_inventory" if deposit else "fixture_storage",
                actor["id"] if deposit else fixture_id,
                actor["id"],
            ),
        ).fetchone()
        if item is None:
            raise InteriorError("物品不在指定位置或不属于你")
        if (
            isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or not 1 <= quantity <= item["quantity"]
        ):
            raise InteriorError("存取数量无效")
        before = InventoryService.snapshot(connection, actor["world_id"])
        if not deposit:
            InventoryService.transfer(connection, item, actor["id"], quantity)
        else:
            stored = connection.execute(
                "SELECT i.*,t.slot_size,t.stack_limit FROM item_instances i JOIN item_types t "
                "ON t.id=i.item_type_id WHERE i.container_type='fixture_storage' "
                "AND i.container_id=?",
                (fixture_id,),
            ).fetchall()
            stack = next(
                (
                    row
                    for row in stored
                    if row["item_type_id"] == item["item_type_id"]
                    and row["owner_character_id"] == actor["id"]
                    and row["condition"] == item["condition"]
                    and InventoryService.same_freshness(row, item)
                    and row["quantity"] + quantity <= max(1, item["stack_limit"])
                ),
                None,
            )
            used = sum(
                math.ceil(row["quantity"] / max(1, row["stack_limit"])) * max(1, row["slot_size"])
                for row in stored
            )
            required = (
                0
                if stack
                else math.ceil(quantity / max(1, item["stack_limit"])) * max(1, item["slot_size"])
            )
            if used + required > fixture["capacity"]:
                raise InventoryError("容器空间不足")
            if stack:
                connection.execute(
                    "UPDATE item_instances SET quantity=quantity+? WHERE id=?",
                    (quantity, stack["id"]),
                )
                InventoryService.consume(connection, item, quantity)
            elif quantity == item["quantity"]:
                connection.execute(
                    "UPDATE item_instances SET "
                    "container_type='fixture_storage',container_id=? WHERE id=?",
                    (fixture_id, item_id),
                )
            else:
                InventoryService.consume(connection, item, quantity)
                new_id = str(uuid4())
                connection.execute(
                    "INSERT INTO item_instances(id,world_id,item_type_id,"
                    "container_type,container_id,"
                    "quantity,condition,owner_character_id) VALUES "
                    "(?,?,?,'fixture_storage',?,?,?,?)",
                    (
                        new_id,
                        actor["world_id"],
                        item["item_type_id"],
                        fixture_id,
                        quantity,
                        item["condition"],
                        actor["id"],
                    ),
                )
                InventoryService.copy_freshness(connection, item, new_id)
        summary = f"你{'存入' if deposit else '取出'}了{quantity}份{item['name']}。"
        event = cls.record(
            connection,
            actor,
            at,
            "store" if deposit else "withdraw",
            summary,
            {"fixture_id": fixture_id, "item_id": item_id, "quantity": quantity},
        )
        InventoryService.audit(connection, actor["world_id"], event, before)
        return {"summary": summary, "event_id": event}

    @classmethod
    def scene(cls, connection, actor):
        from world_engine.food import FoodService
        from world_engine.repository import from_iso

        at = from_iso(connection.execute("SELECT w.current_time FROM worlds w WHERE id=?", (actor["world_id"],)).fetchone()[0])
        room_id = actor["current_room_id"]
        current = cls.room(connection, actor["world_id"], room_id) if room_id else None
        candidates = connection.execute(
            "SELECT * FROM life_rooms WHERE world_id=? AND (parent_room_id IS ? OR id=?)",
            (actor["world_id"], room_id, room_id),
        ).fetchall()
        doors = [
            {
                "id": row["id"],
                "name": row["name"],
                "inside": row["id"] == room_id,
                "open": bool(row["door_open"]),
                "locked": bool(row["door_locked"]),
                "allowed": cls.allowed(connection, row, actor["id"]),
                "owner": row["owner_character_id"] == actor["id"],
                "keyed": cls.can_unlock(connection, row, actor["id"]),
            }
            for row in candidates
            if cls.reachable_door(connection, actor, row)
        ]
        fixtures = []
        if current:
            for row in connection.execute(
                "SELECT * FROM life_fixtures WHERE room_id=? ORDER BY name,id", (room_id,)
            ):
                usable = cls.keyed(connection, current, actor["id"])
                contents = []
                if (
                    row["kind"] == "container"
                    and row["is_open"]
                    and not row["is_locked"]
                    and usable
                ):
                    contents = [
                        {
                            "id": item["id"],
                            "name": item["name"],
                            "quantity": item["quantity"],
                            "owned": item["owner_character_id"] == actor["id"],
                            "freshness": FoodService.view(item, at),
                        }
                        for item in connection.execute(
                            "SELECT i.*,t.name FROM item_instances i "
                            "JOIN item_types t ON t.id=i.item_type_id "
                            "WHERE i.container_type='fixture_storage' AND i.container_id=?",
                            (row["id"],),
                        )
                    ]
                occupied = connection.execute(
                    "SELECT id FROM characters WHERE current_fixture_id=?", (row["id"],)
                ).fetchone()
                fixtures.append(
                    {
                        "id": row["id"],
                        "name": row["name"],
                        "kind": row["kind"],
                        "capacity": row["capacity"],
                        "open": bool(row["is_open"]),
                        "locked": bool(row["is_locked"]),
                        "usable": usable,
                        "occupied": bool(occupied),
                        "seated_here": actor["current_fixture_id"] == row["id"],
                        "contents": contents,
                    }
                )
        from world_engine.repository import from_iso
        from world_engine.visits import VisitService

        at = from_iso(connection.execute("SELECT w.current_time FROM worlds w WHERE id=?", (actor["world_id"],)).fetchone()[0])
        return {
            "room": {"id": current["id"], "name": current["name"]} if current else None,
            "doors": doors,
            "fixtures": fixtures,
            "seated": bool(actor["current_fixture_id"]),
            "visits": VisitService.view(connection, actor, at),
        }
