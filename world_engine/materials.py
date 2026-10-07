"""Yuki 把每份材料的状态跟实物绑定，拆合和消耗都留下不可变物质账。"""

from __future__ import annotations

import json
import math
from contextlib import contextmanager
from dataclasses import asdict, replace
from uuid import uuid4

from world_engine.material_physics import EXTENSIVE, MaterialState, combine, fraction, salt_total


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def pigment_total(states):
    return sum(x.pigment_free + x.pigment_fixed for x in states)


def require_pigment_conservation(before, after):
    if not math.isclose(pigment_total(before), pigment_total(after), rel_tol=1e-9, abs_tol=1e-8):
        raise ValueError("颜料的实际物质转化未守恒")


@contextmanager
def material_transaction(c, item_ids=()):
    name = "material_" + uuid4().hex
    c.execute(f"SAVEPOINT {name}")
    try:
        for iid in item_ids:
            c.execute("INSERT INTO material_mutation_permits(item_id) VALUES (?)", (iid,))
        yield
        for iid in item_ids:
            c.execute("DELETE FROM material_mutation_permits WHERE item_id=?", (iid,))
        c.execute(f"RELEASE SAVEPOINT {name}")
    except Exception:
        c.execute(f"ROLLBACK TO SAVEPOINT {name}")
        c.execute(f"RELEASE SAVEPOINT {name}")
        raise


