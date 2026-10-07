"""哥哥，真实生产失败必须让本人知道具体条件，所有检验均不请求 API。"""

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from test_collective_research import lab  # noqa: F401
from test_collective_runner import ExternalBackend

from world_engine.clock import WorldClockService
from world_engine.collective_clock import settle
from world_engine.collective_research import CollectiveResearchService as R
from world_engine.collective_runner import CollectiveRunner
from world_engine.config import Settings
from world_engine.material_physics import MaterialState
from world_engine.materials import MaterialService as M


@pytest.fixture
def production_lab(lab):
    db, wid, actors, batches, _, at = lab
    water, apparatus = str(uuid4()), str(uuid4())
    with db.write() as c:
        for iid, name, category, quantity in (
            (water, "实际供水", "material", 100),
            (apparatus, "离线浸洗槽", "tool", 1),
        ):
            tid = str(uuid4())
            c.execute(
                "INSERT INTO item_types(id,name,category,stack_limit,slot_size) VALUES (?,?,?,1000,1)",
                (tid, name, category),
            )
            c.execute(
                "INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,owner_character_id,quantity,condition) VALUES (?,?,?,'character_inventory',?,?,?,100)",
                (iid, wid, tid, actors[0], actors[0], quantity),
            )
        M.attach(c, water, MaterialState(carrier="water", mass=0, water=100))
        stations = [
            {"skill_name": "检验", "min_proficiency": 40, "minimum_load": 50},
            {"skill_name": "检验", "min_proficiency": 40, "minimum_load": 50},
        ]
        c.execute(
            "INSERT INTO collective_apparatus(item_id,world_id,operations_json,capacity,throughput,skill_name,min_proficiency,stations_json) VALUES (?,?,'[\"wash\"]',200,200,'检验',40,?)",
            (apparatus, wid, json.dumps(stations)),
        )
        c.execute("UPDATE collective_people SET enabled=0 WHERE character_id=?", (actors[1],))
    return db, wid, actors[0], batches[0], water, apparatus, at


def washing(batch, apparatus, *, water=None, quantity=8, water_quantity=16):
    command = {
        "op": "produce",
        "operation": "wash",
        "batch_id": batch,
        "quantity": quantity,
        "apparatus_id": apparatus,
        "hours": 1,
        "settings": {"water": water_quantity},
        "result_key": "washed",
    }
    if water:
        command.update(auxiliary_ids=[water], auxiliary_quantities={water: water_quantity})
    return {"reason": "离线测试本人安排的真实浸洗操作", "commands": [command]}


@pytest.mark.parametrize(
    ("provided_water", "quantity", "water_quantity", "expected_reason"),
    [(False, 8, 16, "实际水源"), (True, 10, 60, "更多同时操作的位置")],
)
def test_production_failure_keeps_actual_reason_after_submission_rollback(
    production_lab, provided_water, quantity, water_quantity, expected_reason
):
    db, wid, actor, batch, water, apparatus, at = production_lab
    backend = ExternalBackend(
        program=washing(
            batch,
            apparatus,
            water=water if provided_water else None,
            quantity=quantity,
            water_quantity=water_quantity,
        )
    )
    runner = CollectiveRunner(db, backend)
    assert runner.decide(wid, at)
    assert runner.failure_reason(wid) is None
    with db.read() as c:
        call = c.execute("SELECT * FROM collective_model_calls").fetchone()
        assert call["status"] == "rejected"
        assert expected_reason in call["error"]
        view = R.context(c, actor, at)
        assert any(expected_reason in x.get("error", "") for x in view["own_submissions"])
        assert any(
            expected_reason in x["content"].get("reason", "")
            for x in view["observations"]
            if x["kind"] == "blocked"
        )
        assert c.execute("SELECT count(*) FROM collective_programs").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM collective_work_orders").fetchone()[0] == 0
        assert (
            c.execute("SELECT quantity FROM item_instances WHERE id=?", (batch,)).fetchone()[0]
            == 100
        )
        assert (
            c.execute("SELECT quantity FROM item_instances WHERE id=?", (water,)).fetchone()[0]
            == 100
        )
        assert M.audit(c, wid)["balanced"]
    assert runner.usage(wid)["known_tokens"] == 150


def test_own_corrected_small_production_waits_for_real_work_and_preserves_rejection(production_lab):
    db, wid, actor, batch, water, apparatus, at = production_lab
    backend = ExternalBackend(program=washing(batch, apparatus))
    runner = CollectiveRunner(db, backend)
    assert runner.decide(wid, at)
    backend.program = washing(batch, apparatus, water=water)
    assert runner.decide(wid, at)
    with db.read() as c:
        assert [
            r[0] for r in c.execute("SELECT status FROM collective_model_calls ORDER BY rowid")
        ] == ["rejected", "succeeded"]
        program = c.execute("SELECT * FROM collective_programs").fetchone()
        assert program["status"] == "active"
        assert c.execute("SELECT status FROM collective_work_orders").fetchone()[0] == "working"
        assert c.execute("SELECT outputs_json FROM collective_work_orders").fetchone()[0] == "[]"
        assert (
            c.execute("SELECT quantity FROM item_instances WHERE id=?", (batch,)).fetchone()[0]
            == 92
        )
        assert (
            c.execute("SELECT quantity FROM item_instances WHERE id=?", (water,)).fetchone()[0]
            == 84
        )
        assert M.audit(c, wid)["balanced"]
    WorldClockService(db, Settings(database_path=db.path)).heartbeat(wid, elapsed_seconds=3600)
    with db.write() as c:
        settle(c, wid, at + timedelta(hours=1))
        result = c.execute("SELECT * FROM collective_programs").fetchone()
        assert result["status"] == "completed"
        outputs = json.loads(result["bindings_json"])["washed"]
        assert len(outputs) >= 2 and all(M.state(c, iid) is not None for iid in outputs)
        assert M.audit(c, wid)["balanced"]
    assert runner.failure_reason(wid) is None
    assert runner.usage(wid)["known_tokens"] == 300
