"""哥哥，只检查已知样品数量，不预测读数、工艺成功或自动补充材料。"""

from world_engine.collective_research import CollectiveResearchService as R
from world_engine.collective_resources import accessible
from world_engine.science_types import ASSAY_SAMPLE_QUANTITY


class SampleAllocationError(ValueError):
    def __init__(self, index, available, required):
        self.index = index
        super().__init__(
            f"命令{index}的样品分配不足：计划中此前处理或检测后仅剩{available}份，"
            f"本步需{required}份；量器检测完成会消耗取样，须由本人调整样品或步骤"
        )


def check_sample_allocation(c, actor_id, program):
    """只对确定的直接处理与检测作数量检查；其他动作后的库存不作推断。"""
    actor = R.actor(c, actor_id)
    stock = {}
    opaque = False

    def resource_key(ref):
        if not ref.startswith("$"):
            return ref
        parts = ref.split(":")
        if len(parts) < 2 or not all(p.isdigit() for p in parts[1:]):
            return ref
        try:
            return ":".join([parts[0], *(str(int(p)) for p in parts[1:])])
        except ValueError:
            return ref

    def available(ref):
        ref = resource_key(ref)
        if ref not in stock:
            row = None if opaque or ref.startswith("$") else c.execute(
                "SELECT * FROM item_instances WHERE id=? AND world_id=? "
                "AND owner_character_id=? AND condition>0",
                (ref, actor["world_id"], actor_id),
            ).fetchone()
            stock[ref] = row["quantity"] if row is not None and accessible(c, row, actor) else None
        return stock[ref]

    for index, command in enumerate(program.commands):
        if command.op not in {"craft", "assay"}:
            if command.op not in {"declare", "revise_claim", "share_method", "invalidate_method",
                                   "wait", "rest"}:
                # 哥哥，采集、转交、方法调用和旧动作可能改库存，后面的真实数量仍待结算。
                stock.clear()
                opaque = True
            continue
        amount = command.quantity if command.op == "craft" else ASSAY_SAMPLE_QUANTITY
        current = available(command.input)
        if current is not None:
            if amount > current:
                raise SampleAllocationError(index, current, amount)
            stock[resource_key(command.input)] = current - amount
        if command.op == "craft":
            for ref in command.auxiliary_inputs:
                stock[resource_key(ref)] = None
            if command.result_key:
                # 哥哥，这是工艺成功分支的数量约束，不是提前创建实物或保证工艺成功。
                stock["$" + command.result_key + ":0"] = command.quantity
