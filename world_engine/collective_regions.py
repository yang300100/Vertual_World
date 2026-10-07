"""哥哥，身体后果和恢复指标只读取实际摄入与库存；不会替人物挑安全方案。"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

from world_engine.food import FoodService
from world_engine.material_physics import fraction, salt_total
from world_engine.materials import MaterialService as M
from world_engine.materials import encoded, material_transaction
from world_engine.repository import from_iso, to_iso


class RegionService:
    @staticmethod
    def starve(c, actor_id, hours, at):
        if hours <= 0:
            return 0
        actor = c.execute(
            "SELECT world_id,health FROM characters WHERE id=?", (actor_id,)
        ).fetchone()
        if actor["health"] <= 0:
            return 0
        old = c.execute(
            "SELECT * FROM collective_body_stress WHERE actor_id=?", (actor_id,)
        ).fetchone()
        cumulative = (old["starvation_hours"] if old else 0) + hours
        total = (old["damage_residual"] if old else 0) + hours
        damage = int(total + 1e-9)
        c.execute(
            "INSERT INTO collective_body_stress VALUES (?,?,?) ON CONFLICT(actor_id) DO UPDATE SET starvation_hours=excluded.starvation_hours,damage_residual=excluded.damage_residual",
            (actor_id, cumulative, max(0, total - damage)),
        )
        health = max(0, actor["health"] - damage)
        c.execute("UPDATE characters SET health=? WHERE id=?", (health, actor_id))
        if health == 0:
            c.execute(
                "INSERT OR IGNORE INTO collective_deaths VALUES (?,?,?,NULL,1,?,?)",
                (str(uuid4()), actor["world_id"], actor_id, to_iso(at), "starvation"),
            )
        elif cumulative >= 5 and (old is None or old["starvation_hours"] < 5):
            from world_engine.collective_research import CollectiveResearchService as R

            if c.execute(
                "SELECT 1 FROM collective_people WHERE character_id=?", (actor_id,)
            ).fetchone():
                R.record_observation(
                    c,
                    actor_id,
                    at,
                    "持续断粮已经使本人身体衰弱",
                    {"kind": "starvation", "hours": cumulative, "health": health},
                )
        return min(actor["health"], damage)

    @staticmethod
    def uncontained_waste(c, world_id):
        registered = {
            r[k]
            for r in c.execute(
                "SELECT soil_id,water_id FROM collective_regions WHERE world_id=?", (world_id,)
            )
            for k in ("soil_id", "water_id")
        }
        result = []
        for item in c.execute(
            "SELECT i.* FROM item_instances i JOIN material_batches b ON b.item_id=i.id WHERE i.world_id=?",
            (world_id,),
        ):
            state = M.state(c, item["id"])
            waste = (
                state.carrier in {"lab_waste", "dust", "vapor", "sludge", "spent_media"}
                or (state.carrier in {"water", "soil"} and item["id"] not in registered)
                or item["item_type_id"]
                in {"collective-waste:" + world_id, "collective-lab-waste:" + world_id}
            )
            if not waste or (salt_total(state) <= 1e-9 and state.particles <= 1e-9):
                continue
            # 哥哥，只有登记过的真实封存设施能承担污染；仓储、背包和在途货物仍是未封存。
            if (
                item["container_type"] == "waste_site"
                and c.execute(
                    "SELECT 1 FROM collective_waste_sites WHERE id=? AND world_id=?",
                    (item["container_id"], world_id),
                ).fetchone()
            ):
                continue
            result.append((item, state))
        return result

    @staticmethod
    def _loose_waste(c, region):
        result = []
        for item in c.execute(
            "SELECT i.* FROM item_instances i JOIN material_batches b ON b.item_id=i.id LEFT JOIN characters a ON i.container_type='character_inventory' AND a.id=i.container_id WHERE (i.container_type='ground' AND i.container_id=?) OR (i.container_type='character_inventory' AND COALESCE(a.current_location_id,a.location_id)=? AND NOT EXISTS (SELECT 1 FROM character_movements m WHERE m.character_id=a.id AND m.status='moving'))",
            (region["location_id"], region["location_id"]),
        ):
            state = M.state(c, item["id"])
            if (
                state.carrier in {"lab_waste", "dust", "vapor", "sludge", "spent_media"}
                or (state.carrier == "water" and item["id"] != region["water_id"])
                or (state.carrier == "soil" and item["id"] != region["soil_id"])
                or item["item_type_id"]
                in {
                    "collective-waste:" + region["world_id"],
                    "collective-lab-waste:" + region["world_id"],
                }
            ) and (salt_total(state) > 1e-9 or state.particles > 1e-9):
                result.append((item, state))
        return result

    @classmethod
    def _runoff(cls, c, world_id, at):
        for region in c.execute(
            "SELECT * FROM collective_regions WHERE world_id=?", (world_id,)
        ).fetchall():
            last = c.execute(
                "SELECT last_at FROM collective_runoff_times WHERE region_id=?", (region["id"],)
            ).fetchone()
            hours = max(
                0, (at - from_iso(last[0] if last else region["started_at"])).total_seconds() / 3600
            )
            if hours <= 0:
                continue
            if M.state(c, region["water_id"]) is None or M.state(c, region["soil_id"]) is None:
                continue
            ratio = -math.expm1(-hours * 0.001)
            for item, state in cls._loose_waste(c, region):
                water = M.state(c, region["water_id"])
                soil = M.state(c, region["soil_id"])
                dose = state.mobile * ratio
                dust = state.particles * ratio
                M.rebalance(
                    c,
                    {
                        item["id"]: replace(
                            state, mobile=state.mobile - dose, particles=state.particles - dust
                        ),
                        region["water_id"]: replace(water, mobile=water.mobile + dose),
                        region["soil_id"]: replace(soil, particles=soil.particles + dust),
                    },
                    operation="loose_waste_runoff",
                )
                from world_engine.collective_audit import record_exchange_trace

                record_exchange_trace(
                    c, "loose_waste_runoff", region["id"], {"ratio": ratio}, hours
                )
            c.execute(
                "INSERT INTO collective_runoff_times VALUES (?,?) ON CONFLICT(region_id) DO UPDATE SET last_at=excluded.last_at",
                (region["id"], to_iso(at)),
            )

    @staticmethod
    def grant(c, grantor, rid, recipient, operations, reason, at):
        region = c.execute(
            "SELECT * FROM collective_regions WHERE id=? AND manager_id=?", (rid, grantor)
        ).fetchone()
        person = c.execute("SELECT * FROM characters WHERE id=?", (recipient,)).fetchone()
        allowed = {"ration", "plant", "harvest", "land", "water", "gather", "dispose", "maintain"}
        if region is None or person is None or person["world_id"] != region["world_id"]:
            raise ValueError("只有本地区管理者能够向同世界人物授予公共操作权限")
        manager = c.execute("SELECT * FROM characters WHERE id=?", (grantor,)).fetchone()
        if (
            manager["health"] <= 0
            or person["health"] <= 0
            or (manager["current_location_id"] or manager["location_id"])
            != (person["current_location_id"] or person["location_id"])
            or manager["current_room_id"] != person["current_room_id"]
            or c.execute(
                "SELECT 1 FROM character_movements WHERE character_id IN (?,?) AND status='moving'",
                (grantor, recipient),
            ).fetchone()
        ):
            raise ValueError("公共操作许可需要双方实际在现场交流；不能向远方人物瞬时传达")
        if not operations or set(operations) - allowed or not reason.strip():
            raise ValueError("地区权限需要明确操作与理由")
        for operation in set(operations):
            c.execute(
                "INSERT INTO collective_region_permissions VALUES (?,?,?,?,?,?) ON CONFLICT(region_id,actor_id,operation) DO UPDATE SET granted_by=excluded.granted_by,reason=excluded.reason,at=excluded.at",
                (rid, recipient, operation, grantor, reason, to_iso(at)),
            )
        from world_engine.collective_research import CollectiveResearchService as R

        R.record_observation(
            c,
            recipient,
            at,
            "本人获得具体地区操作权限",
            {
                "kind": "region_permission",
                "region_id": rid,
                "operations": sorted(set(operations)),
                "granted_by": grantor,
                "reason": reason,
            },
        )

    @staticmethod
    def _recover_lab_waste(c, world_id):
        sinks = c.execute(
            "SELECT s.* FROM material_sinks s LEFT JOIN collective_sink_recoveries r ON r.sink_id=s.id WHERE s.world_id=? AND r.sink_id IS NULL AND (s.destination LIKE 'lab-waste:%' OR s.destination LIKE 'spent-supplies:%')",
            (world_id,),
        ).fetchall()
        for sink in sinks:
            jid = sink["destination"].split(":", 1)[1]
            place = c.execute(
                "SELECT p.character_id actor_id,a.location_id FROM collective_jobs j JOIN collective_programs p ON p.id=j.program_id JOIN character_life_activities a ON a.id=j.activity_id WHERE j.id=?",
                (jid,),
            ).fetchone()
            if place is None:
                place = c.execute(
                    "SELECT w.worker_id actor_id,a.location_id FROM collective_work_orders w JOIN character_life_activities a ON a.id=w.activity_id WHERE w.id=?",
                    (jid,),
                ).fetchone()
            if place is None:
                continue
            tid = "collective-lab-waste:" + world_id
            c.execute(
                "INSERT OR IGNORE INTO item_types(id,name,category,stack_limit,slot_size,usable) VALUES (?,'实验残留废物','material',1000000,1,0)",
                (tid,),
            )
            M.recover_sink(
                c,
                sink["id"],
                item_type_id=tid,
                container_type="ground",
                container_id=place["location_id"],
                owner_id=place["actor_id"],
                carrier="lab_waste",
            )

    @staticmethod
    def _waste(c, world_id, at):
        for site in c.execute(
            "SELECT s.*,r.water_id FROM collective_waste_sites s JOIN collective_regions r ON r.id=s.region_id WHERE s.world_id=?",
            (world_id,),
        ).fetchall():
            hours = max(0, (at - from_iso(site["last_processed_at"])).total_seconds() / 3600)
            if hours <= 0:
                continue
            if M.state(c, site["water_id"]) is None:
                continue
            liner = c.execute(
                "SELECT * FROM collective_liners WHERE item_type_id=?", (site["liner_type_id"],)
            ).fetchone()
            interval = liner["maintenance_interval_hours"] if liner else 168
            overdue = max(
                0, (at - from_iso(site["last_maintained_at"])).total_seconds() / 3600 - interval
            )
            factor = site["leak_fraction"] * (1 + overdue / interval)
            items = c.execute(
                "SELECT * FROM item_instances WHERE container_type='waste_site' AND container_id=?",
                (site["id"],),
            ).fetchall()
            load = sum(
                M.state(c, i["id"]).mass + M.state(c, i["id"]).water + M.state(c, i["id"]).particles
                for i in items
            )
            loads = {
                i["id"]: {
                    k: getattr(M.state(c, i["id"]), k) for k in ("mass", "water", "particles")
                }
                for i in items
            }
            leaked = 0
            for item in items:
                state = M.state(c, item["id"])
                dose = state.mobile * (-math.expm1(-hours * factor)) + state.mobile * max(
                    0, 1 - site["capacity"] / max(1e-9, load)
                )
                dose = min(state.mobile, dose)
                target = M.state(c, site["water_id"])
                if target is None:
                    raise ValueError("废物实际下游水源丢失")
                M.rebalance(
                    c,
                    {
                        item["id"]: replace(state, mobile=state.mobile - dose),
                        site["water_id"]: replace(target, mobile=target.mobile + dose),
                    },
                    operation="waste_leak",
                )
                from world_engine.collective_audit import record_exchange_trace

                record_exchange_trace(
                    c,
                    "waste_leak",
                    site["id"],
                    {
                        "integrated_rate": hours * factor,
                        "capacity": site["capacity"],
                        "loads": loads,
                    },
                    hours,
                )
                leaked += dose
            c.execute(
                "UPDATE collective_waste_sites SET last_processed_at=?,leaked_salt=leaked_salt+? WHERE id=?",
                (to_iso(at), leaked, site["id"]),
            )

    @staticmethod
    def register(
        c,
        world_id,
        location_id,
        manager_id,
        at,
        *,
        population,
        daily_nutrition,
        soil_id,
        growth_source_id,
        water_id,
    ):
        if (
            isinstance(population, bool)
            or not isinstance(population, int)
            or population < 1
            or not math.isfinite(daily_nutrition)
            or daily_nutrition <= 0
        ):
            raise ValueError("地区需要固定且真实的代表群体需求")
        soil = M.state(c, soil_id)
        for iid, carrier in (
            (soil_id, "soil"),
            (growth_source_id, "growth_reserve"),
            (water_id, "water"),
        ):
            item = c.execute(
                "SELECT * FROM item_instances WHERE id=? AND world_id=? AND owner_character_id=?",
                (iid, world_id, manager_id),
            ).fetchone()
            state = M.state(c, iid)
            if item is None or state is None or state.carrier != carrier:
                raise ValueError("登记来源必须是本地区管理者的真实土地、水源与生长储量")
        rid = str(uuid4())
        c.execute(
            "INSERT INTO collective_regions(id,world_id,location_id,manager_id,population,daily_nutrition,baseline_soil_mass,soil_id,growth_source_id,water_id,started_at,last_consumed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rid,
                world_id,
                location_id,
                manager_id,
                population,
                daily_nutrition,
                soil.mass,
                soil_id,
                growth_source_id,
                water_id,
                to_iso(at),
                to_iso(at),
            ),
        )
        return rid

    @staticmethod
    def ingestion(c, world_id, at, *, batch_id, dose, actor_id=None, region_id=None):
        if dose < 0 or not math.isfinite(dose) or (actor_id is None) == (region_id is None):
            raise ValueError("摄入须有一个真实身体账户与非负暴露")
        c.execute(
            "INSERT INTO collective_exposures(id,world_id,actor_id,region_id,source_batch_id,dose,ingested_at,due_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                str(uuid4()),
                world_id,
                actor_id,
                region_id,
                batch_id,
                dose,
                to_iso(at),
                to_iso(at + timedelta(hours=36)),
            ),
        )

    @staticmethod
    def _consume_day(c, region, at):
        required = region["daily_nutrition"]
        delivered = safe = exposure = 0.0
        entries = []
        for item in c.execute(
            "SELECT i.* FROM item_instances i JOIN material_batches b ON b.item_id=i.id WHERE i.world_id=? AND i.container_type='region_food' AND i.container_id=? ORDER BY i.rowid",
            (region["world_id"], region["id"]),
        ).fetchall():
            if delivered >= required:
                break
            state = M.state(c, item["id"])
            if (
                state.carrier not in {"grain", "fish", "vegetable", "fruit", "mushroom"}
                or state.nutrition <= 0
                or FoodService.spoiled(item, at)
            ):
                continue
            multiplier = 0.5 if FoodService.view(item, at)["state"] == "stale" else 1
            per_unit = state.nutrition / item["quantity"] * multiplier
            count = min(
                item["quantity"], max(1, math.ceil((required - delivered) / per_unit - 1e-10))
            )
            meal = fraction(state, count / item["quantity"])
            amount = meal.nutrition * multiplier
            is_safe = (
                meal.mobile / max(1e-9, meal.mass) <= region["salt_limit"]
                and meal.particles / max(1e-9, meal.mass) <= region["particle_limit"]
            )
            delivered += amount
            safe += amount if is_safe else 0
            dose = meal.mobile / region["population"]
            exposure += dose
            RegionService.ingestion(
                c, region["world_id"], at, batch_id=item["id"], dose=dose, region_id=region["id"]
            )
            entries.append(
                {
                    "batch_id": item["id"],
                    "quantity": count,
                    "nutrition": amount,
                    "mass": meal.mass,
                    "mobile": meal.mobile,
                    "particles": meal.particles,
                }
            )
            M.consume(c, item, count, "region-body:" + region["id"])
        shortage = max(0, required - delivered)
        c.execute(
            "INSERT INTO collective_food_days VALUES (?,?,?,?,?,?,?,?)",
            (
                region["id"],
                to_iso(at),
                required,
                delivered,
                safe,
                encoded(entries),
                exposure,
                shortage,
            ),
        )
        safe_days = region["safe_days"] + 1 if safe + 1e-8 >= required else 0
        c.execute(
            "UPDATE collective_regions SET safe_days=?,hunger=hunger+?,health=MAX(0,health-?),last_consumed_at=? WHERE id=?",
            (safe_days, shortage / required, shortage / required * 2, to_iso(at), region["id"]),
        )

    @classmethod
    def tick(cls, c, world_id, at):
        cls._recover_lab_waste(c, world_id)
        cls._runoff(c, world_id, at)
        cls._waste(c, world_id, at)
        for region in c.execute(
            "SELECT * FROM collective_regions WHERE world_id=?", (world_id,)
        ).fetchall():
            due = from_iso(region["last_consumed_at"]) + timedelta(days=1)
            while due <= at:
                current = c.execute(
                    "SELECT * FROM collective_regions WHERE id=?", (region["id"],)
                ).fetchone()
                with material_transaction(c):
                    cls._consume_day(c, current, due)
                due += timedelta(days=1)
        for exposure in c.execute(
            "SELECT * FROM collective_exposures WHERE world_id=? AND settled_at IS NULL AND due_at<=? ORDER BY due_at,rowid",
            (world_id, to_iso(at)),
        ).fetchall():
            kind = "actor" if exposure["actor_id"] else "region"
            recipient = exposure["actor_id"] or exposure["region_id"]
            old = c.execute(
                "SELECT * FROM collective_toxic_burdens WHERE recipient_kind=? AND recipient_id=?",
                (kind, recipient),
            ).fetchone()
            elapsed = (
                max(
                    0,
                    (from_iso(exposure["due_at"]) - from_iso(old["last_at"])).total_seconds()
                    / 3600,
                )
                if old
                else 0
            )
            previous = old["burden"] * math.exp(-math.log(2) * elapsed / 168) if old else 0
            burden = previous + exposure["dose"]
            damage = max(0, max(0, burden - 0.005) - max(0, previous - 0.005)) * 40
            total_damage = (old["damage_total"] if old else 0) + damage
            charged = math.ceil(total_damage - 1e-10)
            health_loss = max(0, charged - (old["charged_damage"] if old else 0))
            c.execute(
                "INSERT INTO collective_toxic_burdens VALUES (?,?,?,?,?,?,?) ON CONFLICT(recipient_kind,recipient_id) DO UPDATE SET burden=excluded.burden,last_at=excluded.last_at,damage_total=excluded.damage_total,charged_damage=excluded.charged_damage",
                (world_id, kind, recipient, burden, exposure["due_at"], total_damage, charged),
            )
            if exposure["actor_id"]:
                c.execute(
                    "UPDATE characters SET health=MAX(0,health-?) WHERE id=?",
                    (health_loss, exposure["actor_id"]),
                )
                if health_loss:
                    from world_engine.collective_research import CollectiveResearchService as R

                    if c.execute(
                        "SELECT 1 FROM collective_people WHERE character_id=?",
                        (exposure["actor_id"],),
                    ).fetchone():
                        R.record_observation(
                            c,
                            exposure["actor_id"],
                            from_iso(exposure["due_at"]),
                            "摄入后出现了延迟身体不适",
                            {
                                "kind": "symptom",
                                "source_batch_id": exposure["source_batch_id"],
                                "health_loss": health_loss,
                            },
                        )
            elif exposure["region_id"]:
                c.execute(
                    "UPDATE collective_regions SET health=MAX(0,health-?) WHERE id=?",
                    (damage, exposure["region_id"]),
                )
            c.execute(
                "UPDATE collective_exposures SET settled_at=?,damage=? WHERE id=?",
                (exposure["due_at"], damage, exposure["id"]),
            )
        for actor in c.execute(
            "SELECT id FROM characters WHERE world_id=? AND health<=0", (world_id,)
        ).fetchall():
            c.execute(
                "INSERT OR IGNORE INTO collective_deaths VALUES (?,?,?,NULL,1,?,?)",
                (str(uuid4()), world_id, actor["id"], to_iso(at), "health_exhaustion"),
            )
        for region in c.execute(
            "SELECT id,population FROM collective_regions WHERE world_id=? AND health<=0",
            (world_id,),
        ).fetchall():
            c.execute("UPDATE collective_regions SET deaths=population WHERE id=?", (region["id"],))
            c.execute(
                "INSERT OR IGNORE INTO collective_deaths VALUES (?,?,NULL,?,?,?,?)",
                (
                    str(uuid4()),
                    world_id,
                    region["id"],
                    region["population"],
                    to_iso(at),
                    "group_health_exhaustion",
                ),
            )

    @staticmethod
    def view(c, world_id, at):
        result = []
        for region in c.execute(
            "SELECT * FROM collective_regions WHERE world_id=? ORDER BY rowid", (world_id,)
        ):
            row = dict(region)
            safe_food = 0
            for item in c.execute(
                "SELECT * FROM item_instances WHERE container_type='region_food' AND container_id=?",
                (region["id"],),
            ):
                state = M.state(c, item["id"])
                if (
                    state
                    and not FoodService.spoiled(item, at)
                    and state.mobile / max(1e-9, state.mass) <= region["salt_limit"]
                    and state.particles / max(1e-9, state.mass) <= region["particle_limit"]
                ):
                    safe_food += state.nutrition * (
                        0.5 if FoodService.view(item, at)["state"] == "stale" else 1
                    )
            soil = M.state(c, region["soil_id"])
            crops = c.execute(
                "SELECT * FROM collective_crops WHERE region_id=? AND status='harvested' ORDER BY harvested_at DESC",
                (region["id"],),
            ).fetchall()
            sites = c.execute(
                "SELECT * FROM collective_waste_sites WHERE region_id=?", (region["id"],)
            ).fetchall()
            waste_ready = True
            for site in sites:
                liner = c.execute(
                    "SELECT * FROM collective_liners WHERE item_type_id=?", (site["liner_type_id"],)
                ).fetchone()
                load = sum(
                    M.state(c, i[0]).mass + M.state(c, i[0]).water
                    for i in c.execute(
                        "SELECT id FROM item_instances WHERE container_type='waste_site' AND container_id=?",
                        (site["id"],),
                    )
                )
                waste_ready = waste_ready and bool(
                    liner
                    and load <= site["capacity"] + 1e-8
                    and (at - from_iso(site["created_at"])).total_seconds() / 3600
                    >= site["observation_window_hours"]
                    and (at - from_iso(site["last_maintained_at"])).total_seconds() / 3600
                    <= liner["maintenance_interval_hours"]
                )
            loose = len(RegionService.uncontained_waste(c, world_id))
            water = M.state(c, region["water_id"])
            waste_ready = (
                waste_ready
                and loose == 0
                and bool(
                    water
                    and water.mobile / max(1e-9, water.mass + water.water) <= region["salt_limit"]
                )
            )
            row.update(
                waste_ready=waste_ready,
                safe_reserve_days=safe_food / region["daily_nutrition"],
                land_ready=bool(
                    soil
                    and soil.mass + 1e-8 >= region["baseline_soil_mass"]
                    and soil.mobile / max(1e-9, soil.mass) <= region["salt_limit"]
                ),
                crop_cycles=len(crops),
                cycle_supply_days=(
                    crops[0]["yield_nutrition"] / region["daily_nutrition"] if crops else 0
                ),
                seed_viability=(crops[0]["output_seed_viability"] if crops else None),
            )
            result.append(row)
        return result
