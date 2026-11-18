"""燃料规格必须核对现有载具和物品，且不代人物补充燃料。"""

import json

from world_engine.registration.models import (
    Effect, ElementType, HandlerResult, RegistrationRejected, RegistrationStatus,
    VehicleFuelProfileSpec,
)
from world_engine.repository import from_iso


class VehicleFuelProfileHandler:
    element_type = ElementType.VEHICLE_FUEL_PROFILE

    def apply(self, context, payload):
        if not isinstance(payload, VehicleFuelProfileSpec):
            raise TypeError("载具燃料规格处理器收到错误的数据类型")
        if context.requested_by_character_id is not None:
            raise RegistrationRejected("载具燃料规格须由编年者核实，人物不能自行定义容量与消耗")
        c = context.connection
        vehicle = c.execute("SELECT * FROM vehicles WHERE id=? AND world_id=? AND is_available=1",
                            (payload.vehicle_id, context.world_id)).fetchone()
        state = c.execute("SELECT * FROM vehicle_states WHERE vehicle_id=? AND world_id=?",
                          (payload.vehicle_id, context.world_id)).fetchone()
        if vehicle is None or state is None or state["status"] != "parked" or state["current_location_id"] is None:
            raise RegistrationRejected("规格只能核实同世界真实静止且已确认地点的载具")
        # 哥哥，登记时也核对完整物理来源，并按当时来源事件的时间判断已成立。
        from world_engine.vehicle_fuel_inventory import VehicleFuelInventoryService
        try:
            confirmed_vehicle = VehicleFuelInventoryService._vehicle_registration(
                c, context.world_id, vehicle["id"], state,
                from_iso(context.source_event["occurred_at"]),
            )
            source_order = c.execute("SELECT rowid FROM world_events WHERE id=? AND world_id=?",
                                     (context.source_event["id"], context.world_id)).fetchone()[0]
            if not VehicleFuelInventoryService._event_not_after(
                confirmed_vehicle["occurred_at"], confirmed_vehicle["event_order"],
                context.source_event["occurred_at"], source_order,
            ):
                raise ValueError("原车确认不能晚于规格来源")
        except (ValueError, LookupError, TypeError) as exc:
            raise RegistrationRejected("载具原物理登记来源尚未实际成立或不一致") from exc
        if c.execute("SELECT 1 FROM vehicle_fuel_profiles WHERE vehicle_id=?", (payload.vehicle_id,)).fetchone():
            raise RegistrationRejected("该载具已有经确认的燃料规格，不能借新申请覆写")
        if c.execute("SELECT 1 FROM character_movements WHERE vehicle_id=? AND status IN ('moving','suspended')", (payload.vehicle_id,)).fetchone():
            raise RegistrationRejected("存在行驶或挂起路线的载具不能重新核实燃料规格")
        if c.execute("SELECT 1 FROM characters WHERE active_vehicle_id=?", (payload.vehicle_id,)).fetchone():
            raise RegistrationRejected("仍有人正在使用的载具不能重新核实燃料规格")
        if c.execute("SELECT 1 FROM vehicle_rides WHERE vehicle_id=? AND status IN ('pending','accepted','boarding','moving','arrived','blocked')", (payload.vehicle_id,)).fetchone():
            raise RegistrationRejected("仍有共乘安排的载具不能重新核实燃料规格")
        if c.execute("SELECT 1 FROM vehicle_rides r JOIN vehicle_ride_members m ON m.ride_id=r.id "
                     "WHERE r.vehicle_id=? AND m.boarded_at IS NOT NULL AND m.disembarked_at IS NULL", (payload.vehicle_id,)).fetchone():
            raise RegistrationRejected("仍有真实在车成员，不能重新核实燃料规格")
        from world_engine.geo import great_circle_distance_km
        place = c.execute("SELECT * FROM locations WHERE id=? AND world_id=? AND is_active=1", (state["current_location_id"], context.world_id)).fetchone()
        if (place is None or place["longitude"] is None or place["latitude"] is None
                or place["space_layer"] != state["space_layer"]
                or great_circle_distance_km(state["longitude"], state["latitude"], place["longitude"], place["latitude"]) > 0.02):
            raise RegistrationRejected("载具不在同层真实可核验的停靠位置")
        if from_iso(context.source_event["occurred_at"]) > context.world_time or context.source_event["location_id"] != state["current_location_id"]:
            raise RegistrationRejected("规格来源必须是车辆真实地点已发生的事件")
        try:
            source = json.loads(context.source_event["payload_json"] or "{}")
        except (ValueError, TypeError):
            source = None
        if not isinstance(source, dict) or not (source.get("vehicle_id") == payload.vehicle_id or
                (source.get("entity_type") == "vehicle" and source.get("entity_id") == payload.vehicle_id)):
            raise RegistrationRejected("规格来源须明确关联该真实载具")
        item = c.execute(
            "SELECT r.*,e.occurred_at,e.rowid AS source_event_order FROM world_item_profiles p "
            "JOIN element_registration_requests r ON r.id=p.registration_id AND r.world_id=p.world_id "
            "JOIN world_events e ON e.world_id=r.world_id AND e.id=r.source_event_id "
            "WHERE p.item_type_id=? AND p.world_id=? AND r.element_type='commodity' "
            "AND r.requested_by_character_id IS NULL",
            (payload.item_type_id, context.world_id),
        ).fetchone()
        if item is None or item["status"] != "applied" or item["result_entity_id"] != payload.item_type_id:
            raise RegistrationRejected("燃料物品类型须已在当前世界经确认登记")
        try:
            VehicleFuelInventoryService._confirmed(c, item, context.world_time, "item_type", payload.item_type_id)
        except (ValueError, LookupError, TypeError) as exc:
            raise RegistrationRejected("燃料物品类型缺少已发生且完整的确认来源") from exc
        c.execute("INSERT INTO vehicle_fuel_profiles VALUES (?,?,?,?,?,?,?)", (
            vehicle["id"], context.world_id, payload.item_type_id, payload.capacity_units,
            payload.rate_numerator, payload.rate_denominator, context.registration_id,
        ))
        # 哥哥，只登记规格效果，不改写车辆原有登记来源，也不添加油箱余额。
        return HandlerResult(
            status=RegistrationStatus.APPLIED,
            summary=f"载具“{vehicle['name']}”的燃料规格已核实，尚未产生燃料。",
            location_id=state["current_location_id"],
            effects=[Effect(effect_type="vehicle.fuel_profile_registered", entity_type="vehicle", entity_id=vehicle["id"],
                payload={"item_type_id": payload.item_type_id, "capacity_units": payload.capacity_units,
                         "rate_numerator": payload.rate_numerator, "rate_denominator": payload.rate_denominator})],
        )
