"""统一背包、物品数量、合法所有者和库存审计规则。"""

import json
import math
from uuid import uuid4

from world_engine.repository import to_iso, utc_now


class InventoryError(ValueError):
    pass


class InventoryService:
    @staticmethod
    def find(connection, world_id, holder_id, name, container="character_inventory"):
        return connection.execute(
            """SELECT i.*, t.name, t.category, t.stack_limit, t.slot_size, t.heal, t.usable
               FROM item_instances i JOIN item_types t ON t.id=i.item_type_id
               WHERE i.world_id=? AND i.container_id=? AND i.container_type=?
                 AND t.name=? AND i.quantity>0 ORDER BY i.condition DESC, i.id LIMIT 1""",
            (world_id, holder_id, container, name),
        ).fetchone()

    @staticmethod
    def owned(item, owner_id):
        return item is not None and item["owner_character_id"] == owner_id

    @staticmethod
    def snapshot(connection, world_id):
        return {
            row["id"]: dict(row)
            for row in connection.execute(
                "SELECT * FROM item_instances WHERE world_id=?", (world_id,)
            )
        }

    @classmethod
    def audit(cls, connection, world_id, event_id, before):
        after = cls.snapshot(connection, world_id)
        for item_id in before.keys() | after.keys():
            if before.get(item_id) == after.get(item_id):
                continue
            connection.execute(
                """INSERT INTO inventory_changes(
                   id, world_id, source_event_id, item_instance_id,
                   before_json, after_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(uuid4()),
                    world_id,
                    event_id,
                    item_id,
                    json.dumps(before.get(item_id), ensure_ascii=False),
                    json.dumps(after.get(item_id), ensure_ascii=False),
                    to_iso(utc_now()),
                ),
            )

    @staticmethod
    def _capacity(connection, character_id, item, quantity):
        rows = connection.execute(
            """SELECT i.*, t.stack_limit, t.slot_size FROM item_instances i
               JOIN item_types t ON t.id=i.item_type_id
               WHERE i.container_id=? AND i.container_type='character_inventory'""",
            (character_id,),
        ).fetchall()
        used = sum(
            math.ceil(row["quantity"] / max(1, row["stack_limit"])) * max(1, row["slot_size"])
            for row in rows
        )
        # 同类同品质且归接收者所有的堆叠才可以合并，避免混淆借用物品。
        stack = next(
            (
                row
                for row in rows
                if row["item_type_id"] == item["item_type_id"]
                and row["condition"] == item["condition"]
                and row["owner_character_id"] == character_id
                and row["quantity"] + quantity <= max(1, item["stack_limit"])
            ),
            None,
        )
        if stack is not None:
            return stack
        required = math.ceil(quantity / max(1, item["stack_limit"])) * max(1, item["slot_size"])
        row = connection.execute(
            "SELECT inventory_capacity FROM characters WHERE id=?", (character_id,)
        ).fetchone()
        if row is None or used + required > row["inventory_capacity"]:
            raise InventoryError("接收者背包容量不足")
        return None

    @classmethod
    def transfer(cls, connection, item, recipient_id, quantity=1, *, change_owner=True):
        if quantity < 1 or quantity > item["quantity"]:
            raise InventoryError("物品数量不足")
        stack = cls._capacity(connection, recipient_id, item, quantity)
        if not change_owner:
            stack = None
        owner = recipient_id if change_owner else item["owner_character_id"]
        if stack is not None:
            connection.execute(
                "UPDATE item_instances SET quantity=quantity+? WHERE id=?", (quantity, stack["id"])
            )
            cls.consume(connection, item, quantity)
        elif quantity == item["quantity"]:
            connection.execute(
                """UPDATE item_instances SET container_id=?,
                   container_type='character_inventory', owner_character_id=? WHERE id=?""",
                (recipient_id, owner, item["id"]),
            )
        else:
            cls.consume(connection, item, quantity)
            connection.execute(
                """INSERT INTO item_instances(id, world_id, item_type_id, container_id,
                   container_type, quantity, condition, owner_character_id)
                   VALUES (?, ?, ?, ?, 'character_inventory', ?, ?, ?)""",
                (
                    str(uuid4()),
                    item["world_id"],
                    item["item_type_id"],
                    recipient_id,
                    quantity,
                    item["condition"],
                    owner,
                ),
            )

    @staticmethod
    def consume(connection, item, quantity=1):
        if item["quantity"] < quantity or quantity < 1:
            raise InventoryError("物品数量不足")
        if item["quantity"] == quantity:
            connection.execute("DELETE FROM item_instances WHERE id=?", (item["id"],))
        else:
            connection.execute(
                "UPDATE item_instances SET quantity=quantity-? WHERE id=?", (quantity, item["id"])
            )

    @staticmethod
    def equip(connection, actor_id, item):
        if item["category"] not in {"weapon", "charm"}:
            raise InventoryError("该物品不能装备")
        occupied = connection.execute(
            """SELECT 1 FROM item_instances i JOIN item_types t ON t.id=i.item_type_id
               WHERE i.container_id=? AND i.container_type='character_equipment'
               AND t.category=?""",
            (actor_id, item["category"]),
        ).fetchone()
        if occupied:
            raise InventoryError("对应装备位已占用，请先卸下现有装备")
        if item["quantity"] != 1:
            raise InventoryError("请先将装备拆分为单件")
        connection.execute(
            "UPDATE item_instances SET container_type='character_equipment' WHERE id=?",
            (item["id"],),
        )
