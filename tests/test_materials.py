"""Yuki 核验真实库存与批次账，不让转移、消耗或重启洗掉污染。"""

import importlib.util
import sqlite3
from uuid import uuid4

import pytest

from world_engine.repository import WorldRepository


def service():
    assert importlib.util.find_spec("world_engine.materials"), "尚无持久化材料能力"
    from world_engine.materials import MaterialService

    return MaterialService


@pytest.fixture
def stock(database):
    with database.write() as c:
        wid = WorldRepository().create_world(
            c, name="材料账测试", minutes_per_tick=60, seed_demo=False, seed_maps=False
        )
        tid, iid = str(uuid4()), str(uuid4())
        c.execute(
            (
                "INSERT INTO item_types(id,name,category,stack_limit,slot_size) VALUES (?,"
                "?,'food',100,1)"
            ),
            (tid, "试验谷物"),
        )
        c.execute(
            (
                "INSERT INTO item_instances(id,world_id,item_type_id,container_type,"
                "container_id,quantity,condition) VALUES (?,?,?,'ground','laboratory',10,"
                "100)"
            ),
            (iid, wid, tid),
        )
    return database, wid, iid


def attach(c, iid):
    from world_engine.material_physics import MaterialState

    service().attach(c, iid, MaterialState(mass=10, mobile=2, particles=1, nutrition=100))


def test_split_keeps_proportional_salt_and_real_item_quantities(stock):
    db, wid, iid = stock
    s = service()
    with db.write() as c:
        attach(c, iid)
        item = c.execute("SELECT * FROM item_instances WHERE id=?", (iid,)).fetchone()
        new_id = str(uuid4())
        s.split(c, item, 3, new_id)
        assert s.state(c, iid).mobile == pytest.approx(1.4)
        assert s.state(c, new_id).mobile == pytest.approx(0.6)
        assert (
            c.execute("SELECT quantity FROM item_instances WHERE id=?", (iid,)).fetchone()[0] == 7
        )
        assert (
            c.execute("SELECT quantity FROM item_instances WHERE id=?", (new_id,)).fetchone()[0]
            == 3
        )
        assert s.audit(c, wid)["balanced"]


def test_merge_keeps_both_pollutant_loads_and_seed_quality(stock):
    db, wid, iid = stock
    s = service()
    with db.write() as c:
        attach(c, iid)
        item = c.execute("SELECT * FROM item_instances WHERE id=?", (iid,)).fetchone()
        new_id = str(uuid4())
        s.split(c, item, 3, new_id)
        a = c.execute("SELECT * FROM item_instances WHERE id=?", (iid,)).fetchone()
        b = c.execute("SELECT * FROM item_instances WHERE id=?", (new_id,)).fetchone()
        s.merge(c, b, a, 2)
        assert s.state(c, iid).mobile == pytest.approx(1.8)
        assert s.state(c, new_id).mobile == pytest.approx(0.2)
        assert (
            c.execute("SELECT quantity FROM item_instances WHERE id=?", (iid,)).fetchone()[0] == 9
        )
        assert s.audit(c, wid)["balanced"]


def test_consumption_keeps_removed_pollution_in_explicit_destination(stock):
    db, wid, iid = stock
    s = service()
    with db.write() as c:
        attach(c, iid)
        item = c.execute("SELECT * FROM item_instances WHERE id=?", (iid,)).fetchone()
        s.consume(c, item, 4, destination="body:test-character")
        assert s.state(c, iid).mobile == pytest.approx(1.2)
        assert s.audit(c, wid)["sinks"]["mobile"] == pytest.approx(0.8)
        item = c.execute("SELECT * FROM item_instances WHERE id=?", (iid,)).fetchone()
        s.consume(c, item, 6, destination="body:test-character")
        assert s.state(c, iid) is None
        assert c.execute("SELECT 1 FROM item_instances WHERE id=?", (iid,)).fetchone() is None
        assert s.audit(c, wid)["balanced"]
        assert s.audit(c, wid)["sinks"]["mobile"] == pytest.approx(2)


def test_raw_quantity_write_cannot_silently_lose_material(stock):
    db, _, iid = stock
    s = service()
    with db.write() as c:
        attach(c, iid)
        with pytest.raises(Exception, match="材料|material"):
            c.execute("UPDATE item_instances SET quantity=1 WHERE id=?", (iid,))
        with pytest.raises(Exception, match="材料|material"):
            c.execute("DELETE FROM item_instances WHERE id=?", (iid,))
        assert s.state(c, iid).mobile == 2