class MaterialService:
    @staticmethod
    def enabled(c, world_id):
        return bool(
            c.execute("SELECT 1 FROM material_modes WHERE world_id=?", (world_id,)).fetchone()
        )

    @staticmethod
    def state(c, item_id):
        row = c.execute(
            "SELECT state_json FROM material_batches WHERE item_id=?", (item_id,)
        ).fetchone()
        return MaterialState(**json.loads(row[0])) if row else None

    @staticmethod
    def _record(c, world_id, operation, before, after, item_ids, destination=None):
        at = c.execute("SELECT w.current_time FROM worlds w WHERE w.id=?", (world_id,)).fetchone()[
            0
        ]
        c.execute(
            (
                "INSERT INTO material_ledger(id,world_id,operation,before_json,after_json,"
                "item_ids_json,destination,world_time) VALUES (?,?,?,?,?,?,?,?)"
            ),
            (
                str(uuid4()),
                world_id,
                operation,
                encoded([asdict(x) for x in before]),
                encoded([asdict(x) for x in after]),
                encoded(item_ids),
                destination,
                at,
            ),
        )

    @classmethod
    def attach(cls, c, item_id, state):
        if not isinstance(state, MaterialState):
            raise ValueError("材料状态须经物理类型验证")
        item = c.execute("SELECT * FROM item_instances WHERE id=?", (item_id,)).fetchone()
        if item is None or item["quantity"] <= 0 or cls.state(c, item_id) is not None:
            raise ValueError("只能向实际未追踪的物品登记初始材料")
        with material_transaction(c):
            c.execute(
                "INSERT INTO material_batches VALUES (?,?,?,?)",
                (item_id, item["world_id"], item["quantity"], encoded(asdict(state))),
            )
            cls._record(c, item["world_id"], "origin", [], [state], [item_id])

    @staticmethod
    def _item(c, supplied, quantity):
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
            raise ValueError("材料数量须是正整数")
        item = c.execute("SELECT * FROM item_instances WHERE id=?", (supplied["id"],)).fetchone()
        if item is None or item["quantity"] < quantity or item["quantity"] != supplied["quantity"]:
            raise ValueError("材料库存不足或此前读取的批次已改变")
        return item

    @staticmethod
    def _write(c, item_id, quantity, state):
        c.execute(
            "UPDATE material_batches SET quantity_ref=?,state_json=? WHERE item_id=?",
            (quantity, encoded(asdict(state)), item_id),
        )

    @staticmethod
    def _reduce(c, item, quantity):
        if item["quantity"] == quantity:
            c.execute("DELETE FROM material_batches WHERE item_id=?", (item["id"],))
            c.execute("DELETE FROM item_instances WHERE id=?", (item["id"],))
        else:
            c.execute(
                "UPDATE item_instances SET quantity=quantity-? WHERE id=?", (quantity, item["id"])
            )

    @classmethod
    def split(cls, c, supplied, quantity, new_id):
        item = cls._item(c, supplied, quantity)
        original = cls.state(c, item["id"])
        if original is None or quantity >= item["quantity"]:
            raise ValueError("拆批需要实际追踪材料，且不得拆走整堆")
        part = fraction(original, quantity / item["quantity"])
        remainder = fraction(original, 1 - quantity / item["quantity"])
        with material_transaction(c, (item["id"],)):
            c.execute(
                (
                    "INSERT INTO item_instances(id,world_id,item_type_id,container_type,"
                    "container_id,quantity,condition,owner_character_id,"
                    "fresh_until_world_time,spoils_world_time) VALUES (?,?,?,?,?,?,?,?,?,?)"
                ),
                (
                    new_id,
                    item["world_id"],
                    item["item_type_id"],
                    item["container_type"],
                    item["container_id"],
                    quantity,
                    item["condition"],
                    item["owner_character_id"],
                    item["fresh_until_world_time"],
                    item["spoils_world_time"],
                ),
            )
            c.execute(
                "INSERT INTO material_batches VALUES (?,?,?,?)",
                (new_id, item["world_id"], quantity, encoded(asdict(part))),
            )
            cls._reduce(c, item, quantity)
            cls._write(c, item["id"], item["quantity"] - quantity, remainder)
            for decision in c.execute(
                "SELECT * FROM collective_food_decisions WHERE batch_id=?", (item["id"],)
            ).fetchall():
                c.execute(
                    "INSERT INTO collective_food_decisions VALUES (?,?,?,?,?)",
                    (
                        decision["actor_id"],
                        new_id,
                        decision["policy"],
                        decision["reason"],
                        decision["decided_at"],
                    ),
                )
            cls._record(
                c, item["world_id"], "split", [original], [remainder, part], [item["id"], new_id]
            )
        return new_id

    @classmethod
    def compatible(cls, c, first, second):
        if first["id"] == second["id"]:
            return False
        keys = (
            "world_id",
            "item_type_id",
            "condition",
            "fresh_until_world_time",
            "spoils_world_time",
        )
        if any(dict(first).get(k) != dict(second).get(k) for k in keys):
            return False

        def policies(iid):
            return sorted(
                tuple(r)
                for r in c.execute(
                    "SELECT actor_id,policy FROM collective_food_decisions WHERE batch_id=?", (iid,)
                )
            )

        if policies(first["id"]) != policies(second["id"]):
            return False
        a, b = cls.state(c, first["id"]), cls.state(c, second["id"])
        return (a is None and b is None) or (
            a is not None and b is not None and a.carrier == b.carrier
        )

    @classmethod
    def merge(cls, c, supplied, supplied_target, quantity):
        source = cls._item(c, supplied, quantity)
        target = cls._item(c, supplied_target, 1)
        if not cls.compatible(c, source, target):
            raise ValueError("这些材料不能合并为同一批次")
        original, target_state = cls.state(c, source["id"]), cls.state(c, target["id"])
        if original is None or target_state is None:
            raise ValueError("材料合并入口只接受已经追踪的两批材料")
        part = fraction(original, quantity / source["quantity"])
        remaining = fraction(original, 1 - quantity / source["quantity"])
        merged = combine((target_state, part))
        with material_transaction(c, (source["id"], target["id"])):
            c.execute(
                "UPDATE item_instances SET quantity=quantity+? WHERE id=?", (quantity, target["id"])
            )
            cls._write(c, target["id"], target["quantity"] + quantity, merged)
            cls._reduce(c, source, quantity)
            if source["quantity"] > quantity:
                cls._write(c, source["id"], source["quantity"] - quantity, remaining)
            after = [merged] + ([remaining] if source["quantity"] > quantity else [])
            cls._record(
                c,
                source["world_id"],
                "merge",
                [original, target_state],
                after,
                [source["id"], target["id"]],
            )

    @classmethod
    def consume(cls, c, supplied, quantity, destination):
        item = cls._item(c, supplied, quantity)
        if not isinstance(destination, str) or not destination.strip():
            raise ValueError("追踪材料的消耗必须有明确去向")
        original = cls.state(c, item["id"])
        if original is None:
            raise ValueError("此批材料没有已登记状态")
        part = fraction(original, quantity / item["quantity"])
        remaining = fraction(original, 1 - quantity / item["quantity"])
        with material_transaction(c, (item["id"],)):
            cls._reduce(c, item, quantity)
            if item["quantity"] > quantity:
                cls._write(c, item["id"], item["quantity"] - quantity, remaining)
            at = c.execute(
                "SELECT w.current_time FROM worlds w WHERE w.id=?", (item["world_id"],)
            ).fetchone()[0]
            c.execute(
                "INSERT INTO material_sinks VALUES (?,?,?,?,?,?)",
                (
                    str(uuid4()),
                    item["world_id"],
                    destination,
                    encoded(asdict(part)),
                    item["id"],
                    at,
                ),
            )
            after = [part] + ([remaining] if item["quantity"] > quantity else [])
            cls._record(
                c, item["world_id"], "consume", [original], after, [item["id"]], destination
            )
        return part

    @classmethod
    def move(cls, c, supplied, quantity, container_type, container_id, owner_id, *, target=None):
        item = cls._item(c, supplied, quantity)
        original = cls.state(c, item["id"])
        if original is None or not container_type or not container_id:
            raise ValueError("材料转移须有实际批次与明确地点")
        with material_transaction(c):
            if target is not None:
                if (
                    target["container_type"] != container_type
                    or target["container_id"] != container_id
                    or target["owner_character_id"] != owner_id
                ):
                    raise ValueError("合并目标的实际位置或所有者不符")
                cls.merge(c, item, target, quantity)
                return target["id"]
            moved_id = (
                item["id"]
                if quantity == item["quantity"]
                else cls.split(c, item, quantity, str(uuid4()))
            )
            c.execute(
                (
                    "UPDATE item_instances SET container_type=?,container_id=?,"
                    "owner_character_id=? WHERE id=?"
                ),
                (container_type, container_id, owner_id, moved_id),
            )
            moved_state = cls.state(c, moved_id)
            cls._record(
                c,
                item["world_id"],
                "move",
                [moved_state],
                [moved_state],
                [moved_id],
                f"{container_type}:{container_id}",
            )
        return moved_id

    @classmethod
    def transform(cls, c, input_ids, outputs, *, container_type, container_id, owner_id):
        if not input_ids or len(input_ids) != len(set(input_ids)) or not outputs:
            raise ValueError("物质转化需要实际且不重复的输入与完整输出")
        items = [
            c.execute("SELECT * FROM item_instances WHERE id=?", (iid,)).fetchone()
            for iid in input_ids
        ]
        if any(x is None for x in items) or len({x["world_id"] for x in items}) != 1:
            raise ValueError("物质输入不存在或跨越世界")
        world_id = items[0]["world_id"]
        before = [cls.state(c, iid) for iid in input_ids]
        after = [x["state"] for x in outputs]
        if any(x is None for x in before) or any(not isinstance(x, MaterialState) for x in after):
            raise ValueError("物质转化必须引用已经追踪的材料")
        require_pigment_conservation(before, after)
        for key in ("mass", "particles", "water", "nutrition"):
            if not math.isclose(
                sum(getattr(x, key) for x in before),
                sum(getattr(x, key) for x in after),
                rel_tol=1e-9,
                abs_tol=1e-8,
            ):
                raise ValueError("物质转化未守恒：" + key)
        if not math.isclose(
            sum(salt_total(x) for x in before),
            sum(salt_total(x) for x in after),
            rel_tol=1e-9,
            abs_tol=1e-8,
        ):
            raise ValueError("盐的物质转化未守恒")
        for out in outputs:
            if (
                isinstance(out["quantity"], bool)
                or not isinstance(out["quantity"], int)
                or out["quantity"] < 1
            ):
                raise ValueError("产物包装数量须是正整数，真实质量另留在批次账")
            if (
                c.execute("SELECT 1 FROM item_types WHERE id=?", (out["item_type_id"],)).fetchone()
                is None
            ):
                raise ValueError("产物须有实际登记的载体类型")
        ids = []
        with material_transaction(c, tuple(input_ids)):
            for item in items:
                cls._reduce(c, item, item["quantity"])
            for out in outputs:
                iid = str(uuid4())
                ids.append(iid)
                c.execute(
                    "INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,owner_character_id,quantity,condition,fresh_until_world_time,spoils_world_time) VALUES (?,?,?,?,?,?,?,100,?,?)",
                    (
                        iid,
                        world_id,
                        out["item_type_id"],
                        container_type,
                        container_id,
                        owner_id,
                        out["quantity"],
                        items[0]["fresh_until_world_time"],
                        items[0]["spoils_world_time"],
                    ),
                )
                c.execute(
                    "INSERT INTO material_batches VALUES (?,?,?,?)",
                    (iid, world_id, out["quantity"], encoded(asdict(out["state"]))),
                )
            cls._record(c, world_id, "transform", before, after, [*input_ids, *ids])
        return ids

    @classmethod
    def rebalance(cls, c, updates, *, operation="exchange"):
        """哥哥，自然交换保留实物标识，但物质仍须从另一份实际材料转入。"""
        if not updates:
            raise ValueError("物质交换需要实际输入")
        before = [cls.state(c, iid) for iid in updates]
        items = [
            c.execute("SELECT * FROM item_instances WHERE id=?", (iid,)).fetchone()
            for iid in updates
        ]
        if (
            any(s is None for s in before)
            or any(i is None for i in items)
            or len({i["world_id"] for i in items}) != 1
        ):
            raise ValueError("物质交换引用了不存在或跨世界的实物")
        after = list(updates.values())
        if any(not isinstance(s, MaterialState) for s in after):
            raise ValueError("交换状态须经材料类型验证")
        require_pigment_conservation(before, after)
        if operation == "natural_age" and any(
            replace(new, age_hours=old.age_hours) != old or new.age_hours < old.age_hours
            for old, new in zip(before, after, strict=True)
        ):
            raise ValueError("紧凑年龄账只允许记录年龄增长，不能隐藏任何实际材料变化")
        for key in ("mass", "particles", "water", "nutrition", "salt"):

            def total(states, key=key):
                return sum(salt_total(s) if key == "salt" else getattr(s, key) for s in states)

            if not math.isclose(total(before), total(after), rel_tol=1e-9, abs_tol=1e-8):
                raise ValueError("物质交换未守恒：" + key)
        with material_transaction(c, tuple(updates)):
            for item, state in zip(items, after):
                cls._write(c, item["id"], item["quantity"], state)
            if operation == "natural_age":
                wid = items[0]["world_id"]
                at = c.execute(
                    "SELECT w.current_time FROM worlds w WHERE w.id=?", (wid,)
                ).fetchone()[0]
                c.execute(
                    "INSERT INTO material_clock_steps VALUES (?,?,?,?,?,?)",
                    (
                        str(uuid4()),
                        wid,
                        encoded(list(updates)),
                        encoded([s.age_hours for s in before]),
                        encoded([s.age_hours for s in after]),
                        at,
                    ),
                )
            else:
                cls._record(c, items[0]["world_id"], operation, before, after, list(updates))

    @classmethod
    def recover_sink(
        cls, c, sink_id, *, item_type_id, container_type, container_id, owner_id, carrier=None
    ):
        from dataclasses import replace

        sink = c.execute("SELECT * FROM material_sinks WHERE id=?", (sink_id,)).fetchone()
        if (
            sink is None
            or c.execute(
                "SELECT 1 FROM collective_sink_recoveries WHERE sink_id=?", (sink_id,)
            ).fetchone()
        ):
            raise ValueError("物质汇不存在或已经实物回收")
        state = MaterialState(**json.loads(sink["state_json"]))
        iid = str(uuid4())
        with material_transaction(c):
            c.execute(
                "INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,owner_character_id,quantity,condition) VALUES (?,?,?,?,?,?,1,100)",
                (iid, sink["world_id"], item_type_id, container_type, container_id, owner_id),
            )
            after = replace(state, carrier=carrier or state.carrier)
            c.execute(
                "INSERT INTO material_batches VALUES (?,?,1,?)",
                (iid, sink["world_id"], encoded(asdict(after))),
            )
            c.execute(
                "UPDATE material_sinks SET state_json=? WHERE id=?",
                (encoded(asdict(fraction(state, 0))), sink_id),
            )
            at = c.execute(
                "SELECT w.current_time FROM worlds w WHERE w.id=?", (sink["world_id"],)
            ).fetchone()[0]
            c.execute("INSERT INTO collective_sink_recoveries VALUES (?,?,?)", (sink_id, iid, at))
            cls._record(
                c,
                sink["world_id"],
                "recover_sink",
                [state],
                [after],
                [sink_id, iid],
                container_type + ":" + container_id,
            )
        return iid

    @classmethod
    def audit(cls, c, world_id):
        origin = [
            MaterialState(**v)
            for row in c.execute(
                "SELECT after_json FROM material_ledger WHERE world_id=? AND operation='origin'",
                (world_id,),
            )
            for v in json.loads(row[0])
        ]
        rows = c.execute(
            (
                "SELECT b.*,i.quantity FROM material_batches b JOIN item_instances i ON "
                "i.id=b.item_id WHERE b.world_id=?"
            ),
            (world_id,),
        ).fetchall()
        batches = [MaterialState(**json.loads(row["state_json"])) for row in rows]
        sinks = [
            MaterialState(**json.loads(row[0]))
            for row in c.execute(
                "SELECT state_json FROM material_sinks WHERE world_id=?", (world_id,)
            )
        ]

        def totals(states):
            values = {k: sum(getattr(s, k) for s in states) for k in EXTENSIVE}
            values["salt"] = sum(salt_total(s) for s in states)
            values["pigment"] = pigment_total(states)
            return values

        a, b, d = totals(origin), totals(batches), totals(sinks)
        conserved = ("mass", "particles", "water", "salt", "nutrition", "pigment")
        mismatches = [row["item_id"] for row in rows if row["quantity_ref"] != row["quantity"]]
        balanced = not mismatches and all(
            math.isclose(a[k], b[k] + d[k], abs_tol=1e-8, rel_tol=1e-9) for k in conserved
        )
        return {
            "balanced": balanced,
            "origins": a,
            "batches": b,
            "sinks": d,
            "quantity_mismatches": mismatches,
        }
