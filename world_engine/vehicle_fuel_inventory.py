"""载具燃料装卸只移动本人物质批次，并留下原子回执。"""

from __future__ import annotations

import json
from uuid import uuid4

from world_engine.actions import ActionService
from world_engine.contracts import ContractService
from world_engine.geo import great_circle_distance_km
from world_engine.inventory import InventoryService
from world_engine.life import LifeActivityService
from world_engine.materials import MaterialService
from world_engine.player_steering import PlayerSteeringService
from world_engine.repository import WorldRepository, from_iso


FUEL_INVENTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS vehicle_fuel_inventory_receipts (
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    actor_id TEXT NOT NULL REFERENCES characters(id),
    idempotency_key TEXT NOT NULL,
    operation TEXT NOT NULL CHECK(operation IN ('load','unload')),
    vehicle_id TEXT NOT NULL,
    source_item_id TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity>0),
    moved_item_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    PRIMARY KEY(world_id,actor_id,idempotency_key)
);
"""


def install_fuel_inventory(connection):
    """哥哥，逐条建表，不替调用方提交已有事务。"""
    for statement in FUEL_INVENTORY_SCHEMA.split(";"):
        if statement.strip():
            connection.execute(statement)


class VehicleFuelInventoryService:
    @staticmethod
    def _event_not_after(first_time, first_order, second_time, second_order):
        # 哥哥，同世界时刻仍按事件真实写入顺序判断，不用相同时间伪造因果。
        return (from_iso(first_time), first_order) <= (from_iso(second_time), second_order)

    @staticmethod
    def _confirmed(c, registration, at, entity_type, entity_id):
        """哥哥，规格生效必须追溯实际登记事件，而非仅看申请状态。"""
        events = c.execute(
            "SELECT rowid AS event_order,* FROM world_events WHERE world_id=? AND tick_id=? AND event_type='world.element_registered'",
            (registration["world_id"], registration["id"]),
        ).fetchall()
        valid = []
        for event in events:
            try:
                payload = json.loads(event["payload_json"])
                if (isinstance(payload, dict) and all(k in payload for k in ("registration_id", "source_event_id", "entity_type", "entity_id"))
                        and payload.get("registration_id") == registration["id"]
                        and payload.get("source_event_id") == registration["source_event_id"]
                        and payload.get("entity_type") == entity_type and payload.get("entity_id") == entity_id
                        and VehicleFuelInventoryService._event_not_after(
                            registration["occurred_at"], registration["source_event_order"],
                            event["occurred_at"], event["event_order"])
                        and from_iso(event["occurred_at"]) <= at):
                    valid.append(event)
            except (ValueError, TypeError):
                continue
        if len(valid) != 1:
            raise ValueError("登记缺少唯一真实且已发生的确认事件")
        return valid[0]

    @staticmethod
    def _vehicle_registration(c, world_id, vehicle_id, state, at):
        """哥哥，物理载具必须保留其原地点登记与实际确认来源。"""
        registration = c.execute(
            "SELECT r.*,e.occurred_at,e.rowid AS source_event_order,e.location_id AS source_location_id FROM element_registration_requests r "
            "JOIN world_events e ON e.id=r.source_event_id AND e.world_id=r.world_id "
            "WHERE r.id=? AND r.world_id=? AND r.element_type='vehicle' AND r.status='applied' "
            "AND r.requested_by_character_id IS NULL AND r.result_entity_id=?",
            (state["registration_id"], world_id, vehicle_id),
        ).fetchone()
        if registration is None:
            raise ValueError("载具缺少实际物理登记来源")
        confirmed = VehicleFuelInventoryService._confirmed(c, registration, at, "vehicle", vehicle_id)
        try:
            payload = json.loads(registration["payload_json"])
            effects = c.execute(
                "SELECT payload_json FROM element_registration_effects WHERE registration_id=? AND world_id=? "
                "AND effect_type='vehicle.physical_registered' AND entity_type='vehicle' AND entity_id=?",
                (registration["id"], world_id, vehicle_id),
            ).fetchall()
            effect = json.loads(effects[0]["payload_json"]) if len(effects) == 1 else None
            valid = (isinstance(payload, dict) and payload.get("element_type") == "vehicle"
                     and payload.get("vehicle_id") in (None, vehicle_id)
                     and type(payload.get("capacity")) is int and payload["capacity"] == state["capacity"]
                     and payload.get("location_id") == registration["source_location_id"] == confirmed["location_id"]
                     and confirmed["location_id"] is not None
                     and isinstance(effect, dict) and type(effect.get("capacity")) is int
                     and effect["capacity"] == payload["capacity"] and effect.get("location_id") == confirmed["location_id"])
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise ValueError("载具物理登记的地点或规格来源不一致")
        return confirmed

    @staticmethod
    def _profile(c, world_id, vehicle_id, at):
        profile = c.execute(
            "SELECT * FROM vehicle_fuel_profiles WHERE world_id=? AND vehicle_id=?",
            (world_id, vehicle_id),
        ).fetchone()
        if profile is None:
            raise ValueError("载具没有经确认的燃料规格")
        registration = c.execute(
            "SELECT r.*,e.occurred_at,e.rowid AS source_event_order,e.location_id AS source_location_id,e.payload_json AS source_payload_json FROM element_registration_requests r "
            "JOIN world_events e ON e.id=r.source_event_id AND e.world_id=r.world_id "
            "WHERE r.id=? AND r.world_id=? AND r.element_type='vehicle_fuel_profile' "
            "AND r.status='applied' AND r.requested_by_character_id IS NULL",
            (profile["registration_id"], world_id),
        ).fetchone()
        if registration is None or from_iso(registration["occurred_at"]) > at:
            raise ValueError("燃料规格缺少本世界已确认的来源")
        confirmed = VehicleFuelInventoryService._confirmed(c, registration, at, None, None)
        # 哥哥，规格来源发生时，原车辆的真实登记就必须已经确认；当前地点不参与追溯。
        vehicle_state = c.execute(
            "SELECT * FROM vehicle_states WHERE vehicle_id=? AND world_id=?", (vehicle_id, world_id)
        ).fetchone()
        if vehicle_state is None:
            raise ValueError("燃料规格缺少原载具物理状态")
        original_vehicle = VehicleFuelInventoryService._vehicle_registration(
            c, world_id, vehicle_id, vehicle_state, from_iso(registration["occurred_at"])
        )
        if not VehicleFuelInventoryService._event_not_after(
            original_vehicle["occurred_at"], original_vehicle["event_order"],
            registration["occurred_at"], registration["source_event_order"],
        ):
            raise ValueError("载具确认晚于燃料规格来源")
        try:
            source = json.loads(registration["source_payload_json"])
            source_valid = (isinstance(source, dict) and
                            (source.get("vehicle_id") == vehicle_id or
                             (source.get("entity_type") == "vehicle" and source.get("entity_id") == vehicle_id))
                            and registration["source_location_id"] is not None
                            and registration["source_location_id"] == confirmed["location_id"])
        except (ValueError, TypeError):
            source_valid = False
        if not source_valid:
            raise ValueError("燃料规格的原载具与原地点来源不一致")
        try:
            payload = json.loads(registration["payload_json"])
            fields = ("vehicle_id", "item_type_id", "capacity_units", "rate_numerator", "rate_denominator")
            valid = (isinstance(payload, dict) and payload.get("element_type") == "vehicle_fuel_profile"
                     and all(payload.get(key) == profile[key] for key in fields)
                     and all(type(payload.get(key)) is int for key in ("capacity_units", "rate_numerator", "rate_denominator")))
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise ValueError("燃料规格与原登记内容不一致")
        if any(isinstance(profile[k], bool) or not isinstance(profile[k], int) or profile[k] < 1
               for k in ("capacity_units", "rate_numerator", "rate_denominator")):
            raise ValueError("燃料容量或消耗规格无效")
        effects = c.execute(
            "SELECT * FROM element_registration_effects WHERE registration_id=? AND world_id=? "
            "AND effect_type='vehicle.fuel_profile_registered' AND entity_type='vehicle' AND entity_id=?",
            (registration["id"], world_id, vehicle_id),
        ).fetchall()
        try:
            effect_payload = json.loads(effects[0]["payload_json"]) if len(effects) == 1 else None
            effect_valid = isinstance(effect_payload, dict) and all(
                effect_payload.get(key) == profile[key] for key in ("item_type_id", "capacity_units", "rate_numerator", "rate_denominator")
            ) and all(type(effect_payload.get(key)) is int for key in ("capacity_units", "rate_numerator", "rate_denominator"))
        except (ValueError, TypeError):
            effect_valid = False
        if not effect_valid:
            raise ValueError("燃料规格缺少与原登记一致的确认效果")
        item_profile = c.execute(
            "SELECT r.*,e.occurred_at,e.rowid AS source_event_order FROM world_item_profiles p "
            "JOIN element_registration_requests r ON r.id=p.registration_id AND r.world_id=p.world_id "
            "JOIN world_events e ON e.id=r.source_event_id AND e.world_id=r.world_id "
            "WHERE p.world_id=? AND p.item_type_id=? AND r.element_type='commodity' AND r.status='applied' "
            "AND r.result_entity_id=p.item_type_id AND r.requested_by_character_id IS NULL",
            (world_id, profile["item_type_id"]),
        ).fetchone()
        if item_profile is None or from_iso(item_profile["occurred_at"]) > at:
            raise ValueError("燃料类型缺少本世界已确认的登记来源")
        # 哥哥，物品登记在规格确认时已经成立，不能用之后的确认倒填过去。
        item_confirmed = VehicleFuelInventoryService._confirmed(c, item_profile, from_iso(confirmed["occurred_at"]), "item_type", profile["item_type_id"])
        if not VehicleFuelInventoryService._event_not_after(
            item_confirmed["occurred_at"], item_confirmed["event_order"],
            confirmed["occurred_at"], confirmed["event_order"],
        ):
            raise ValueError("燃料类型确认晚于规格确认")
        return profile

    @staticmethod
    def _position(c, world_id, actor_id, vehicle_id, at):
        actor = c.execute("SELECT * FROM characters WHERE id=? AND world_id=?", (actor_id, world_id)).fetchone()
        world = c.execute("SELECT w.current_time FROM worlds w WHERE w.id=?", (world_id,)).fetchone()
        if actor is None or world is None or from_iso(world[0]) != at:
            raise ValueError("人物或世界时间已变化，请重新确认")
        if actor["health"] <= 0 or actor["current_room_id"] or actor["current_fixture_id"]:
            raise ValueError("本人当前不能在载具旁装卸燃料")
        LifeActivityService.assert_available(c, actor_id)
        if PlayerSteeringService.active(c, actor_id) or c.execute(
            "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'", (actor_id,)
        ).fetchone():
            raise ValueError("本人仍在移动，不能装卸燃料")
        vehicle = ContractService.vehicle_for(c, world_id, actor_id, vehicle_id, at=at)
        if vehicle is None:
            raise ValueError("本人不是当前车主或有效租客")
        state = c.execute("SELECT * FROM vehicle_states WHERE vehicle_id=? AND world_id=?", (vehicle_id, world_id)).fetchone()
        if state is None or state["status"] not in {"parked", "blocked"} or c.execute(
            "SELECT 1 FROM character_movements WHERE world_id=? AND vehicle_id=? AND status='moving'", (world_id, vehicle_id)
        ).fetchone():
            raise ValueError("载具没有真实静止的已确认位置")
        VehicleFuelInventoryService._vehicle_registration(c, world_id, vehicle_id, state, at)
        from world_engine.rides import RideService
        aboard = RideService.aboard(c, actor_id)
        if aboard and aboard["vehicle_id"] != vehicle_id:
            raise ValueError("本人仍在另一辆载具内")
        if (actor["space_layer"] != state["space_layer"] or actor["current_location_id"] != state["current_location_id"]
                or actor["longitude"] is None or actor["latitude"] is None
                or great_circle_distance_km(actor["longitude"], actor["latitude"], state["longitude"], state["latitude"]) > 0.02):
            raise ValueError("本人和载具不在同层同一真实位置")
        return actor, state

    @staticmethod
    def _tracked_fuel(c, item):
        batch = c.execute("SELECT * FROM material_batches WHERE item_id=? AND world_id=?", (item["id"], item["world_id"])).fetchone()
        state = MaterialService.state(c, item["id"])
        if batch is None or batch["quantity_ref"] != item["quantity"] or state is None or state.carrier != "fuel" or state.mass <= 0:
            raise ValueError("物品缺少有效的真实燃料物质批次")

    @classmethod
    def options(cls, c, *, world_id, actor_id, at):
        """哥哥，只读展示本人的可操作车辆与本人所有的实际批次。"""
        values = []
        vehicles = c.execute(
            "SELECT v.id,v.name FROM vehicles v JOIN vehicle_fuel_profiles p ON p.vehicle_id=v.id "
            "WHERE v.world_id=? AND p.world_id=? AND v.is_available=1 ORDER BY v.name,v.id", (world_id, world_id)
        ).fetchall()
        for vehicle in vehicles:
            try:
                _, state = cls._position(c, world_id, actor_id, vehicle["id"], at)
                profile = cls._profile(c, world_id, vehicle["id"], at)
                stored = c.execute("SELECT * FROM item_instances WHERE world_id=? AND container_type='vehicle_fuel' AND container_id=?", (world_id, vehicle["id"])).fetchall()
                for item in stored:
                    if item["item_type_id"] != profile["item_type_id"] or item["quantity"] < 1 or not item["owner_character_id"]:
                        raise ValueError("载具燃料状态不一致")
                    cls._tracked_fuel(c, item)
                total = sum(item["quantity"] for item in stored)
                if total > profile["capacity_units"]:
                    raise ValueError("载具燃料超出容量")
                candidates = c.execute(
                    "SELECT i.*,t.name,t.stack_limit,t.slot_size FROM item_instances i JOIN item_types t ON t.id=i.item_type_id "
                    "WHERE i.world_id=? AND i.owner_character_id=? AND i.item_type_id=? AND i.quantity>0 AND "
                    "((i.container_type='character_inventory' AND i.container_id=?) OR "
                    "(i.container_type='vehicle_fuel' AND i.container_id=?)) ORDER BY i.id",
                    (world_id, actor_id, profile["item_type_id"], actor_id, vehicle["id"]),
                ).fetchall()
                loadable, unloadable = [], []
                for item in candidates:
                    try:
                        cls._tracked_fuel(c, item)
                        if item["container_type"] == "character_inventory":
                            maximum = min(item["quantity"], profile["capacity_units"] - total)
                            if maximum:
                                loadable.append({"id": item["id"], "name": item["name"], "max_quantity": maximum})
                        else:
                            low, high = 0, item["quantity"]
                            while low < high:
                                middle = (low + high + 1) // 2
                                try:
                                    InventoryService._capacity(c, actor_id, item, middle, allow_merge=False)
                                    low = middle
                                except ValueError:
                                    high = middle - 1
                            if low:
                                unloadable.append({"id": item["id"], "name": item["name"], "max_quantity": low})
                    except (ValueError, LookupError, TypeError):
                        continue
                values.append({"vehicle_id": vehicle["id"], "name": vehicle["name"],
                               "capacity_units": profile["capacity_units"], "loaded_units": total,
                               "location_id": state["current_location_id"], "loadable": loadable, "unloadable": unloadable})
            except (ValueError, LookupError, TypeError):
                continue
        return values

    @classmethod
    def _apply(cls, c, *, world_id, actor_id, vehicle_id, item_id, quantity, idempotency_key, at, operation):
        if (isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1
                or not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key.strip()) <= 128
                or idempotency_key != idempotency_key.strip()):
            raise ValueError("数量或本人请求的幂等键无效")
        c.execute("SAVEPOINT vehicle_fuel_inventory")
        try:
            prior = c.execute(
                "SELECT * FROM vehicle_fuel_inventory_receipts WHERE world_id=? AND actor_id=? AND idempotency_key=?",
                (world_id, actor_id, idempotency_key),
            ).fetchone()
            if prior:
                if (prior["operation"], prior["vehicle_id"], prior["source_item_id"], prior["quantity"]) != (operation, vehicle_id, item_id, quantity):
                    raise ValueError("幂等键已用于另一份燃料装卸请求")
                event = c.execute("SELECT summary FROM world_events WHERE id=? AND world_id=?", (prior["event_id"], world_id)).fetchone()
                if event is None:
                    raise ValueError("原装卸回执的事件已不存在，不能重放消费")
                c.execute("RELEASE vehicle_fuel_inventory")
                return {"status": "already", "event_id": prior["event_id"], "moved_item_id": prior["moved_item_id"], "summary": event["summary"]}
            actor, state = cls._position(c, world_id, actor_id, vehicle_id, at)
            profile = cls._profile(c, world_id, vehicle_id, at)
            item = c.execute(
                "SELECT i.*,t.name,t.stack_limit,t.slot_size FROM item_instances i JOIN item_types t ON t.id=i.item_type_id "
                "WHERE i.id=? AND i.world_id=?", (item_id, world_id),
            ).fetchone()
            if item is None or item["item_type_id"] != profile["item_type_id"] or item["owner_character_id"] != actor_id or item["quantity"] < quantity:
                raise ValueError("本人没有足量且符合规格的燃料")
            expected_container, expected_holder = (("character_inventory", actor_id) if operation == "load" else ("vehicle_fuel", vehicle_id))
            if item["container_type"] != expected_container or item["container_id"] != expected_holder:
                raise ValueError("燃料不在本人背包或指定载具的真实容器中")
            cls._tracked_fuel(c, item)
            stored = c.execute("SELECT * FROM item_instances WHERE world_id=? AND container_type='vehicle_fuel' AND container_id=?", (world_id, vehicle_id)).fetchall()
            for batch in stored:
                if batch["item_type_id"] != profile["item_type_id"] or batch["quantity"] < 1 or not batch["owner_character_id"]:
                    raise ValueError("载具现有燃料与确认规格或物权不一致")
                cls._tracked_fuel(c, batch)
            if sum(batch["quantity"] for batch in stored) + (quantity if operation == "load" else 0) > profile["capacity_units"]:
                raise ValueError("载具燃料容量不足或现有数量已异常")
            if operation == "unload":
                InventoryService._capacity(c, actor_id, item, quantity, allow_merge=False)
            before = InventoryService.snapshot(c, world_id)
            container_type, container_id = (("vehicle_fuel", vehicle_id) if operation == "load" else ("character_inventory", actor_id))
            moved_id = MaterialService.move(c, item, quantity, container_type, container_id, actor_id)
            summary = f"{actor['name']}将本人持有的{quantity}份{item['name']}{'装入载具' if operation == 'load' else '从载具取回'}。"
            event_id = ActionService._record_event(c, world_id=world_id, tick_id=f"fuel:{uuid4()}", occurred_at=at,
                event_type="action.vehicle_fuel_" + ("loaded" if operation == "load" else "unloaded"), actor_id=actor_id,
                target_id=None, location_id=state["current_location_id"], summary=summary,
                payload={"vehicle_id": vehicle_id, "profile_registration_id": profile["registration_id"],
                         "source_item_id": item_id, "moved_item_id": moved_id, "quantity": quantity})
            InventoryService.audit(c, world_id, event_id, before)
            ActionService._record_memory(c, world_id=world_id, character_id=actor_id, event_id=event_id,
                memory_type="experienced", summary=summary, importance=2)
            c.execute("INSERT INTO vehicle_fuel_inventory_receipts VALUES (?,?,?,?,?,?,?,?,?)",
                (world_id, actor_id, idempotency_key, operation, vehicle_id, item_id, quantity, moved_id, event_id))
            WorldRepository().bump_version(c, world_id)
            c.execute("RELEASE vehicle_fuel_inventory")
            return {"status": "applied", "event_id": event_id, "moved_item_id": moved_id, "summary": summary}
        except Exception:
            c.execute("ROLLBACK TO vehicle_fuel_inventory")
            c.execute("RELEASE vehicle_fuel_inventory")
            raise

    @classmethod
    def load(cls, c, *, world_id, actor_id, vehicle_id, item_id, quantity, idempotency_key, at):
        return cls._apply(c, world_id=world_id, actor_id=actor_id, vehicle_id=vehicle_id, item_id=item_id,
                          quantity=quantity, idempotency_key=idempotency_key, at=at, operation="load")

    @classmethod
    def unload(cls, c, *, world_id, actor_id, vehicle_id, item_id, quantity, idempotency_key, at):
        return cls._apply(c, world_id=world_id, actor_id=actor_id, vehicle_id=vehicle_id, item_id=item_id,
                          quantity=quantity, idempotency_key=idempotency_key, at=at, operation="unload")
