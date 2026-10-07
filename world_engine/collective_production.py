"""哥哥，种植和收获需要人物亲自工作，作物从有限环境储量获得物质。"""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

from world_engine.action_checks import CheckService
from world_engine.collective_research import CollectiveResearchService as R
from world_engine.collective_resources import accessible
from world_engine.life import LifeActivityService
from world_engine.material_physics import advance, fraction
from world_engine.materials import MaterialService as M
from world_engine.materials import encoded, material_transaction
from world_engine.repository import from_iso, to_iso


class ProductionService:
    @staticmethod
    def integrate_growth(plant, soil, source, target, hours, *, accessible=True):
        # 哥哥，根系吸收、盐返溶和活力在同一段时间内共同积分，不用步初活力乘整段。
        wetness = soil.water / max(0.001, soil.mass + soil.water)
        acid = max(0, 7 - soil.ph)
        ka, kb = wetness * (0.0005 + acid * 0.003), wetness * (0.000001 + acid * 0.002)

        def derivative(t, values, growing):
            grown, uptake, vitality = values
            mobile = max(
                0,
                soil.mobile
                + soil.adsorbed * (-math.expm1(-ka * t))
                + soil.bound * (-math.expm1(-kb * t))
                - uptake,
            )
            concentration = mobile / max(1e-9, soil.mass)
            stress = concentration * 200
            rate = target / 2160 * max(0, vitality) / (1 + stress) if accessible and growing else 0
            return (rate, rate * concentration * 0.15, -stress / 2160 * vitality)

        def integrate_step(t, values, step, growing):
            k1 = derivative(t, values, growing)
            k2 = derivative(
                t + step / 2,
                tuple(x + step * d / 2 for x, d in zip(values, k1, strict=True)),
                growing,
            )
            k3 = derivative(
                t + step / 2,
                tuple(x + step * d / 2 for x, d in zip(values, k2, strict=True)),
                growing,
            )
            k4 = derivative(
                t + step, tuple(x + step * d for x, d in zip(values, k3, strict=True)), growing
            )
            return tuple(
                x + step * (a + 2 * b + 2 * d + e) / 6
                for x, a, b, d, e in zip(values, k1, k2, k3, k4, strict=True)
            )

        values = (0.0, 0.0, plant.viability)
        t = 0.0
        while t < hours - 1e-10:
            step = min(0.05, hours - t)
            growing = values[0] < source.mass - 1e-12
            following = integrate_step(t, values, step, growing)
            if growing and following[0] > source.mass:
                # 哥哥，找到实际来源耗尽的时刻；之后不再用不存在的生长量吸盐。
                low, high = 0.0, step
                for _ in range(40):
                    middle = (low + high) / 2
                    if integrate_step(t, values, middle, True)[0] > source.mass:
                        high = middle
                    else:
                        low = middle
                step = (low + high) / 2
                following = integrate_step(t, values, step, True)
                following = (source.mass, following[1], following[2])
            values = following
            t += step
        grown = min(source.mass, max(0, values[0]))
        aged_soil = advance(soil, hours)
        uptake = min(aged_soil.mobile, max(0, values[1]))
        return (
            grown,
            uptake,
            min(1, max(0, values[2])),
            replace(aged_soil, mobile=aged_soil.mobile - uptake),
        )

    @staticmethod
    def growth_states(plant, soil, source, target, hours, *, accessible=True):
        grown, uptake, viability, new_soil = ProductionService.integrate_growth(
            plant, soil, source, target, hours, accessible=accessible
        )
        incoming = fraction(source, grown / source.mass) if source.mass > 0 else fraction(source, 0)
        actual = replace(
            plant,
            mass=plant.mass + incoming.mass,
            water=plant.water + incoming.water,
            nutrition=plant.nutrition + incoming.nutrition,
            particles=plant.particles + incoming.particles,
            mobile=plant.mobile + incoming.mobile + uptake,
            adsorbed=plant.adsorbed + incoming.adsorbed,
            bound=plant.bound + incoming.bound,
            viability=viability,
            age_hours=plant.age_hours + hours,
        )
        remainder = fraction(source, 1 - grown / source.mass) if source.mass > 0 else source
        return (actual, new_soil, remainder), grown

    @staticmethod
    def _schedule(c, actor, command, at):
        if command.repeat_count <= 1:
            return
        if command.repeat_interval_hours < command.hours:
            raise ValueError("重复劳动不能要求同一个身体重叠工作")
        spec = command.model_dump(mode="json")
        spec["repeat_count"] = 1
        spec["result_key"] = None
        c.execute(
            "INSERT INTO collective_recurring_jobs(id,world_id,actor_id,spec_json,next_due_at,remaining) VALUES (?,?,?,?,?,?)",
            (
                str(uuid4()),
                actor["world_id"],
                actor["id"],
                encoded(spec),
                to_iso(at + timedelta(hours=command.repeat_interval_hours)),
                command.repeat_count - 1,
            ),
        )

    @classmethod
    def gather(cls, c, actor, at, *, food_only=False, item_type_ids=None, return_event=False):
        from world_engine.collective_types import CollectiveCommand

        region = c.execute(
            "SELECT id FROM collective_regions WHERE world_id=? AND manager_id=? AND location_id=?",
            (actor["world_id"], actor["id"], actor["current_location_id"] or actor["location_id"]),
        ).fetchone()
        if region is None:
            return False
        for item in c.execute(
            "SELECT i.* FROM item_instances i JOIN material_batches b ON b.item_id=i.id JOIN item_types t ON t.id=i.item_type_id WHERE i.world_id=? AND i.container_type='natural_resource' AND i.container_id=? AND i.owner_character_id=? AND (t.category='food' OR ?=0) ORDER BY i.rowid",
            (
                actor["world_id"],
                actor["current_location_id"] or actor["location_id"],
                actor["id"],
                int(food_only),
            ),
        ).fetchall():
            if item_type_ids and item["item_type_id"] not in item_type_ids:
                continue
            try:
                jid = cls.start(
                    c,
                    actor["id"],
                    CollectiveCommand(
                        op="gather", batch_id=item["id"], region_id=region[0], quantity=1, hours=1
                    ),
                    at,
                )
            except ValueError:
                continue
            oid = R.record_observation(
                c,
                actor["id"],
                at,
                "本人开始从真实有限来源采集",
                {"kind": "gather_started", "job_id": jid, "source_batch_id": item["id"]},
            )
            return (
                c.execute(
                    "SELECT source_event_id FROM collective_observations WHERE id=?", (oid,)
                ).fetchone()[0]
                if return_event
                else True
            )
        return False

    @staticmethod
    def _region(c, actor, rid, operation):
        region = c.execute(
            "SELECT * FROM collective_regions WHERE id=? AND world_id=?",
            (rid, actor["world_id"]),
        ).fetchone()
        if (
            region is None
            or (
                region["manager_id"] != actor["id"]
                and not c.execute(
                    "SELECT 1 FROM collective_region_permissions WHERE region_id=? AND actor_id=? AND operation=?",
                    (rid, actor["id"], operation),
                ).fetchone()
            )
            or region["location_id"] != (actor["current_location_id"] or actor["location_id"])
            or actor["current_room_id"]
        ):
            raise ValueError("没有本地地区的管理权限，或尚未实际抵达")
        return region

    @staticmethod
    def _material(c, actor, iid, count):
        item = c.execute(
            "SELECT * FROM item_instances WHERE id=? AND owner_character_id=? AND world_id=?",
            (iid, actor["id"], actor["world_id"]),
        ).fetchone()
        if (
            item is None
            or M.state(c, iid) is None
            or item["quantity"] < count
            or not accessible(c, item, actor)
        ):
            raise ValueError("没有在现场可实际使用的自有材料")
        return item

    @classmethod
    def ration(cls, c, actor_id, rid, iid, quantity, at):
        actor = R.actor(c, actor_id)
        region = cls._region(c, actor, rid, "ration")
        item = cls._material(c, actor, iid, quantity)
        state = M.state(c, iid)
        if (
            state.carrier not in {"grain", "fish", "vegetable", "fruit", "mushroom"}
            or state.nutrition <= 0
        ):
            raise ValueError("这份材料没有可食物来源，不能把种子或环境储量算作口粮")
        output = M.move(c, item, quantity, "region_food", rid, actor_id)
        R.record_observation(
            c,
            actor_id,
            at,
            "本人将实物交付本地区口粮账户",
            {
                "kind": "ration_delivery",
                "region_id": region["id"],
                "batch_id": output,
                "quantity": quantity,
            },
        )
        return output

    @classmethod
    def start(cls, c, actor_id, command, at, program_id=None):
        actor = R.actor(c, actor_id)
        if command.op == "ration":
            return cls.ration(
                c, actor_id, command.region_id, command.batch_id, command.quantity, at
            )
        if command.op == "diet":
            cls._material(c, actor, command.batch_id, 1)
            if command.food_policy is None or not command.text.strip():
                raise ValueError("饮食安排需要本人选择与理由")
            c.execute(
                "INSERT INTO collective_food_decisions VALUES (?,?,?,?,?) ON CONFLICT(actor_id,batch_id) DO UPDATE SET policy=excluded.policy,reason=excluded.reason,decided_at=excluded.decided_at",
                (actor_id, command.batch_id, command.food_policy, command.text, to_iso(at)),
            )
            return command.batch_id
        if command.op in {"dispose", "maintain"}:
            return cls._start_waste(c, actor, command, at, program_id)
        LifeActivityService.assert_available(c, actor_id)
        if actor["satiety"] <= 10 or actor["energy"] < 15 or actor["health"] < 40:
            raise ValueError("身体需要先用饭或休息")
        if command.op == "harvest":
            crop = c.execute(
                "SELECT * FROM collective_crops WHERE id=? AND actor_id=? AND status='mature'",
                (command.crop_id, actor_id),
            ).fetchone()
            if crop is None or at < from_iso(crop["due_at"]):
                raise ValueError("作物还没有完成90日生长与成熟")
            region = cls._region(c, actor, crop["region_id"], command.op)
            data = command.model_dump(mode="json")
            inputs = []
        else:
            region = cls._region(c, actor, command.region_id, command.op)
            item = cls._material(c, actor, command.batch_id, command.quantity)
            state = fraction(M.state(c, item["id"]), command.quantity / item["quantity"])
            if command.op == "land":
                if state.carrier != "soil" or state.mass + 1e-8 < region["baseline_soil_mass"]:
                    raise ValueError("登记的耕作层必须有足够真实土壤质量")
            elif command.op == "water":
                if state.carrier != "water" or state.water <= 0:
                    raise ValueError("登记的下游水源须是实际水体")
            elif command.op == "plant":
                if (
                    state.carrier != "seed"
                    or state.mass <= 0
                    or CheckService.skill(c, actor, "农作") < 40
                ):
                    raise ValueError("播种须有真实种子与农作能力")
                if c.execute(
                    "SELECT 1 FROM collective_crops WHERE region_id=? AND status IN ('growing','mature','harvesting')",
                    (region["id"],),
                ).fetchone():
                    raise ValueError("本地区代表性田地已有在地粮作，不能重叠种植")
            elif command.op == "gather":
                if item["container_type"] != "natural_resource" or state.carrier not in {
                    "fish",
                    "fruit",
                    "vegetable",
                    "mushroom",
                }:
                    raise ValueError("采集须来自本地有限自然来源")
            else:
                raise ValueError("未登记的生产动作")
            inputs = [item]
            data = command.model_dump(mode="json")
        if command.hours > 12 or command.hours < 1:
            raise ValueError("田间操作的每段实际劳动须为1至12小时")
        jid = str(uuid4())
        with material_transaction(c):
            aid = LifeActivityService.start(c, actor, at, "research", round(command.hours * 60))
            material_ids = [
                M.move(c, item, command.quantity, "collective_domain", jid, actor_id)
                for item in inputs
            ]
            if command.op == "harvest":
                c.execute(
                    "UPDATE collective_crops SET status='harvesting' WHERE id=?", (command.crop_id,)
                )
            c.execute(
                "INSERT INTO collective_domain_jobs(id,world_id,actor_id,activity_id,program_id,op,spec_json,inputs_json,started_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    jid,
                    actor["world_id"],
                    actor_id,
                    aid,
                    program_id,
                    command.op,
                    encoded(data),
                    encoded(material_ids),
                    to_iso(at),
                ),
            )
            cls._schedule(c, actor, command, at)
        return jid

    @classmethod
    def _start_waste(cls, c, actor, command, at, program_id):
        LifeActivityService.assert_available(c, actor["id"])
        if actor["health"] < 40 or actor["energy"] < 15 or actor["satiety"] <= 10:
            raise ValueError("废物操作前需要先恢复身体")
        region = cls._region(c, actor, command.region_id, command.op)
        inputs = []
        data = command.model_dump(mode="json")
        if command.site_id:
            site = c.execute(
                "SELECT * FROM collective_waste_sites WHERE id=? AND region_id=? AND owner_id=?",
                (command.site_id, region["id"], actor["id"]),
            ).fetchone()
            if site is None:
                raise ValueError("没有该本地封存场所的实际权限")
        else:
            site = None
        if command.op == "maintain" and site is None:
            raise ValueError("维护须指定实际封存场所")
        liner = None
        if command.auxiliary_ids:
            if len(command.auxiliary_ids) != 1:
                raise ValueError("每次封存操作须明确一份实际容器或衬层")
            iid = command.auxiliary_ids[0]
            count = command.auxiliary_quantities.get(iid, 1)
            lining = cls._material(c, actor, iid, count)
            liner = c.execute(
                "SELECT * FROM collective_liners WHERE item_type_id=?", (lining["item_type_id"],)
            ).fetchone()
            if liner is None:
                raise ValueError("这份实物不是登记的封存容器或衬层")
            inputs.append((lining, count))
            data["_liner_type"] = liner["item_type_id"]
            data["_capacity"] = count * liner["capacity_per_unit"]
        if command.op == "maintain":
            if (
                liner is None
                or liner["item_type_id"] != site["liner_type_id"]
                or command.hours < liner["maintenance_hours"]
            ):
                raise ValueError("维护需要适当的真实耗材与劳动时间")
        else:
            item = cls._material(c, actor, command.batch_id, command.quantity)
            state = fraction(M.state(c, item["id"]), command.quantity / item["quantity"])
            load = state.mass + state.water + state.particles
            used = (
                sum(
                    M.state(c, i[0]).mass + M.state(c, i[0]).water + M.state(c, i[0]).particles
                    for i in c.execute(
                        "SELECT id FROM item_instances WHERE container_type='waste_site' AND container_id=?",
                        (site["id"],),
                    )
                )
                if site
                else 0
            )
            capacity = site["capacity"] if site else (data.get("_capacity", 0))
            if load + used > capacity + 1e-8:
                raise ValueError("实际容器封存容量不足")
            if load > command.hours * 200:
                raise ValueError("废物转运量超过这段人工劳动的吞吐量")
            if site is None and liner is None:
                raise ValueError("新封存场所需要实际容器")
            inputs.insert(0, (item, command.quantity))
        if not 1 <= command.hours <= 12:
            raise ValueError("封存劳动每段需要1至12小时")
        jid = str(uuid4())
        with material_transaction(c):
            aid = LifeActivityService.start(c, actor, at, "research", round(command.hours * 60))
            ids = [M.move(c, i, n, "collective_domain", jid, actor["id"]) for i, n in inputs]
            c.execute(
                "INSERT INTO collective_domain_jobs(id,world_id,actor_id,activity_id,program_id,op,spec_json,inputs_json,started_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    jid,
                    actor["world_id"],
                    actor["id"],
                    aid,
                    program_id,
                    command.op,
                    encoded(data),
                    encoded(ids),
                    to_iso(at),
                ),
            )
            cls._schedule(c, actor, command, at)
        return jid

    @classmethod
    def _complete(cls, c, job, at, completed):
        data = json.loads(job["spec_json"])
        inputs = json.loads(job["inputs_json"])
        activity = c.execute(
            "SELECT * FROM character_life_activities WHERE id=?", (job["activity_id"],)
        ).fetchone()
        output = []
        crop_id = None
        if not completed:
            for iid in inputs:
                item = c.execute("SELECT * FROM item_instances WHERE id=?", (iid,)).fetchone()
                M.move(
                    c, item, item["quantity"], "ground", activity["location_id"], job["actor_id"]
                )
            if job["op"] == "harvest":
                c.execute(
                    "UPDATE collective_crops SET status='mature' WHERE id=?", (data["crop_id"],)
                )
        elif job["op"] == "plant":
            region = c.execute(
                "SELECT * FROM collective_regions WHERE id=?", (data["region_id"],)
            ).fetchone()
            seed = M.state(c, inputs[0])
            soil = M.state(c, region["soil_id"])
            if soil is None:
                raise ValueError("实际耕作层已丢失，不能继续播种")
            crop_id = str(uuid4())
            item = c.execute("SELECT * FROM item_instances WHERE id=?", (inputs[0],)).fetchone()
            M.move(c, item, item["quantity"], "collective_crop", crop_id, job["actor_id"])
            # 哥哥，这是登记粮作品种的普通播种量/产量简化，不识别灾难应对步骤。
            target = min(seed.mass * 50, soil.mass * 0.8)
            c.execute(
                "INSERT INTO collective_crops(id,world_id,region_id,actor_id,crop_item_id,planted_at,due_at,last_grown_at,target_growth_mass) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    crop_id,
                    job["world_id"],
                    region["id"],
                    job["actor_id"],
                    item["id"],
                    to_iso(at),
                    to_iso(at + timedelta(days=90)),
                    to_iso(at),
                    target,
                ),
            )
        elif job["op"] == "land":
            item = c.execute("SELECT * FROM item_instances WHERE id=?", (inputs[0],)).fetchone()
            M.move(c, item, item["quantity"], "ground", activity["location_id"], job["actor_id"])
            c.execute(
                "UPDATE collective_regions SET soil_id=? WHERE id=?",
                (item["id"], data["region_id"]),
            )
            output = [item["id"]]
        elif job["op"] == "water":
            item = c.execute("SELECT * FROM item_instances WHERE id=?", (inputs[0],)).fetchone()
            M.move(c, item, item["quantity"], "ground", activity["location_id"], job["actor_id"])
            c.execute(
                "UPDATE collective_regions SET water_id=? WHERE id=?",
                (item["id"], data["region_id"]),
            )
            output = [item["id"]]
        elif job["op"] == "gather":
            item = c.execute("SELECT * FROM item_instances WHERE id=?", (inputs[0],)).fetchone()
            M.move(c, item, item["quantity"], "ground", activity["location_id"], job["actor_id"])
            output = [item["id"]]
        elif job["op"] in {"dispose", "maintain"}:
            from world_engine.inventory import InventoryService

            site_id = data.get("site_id")
            if site_id is None:
                site_id = str(uuid4())
                liner = c.execute(
                    "SELECT * FROM collective_liners WHERE item_type_id=?", (data["_liner_type"],)
                ).fetchone()
                c.execute(
                    "INSERT INTO collective_waste_sites VALUES (?,?,?,?,?,?,?,?,?,?,?,0)",
                    (
                        site_id,
                        job["world_id"],
                        data["region_id"],
                        job["actor_id"],
                        data["_capacity"],
                        liner["item_type_id"],
                        liner["leak_fraction"],
                        to_iso(at),
                        to_iso(at),
                        to_iso(at),
                        liner["observation_window_hours"],
                    ),
                )
            if job["op"] == "dispose":
                waste = c.execute(
                    "SELECT * FROM item_instances WHERE id=?", (inputs[0],)
                ).fetchone()
                M.move(c, waste, waste["quantity"], "waste_site", site_id, job["actor_id"])
                output = [waste["id"]]
                lining_ids = inputs[1:]
            else:
                lining_ids = inputs
                c.execute(
                    "UPDATE collective_waste_sites SET last_maintained_at=? WHERE id=?",
                    (to_iso(at), site_id),
                )
            for iid in lining_ids:
                liner = c.execute("SELECT * FROM item_instances WHERE id=?", (iid,)).fetchone()
                InventoryService.consume(
                    c, liner, liner["quantity"], destination="lining-site:" + site_id
                )
            data["_site_id"] = site_id
        elif job["op"] == "harvest":
            crop = c.execute(
                "SELECT * FROM collective_crops WHERE id=?", (data["crop_id"],)
            ).fetchone()
            state = M.state(c, crop["crop_item_id"])
            seed = fraction(state, crop["seed_retention"])
            food = fraction(state, 1 - crop["seed_retention"])
            food_type = "collective-grain:" + job["world_id"]
            seed_type = "collective-seed:" + job["world_id"]
            for tid, name, category in (
                (food_type, "实收粮作", "food"),
                (seed_type, "下轮留种", "material"),
            ):
                c.execute(
                    "INSERT OR IGNORE INTO item_types(id,name,category,stack_limit,slot_size) VALUES (?,?,?,1000000,1)",
                    (tid, name, category),
                )
            output = M.transform(
                c,
                [crop["crop_item_id"]],
                [
                    {
                        "item_type_id": food_type,
                        "quantity": max(1, math.ceil(food.mass)),
                        "state": replace(food, carrier="grain"),
                    },
                    {
                        "item_type_id": seed_type,
                        "quantity": max(1, math.ceil(seed.mass)),
                        "state": replace(seed, carrier="seed"),
                    },
                ],
                container_type="ground",
                container_id=activity["location_id"],
                owner_id=job["actor_id"],
            )
            c.execute(
                "UPDATE collective_crops SET status='harvested',harvested_at=?,food_id=?,seed_id=?,yield_nutrition=?,output_seed_viability=? WHERE id=?",
                (to_iso(at), output[0], output[1], food.nutrition, seed.viability, crop["id"]),
            )
            crop_id = crop["id"]
        result = {
            "kind": "production_result",
            "op": job["op"],
            "completed": completed,
            "outputs": output,
            "crop_id": crop_id,
            "site_id": data.get("_site_id"),
        }
        c.execute(
            "UPDATE collective_domain_jobs SET status=?,result_json=? WHERE id=?",
            ("completed" if completed else "interrupted", encoded(result), job["id"]),
        )
        R.record_observation(c, job["actor_id"], at, "本人实际完成或停止一段生产劳动", result)
        if job["program_id"]:
            if not completed:
                c.execute(
                    "UPDATE collective_programs SET status='interrupted' WHERE id=?",
                    (job["program_id"],),
                )
            elif data.get("result_key"):
                bindings = json.loads(
                    c.execute(
                        "SELECT bindings_json FROM collective_programs WHERE id=?",
                        (job["program_id"],),
                    ).fetchone()[0]
                )
                bindings[data["result_key"]] = crop_id or data.get("_site_id") or output
                c.execute(
                    "UPDATE collective_programs SET bindings_json=? WHERE id=?",
                    (encoded(bindings), job["program_id"]),
                )

    @staticmethod
    def _grow(c, crop, at):
        effective = min(at, from_iso(crop["due_at"]))
        hours = (effective - from_iso(crop["last_grown_at"])).total_seconds() / 3600
        if hours <= 0:
            return
        region = c.execute(
            "SELECT * FROM collective_regions WHERE id=?", (crop["region_id"],)
        ).fetchone()
        plant = M.state(c, crop["crop_item_id"])
        soil = M.state(c, region["soil_id"])
        source = M.state(c, region["growth_source_id"])
        if plant is None or soil is None or source is None:
            raise ValueError("作物的真实物质来源丢失")
        soil_item = c.execute(
            "SELECT container_type FROM item_instances WHERE id=?", (region["soil_id"],)
        ).fetchone()
        accessible = soil_item[0] in {"ground", "collective_land"}
        (actual, new_soil, remainder), source_mass = ProductionService.growth_states(
            plant, soil, source, crop["target_growth_mass"], hours, accessible=accessible
        )
        M.rebalance(
            c,
            {
                crop["crop_item_id"]: actual,
                region["soil_id"]: new_soil,
                region["growth_source_id"]: remainder,
            },
            operation="crop_growth",
        )
        ledger = c.execute(
            "SELECT id FROM material_ledger WHERE operation='crop_growth' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0]
        c.execute(
            "INSERT INTO collective_process_traces VALUES (?,?,?,?,?,?)",
            (
                ledger,
                "growth",
                crop["id"],
                "crop_growth",
                encoded({"target": crop["target_growth_mass"], "accessible": accessible}),
                hours,
            ),
        )
        status = "mature" if effective >= from_iso(crop["due_at"]) else "growing"
        c.execute(
            "UPDATE collective_crops SET last_grown_at=?,grown_mass=grown_mass+?,status=? WHERE id=?",
            (to_iso(effective), source_mass, status, crop["id"]),
        )
        farmer = R.actor(c, crop["actor_id"])
        if (
            status == "mature"
            and farmer["health"] > 0
            and not farmer["current_room_id"]
            and not c.execute(
                "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",
                (farmer["id"],),
            ).fetchone()
            and (farmer["current_location_id"] or farmer["location_id"]) == region["location_id"]
        ):
            R.record_observation(
                c,
                crop["actor_id"],
                effective,
                "田间粮作完成了登记的90日生长周期，等待实际收获",
                {"kind": "crop_mature", "crop_id": crop["id"], "elapsed_days": 90},
            )

    @classmethod
    def tick(cls, c, world_id, at):
        # 哥哥，先结束旧土地上的生长区间，再结算边界上完成的土地更换。
        for crop in c.execute(
            "SELECT * FROM collective_crops WHERE world_id=? AND status='growing'", (world_id,)
        ).fetchall():
            with material_transaction(c):
                cls._grow(c, crop, at)
        for job in c.execute(
            "SELECT j.*,a.status activity_status,a.finished_world_time FROM collective_domain_jobs j JOIN character_life_activities a ON a.id=j.activity_id WHERE j.world_id=? AND j.status='working'",
            (world_id,),
        ).fetchall():
            if job["activity_status"] == "running":
                continue
            with material_transaction(c):
                cls._complete(
                    c,
                    job,
                    from_iso(job["finished_world_time"]),
                    job["activity_status"] == "completed",
                )
            if job["program_id"] and job["activity_status"] == "completed":
                try:
                    with material_transaction(c):
                        R._drive(c, job["program_id"], at)
                except ValueError as exc:
                    c.execute(
                        "UPDATE collective_programs SET status='blocked',error=? WHERE id=?",
                        (str(exc), job["program_id"]),
                    )
                    R.record_observation(
                        c,
                        job["actor_id"],
                        at,
                        "下一段生产程序实际无法开始",
                        {"kind": "blocked", "reason": str(exc)},
                    )
        for crop in c.execute(
            "SELECT * FROM collective_crops WHERE world_id=? AND status='growing'", (world_id,)
        ).fetchall():
            with material_transaction(c):
                cls._grow(c, crop, at)
        from world_engine.collective_types import CollectiveCommand

        for routine in c.execute(
            "SELECT * FROM collective_recurring_jobs WHERE world_id=? AND status='active' AND next_due_at<=? ORDER BY next_due_at,rowid",
            (world_id, to_iso(at)),
        ).fetchall():
            actor = R.actor(c, routine["actor_id"])
            if (
                LifeActivityService.running(c, actor["id"])
                or actor["energy"] < 15
                or actor["satiety"] <= 10
                or actor["health"] < 40
            ):
                continue
            try:
                with material_transaction(c):
                    command = CollectiveCommand.model_validate_json(routine["spec_json"])
                    cls.start(c, actor["id"], command, at)
                    remaining = routine["remaining"] - 1
                    c.execute(
                        "UPDATE collective_recurring_jobs SET remaining=?,status=?,next_due_at=? WHERE id=?",
                        (
                            remaining,
                            "completed" if remaining == 0 else "active",
                            to_iso(
                                from_iso(routine["next_due_at"])
                                + timedelta(hours=command.repeat_interval_hours)
                            ),
                            routine["id"],
                        ),
                    )
            except ValueError as exc:
                c.execute(
                    "UPDATE collective_recurring_jobs SET status='blocked',error=? WHERE id=?",
                    (str(exc), routine["id"]),
                )
                R.record_observation(
                    c,
                    actor["id"],
                    at,
                    "本人事先安排的重复劳动遇到实际障碍",
                    {"kind": "blocked", "routine_id": routine["id"], "reason": str(exc)},
                )
