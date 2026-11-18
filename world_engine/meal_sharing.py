"""哥哥，请客只使用邀请者自己的两份食物，接受之后才真实转交并进食。"""

from world_engine.food import FoodService
from world_engine.inventory import InventoryService

MEAL_SHARING_SCHEMA = """
CREATE TABLE IF NOT EXISTS shared_meal_offers (
 moment_id TEXT PRIMARY KEY REFERENCES shared_life_moments(id) ON DELETE CASCADE,
 provider_id TEXT NOT NULL REFERENCES characters(id),
 source_event_id TEXT NOT NULL REFERENCES world_events(id)
);
CREATE TABLE IF NOT EXISTS shared_meal_servings (
 moment_id TEXT NOT NULL REFERENCES shared_life_moments(id) ON DELETE CASCADE,
 eater_id TEXT NOT NULL REFERENCES characters(id), provider_id TEXT NOT NULL REFERENCES characters(id),
 source_item_id TEXT NOT NULL, consumed_item_id TEXT NOT NULL,
 quantity INTEGER NOT NULL CHECK(quantity=1),
 source_event_id TEXT NOT NULL REFERENCES world_events(id), PRIMARY KEY(moment_id,eater_id)
);
"""


class MealSharingService:
    @staticmethod
    def terms(c, moment_id):
        offer = c.execute("SELECT provider_id FROM shared_meal_offers WHERE moment_id=?", (moment_id,)).fetchone()
        return {"meal_supply": "proposer" if offer else "each_own", "meal_provider_id": offer[0] if offer else None}

    @staticmethod
    def foods(c, owner, at, eater=None):
        eater = eater if eater is not None else owner
        return [r for r in c.execute(
            "SELECT i.*,t.name,t.stack_limit,t.slot_size,p.nutrition FROM item_instances i "
            "JOIN item_types t ON t.id=i.item_type_id JOIN world_item_profiles p ON p.item_type_id=i.item_type_id AND p.world_id=i.world_id "
            "WHERE i.world_id=? AND i.container_type='character_inventory' AND i.container_id=? AND i.owner_character_id=? "
            "AND i.quantity>0 AND i.condition>0 AND t.category='food' AND p.nutrition>0 "
            "AND NOT EXISTS (SELECT 1 FROM collective_food_decisions d WHERE d.batch_id=i.id "
            "AND d.actor_id IN (?,?) AND d.policy IN ('avoid','reserve')) ORDER BY i.spoils_world_time,i.id",
            (owner["world_id"], owner["id"], owner["id"], owner["id"], eater["id"]),
        ) if not FoodService.spoiled(r, at)]

    @classmethod
    def prepare(cls, c, moment, actor, other, at):
        if moment["kind"] != "meal":
            return []
        terms = cls.terms(c, moment["id"])
        if terms["meal_supply"] == "each_own":
            servings = []
            for person in (actor, other):
                foods = cls.foods(c, person, at)
                if not foods:
                    raise ValueError("共同用餐需要双方各有一份可食用的随身食物；也可另行商量由邀请者请客")
                servings.append((person, person, foods[0]))
            return servings
        provider = actor if actor["id"] == moment["proposer_id"] else other
        guest = other if provider["id"] == actor["id"] else actor
        if terms["meal_provider_id"] != provider["id"]:
            raise ValueError("请客承诺只能使用邀请者自己的食物")
        foods = cls.foods(c, provider, at)
        guest_foods = cls.foods(c, provider, at, guest)
        # 哥哥，先选客人可接受的那份，再为主人留一份，不能把同一个单位算两遍。
        for guest_food in guest_foods:
            own_food = next((r for r in foods if r["quantity"] - int(r["id"] == guest_food["id"]) >= 1), None)
            if own_food is not None:
                return [(provider, provider, own_food), (guest, provider, guest_food)]
        raise ValueError("邀请者需要两份当前可用的自有随身食物，且招待食物须符合双方已有饮食安排；可以继续准备或撤回重议")

    @staticmethod
    def consume(c, servings, at):
        receipts = []
        for eater, provider, selected in servings:
            # 哥哥，同一堆食物可能连续使用两次，每次都重读剩余实物。
            item = c.execute(
                "SELECT i.*,t.stack_limit,t.slot_size,p.nutrition FROM item_instances i JOIN item_types t ON t.id=i.item_type_id "
                "JOIN world_item_profiles p ON p.item_type_id=i.item_type_id AND p.world_id=i.world_id "
                "WHERE i.id=? AND i.world_id=? AND i.owner_character_id=? AND i.container_id=? AND i.container_type='character_inventory'",
                (selected["id"], provider["world_id"], provider["id"], provider["id"]),
            ).fetchone()
            if item is None or item["quantity"] < 1:
                raise ValueError("准备使用的食物已经不可用，请重新商量")
            if eater["id"] != provider["id"]:
                before_ids = {r[0] for r in c.execute("SELECT id FROM item_instances WHERE container_type='character_inventory' AND container_id=?", (eater["id"],))}
                InventoryService.transfer(c, item, eater["id"], 1, preserve_identity=True)
                received = [r for r in c.execute(
                    "SELECT i.*,p.nutrition FROM item_instances i JOIN world_item_profiles p ON p.item_type_id=i.item_type_id AND p.world_id=i.world_id "
                    "WHERE i.world_id=? AND i.container_id=? AND i.owner_character_id=? AND i.container_type='character_inventory'",
                    (eater["world_id"], eater["id"], eater["id"]),
                ) if r["id"] not in before_ids]
                if len(received) != 1:
                    raise ValueError("未能核验本次实际转交的一份食物")
                item = received[0]
            FoodService.eat(c, eater, item, at)
            receipts.append({"eater_id": eater["id"], "provider_id": provider["id"], "source_item_id": selected["id"], "consumed_item_id": item["id"], "quantity": 1})
        return receipts

    @staticmethod
    def record(c, moment_id, event_id, receipts):
        for r in receipts:
            c.execute("INSERT INTO shared_meal_servings VALUES (?,?,?,?,?,?,?)", (
                moment_id, r["eater_id"], r["provider_id"], r["source_item_id"], r["consumed_item_id"], r["quantity"], event_id))
