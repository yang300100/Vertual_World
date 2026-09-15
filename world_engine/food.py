"""食品批次按世界时间变质；转移、拆分和保存不会重置期限。"""

from datetime import timedelta

from world_engine.inventory import InventoryError, InventoryService
from world_engine.repository import from_iso, to_iso

FOOD_SCHEMA = """
CREATE TABLE IF NOT EXISTS food_storage_rules (
 item_type_id TEXT PRIMARY KEY REFERENCES item_types(id),
 world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 shelf_life_hours INTEGER NOT NULL CHECK(shelf_life_hours BETWEEN 1 AND 8760)
);
CREATE TABLE IF NOT EXISTS character_food_discomfort (
 character_id TEXT PRIMARY KEY REFERENCES characters(id) ON DELETE CASCADE,
 world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 source_event_id TEXT NOT NULL REFERENCES world_events(id) ON DELETE CASCADE,
 expires_world_time TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS food_eat_requests (
 world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,request_id TEXT NOT NULL,
 payload_json TEXT NOT NULL,response_json TEXT NOT NULL,PRIMARY KEY(world_id,request_id)
);
"""


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
    def eat(cls, c, actor, item, at, *, accept_spoiled=False):
        if (
            item["container_type"] != "character_inventory"
            or item["container_id"] != actor["id"]
            or item["owner_character_id"] != actor["id"]
        ):
            raise InventoryError("只能食用自己随身携带的食物")
        state = cls.view(item, at)["state"]
        if state == "spoiled" and not accept_spoiled:
            raise InventoryError("食物已变质；自动进食不会选择它，请明确确认风险后再食用")
        nutrition = (
            0
            if state == "spoiled"
            else max(1, item["nutrition"] // 2)
            if state == "stale"
            else item["nutrition"]
        )
        InventoryService.consume(c, item, 1)
        energy_loss = min(actor["energy"], 8) if state == "spoiled" else 0
        c.execute(
            "UPDATE characters SET satiety=MIN(100,satiety+?),energy=MAX(0,energy-?) WHERE id=?",
            (nutrition, energy_loss, actor["id"]),
        )
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
