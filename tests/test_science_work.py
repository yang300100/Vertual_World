"""哥哥，工艺必须通过真实工单消耗燃料，检验也要花时间。"""

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from world_engine.collective_cooperation import CooperativeService as C
from world_engine.collective_work import WorkService as W
from world_engine.life import LifeActivityService as L
from world_engine.materials import MaterialService as M
from world_engine.repository import to_iso
from world_engine.science_workshop import seed_workshop

pytest_plugins = ["test_collective_research"]


def queue(c, actor, task, at):
    task = {"key": "test", "worker_id": actor, "quantity": 1, "hours": 1, **task}
    pid = C.propose(c, actor, {"reason": "测试实际工艺", "tasks": [task]}, at)
    C.accept(
        c,
        actor,
        {"proposal_id": pid, "version": 1, "task_keys": ["test"], "reason": "本人安排测试"},
        at,
    )
    C.start(c, actor, {"proposal_id": pid, "version": 1}, at)
    return pid


def finish(c, wid, at):
    # 哥哥，这里只借用真实心跳记录，随后沿同样的劳动推进路径结算。
    heartbeat = "test:" + to_iso(at)
    before = c.execute("SELECT w.current_time FROM worlds w WHERE id=?", (wid,)).fetchone()[0]
    c.execute(
        (
            "INSERT INTO world_heartbeats(id,world_id,real_time,real_elapsed_s"
            "econds,time_scale,world_delta_seconds,world_time_before,world_tim"
            "e_after,created_at) VALUES (?,?,?,?,?,?,?,?,?)"
        ),
        (heartbeat, wid, to_iso(at), 3600, 1, 3600, before, to_iso(at), to_iso(at)),
    )
    c.execute("UPDATE worlds SET current_time=? WHERE id=?", (to_iso(at), wid))
    L.advance(c, wid, at, heartbeat)
    W.tick(c, wid, at)


def test_heat_consumes_actual_fuel_and_waits_for_labor(lab):
    db, wid, actors, _, _, at = lab
    with db.write() as c:
        a = seed_workshop(c, wid, at, seed=7)["actors"][actors[0]]
        before = c.execute(
            "SELECT quantity FROM item_instances WHERE id=?", (a["energy"],)
        ).fetchone()[0]
        pid = queue(
            c,
            actors[0],
            {
                "batch_id": a["materials"]["clay"],
                "operation": "heat",
                "settings": {"temperature": 1000, "power": 4},
                "apparatus_id": a["apparatus"],
                "energy_id": a["energy"],
            },
            at,
        )
        W.tick(c, wid, at)
        order = c.execute(
            "SELECT * FROM collective_work_orders WHERE proposal_id=?", (pid,)
        ).fetchone()
        assert order["status"] == "working", order["result_json"]
        assert json.loads(order["outputs_json"]) == []
        assert (
            c.execute("SELECT quantity FROM item_instances WHERE id=?", (a["energy"],)).fetchone()[
                0
            ]
            < before
        )
        finish(c, wid, at + timedelta(hours=1))
        order = c.execute(
            "SELECT * FROM collective_work_orders WHERE proposal_id=?", (pid,)
        ).fetchone()
        assert order["status"] == "completed"
        product = json.loads(order["outputs_json"])[0]
        assert M.state(c, product).temperature > 20
        assert M.audit(c, wid)["balanced"]


@pytest.mark.parametrize("fuel", [None, "missing"])
def test_heat_without_real_energy_never_produces_results(lab, fuel):
    db, wid, actors, _, _, at = lab
    with db.write() as c:
        a = seed_workshop(c, wid, at, seed=7)["actors"][actors[0]]
        pid = queue(
            c,
            actors[0],
            {
                "batch_id": a["materials"]["clay"],
                "operation": "heat",
                "settings": {"temperature": 900},
                "apparatus_id": a["apparatus"],
                "energy_id": fuel,
            },
            at,
        )
        W.tick(c, wid, at)
        order = c.execute(
            "SELECT * FROM collective_work_orders WHERE proposal_id=?", (pid,)
        ).fetchone()
        assert order["status"] == "blocked"
        assert "能源" in order["result_json"] or "材料" in order["result_json"]
        assert not json.loads(order["outputs_json"])


def test_craft_assay_has_real_precision_and_does_not_read_hidden_profile(lab):
    db, wid, actors, _, _, at = lab
    with db.write() as c:
        a = seed_workshop(c, wid, at, seed=7)["actors"][actors[0]]
        pid = queue(
            c,
            actors[0],
            {
                "batch_id": a["references"]["clay"],
                "operation": "assay",
                "method": "strength",
                "instrument_id": a["instruments"]["strength"],
                "prediction": {"metric": "strength", "low": 0, "high": 100},
            },
            at,
        )
        W.tick(c, wid, at)
        finish(c, wid, at + timedelta(hours=1))
        order = c.execute(
            "SELECT * FROM collective_work_orders WHERE proposal_id=?", (pid,)
        ).fetchone()
        assert order["status"] == "completed", order["result_json"]
        result = json.loads(order["result_json"])["measurement"]
        assert result["precision"] == 0.1 and result["value"] > 0
        assert "strength_scale" not in order["result_json"]
        assert M.audit(c, wid)["balanced"]


def test_actual_material_transform_cannot_invent_pigment(lab):
    db, wid, actors, _, _, at = lab
    with db.write() as c:
        a = seed_workshop(c, wid, at, seed=7)["actors"][actors[0]]
        iid = a["materials"]["fiber"]
        item = c.execute("SELECT * FROM item_instances WHERE id=?", (iid,)).fetchone()
        with pytest.raises(ValueError, match="颜料"):
            M.transform(
                c,
                [iid],
                [
                    {
                        "item_type_id": item["item_type_id"],
                        "quantity": item["quantity"],
                        "state": replace(M.state(c, iid), pigment_fixed=999),
                    }
                ],
                container_type="ground",
                container_id=a["site_id"],
                owner_id=actors[0],
            )
