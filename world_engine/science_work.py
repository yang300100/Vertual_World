"""哥哥，工艺仍由原有工单、劳动和物质账执行，能源只能来自实际燃料。"""

import math

from world_engine.craft_physics import CRAFT_METRICS, assay_craft, energy_required, process_craft
from world_engine.science_workshop import profile_for


def enabled(c, world_id):
    return (
        c.execute("SELECT 1 FROM science_workshops WHERE world_id=?", (world_id,)).fetchone()
        is not None
    )


def prepare_energy(c, worker, order, task, state, auxiliaries):
    from world_engine.collective_work import WorkService

    profile = profile_for(c, worker["world_id"], state)
    need = energy_required(state, task["operation"], task["settings"], task["hours"], profile)
    # 哥哥，这只是预留需求，稍后必须实际消耗能源。
    process_craft(
        state, task["operation"], task["settings"], auxiliaries, task["hours"], need, profile
    )
    if need <= 0:
        return None, 0, 0.0
    energy_id = task.get("energy_id")
    stock = c.execute(
        "SELECT * FROM science_energy_stocks WHERE item_id=? AND world_id=?",
        (energy_id, worker["world_id"]),
    ).fetchone()
    if stock is None:
        raise ValueError(
            f"操作 {task['operation']} 需要真实有限能源：energy 字段缺失或不是现场能源库存实物id；"
            "不能以供能文字代替燃料"
        )
    if energy_id in {task["batch_id"], *task["auxiliary_ids"]}:
        raise ValueError("燃料不能同时作为本次原料或辅助材料")
    item = WorkService._source(c, order, worker, energy_id)
    units = math.ceil(need / stock["energy_per_unit"])
    if item["quantity"] < units:
        raise ValueError("实际能源库存不足")
    return item, units, units * stock["energy_per_unit"]


def process_work(c, world_id, state, task, hours, auxiliaries):
    return process_craft(
        state,
        task["operation"],
        task["settings"],
        auxiliaries,
        hours,
        task.get("_supplied_energy", 0),
        profile_for(c, world_id, state),
    )


def measure(c, world_id, state, metric, tool):
    if metric in CRAFT_METRICS:
        return assay_craft(state, metric, dict(tool), profile_for(c, world_id, state))
    from world_engine.collective_research import CollectiveResearchService

    raw = CollectiveResearchService._measurement(state, metric)
    return {
        "value": None
        if raw < tool["detection_limit"]
        else round(raw / tool["precision"]) * tool["precision"],
        "precision": tool["precision"],
        "detection_limit": tool["detection_limit"],
    }
