"""哥哥，器具和耗材都要在现场，预留与归还共用这份实物检查。"""

import json

from world_engine.inventory import InventoryService
from world_engine.materials import MaterialService as M


def accessible(c, item, actor):
    location = actor["current_location_id"] or actor["location_id"]
    if item["container_type"] in {"ground", "natural_resource"}:
        return item["container_id"] == location and not actor["current_room_id"]
    if item["container_type"] == "character_inventory":
        holder = c.execute(
            "SELECT * FROM characters WHERE id=?", (item["container_id"],)
        ).fetchone()
        return (
            holder is not None
            and not holder["current_room_id"]
            and not actor["current_room_id"]
            and (holder["current_location_id"] or holder["location_id"]) == location
        )
    return False


def supplies(c, actor, requirements, at, *, excluded=(), multiplier=1):
    from world_engine.repository import to_iso

    result = []
    used = set(excluded)
    for requirement in requirements:
        count = requirement["quantity"] * multiplier
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("耗材登记数量不合法")
        found = None
        for item in c.execute(
            "SELECT * FROM item_instances WHERE world_id=? AND owner_character_id=? AND item_type_id=? AND quantity>=? AND (spoils_world_time IS NULL OR spoils_world_time>?) ORDER BY quantity DESC",
            (actor["world_id"], actor["id"], requirement["item_type_id"], count, to_iso(at)),
        ):
            if item["id"] not in used and accessible(c, item, actor):
                found = item
                break
        if found is None:
            raise ValueError("真实操作耗材不足：" + requirement["item_type_id"])
        used.add(found["id"])
        result.append((found, count))
    return result


def reserve(c, actor, inputs, task_id):
    from world_engine.activity_tasks import TaskService

    return [TaskService._reserve(c, actor, item, n, task_id) for item, n in inputs]


def finish_supplies(c, ids, location, destination, *, consumed):
    for iid in ids:
        item = c.execute("SELECT * FROM item_instances WHERE id=?", (iid,)).fetchone()
        if item is None:
            raise ValueError("已预留的耗材在结算前丢失")
        if consumed:
            InventoryService.consume(c, item, item["quantity"], destination=destination)
        elif M.state(c, iid) is not None:
            M.move(c, item, item["quantity"], "ground", location, item["owner_character_id"])
        else:
            c.execute(
                "UPDATE item_instances SET container_type='ground',container_id=? WHERE id=?",
                (location, iid),
            )


def return_tool(c, iid, place, location, *, wear=0):
    destination = dict(place)
    if destination["container_type"] == "character_inventory":
        holder = c.execute(
            "SELECT * FROM characters WHERE id=?", (destination["container_id"],)
        ).fetchone()
        if (
            holder is None
            or holder["health"] <= 0
            or holder["current_room_id"]
            or (holder["current_location_id"] or holder["location_id"]) != location
            or c.execute(
                "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",
                (destination["container_id"],),
            ).fetchone()
        ):
            destination = {"container_type": "ground", "container_id": location}
    c.execute(
        "UPDATE item_instances SET container_type=?,container_id=?,condition=MAX(0,condition-?) WHERE id=?",
        (destination["container_type"], destination["container_id"], wear, iid),
    )


def hydrate(c, sample_ids, supply_ids, requirements, method):
    from world_engine.material_physics import combine, fraction

    if method != "germination":
        return
    pairs = [iid for iid, req in zip(supply_ids, requirements) if req.get("role") == "hydration"]
    if len(pairs) != 1:
        raise ValueError("萌发检验需要登记一份实际供水耗材")
    iid = pairs[0]
    water = M.state(c, iid)
    if water is None or water.carrier != "water" or water.mass != 0 or water.water <= 0:
        raise ValueError("萌发检验的供水没有真实液体来源")
    samples = [M.state(c, sid) for sid in sample_ids]
    if any(s is None or s.carrier != "seed" for s in samples):
        raise ValueError("萌发检验须对真实种子进行，不能把土壤混合物当作种子")
    additions = fraction(water, 1 / len(samples))
    updates = {
        sid: combine((state, additions), carrier="seed") for sid, state in zip(sample_ids, samples)
    }
    updates[iid] = fraction(water, 0)
    M.rebalance(c, updates, operation="germination_hydration")
