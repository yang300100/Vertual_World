"""哥哥，工艺仍由原有工单、劳动和物质账执行，能源只能来自实际燃料。"""

import math

from world_engine.craft_physics import energy_required
from world_engine.phenomena_rules import REGISTRY
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
    outcome = REGISTRY.evaluate(
        "material_processing",
        task["operation"],
        state=state,
        settings=task["settings"],
        auxiliaries=auxiliaries,
        hours=task["hours"],
        supplied_energy=need,
        profile=profile,
    )
    if outcome.status != "computed":
        raise ValueError(outcome.reason)
    from world_engine.phenomena import ensure_rule
    from world_engine.repository import from_iso

    at = from_iso(
        c.execute(
            "SELECT w.current_time FROM worlds w WHERE w.id=?", (worker["world_id"],)
        ).fetchone()[0]
    )
    task["_phenomenon_rule_id"] = ensure_rule(c, worker["world_id"], outcome, state.carrier, at)
    task["_phenomenon_version"] = outcome.version
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
    outcome = REGISTRY.evaluate(
        "material_processing",
        task["operation"],
        version=task.get("_phenomenon_version"),
        state=state,
        settings=task["settings"],
        auxiliaries=auxiliaries,
        hours=hours,
        supplied_energy=task.get("_supplied_energy", 0),
        profile=profile_for(c, world_id, state),
    )
    if outcome.status != "computed":
        raise ValueError(outcome.reason)
    from world_engine.phenomena import ensure_rule
    from world_engine.repository import from_iso

    at = from_iso(
        c.execute("SELECT w.current_time FROM worlds w WHERE w.id=?", (world_id,)).fetchone()[0]
    )
    rid = ensure_rule(c, world_id, outcome, state.carrier, at)
    if task.get("_phenomenon_rule_id") and task["_phenomenon_rule_id"] != rid:
        raise ValueError("工单绑定的规则来源已改变，不能按其他规则结算")
    return outcome.outputs


def measure(c, world_id, state, metric, tool, *, rule_version=None):
    outcome = REGISTRY.evaluate(
        "material_measurement",
        "assay",
        version=rule_version,
        state=state,
        metric=metric,
        instrument=dict(tool),
        profile=profile_for(c, world_id, state),
    )
    if outcome.status != "computed":
        raise ValueError(outcome.reason)
    from world_engine.phenomena import ensure_rule
    from world_engine.repository import from_iso

    at = from_iso(
        c.execute("SELECT w.current_time FROM worlds w WHERE w.id=?", (world_id,)).fetchone()[0]
    )
    rid = ensure_rule(c, world_id, outcome, state.carrier, at)
    return outcome.outputs[0] | {"rule_version": outcome.version, "rule_id": rid}
