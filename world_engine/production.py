"""制作前按真实短缺逐份准备物资，采购有预算，资源有所有者。"""

from world_engine.activity_tasks import TaskService
from world_engine.economy import EconomyService
from world_engine.repository import to_iso


class ProductionService:
    @staticmethod
    def prepare_routine(c, actor, at, context, recipe, free_minutes, task_minutes):
        missing = TaskService.missing_supplies(c, actor, recipe, at)
        if not missing:
            return None
        if free_minutes < task_minutes + 15:
            return "来不及准备材料并完成工序，暂缓制作"
        occurrence_id = context["occurrence"]["id"]
        spent = c.execute(
            "SELECT COALESCE(SUM(spent),0) FROM npc_routine_supplies WHERE occurrence_id=?",
            (occurrence_id,),
        ).fetchone()[0]
        budget = min(
            max(0, context["slot"].supply_purchase_budget - spent),
            max(0, actor["money"] - context["spec"].income_reserve),
        )
        # 每次只补一种短缺的一份；已买材料留在本人库存，不随时段中止消失。
        for need in missing:
            item_type = need["item_type_id"]
            c.execute("SAVEPOINT production_supply")
            try:
                harvested = actor["energy"] >= recipe["energy_cost"] + 3 and EconomyService.harvest(
                    c, actor, at, item_type_ids={item_type}, return_event=True
                )
                purchase = (
                    None
                    if harvested
                    else EconomyService.buy_supply(
                        c,
                        actor,
                        at,
                        item_type_id=item_type,
                        minimum_condition=need["minimum_condition"],
                        budget=budget,
                    )
                )
                if not harvested and not purchase:
                    c.execute("RELEASE SAVEPOINT production_supply")
                    continue
                source = purchase["source_event_id"] if purchase else harvested
                c.execute(
                    "INSERT INTO npc_routine_supplies VALUES (?,?,?,?,?,?)",
                    (
                        occurrence_id,
                        to_iso(at),
                        item_type,
                        "harvest" if harvested else "purchase",
                        purchase["spent"] if purchase else 0,
                        source,
                    ),
                )
                c.execute("RELEASE SAVEPOINT production_supply")
                return "已合法取得一份制作物资，整理后再继续准备或开工"
            except Exception:
                c.execute("ROLLBACK TO SAVEPOINT production_supply")
                c.execute("RELEASE SAVEPOINT production_supply")
                raise
        return "缺少可合法取得的材料或工具，或采购预算与生活储备不足，暂缓制作"
