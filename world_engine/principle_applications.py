"""哥哥，原理研发后的应用走真实生活路径，不凭文字给人物加收益。"""

from datetime import timedelta

from world_engine.activity_tasks import TaskService
from world_engine.clock import WorldClockService
from world_engine.food import FoodService
from world_engine.life import LifeActivityService
from world_engine.principle_service import (
    application_technology,
    known_technologies,
    prerequisites_known,
)
from world_engine.repository import from_iso


class PrincipleApplications:
    def __init__(self, database, settings, world_id):
        self.database, self.world_id = database, world_id
        self.clock = WorldClockService(database, settings)

    def advance_to(self, target):
        while True:
            with self.database.read() as c:
                at = from_iso(
                    c.execute(
                        "SELECT w.current_time FROM worlds w WHERE id=?", (self.world_id,)
                    ).fetchone()[0]
                )
                scale = c.execute(
                    "SELECT time_scale FROM world_clock WHERE world_id=?", (self.world_id,)
                ).fetchone()[0]
            if at >= target:
                return
            if scale <= 0:
                raise ValueError("世界已暂停，应用等待时钟继续，不修改流速")
            self.clock.heartbeat(
                self.world_id, elapsed_seconds=min(3600, (target - at).total_seconds()) / scale
            )

    def ready(self, actor_id):
        with self.database.read() as c:
            at = from_iso(
                c.execute(
                    "SELECT w.current_time FROM worlds w WHERE id=?", (self.world_id,)
                ).fetchone()[0]
            )
            active = LifeActivityService.running(c, actor_id)
            moving = c.execute(
                "SELECT estimated_arrival_world FROM character_movements "
                "WHERE character_id=? AND status='moving'",
                (actor_id,),
            ).fetchone()
            deadlines = [at]
            if active:
                deadlines.append(from_iso(active["ends_world_time"]))
            if moving:
                deadlines.append(from_iso(moving[0]))
        self.advance_to(max(deadlines))
        return max(deadlines)

    def work(
        self,
        actor_id,
        description,
        *,
        technology_ids=None,
        application_skill=None,
        application_reference=None,
    ):
        self.ensure_energy(actor_id)
        with self.database.write() as c:
            at = from_iso(
                c.execute(
                    "SELECT w.current_time FROM worlds w WHERE id=?", (self.world_id,)
                ).fetchone()[0]
            )
            actor = c.execute("SELECT * FROM characters WHERE id=?", (actor_id,)).fetchone()
            aid = TaskService.start_personal_work(
                c,
                actor,
                at,
                description,
                technology_ids=technology_ids,
                application_skill=application_skill,
                application_reference=application_reference,
            )
            end = from_iso(
                c.execute(
                    "SELECT ends_world_time FROM character_life_activities WHERE id=?", (aid,)
                ).fetchone()[0]
            )
        self.advance_to(end)
        return aid

    def ensure_energy(self, actor_id):
        self.ready(actor_id)
        for _ in range(6):
            with self.database.write() as c:
                actor = c.execute("SELECT * FROM characters WHERE id=?", (actor_id,)).fetchone()
                if actor["health"] <= 0:
                    raise ValueError("人物已经死亡，不能安排应用")
                if actor["energy"] >= 25:
                    return
                at = from_iso(
                    c.execute(
                        "SELECT w.current_time FROM worlds w WHERE id=?", (self.world_id,)
                    ).fetchone()[0]
                )
                LifeActivityService.start(c, actor, at, "rest", 120)
            self.advance_to(at + timedelta(minutes=120))
        raise ValueError("正常休息后仍无法持续劳动，保留待应用状态")

    def prototype(self, technology_id, invention_id=None):
        from world_engine.principle_types import CapabilityEffect
        from world_engine.principle_workshop import (
            accessible,
            consume_inputs,
            has_skill,
            produce_prototype,
            prototype_accessible,
        )

        with self.database.read() as c:
            tech = dict(
                c.execute(
                    "SELECT * FROM principle_technologies WHERE id=?", (technology_id,)
                ).fetchone()
            )
            tech = application_technology(c, tech, invention_id)
            if prototype_accessible(c, tech["inventor_id"], technology_id, tech["invention_id"]):
                return
            effect = CapabilityEffect.model_validate_json(tech["effect_json"])
        actor_id = tech["inventor_id"]
        self.ensure_energy(actor_id)
        with self.database.write() as c:
            at = from_iso(
                c.execute(
                    "SELECT w.current_time FROM worlds w WHERE id=?", (self.world_id,)
                ).fetchone()[0]
            )
            actor = c.execute("SELECT * FROM characters WHERE id=?", (actor_id,)).fetchone()
            if not has_skill(c, actor, effect.required_skill, at):
                raise ValueError("原型制作需要本人尚未掌握的职业技能")
            tools = c.execute(
                "SELECT i.* FROM item_instances i JOIN principle_material_catalogue m "
                "ON m.item_type_id=i.item_type_id AND m.world_id=i.world_id "
                "WHERE m.kind='basic_tools' AND i.owner_character_id=? "
                "AND i.quantity>0 AND i.condition>0",
                (actor_id,),
            ).fetchall()
            if not any(accessible(tool, actor) for tool in tools):
                raise ValueError("现场缺少可用的普通手工器具")
            if actor["energy"] < 25:
                raise ValueError("当前精力不足以制作原型，需先正常休息")
            inputs = consume_inputs(c, actor, technology_id, effect.materials, "prototype", at)
            aid = TaskService.start_personal_work(
                c, actor, at, "本人按已研发原理制作“" + tech["name"] + "”原型", technology_ids=()
            )
            end = from_iso(
                c.execute(
                    "SELECT ends_world_time FROM character_life_activities WHERE id=?", (aid,)
                ).fetchone()[0]
            )
        self.advance_to(end)
        with self.database.write() as c:
            actor = c.execute("SELECT * FROM characters WHERE id=?", (actor_id,)).fetchone()
            produce_prototype(c, actor, tech, aid, inputs, end)

    def apply(self, technology_id, invention_id=None):
        with self.database.read() as c:
            tech = c.execute(
                "SELECT * FROM principle_technologies WHERE id=? AND world_id=?",
                (technology_id, self.world_id),
            ).fetchone()
            if tech is None:
                raise ValueError("科技不属于本世界")
            tech = application_technology(c, tech, invention_id)
            aid, title = tech["inventor_id"], tech["name"]
            adopted = c.execute(
                "SELECT 1 FROM principle_adoptions WHERE actor_id=? AND technology_id=?",
                (aid, technology_id),
            ).fetchone()
            if not adopted:
                return {"status": "not_adopted", "technology_id": technology_id}
            if not tech["design"].get("adopt_if_approved", True):
                return {
                    "status": "not_adopted",
                    "technology_id": technology_id,
                    "invention_id": tech["invention_id"],
                }
            at = from_iso(
                c.execute(
                    "SELECT w.current_time FROM worlds w WHERE id=?", (self.world_id,)
                ).fetchone()[0]
            )
            if technology_id not in {t["id"] for t in known_technologies(c, aid, at)}:
                raise ValueError("应用者没有实际掌握这项科技")
            if not prerequisites_known(c, aid, technology_id, at):
                return {"status": "needs_prerequisite", "technology_id": technology_id}
            import json

            effect = json.loads(tech["effect_json"])
            from world_engine.principle_workshop import has_skill

            actor = c.execute("SELECT * FROM characters WHERE id=?", (aid,)).fetchone()
            if not has_skill(c, actor, effect["required_skill"], at):
                return {"status": "needs_skill", "technology_id": technology_id}
            if effect.get("location_kinds"):
                location = c.execute(
                    "SELECT kind FROM locations WHERE id=?",
                    (actor["current_location_id"] or actor["location_id"],),
                ).fetchone()
                if location[0] not in effect["location_kinds"]:
                    return {"status": "needs_location", "technology_id": technology_id}
        if effect["kind"] != "food_nutrition":
            if effect["carrier"] == "tool":
                self.prototype(technology_id, tech["invention_id"])
            if effect["kind"] in {"material_strength", "craft_precision"}:
                return self.apply_product(tech)
            if effect["kind"] in {"magic_energy_cost", "magic_transfer_efficiency"}:
                return self.apply_magic(tech)
            if effect["carrier"] != "tool" and effect["materials"]:
                self.ready(aid)
                from world_engine.principle_types import CapabilityEffect
                from world_engine.principle_workshop import consume_inputs

                with self.database.write() as c:
                    actor = c.execute("SELECT * FROM characters WHERE id=?", (aid,)).fetchone()
                    at = from_iso(
                        c.execute(
                            "SELECT w.current_time FROM worlds w WHERE id=?", (self.world_id,)
                        ).fetchone()[0]
                    )
                    contract = CapabilityEffect.model_validate(effect)
                    consume_inputs(c, actor, technology_id, contract.materials, "application", at)
            activity = self.work(
                aid,
                tech["application"],
                technology_ids=(tech["selection_id"],),
                application_skill=effect["required_skill"],
            )
            with self.database.write() as c:
                impact = c.execute(
                    "SELECT 1 FROM principle_impacts WHERE technology_id=? AND source_id=?",
                    (technology_id, activity),
                ).fetchone()
                status = c.execute(
                    "SELECT status FROM character_life_activities WHERE id=?", (activity,)
                ).fetchone()[0]
                proto = c.execute(
                    "SELECT item_id FROM principle_invention_prototypes WHERE invention_id=?"
                    if tech["invention_id"]
                    else "SELECT item_id FROM principle_prototypes WHERE technology_id=?",
                    (tech["invention_id"] or technology_id,),
                ).fetchone()
                if proto and impact and status == "completed":
                    from world_engine.principle_inventions import wear_prototype

                    wear_prototype(c, proto[0])
                if impact and status == "completed":
                    from world_engine.principle_inventions import record_reuse

                    record_reuse(c, tech, aid, activity, end_time(c, activity))
            return {
                "status": "applied"
                if impact and status == "completed"
                else (status if status != "completed" else "no_applicable_effect"),
                "technology_id": technology_id,
                "activity_id": activity,
                "invention_id": tech["invention_id"],
            }
        # 哥哥，实际用饭才有营养收益；日常工作结束后按本人需要用饭，不改写饥饿值。
        for _ in range(4):
            self.ready(aid)
            with self.database.read() as c:
                actor = c.execute("SELECT * FROM characters WHERE id=?", (aid,)).fetchone()
                if actor["satiety"] <= 79:
                    break
            self.work(aid, "本人日常工作后准备采用“" + title + "”用饭")
        with self.database.write() as c:
            actor = c.execute("SELECT * FROM characters WHERE id=?", (aid,)).fetchone()
            at = from_iso(
                c.execute(
                    "SELECT w.current_time FROM worlds w WHERE id=?", (self.world_id,)
                ).fetchone()[0]
            )
            item = c.execute(
                "SELECT i.*,p.nutrition,t.name FROM item_instances i "
                "JOIN item_types t ON t.id=i.item_type_id "
                "JOIN world_item_profiles p ON p.item_type_id=i.item_type_id "
                "AND p.world_id=i.world_id WHERE i.owner_character_id=? "
                "AND i.container_type='character_inventory' AND i.container_id=? "
                "AND p.nutrition>0 AND i.quantity>0 ORDER BY i.id LIMIT 1",
                (aid, aid),
            ).fetchone()
            if item is None:
                return {"status": "needs_food", "technology_id": technology_id}
            from world_engine.principle_types import CapabilityEffect
            from world_engine.principle_workshop import consume_inputs

            contract = CapabilityEffect.model_validate(effect)
            consume_inputs(c, actor, technology_id, contract.materials, "food_process", at)
            FoodService.eat(c, actor, item, at, technology_ids=(tech["selection_id"],))
            applied = c.execute(
                "SELECT 1 FROM principle_impacts i JOIN principle_meals m "
                "ON m.id=i.source_id WHERE i.technology_id=? "
                "AND i.source_kind='meal' AND m.quantity_before>m.quantity_after "
                "AND m.satiety_after>m.satiety_before AND i.invention_id IS ?",
                (technology_id, tech["invention_id"]),
            ).fetchone()
            if applied:
                from world_engine.principle_inventions import record_reuse

                latest = c.execute(
                    "SELECT source_id FROM principle_impacts WHERE technology_id=? "
                    "AND invention_id IS ? ORDER BY rowid DESC LIMIT 1",
                    (technology_id, tech["invention_id"]),
                ).fetchone()
                record_reuse(c, tech, aid, latest[0], at)
        return {
            "status": "applied" if applied else "no_applicable_effect",
            "technology_id": technology_id,
            "invention_id": tech["invention_id"],
            **({"meal_id": latest[0]} if applied else {}),
        }

    def apply_product(self, tech):
        """哥哥，物品属性来自完成的制造，后续使用仍付出劳动与实际磨损。"""
        self.ensure_energy(tech["inventor_id"])
        from world_engine.principle_workshop import prototype_accessible

        with self.database.read() as c:
            if not prototype_accessible(c, tech["inventor_id"], tech["id"], tech["invention_id"]):
                return {
                    "status": "needs_equipment",
                    "technology_id": tech["id"],
                    "invention_id": tech["invention_id"],
                }
        activity = self.work(
            tech["inventor_id"],
            tech["application"],
            technology_ids=(),
            application_reference={
                "technology_id": tech["id"],
                "invention_id": tech["invention_id"],
            },
        )
        with self.database.write() as c:
            proto = c.execute(
                "SELECT * FROM principle_invention_prototypes WHERE invention_id=?",
                (tech["invention_id"],),
            ).fetchone()
            properties = (
                c.execute(
                    "SELECT * FROM principle_product_properties WHERE item_id=?",
                    (proto["item_id"],),
                ).fetchone()
                if proto
                else None
            )
            if not properties:
                return {
                    "status": "pending",
                    "technology_id": tech["id"],
                    "invention_id": tech["invention_id"],
                }
            from world_engine.principle_inventions import record_reuse, wear_prototype

            # 哥哥，完成一次实际使用才记复用；材料强度实际改变累积磨损速率。
            latest = c.execute(
                "SELECT id,status,ends_world_time FROM character_life_activities WHERE id=?",
                (activity,),
            ).fetchone()
            if latest["status"] != "completed":
                return {"status": latest["status"], "invention_id": tech["invention_id"]}
            wear_prototype(c, proto["item_id"])
            record_reuse(
                c, tech, tech["inventor_id"], latest["id"], from_iso(latest["ends_world_time"])
            )
            return {
                "status": "applied",
                "technology_id": tech["id"],
                "invention_id": tech["invention_id"],
                "item_id": proto["item_id"],
                "activity_id": activity,
                "strength": properties["strength"],
                "precision": properties["precision"],
            }

    def apply_magic(self, tech):
        from world_engine.magic_research import settle, validate
        from world_engine.phenomena import seal_magic
        from world_engine.principle_types import CapabilityEffect

        self.ensure_energy(tech["inventor_id"])
        with self.database.write() as c:
            actor = c.execute(
                "SELECT * FROM characters WHERE id=?", (tech["inventor_id"],)
            ).fetchone()
            at = from_iso(
                c.execute(
                    "SELECT w.current_time FROM worlds w WHERE id=?", (self.world_id,)
                ).fetchone()[0]
            )
            pattern, _, _ = validate(
                c, actor, CapabilityEffect.model_validate_json(tech["effect_json"]), at
            )
            seal_magic(c, actor["world_id"], pattern, at)
        effect = CapabilityEffect.model_validate_json(tech["effect_json"])
        auxiliary_use_id = None
        if effect.carrier != "tool" and effect.materials:
            from world_engine.principle_workshop import consume_inputs

            with self.database.write() as c:
                actor = c.execute(
                    "SELECT * FROM characters WHERE id=?", (tech["inventor_id"],)
                ).fetchone()
                consume_inputs(c, actor, tech["id"], effect.materials, "magic_application", at)
                auxiliary_use_id = c.execute(
                    "SELECT id FROM principle_material_uses "
                    "WHERE actor_id=? AND technology_id=? AND purpose='magic_application' "
                    "ORDER BY rowid DESC LIMIT 1",
                    (actor["id"], tech["id"]),
                ).fetchone()[0]
        activity = self.work(
            tech["inventor_id"],
            tech["application"],
            technology_ids=(),
            application_reference={
                "technology_id": tech["id"],
                "invention_id": tech["invention_id"],
                "auxiliary_use_id": auxiliary_use_id,
            },
        )
        with self.database.write() as c:
            actor = c.execute(
                "SELECT * FROM characters WHERE id=?", (tech["inventor_id"],)
            ).fetchone()
            result = settle(c, tech, actor, activity, end_time(c, activity))
        return result


def end_time(c, activity_id):
    return from_iso(
        c.execute(
            "SELECT ends_world_time FROM character_life_activities WHERE id=?", (activity_id,)
        ).fetchone()[0]
    )
