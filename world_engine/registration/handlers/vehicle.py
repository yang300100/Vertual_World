"""核实载具的物理基础，不把人物描述当成现成座位或位置。"""

from __future__ import annotations

from uuid import uuid4

from world_engine.registration.models import (
    Effect,
    ElementType,
    HandlerResult,
    RegistrationContext,
    RegistrationRejected,
    RegistrationStatus,
    VehicleSpec,
    _require_location,
)
from world_engine.repository import from_iso, to_iso


class VehicleHandler:
    element_type = ElementType.VEHICLE

    def apply(self, context: RegistrationContext, payload: VehicleSpec) -> HandlerResult:
        if not isinstance(payload, VehicleSpec):
            raise TypeError("载具处理器收到错误的数据类型")
        if context.requested_by_character_id is not None:
            raise RegistrationRejected("载具实物、座位与停放位置须由编年者核实，人物不能凭描述造车")
        location = _require_location(context.connection, context.world_id, payload.location_id)
        if location["longitude"] is None or location["latitude"] is None:
            raise RegistrationRejected("载具停放地点缺少已确认的实际坐标")
        if context.source_event["location_id"] != location["id"]:
            raise RegistrationRejected("载具来源事件必须发生在已确认的停放地点")
        stops = {
            "ship": {"port", "dock", "harbor"},
            "water": {"port", "dock", "harbor"},
            "flight": {"airfield", "landing_site"},
            "underground": {"underground_station"},
        }
        if payload.movement_type in stops and location["kind"] not in stops[payload.movement_type]:
            raise RegistrationRejected("该载具须在已经登记的适用停靠设施核实位置，不能凭空补建泊位")
        if from_iso(context.source_event["occurred_at"]) > context.world_time:
            raise RegistrationRejected("未来事件不能确认当前载具的位置")
        if not payload.name.strip():
            raise RegistrationRejected("载具名称不能为空白")
        if payload.owner_character_id is not None and context.connection.execute(
            "SELECT 1 FROM characters WHERE id=? AND world_id=?",
            (payload.owner_character_id, context.world_id),
        ).fetchone() is None:
            raise RegistrationRejected("载具所有者不属于当前世界")

        vehicle_id = payload.vehicle_id or str(uuid4())
        vehicle = None
        if payload.vehicle_id:
            vehicle = context.connection.execute(
                "SELECT * FROM vehicles WHERE id=? AND world_id=?",
                (vehicle_id, context.world_id),
            ).fetchone()
            if vehicle is None:
                raise RegistrationRejected("待补登记的旧载具不属于当前世界")
            if context.connection.execute(
                "SELECT 1 FROM vehicle_states WHERE vehicle_id=?", (vehicle_id,),
            ).fetchone():
                raise RegistrationRejected("已有真实状态的载具不能通过补登记扩座或改变位置")
            if (
                vehicle["name"] != payload.name.strip()
                or vehicle["movement_type"] != payload.movement_type
                or float(vehicle["speed_kmh"]) != payload.speed_kmh
                or vehicle["owner_character_id"] != payload.owner_character_id
            ):
                raise RegistrationRejected("补登记须保留旧载具的名称、类型、速度与所有权")
            if context.connection.execute(
                "SELECT 1 FROM character_movements WHERE vehicle_id=? AND status='moving'",
                (vehicle_id,),
            ).fetchone():
                raise RegistrationRejected("行驶中的旧载具须先实际停靠，再核实物理状态")
            if context.connection.execute(
                "SELECT 1 FROM characters WHERE active_vehicle_id=?", (vehicle_id,),
            ).fetchone():
                raise RegistrationRejected("仍有人正在使用的旧载具不能重新登记为静止位置")
            if context.connection.execute(
                "SELECT 1 FROM vehicle_rides WHERE vehicle_id=? "
                "AND status IN ('pending','accepted','boarding','moving','arrived','blocked')",
                (vehicle_id,),
            ).fetchone():
                raise RegistrationRejected("仍有共乘安排的旧载具不能重新登记位置或座位")
        elif context.connection.execute(
            "SELECT 1 FROM vehicles WHERE world_id=? AND name=?",
            (context.world_id, payload.name.strip()),
        ).fetchone():
            raise RegistrationRejected("当前世界已有同名载具，请核实已有载具而非重复创建")

        now = to_iso(context.now)
        if vehicle is None:
            context.connection.execute(
                """INSERT INTO vehicles(
                    id,world_id,name,movement_type,speed_kmh,owner_character_id,
                    is_available,metadata_json,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,1,'{}',?,?)""",
                (vehicle_id, context.world_id, payload.name.strip(), payload.movement_type,
                 payload.speed_kmh, payload.owner_character_id, now, now),
            )
        # 哥哥，旧载具的未知信息只在这里经确认补齐，不从车主当前位置推断。
        context.connection.execute(
            """INSERT INTO vehicle_states(
                vehicle_id,world_id,capacity,longitude,latitude,space_layer,
                current_location_id,status,distance_travelled_km,registration_id,updated_at
            ) VALUES (?,?,?,?,?,?,?,'parked',0,?,?)""",
            (vehicle_id, context.world_id, payload.capacity, location["longitude"],
             location["latitude"], location["space_layer"], location["id"],
             context.registration_id, now),
        )
        return HandlerResult(
            status=RegistrationStatus.APPLIED,
            summary=f"载具“{payload.name.strip()}”的实物与{payload.capacity}个座位已核实登记（含驾驶者）。",
            entity_type="vehicle", entity_id=vehicle_id, entity_name=payload.name.strip(),
            location_id=location["id"],
            effects=[Effect(
                effect_type="vehicle.physical_registered", entity_type="vehicle", entity_id=vehicle_id,
                payload={"capacity": payload.capacity, "location_id": location["id"],
                         "legacy_backfill": vehicle is not None},
            )],
        )
