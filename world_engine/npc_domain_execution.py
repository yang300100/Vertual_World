"""类型化领域操作统一回到已有服务执行。"""

from world_engine.economy import EconomyService
from world_engine.npc_planning import DomainOperation
from world_engine.opportunities import OpportunityService


def execute_operation(connection, actor, operation: DomainOperation, at) -> str | None:
    name, arguments = operation.name, operation.arguments
    if name == "cooperation_operation":
        from world_engine.npc_cooperation import execute

        return execute(connection, actor, arguments, at)
    if name in {"buy_food", "buy_supply"}:
        result = EconomyService.buy_supply(
            connection, actor, at, food_only=name == "buy_food",
            item_type_id=arguments.get("item_type_id"),
        )
        return result["source_event_id"] if result else None
    if name in {"harvest_food", "harvest"}:
        item_id = arguments.get("item_type_id")
        return EconomyService.harvest(
            connection, actor, at, food_only=name == "harvest_food",
            item_type_ids=[item_id] if item_id else None, return_event=True,
        ) or None
    if name == "collect_output":
        return EconomyService.collect_outputs(
            connection, actor, at, item_type_ids=[arguments["item_type_id"]],
            return_event=True,
        )
    if name in {"accept_opportunity", "claim_opportunity"}:
        method = OpportunityService.accept if name == "accept_opportunity" else OpportunityService.claim
        return method(connection, str(arguments["opportunity_id"]), actor["id"], at)
    raise ValueError(f"没有登记领域操作：{name}")
