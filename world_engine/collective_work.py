"""哥哥，已同意的工作需要真实身体、器具、材料和时间，不能用签名结算。"""

from __future__ import annotations

import json
from datetime import timedelta

from world_engine import collective_resources as resources
from world_engine.action_checks import CheckService
from world_engine.collective_research import CollectiveResearchService as R
from world_engine.life import LifeActivityService
from world_engine.material_physics import fraction, process
from world_engine.materials import MaterialService as M
from world_engine.materials import encoded, material_transaction
from world_engine.repository import from_iso, to_iso
from world_engine import science_work


def order_world(c, order):
    return c.execute("SELECT world_id FROM collective_proposals WHERE id=?", (order["proposal_id"],)).fetchone()[0]


class WorkService:
    @staticmethod
    def _resolve(c, order, reference):
        if not reference.startswith("task:"):
            return reference
        _, key, index = reference.split(":")
        row = c.execute(
            "SELECT outputs_json FROM collective_work_orders WHERE proposal_id=? AND version=? AND task_key=? AND status='completed'",
            (order["proposal_id"], order["version"], key),
        ).fetchone()
        if row is None:
            raise ValueError("前置任务还没有实际产物")
        outputs = json.loads(row[0])
        try:
            return outputs[int(index)]
        except (IndexError, ValueError) as exc:
            raise ValueError("引用的前置产物不存在") from exc

    @classmethod
    def _source(cls, c, order, worker, iid, reference=None):
        item = c.execute(
            "SELECT * FROM item_instances WHERE id=? AND world_id=?", (iid, worker["world_id"])
        ).fetchone()
        if item is None or M.state(c, iid) is None:
            raise ValueError("实际追踪材料已不存在")
        if item["owner_character_id"] != worker["id"]:
            permitted = c.execute(
                "SELECT 1 FROM collective_resource_permissions WHERE proposal_id=? AND version=? AND item_id=? AND allowed_actor_id=? AND owner_id=?",
                (
                    order["proposal_id"],
                    order["version"],
                    iid,
                    worker["id"],
                    item["owner_character_id"],
                ),
            ).fetchone()
            if permitted is None:
                prior = c.execute(
                    "SELECT task_key,spec_json,outputs_json FROM collective_work_orders WHERE proposal_id=? AND version=? AND status='completed'",
                    (order["proposal_id"], order["version"]),
                ).fetchall()
                if not any(
                    reference
                    and reference.startswith("task:" + x["task_key"] + ":")
                    and iid in json.loads(x["outputs_json"])
                    and worker["id"] in json.loads(x["spec_json"]).get("release_to", [])
                    for x in prior
                ):
                    raise ValueError("原料所有者尚未许可这一版本的工作")
        loc = worker["current_location_id"] or worker["location_id"]
        if item["container_type"] == "ground" and item["container_id"] == loc:
            return item
        if item["container_type"] == "character_inventory":
            holder = c.execute(
                "SELECT * FROM characters WHERE id=?", (item["container_id"],)
            ).fetchone()
            if (
                holder is not None
                and (holder["current_location_id"] or holder["location_id"]) == loc
                and holder["current_room_id"] is None
            ):
                return item
        raise ValueError("材料不在实际工作地点，不能从远处仓库取出")

    @classmethod
    def _start(cls, c, order, task, at):
        worker = R.actor(c, order["worker_id"])
        people = [worker, *[R.actor(c, x) for x in task["assistants"]]]
        if len({x["id"] for x in people}) != len(people):
            raise ValueError("同一个身体不能同时担任多个操作位置")
        location = task.get("location_id") or worker["lab_location_id"]
        for actor in people:
            if (actor["current_location_id"] or actor["location_id"]) != location or actor[
                "current_room_id"
            ]:
                raise ValueError("参与者尚未实际抵达共同工作地点")
            LifeActivityService.assert_available(c, actor["id"])
            if actor["energy"] < 15 or actor["satiety"] <= 10 or actor["health"] < 40:
                raise ValueError("参与者当前身体不能持续操作")
        iid = cls._resolve(c, order, task["batch_id"])
        source = cls._source(c, order, worker, iid, task["batch_id"])
        if source["quantity"] < task["quantity"]:
            raise ValueError("真实原料数量不足")
        original = fraction(M.state(c, iid), task["quantity"] / source["quantity"])
        from world_engine.technology_methods import check_work_span
        auxiliaries = []
        for reference in task["auxiliary_ids"]:
            auxid = cls._resolve(c, order, reference)
            aux = cls._source(c, order, worker, auxid, reference)
            quantity = task["auxiliary_quantities"].get(reference, aux["quantity"])
            if (
                isinstance(quantity, bool)
                or not isinstance(quantity, int)
                or not 1 <= quantity <= aux["quantity"]
                or auxid == iid
            ):
                raise ValueError("辅助材料数量不合法")
            auxiliaries.append((aux, quantity))
        if len({x[0]["id"] for x in auxiliaries}) != len(auxiliaries):
            raise ValueError("不能重复使用同一辅助材料")
        check_work_span(c, order, worker, original, at,
                        material_ids=(iid, *(x[0]["id"] for x in auxiliaries)))
        if task["operation"] == "assay":
            cls._start_assay(c, order, task, at, worker, people, source, original)
            return
        apparatus = None
        if task["operation"] not in {"wash", "mix", "store"} or original.mass + original.water > 5:
            apparatus = c.execute(
                "SELECT s.*,i.condition,i.container_type,i.container_id,i.owner_character_id FROM collective_apparatus s JOIN item_instances i ON i.id=s.item_id WHERE s.item_id=? AND s.world_id=?",
                (task["apparatus_id"], worker["world_id"]),
            ).fetchone()
            if (
                apparatus is None
                or task["operation"] not in json.loads(apparatus["operations_json"])
                or apparatus["condition"] <= 0
            ):
                raise ValueError("没有可实际执行该操作的器具")
            if apparatus["container_type"] not in {"character_inventory", "ground"}:
                raise ValueError("器具正被其他工作占用")
            if not resources.accessible(c, apparatus, worker):
                raise ValueError("器具不在实际工作地点")
            if (
                apparatus["owner_character_id"] != worker["id"]
                and c.execute(
                    "SELECT 1 FROM collective_resource_permissions WHERE proposal_id=? AND version=? AND item_id=? AND allowed_actor_id=?",
                    (order["proposal_id"], order["version"], apparatus["item_id"], worker["id"]),
                ).fetchone()
                is None
            ):
                raise ValueError("器具所有者未许可本次操作")
            load = (
                original.mass
                + original.water
                + sum(
                    (M.state(c, x["id"]).mass + M.state(c, x["id"]).water) * n / x["quantity"]
                    for x, n in auxiliaries
                )
            )
            if (
                load > apparatus["capacity"]
                or task["hours"] + 1e-9 < load / apparatus["throughput"]
            ):
                raise ValueError("处理量超过容量，或劳动时间不足以达到真实吞吐量")
            if (
                CheckService.skill(c, worker, apparatus["skill_name"])
                < apparatus["min_proficiency"]
            ):
                raise ValueError("器具主操作熟练度不足")
            if task["settings"].get("power", 1) > apparatus["max_power"]:
                raise ValueError("供能超过器具能力")
            stations = [
                s
                for s in json.loads(apparatus["stations_json"])
                if load >= s.get("minimum_load", 0)
            ]
            if len(people) < len(stations):
                raise ValueError("器具在此负荷下需要更多同时操作的位置")
            for actor, station in zip(people, stations, strict=False):
                if CheckService.skill(c, actor, station["skill_name"]) < station["min_proficiency"]:
                    raise ValueError("操作位置的真实熟练度不足")
        if task["operation"] == "wash" and not auxiliaries:
            raise ValueError("浸洗必须提供实际水源")
        auxstates = tuple(fraction(M.state(c, x["id"]), n / x["quantity"]) for x, n in auxiliaries)
        fuel, fuel_units, supplied = None, 0, 0.0
        if science_work.enabled(c, worker["world_id"]):
            fuel, fuel_units, supplied = science_work.prepare_energy(c, worker, order, task, original, auxstates)
        else:
            process(original, task["operation"], task["settings"], task["hours"], auxiliaries=auxstates)
        requirements = json.loads(apparatus["supplies_json"]) if apparatus else []
        supplies = resources.supplies(
            c, worker, requirements, at, excluded=[iid, *[x["id"] for x, n in auxiliaries]]
        )
        ids = []
        for actor in people:
            aid = LifeActivityService.start(c, actor, at, "research", round(task["hours"] * 60))
            c.execute(
                "INSERT INTO collective_work_people VALUES (?,?,?)", (order["id"], actor["id"], aid)
            )
            ids.append(aid)
        materialids = [
            M.move(
                c,
                source,
                task["quantity"],
                "collective_work",
                order["id"],
                source["owner_character_id"],
            )
        ]
        materialids += [
            M.move(c, x, n, "collective_work", order["id"], x["owner_character_id"])
            for x, n in auxiliaries
        ]
        task["_supply_ids"] = resources.reserve(c, worker, supplies, order["id"])
        if fuel is not None:
            M.consume(c, fuel, fuel_units, "spent-energy:" + order["id"])
        task["_supplied_energy"] = supplied
        task["_energy_units"] = fuel_units
        if apparatus:
            task["_apparatus_location"] = {
                "container_type": apparatus["container_type"],
                "container_id": apparatus["container_id"],
            }
            c.execute(
                "UPDATE item_instances SET container_type='collective_work_tool',container_id=? WHERE id=?",
                (order["id"], apparatus["item_id"]),
            )
        task["_location"] = location
        c.execute(
            "UPDATE collective_work_orders SET status='working',activity_id=?,started_at=?,due_at=?,inputs_json=?,spec_json=? WHERE id=?",
            (
                ids[0],
                to_iso(at),
                to_iso(at + timedelta(hours=task["hours"])),
                encoded(materialids),
                encoded(task),
                order["id"],
            ),
        )

    @classmethod
    def _start_assay(cls, c, order, task, at, worker, people, source, state):
        from world_engine.collective_types import Prediction

        prediction = Prediction.model_validate(task["prediction"])
        if prediction.metric != task["method"]:
            raise ValueError("检验方法与事先预测不匹配")
        tool = c.execute(
            "SELECT s.*,i.condition,i.container_type,i.container_id,i.owner_character_id FROM collective_instruments s JOIN item_instances i ON i.id=s.item_id WHERE s.item_id=? AND s.world_id=?",
            (task["instrument_id"], worker["world_id"]),
        ).fetchone()
        if (
            tool is None
            or tool["condition"] <= 0
            or tool["method"] != task["method"]
            or (
                tool["owner_character_id"] != worker["id"]
                and not c.execute(
                    "SELECT 1 FROM collective_resource_permissions WHERE proposal_id=? AND version=? AND item_id=? AND allowed_actor_id=? AND owner_id=?",
                    (
                        order["proposal_id"],
                        order["version"],
                        tool["item_id"],
                        worker["id"],
                        tool["owner_character_id"],
                    ),
                ).fetchone()
            )
            or not resources.accessible(c, tool, worker)
        ):
            raise ValueError("检验器具不存在、不在现场或未许可")
        if CheckService.skill(c, worker, tool["skill_name"]) < tool["min_proficiency"]:
            raise ValueError("检验熟练度不足")
        if task["quantity"] > tool["sample_capacity"] or task["hours"] < tool["duration_hours"]:
            raise ValueError("检验样品容量或实际人工时间不足")
        if task["auxiliary_ids"] or task["assistants"]:
            raise ValueError("单台检验不能把辅助材料或签名当成实测")
        science_work.measure(c, worker["world_id"], state, task["method"], tool)
        inputs = resources.supplies(
            c,
            worker,
            json.loads(tool["supplies_json"]),
            at,
            excluded=[source["id"], tool["item_id"]],
        )
        aid = LifeActivityService.start(c, worker, at, "research", round(task["hours"] * 60))
        c.execute(
            "INSERT INTO collective_work_people VALUES (?,?,?)", (order["id"], worker["id"], aid)
        )
        iid = M.move(
            c,
            source,
            task["quantity"],
            "collective_work",
            order["id"],
            source["owner_character_id"],
        )
        task["_supply_ids"] = resources.reserve(c, worker, inputs, order["id"])
        resources.hydrate(
            c, [iid], task["_supply_ids"], json.loads(tool["supplies_json"]), task["method"]
        )
        task["_instrument_location"] = {
            "container_type": tool["container_type"],
            "container_id": tool["container_id"],
        }
        task["_location"] = task.get("location_id") or worker["lab_location_id"]
        task["_source_batch_id"] = source["id"]
        c.execute(
            "UPDATE item_instances SET container_type='collective_work_tool',container_id=? WHERE id=?",
            (order["id"], tool["item_id"]),
        )
        c.execute(
            "UPDATE collective_work_orders SET status='working',activity_id=?,started_at=?,due_at=?,inputs_json=?,spec_json=? WHERE id=?",
            (
                aid,
                to_iso(at),
                to_iso(at + timedelta(hours=task["hours"] + tool["incubation_hours"])),
                encoded([iid]),
                encoded(task),
                order["id"],
            ),
        )

    @classmethod
    def _finish(cls, c, order, task, at, *, interrupted=False):
        input_ids = json.loads(order["inputs_json"])
        states = [M.state(c, iid) for iid in input_ids]
        hours = task["hours"]
        if interrupted:
            hours = min(hours, max(0, (at - from_iso(order["started_at"])).total_seconds() / 3600))
        if task["operation"] == "assay":
            result = {"actual_hours": hours, "outputs": [], "completed": not interrupted}
            source = c.execute(
                "SELECT * FROM item_instances WHERE id=?", (input_ids[0],)
            ).fetchone()
            if interrupted:
                M.move(
                    c,
                    source,
                    source["quantity"],
                    "ground",
                    task["_location"],
                    source["owner_character_id"],
                )
            else:
                tool = c.execute(
                    "SELECT * FROM collective_instruments WHERE item_id=?", (task["instrument_id"],)
                ).fetchone()
                measured = science_work.measure(c, order_world(c, order), states[0], task["method"], tool)
                result["measurement"] = {
                    "batch_id": task["_source_batch_id"],
                    "metric": task["method"],
                    "value": measured["value"],
                    "unit": measured.get("unit", "relative"),
                    "instrument_id": task["instrument_id"],
                    "sample_conditions": {"carrier": states[0].carrier, "mass": states[0].mass,
                                          "water": states[0].water, "temperature": states[0].temperature},
                    "detection_limit": tool["detection_limit"],
                    "precision": tool["precision"],
                    "prediction": task["prediction"],
                    "prediction_recorded_at": order["started_at"],
                    "measured_at": to_iso(at),
                }
                M.consume(c, source, source["quantity"], "lab-waste:" + order["id"])
            place = task["_instrument_location"]
            resources.return_tool(
                c, task["instrument_id"], place, task["_location"], wear=0 if interrupted else 1
            )
            resources.finish_supplies(
                c,
                task.get("_supply_ids", []),
                task["_location"],
                "lab-waste:" + order["id"],
                consumed=not interrupted,
            )
            c.execute(
                "UPDATE collective_work_orders SET status=?,result_json=? WHERE id=?",
                ("interrupted" if interrupted else "completed", encoded(result), order["id"]),
            )
            oid = R.record_observation(
                c,
                order["worker_id"],
                at,
                "本人共同研究的检验实际结算",
                {
                    "kind": "work_measurement",
                    "proposal_id": order["proposal_id"],
                    "version": order["version"],
                    "task_key": order["task_key"],
                    **result,
                },
            )
            if not interrupted:
                c.execute("INSERT INTO collective_assay_results VALUES (?,?)", (order["id"], oid))
            return
        if science_work.enabled(c, order_world(c, order)):
            outputs = science_work.process_work(c, order_world(c, order), states[0], task, hours, tuple(states[1:]))
        else:
            outputs = process(states[0], task["operation"], task["settings"], hours, auxiliaries=tuple(states[1:]))
        source = c.execute("SELECT * FROM item_instances WHERE id=?", (input_ids[0],)).fetchone()
        specs = [
            {
                "item_type_id": source["item_type_id"],
                "quantity": source["quantity"],
                "state": outputs[0],
            }
        ]
        if len(outputs) > 1:
            waste = "collective-waste:" + source["world_id"]
            c.execute(
                "INSERT OR IGNORE INTO item_types(id,name,category,stack_limit,slot_size,usable) VALUES (?,'处理副产物','material',1000000,1,0)",
                (waste,),
            )
            specs.extend({"item_type_id": waste, "quantity": 1, "state": x} for x in outputs[1:])
        ids = M.transform(
            c,
            input_ids,
            specs,
            container_type="ground",
            container_id=task["_location"],
            owner_id=order["worker_id"],
        )
        from world_engine.collective_audit import record_processing

        record_processing(
            c, input_ids, ids, "work", order["id"], task["operation"], task["settings"], hours
        )
        status = "interrupted" if interrupted else "completed"
        c.execute(
            "UPDATE collective_work_orders SET status=?,outputs_json=?,result_json=? WHERE id=?",
            (
                status,
                encoded(ids),
                encoded({"actual_hours": hours, "outputs": ids, "completed": not interrupted}),
                order["id"],
            ),
        )
        if task.get("_apparatus_location"):
            place = task["_apparatus_location"]
            resources.return_tool(c, task["apparatus_id"], place, task["_location"], wear=1)
        resources.finish_supplies(
            c,
            task.get("_supply_ids", []),
            task["_location"],
            "spent-supplies:" + order["id"],
            consumed=hours > 0,
        )
        for person in c.execute(
            "SELECT * FROM collective_work_people WHERE order_id=?", (order["id"],)
        ).fetchall():
            if interrupted:
                LifeActivityService.interrupt(
                    c, person["actor_id"], at, "共同操作中有人中断，停止本批工作"
                )
            R.record_observation(
                c,
                person["actor_id"],
                at,
                "本人实际参与的共同工作已结算",
                {
                    "kind": "work_result",
                    "proposal_id": order["proposal_id"],
                    "version": order["version"],
                    "task_key": order["task_key"],
                    "operation": task["operation"],
                    "settings": task["settings"],
                    "actual_hours": hours,
                    "outputs": ids,
                    "status": status,
                },
            )

    @classmethod
    def tick(cls, c, world_id, at):
        rows = c.execute(
            "SELECT w.* FROM collective_work_orders w JOIN collective_proposals p ON p.id=w.proposal_id "
            "WHERE p.world_id=? AND w.status IN ('queued','working') ORDER BY w.rowid",
            (world_id,),
        ).fetchall()
        for order in rows:
            task = json.loads(order["spec_json"])
            if order["status"] == "queued":
                if any(
                    c.execute(
                        "SELECT 1 FROM collective_work_orders WHERE proposal_id=? AND version=? AND task_key=? AND status='completed'",
                        (order["proposal_id"], order["version"], key),
                    ).fetchone()
                    is None
                    for key in task["depends_on"]
                ):
                    continue
                try:
                    with material_transaction(c):
                        cls._start(c, order, task, at)
                except ValueError as exc:
                    message = str(exc)
                    if "当前活动" in message or "先结束" in message or "被其他工作占用" in message:
                        continue
                    c.execute(
                        "UPDATE collective_work_orders SET status='blocked',result_json=? WHERE id=?",
                        (encoded({"error": message}), order["id"]),
                    )
                    R.record_observation(
                        c,
                        order["worker_id"],
                        at,
                        "本人同意的工作实际无法开始",
                        {"kind": "blocked", "task_key": order["task_key"], "reason": message},
                    )
                continue
            people = c.execute(
                "SELECT a.* FROM collective_work_people p JOIN character_life_activities a ON a.id=p.activity_id WHERE p.order_id=?",
                (order["id"],),
            ).fetchall()
            interrupted = any(x["status"] not in {"running", "completed"} for x in people)
            if interrupted or (
                all(x["status"] == "completed" for x in people) and at >= from_iso(order["due_at"])
            ):
                effective = at
                if interrupted:
                    effective = min(
                        from_iso(x["finished_world_time"])
                        for x in people
                        if x["status"] not in {"running", "completed"}
                    )
                if not interrupted:
                    if task["operation"] == "assay":
                        reader = R.actor(c, order["worker_id"])
                        if (
                            reader["health"] < 40
                            or reader["current_room_id"]
                            or (reader["current_location_id"] or reader["location_id"])
                            != task["_location"]
                            or LifeActivityService.running(c, reader["id"])
                            or c.execute(
                                "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",
                                (reader["id"],),
                            ).fetchone()
                        ):
                            continue
                        effective = at
                    else:
                        effective = from_iso(order["due_at"])
                cls._finish(c, order, task, effective, interrupted=interrupted)
        proposals = c.execute(
            "SELECT id,current_version FROM collective_proposals WHERE world_id=? AND status='executing'",
            (world_id,),
        ).fetchall()
        for p in proposals:
            statuses = {
                x[0]
                for x in c.execute(
                    "SELECT status FROM collective_work_orders WHERE proposal_id=? AND version=?",
                    (p["id"], p["current_version"]),
                )
            }
            if statuses == {"completed"}:
                c.execute(
                    "UPDATE collective_proposals SET status='completed' WHERE id=?", (p["id"],)
                )
            elif statuses & {"blocked", "interrupted"} and not statuses & {"working"}:
                c.execute("UPDATE collective_proposals SET status='failed' WHERE id=?", (p["id"],))
