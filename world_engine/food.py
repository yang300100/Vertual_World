"""食品批次按世界时间变质；转移、拆分和保存不会重置期限。"""

from datetime import timedelta
from uuid import uuid4

from world_engine.inventory import InventoryError, InventoryService
from world_engine.repository import from_iso, to_iso


class FoodService:
    @staticmethod
    def stamp(c, item_id, item_type, at):
        rule = c.execute(
            "SELECT shelf_life_hours FROM food_storage_rules WHERE item_type_id=?", (item_type,)
        ).fetchone()
        if rule:
            c.execute(
                "UPDATE item_instances SET fresh_until_world_time=?,spoils_world_time=? WHERE id=?",
                (
                    to_iso(at + timedelta(hours=rule[0] * 0.75)),
                    to_iso(at + timedelta(hours=rule[0])),
                    item_id,
                ),
            )

    @staticmethod
    def view(item, at):
        data = dict(item)
        end, fresh = data.get("spoils_world_time"), data.get("fresh_until_world_time")
        state = (
            "untracked"
            if not end
            else "spoiled"
            if from_iso(end) <= at
            else "stale"
            if fresh and from_iso(fresh) <= at
            else "fresh"
        )
        return {
            "state": state,
            "spoils_world_time": end,
            "label": {
                "untracked": "未登记保鲜期限",
                "fresh": "新鲜",
                "stale": "新鲜度下降",
                "spoiled": "已变质",
            }[state],
        }

    @classmethod
    def spoiled(cls, item, at):
        return cls.view(item, at)["state"] == "spoiled"

    @staticmethod
    def discomfort(c, cid, at):
        row = c.execute(
            "SELECT expires_world_time FROM character_food_discomfort WHERE character_id=? AND expires_world_time>?",
            (cid, to_iso(at)),
        ).fetchone()
        return (
            {"name": "肠胃不适", "expires_world_time": row[0], "check_modifier": -10}
            if row
            else None
        )

    @classmethod
    def eat(cls, c, actor, item, at, *, accept_spoiled=False, technology_ids=None):
        # 哥哥，重新读取当前本人状态，旧快照不能让生命已耗尽的 NPC 自行进食。
        current = c.execute("SELECT * FROM characters WHERE id=? AND world_id=?", (actor["id"], actor["world_id"])).fetchone()
        if current is None or (not current["is_player"] and current["health"] <= 0):
            raise InventoryError("当前身体状态无法自行进食")
        # 哥哥，异步回复可能持有旧物品快照；只使用当前同世界的同一实物。
        actual = c.execute("SELECT * FROM item_instances WHERE id=? AND world_id=?",
                           (item["id"], current["world_id"])).fetchone()
        if (actual is None or actual["item_type_id"] != item["item_type_id"]
                or actual["quantity"] <= 0):
            raise InventoryError("原食物已不存在、类型变化或数量不足")
        actor = current
        # 哥哥，所有实物属性以当前行覆盖旧快照；有世界登记时营养也以当前登记为准。
        item = {**dict(item), **dict(actual)}
        profile = c.execute("SELECT nutrition FROM world_item_profiles WHERE world_id=? AND item_type_id=?",
                            (current["world_id"], actual["item_type_id"])).fetchone()
        if profile is not None:
            item["nutrition"] = profile["nutrition"]
        if (
            item["container_type"] != "character_inventory"
            or item["container_id"] != actor["id"]
            or item["owner_character_id"] != actor["id"]
        ):
            raise InventoryError("只能食用自己随身携带的食物")
        state = cls.view(item, at)["state"]
        decision = c.execute(
            "SELECT policy FROM collective_food_decisions WHERE actor_id=? AND batch_id=?",
            (actor["id"], item["id"]),
        ).fetchone()
        if decision and decision[0] in {"avoid", "reserve"}:
            raise InventoryError("本人已作出隔离或留存饮食安排，不能自动食用这一批")
        if state == "spoiled" and not accept_spoiled:
            raise InventoryError("食物已变质；自动进食不会选择它，请明确确认风险后再食用")
        nutrition = (
            0
            if state == "spoiled"
            else max(1, item["nutrition"] // 2)
            if state == "stale"
            else item["nutrition"]
        )
        from world_engine.materials import MaterialService

        material = MaterialService.state(c, item["id"])
        gain = nutrition
        if material is not None:
            nutrition = (
                0
                if state == "spoiled"
                else material.nutrition / item["quantity"] * (0.5 if state == "stale" else 1)
            )
            from world_engine.collective_regions import RegionService

            RegionService.ingestion(
                c,
                actor["world_id"],
                at,
                batch_id=item["id"],
                dose=material.mobile / item["quantity"],
                actor_id=actor["id"],
            )
            residual = c.execute(
                "SELECT value FROM collective_nutrition_residuals WHERE actor_id=?", (actor["id"],)
            ).fetchone()
            available = nutrition + (residual[0] if residual else 0)
            gain = int(available)
            c.execute(
                "INSERT INTO collective_nutrition_residuals VALUES (?,?) ON CONFLICT(actor_id) DO UPDATE SET value=excluded.value",
                (actor["id"], available - gain),
            )
        from world_engine.principle_effects import adjust_meal, record_impacts

        gain, technology_changes = adjust_meal(c, actor, gain, at, technology_ids)
        nutrition = gain if technology_changes else nutrition
        meal_id = str(uuid4())
        before_quantity = c.execute("SELECT quantity FROM item_instances WHERE id=?",
                                    (item["id"],)).fetchone()[0]
        before_satiety = c.execute("SELECT satiety FROM characters WHERE id=?",
                                   (actor["id"],)).fetchone()[0]
        InventoryService.consume(c, item, 1, destination="body:" + actor["id"])
        energy_loss = min(actor["energy"], 8) if state == "spoiled" else 0
        c.execute(
            "UPDATE characters SET satiety=MIN(100,satiety+?),energy=MAX(0,energy-?) WHERE id=?",
            (gain, energy_loss, actor["id"]),
        )
        if technology_changes:
            remaining = c.execute("SELECT quantity FROM item_instances WHERE id=?",
                                  (item["id"],)).fetchone()
            after_satiety = c.execute("SELECT satiety FROM characters WHERE id=?",
                                     (actor["id"],)).fetchone()[0]
            c.execute("INSERT INTO principle_meals VALUES (?,?,?,?,?,?,?,?,?)",
                      (meal_id, actor["world_id"], actor["id"], item["id"], before_quantity,
                       remaining[0] if remaining else 0, before_satiety, after_satiety, to_iso(at)))
            record_impacts(c, actor["id"], technology_changes, "food_nutrition", "meal", meal_id, at)
        return {
            "nutrition": nutrition,
            "energy_loss": energy_loss,
            "spoiled": state == "spoiled",
            "freshness": state,
        }

    @staticmethod
    def record_discomfort(c, actor, event_id, at):
        c.execute(
            "INSERT INTO character_food_discomfort VALUES (?,?,?,?) ON CONFLICT(character_id) DO UPDATE "
            "SET source_event_id=excluded.source_event_id,expires_world_time=MAX(expires_world_time,excluded.expires_world_time)",
            (actor["id"], actor["world_id"], event_id, to_iso(at + timedelta(hours=6))),
        )