def test_missing_destination_is_rejected_before_any_change(stock):
    db, wid, iid = stock
    s = service()
    with db.write() as c:
        attach(c, iid)
        item = c.execute("SELECT * FROM item_instances WHERE id=?", (iid,)).fetchone()
        with pytest.raises(ValueError):
            s.consume(c, item, 2, destination="")
        assert s.state(c, iid).mobile == 2
        assert s.audit(c, wid)["balanced"]


def test_failed_split_rolls_back_even_when_error_is_caught(stock):
    db, wid, iid = stock
    s = service()
    with db.write() as c:
        attach(c, iid)
        item = c.execute("SELECT * FROM item_instances WHERE id=?", (iid,)).fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            s.split(c, item, 3, iid)
        assert s.state(c, iid).mobile == 2
        assert (
            c.execute("SELECT quantity FROM item_instances WHERE id=?", (iid,)).fetchone()[0] == 10
        )
        assert s.audit(c, wid)["balanced"]


def test_reinitialization_preserves_material_and_immutable_origin(stock):
    db, wid, iid = stock
    s = service()
    with db.write() as c:
        attach(c, iid)
    db.initialize()
    with db.read() as c:
        assert s.state(c, iid).mobile == 2
        assert s.audit(c, wid)["balanced"]
        assert (
            c.execute("SELECT COUNT(*) FROM material_ledger WHERE operation='origin'").fetchone()[0]
            == 1
        )


def test_ledger_cannot_be_rewritten(stock):
    db, _, iid = stock
    service()
    with db.write() as c:
        attach(c, iid)
        with pytest.raises(Exception, match="材料|material"):
            c.execute("UPDATE material_ledger SET operation='other'")


def test_processing_outputs_replace_inputs_without_double_crediting_origin(stock):
    from world_engine.material_physics import MaterialState

    db, wid, iid = stock
    s = service()
    with db.write() as c:
        attach(c, iid)
        tid = c.execute("SELECT item_type_id FROM item_instances WHERE id=?", (iid,)).fetchone()[0]
        outputs = s.transform(
            c,
            [iid],
            [
                {
                    "item_type_id": tid,
                    "quantity": 7,
                    "state": MaterialState(mass=7, mobile=0.5, particles=0.1, nutrition=70),
                },
                {
                    "item_type_id": tid,
                    "quantity": 3,
                    "state": MaterialState(
                        carrier="screenings", mass=3, mobile=1.5, particles=0.9, nutrition=30
                    ),
                },
            ],
            container_type="ground",
            container_id="laboratory",
            owner_id=None,
        )
        assert len(outputs) == 2
        assert s.audit(c, wid)["origins"]["salt"] == 2
        assert s.audit(c, wid)["balanced"]
        assert c.execute("SELECT 1 FROM item_instances WHERE id=?", (iid,)).fetchone() is None


def test_processing_cannot_claim_salt_vanished(stock):
    from world_engine.material_physics import MaterialState

    db, wid, iid = stock
    s = service()
    with db.write() as c:
        attach(c, iid)
        tid = c.execute("SELECT item_type_id FROM item_instances WHERE id=?", (iid,)).fetchone()[0]
        with pytest.raises(ValueError, match="守恒|物质"):
            s.transform(
                c,
                [iid],
                [
                    {
                        "item_type_id": tid,
                        "quantity": 10,
                        "state": MaterialState(mass=10, nutrition=100),
                    }
                ],
                container_type="ground",
                container_id="laboratory",
                owner_id=None,
            )
        assert s.state(c, iid).mobile == 2
        assert s.audit(c, wid)["balanced"]


@pytest.mark.parametrize("quantity", [0, -1, 11, 1.5, True])
def test_quantity_validation_rejects_before_mutation(stock, quantity):
    db, wid, iid = stock
    s = service()
    with db.write() as c:
        attach(c, iid)
        item = c.execute("SELECT * FROM item_instances WHERE id=?", (iid,)).fetchone()
        with pytest.raises(ValueError):
            s.split(c, item, quantity, str(uuid4()))
        assert s.audit(c, wid)["balanced"]
        assert s.state(c, iid).mobile == 2
