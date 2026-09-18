"""物品与生活类世界元素处理器：室内布局、活动规则、物品、工资与作息。"""

from __future__ import annotations

from world_engine.activity_tasks import TaskService
from world_engine.economy import EconomyService
from world_engine.elements import WorldElementCatalog
from world_engine.interiors import InteriorError, InteriorRoomSpec, InteriorService
from world_engine.registration.models import (
    Effect,
    ElementType,
    HandlerResult,
    RegistrationContext,
    RegistrationRejected,
    RegistrationStatus,
)
from world_engine.routines import RoutineService


class InteriorRoomHandler:
    element_type = ElementType.INTERIOR_ROOM

    def apply(self, context: RegistrationContext, payload: InteriorRoomSpec) -> HandlerResult:
        if context.requested_by_character_id is not None:
            raise RegistrationRejected("已有室内布局由编年者核实登记，人物不能凭描述创建房产或家具")
        try:
            room_id = InteriorService.register(
                context.connection, context.world_id, context.registration_id, payload,
            )
        except InteriorError as exc:
            raise RegistrationRejected(str(exc)) from exc
        for fixture in context.connection.execute(
            "SELECT * FROM life_fixtures WHERE room_id=?", (room_id,),
        ).fetchall():
            WorldElementCatalog().upsert(
                context.connection, world_id=context.world_id, entity_type="interior_fixture",
                entity_id=fixture["id"], name=fixture["name"], source_kind="registration",
                source_registration_id=context.registration_id,
                source_event_id=context.source_event["id"],
                metadata={"room_id": room_id, "kind": fixture["kind"]},
            )
        return HandlerResult(
            status=RegistrationStatus.APPLIED, summary=f"{payload.name}的室内布局已确认登记。",
            entity_type="interior_room", entity_id=room_id, entity_name=payload.name,
            location_id=payload.location_id,
            effects=[Effect(
                effect_type="interior.registered", entity_type="interior_room", entity_id=room_id,
            )],
        )


class ActivityRecipeHandler:
    element_type = ElementType.ACTIVITY_RECIPE

    def apply(self, context, payload):
        if context.requested_by_character_id is not None:
            raise RegistrationRejected("生产规则需要编年者核实，人物不能自定原料与产物")
        try:
            recipe_id = TaskService.register(
                context.connection, context.world_id, context.registration_id, payload,
            )
        except ValueError as exc:
            raise RegistrationRejected(str(exc)) from exc
        return HandlerResult(
            status=RegistrationStatus.APPLIED, summary=f"{payload.name}的活动规则已登记。",
            entity_type="activity_recipe", entity_id=recipe_id, entity_name=payload.name,
            location_id=payload.location_id,
        )


class CommodityHandler:
    element_type=ElementType.COMMODITY
    def apply(self,context,payload):
        if context.requested_by_character_id is not None:raise RegistrationRejected("物品与补给规则需要编年者核实")
        try:iid=EconomyService.register_item(context.connection,context.world_id,context.registration_id,payload,context.world_time)
        except ValueError as exc:raise RegistrationRejected(str(exc)) from exc
        return HandlerResult(status=RegistrationStatus.APPLIED,summary=f"{payload.name}的物品规则已登记。",entity_type="item_type",entity_id=iid,entity_name=payload.name,location_id=payload.resource_location_id)


class WorkplaceBudgetHandler:
    element_type=ElementType.WORKPLACE_BUDGET
    def apply(self,context,payload):
        if context.requested_by_character_id is not None:raise RegistrationRejected("初始工资资金须由编年者登记，不能由人物凭空增加")
        try:lid=EconomyService.register_budget(context.connection,context.world_id,context.registration_id,payload)
        except ValueError as exc:raise RegistrationRejected(str(exc)) from exc
        return HandlerResult(status=RegistrationStatus.APPLIED,summary="工作场所工资账户已登记。",entity_type="workplace_account",entity_id=lid,entity_name=payload.name,location_id=payload.location_id)


class RoutinePlanHandler:
    element_type = ElementType.NPC_ROUTINE

    def apply(self,context,payload):
        if context.requested_by_character_id is not None:
            raise RegistrationRejected("作息配置须由编年者核对，人物不能用此入口替别人安排生活")
        try:
            plan_id=RoutineService.register(context.connection,context.world_id,context.registration_id,payload,context.world_time)
        except ValueError as exc:
            raise RegistrationRejected(str(exc)) from exc
        return HandlerResult(status=RegistrationStatus.APPLIED,summary=f"{payload.name}的周期作息已登记。",entity_type="npc_routine",entity_id=plan_id,entity_name=payload.name)
