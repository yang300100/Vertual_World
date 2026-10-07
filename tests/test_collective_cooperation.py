"""Yuki 核对实际送达、独立同意和版本修改，签名不能代替劳动。"""

import importlib.util
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from test_collective_research import lab  # noqa: F401


def cooperation():
    assert importlib.util.find_spec("world_engine.collective_cooperation"), "尚无独立共同研究能力"
    from world_engine.collective_cooperation import CooperativeService

    return CooperativeService


def test_received_report_is_not_knowledge_before_delivery(lab):
    cooperation()
    from world_engine.collective_messages import MessageService

    db, wid, actors, _, _, at = lab
    with db.write() as c:
        c.execute("INSERT INTO collective_contacts VALUES (?,?,?)", (actors[0], actors[1], 2))
        oid = c.execute(
            "SELECT id FROM collective_observations WHERE character_id=?", (actors[0],)
        ).fetchone()[0]
        MessageService.send(
            c, actors[0], actors[1], at, text="请独立检查这份记录", evidence_ids=[oid]
        )
        assert (
            c.execute(
                "SELECT COUNT(*) FROM collective_observations WHERE character_id=?", (actors[1],)
            ).fetchone()[0]
            == 0
        )
        MessageService.deliver(c, wid, at + timedelta(hours=1))
        assert (
            c.execute(
                "SELECT COUNT(*) FROM collective_observations WHERE character_id=?", (actors[1],)
            ).fetchone()[0]
            == 0
        )
        MessageService.deliver(c, wid, at + timedelta(hours=2))
        rows = c.execute(
            "SELECT * FROM collective_observations WHERE character_id=?", (actors[1],)
        ).fetchall()
        assert len(rows) == 1 and rows[0]["source_observation_id"] == oid


def proposal(actors, batches):
    return {
        "reason": "请合作验证物理处理与检验",
        "tasks": [
            {
                "key": "raw-check",
                "worker_id": actors[0],
                "operation": "store",
                "batch_id": batches[0],
                "quantity": 1,
                "hours": 1,
            },
            {
                "key": "second-check",
                "worker_id": actors[1],
                "operation": "store",
                "batch_id": batches[1],
                "quantity": 1,
                "hours": 2,
            },
        ],
    }


def test_acceptance_cannot_grant_resources_for_unaccepted_tasks(lab):
    db, _, actors, batches, _, at = lab
    with db.write() as c:
        body = proposal(actors, batches)
        body["tasks"][1]["batch_id"] = batches[0]
        pid = cooperation().propose(c, actors[0], body, at)
        cooperation().accept(
            c,
            actors[0],
            {
                "proposal_id": pid,
                "version": 1,
                "task_keys": ["raw-check"],
                "reason": "只同意本人这项工作",
            },
            at,
        )
        assert not c.execute(
            "SELECT 1 FROM collective_resource_permissions WHERE proposal_id=? AND allowed_actor_id=?",
            (pid, actors[1]),
        ).fetchone()


def test_unreceived_proposal_cannot_be_accepted_by_guessing_identifier(lab):
    s = cooperation()
    db, _, actors, batches, _, at = lab
    with db.write() as c:
        pid = s.propose(c, actors[0], proposal(actors, batches), at)
        with pytest.raises(ValueError, match="收到|已知"):
            s.accept(
                c,
                actors[1],
                {
                    "proposal_id": pid,
                    "version": 1,
                    "task_keys": ["second-check"],
                    "reason": "愿意审查",
                },
                at,
            )


def test_revision_invalidates_only_affected_acceptance(lab):
    s = cooperation()
    db, _, actors, batches, _, at = lab
    from world_engine.collective_messages import MessageService

    with db.write() as c:
        pid = s.propose(c, actors[0], proposal(actors, batches), at)
        c.execute("INSERT INTO collective_contacts VALUES (?,?,?)", (actors[0], actors[1], 0))
        MessageService.send(c, actors[0], actors[1], at, text="请评议", proposal_id=pid, version=1)
        MessageService.deliver(
            c,
            c.execute("SELECT world_id FROM characters WHERE id=?", (actors[0],)).fetchone()[0],
            at,
        )
        s.accept(
            c,
            actors[0],
            {
                "proposal_id": pid,
                "version": 1,
                "task_keys": ["raw-check"],
                "reason": "本人同意这一任务",
            },
            at,
        )
        s.accept(
            c,
            actors[1],
            {
                "proposal_id": pid,
                "version": 1,
                "task_keys": ["second-check"],
                "reason": "本人的独立判断",
            },
            at,
        )
        revised = proposal(actors, batches)
        revised.update(proposal_id=pid, version=1)
        revised["tasks"][1]["hours"] = 3
        version = s.revise(c, actors[0], revised, at + timedelta(hours=1))
        assert version == 2
        active = c.execute(
            "SELECT task_key,actor_id FROM collective_acceptances WHERE proposal_id=? AND version=2",
            (pid,),
        ).fetchall()
        assert [(x["task_key"], x["actor_id"]) for x in active] == [("raw-check", actors[0])]
        assert (
            c.execute(
                "SELECT COUNT(*) FROM collective_proposal_versions WHERE proposal_id=?", (pid,)
            ).fetchone()[0]
            == 2
        )


def test_start_requires_real_worker_consent_and_does_not_reserve_on_rejection(lab):
    s = cooperation()
    db, _, actors, batches, _, at = lab
    with db.write() as c:
        pid = s.propose(c, actors[0], proposal(actors, batches), at)
        s.accept(
            c,
            actors[0],
            {"proposal_id": pid, "version": 1, "task_keys": ["raw-check"], "reason": "本人同意"},
            at,
        )
        with pytest.raises(ValueError, match="同意|参与"):
            s.start(c, actors[0], {"proposal_id": pid, "version": 1}, at)
        assert (
            c.execute("SELECT quantity FROM item_instances WHERE id=?", (batches[0],)).fetchone()[0]
            == 100
        )
        assert c.execute("SELECT COUNT(*) FROM collective_work_orders").fetchone()[0] == 0


def test_independently_accepted_work_uses_actual_bodies_and_resources(lab):
    s = cooperation()
    db, wid, actors, batches, _, at = lab
    from world_engine.collective_messages import MessageService

    with db.write() as c:
        pid = s.propose(c, actors[0], proposal(actors, batches), at)
        c.execute("INSERT INTO collective_contacts VALUES (?,?,?)", (actors[0], actors[1], 0))
        MessageService.send(
            c, actors[0], actors[1], at, text="本人提出的可评议任务", proposal_id=pid, version=1
        )
        MessageService.deliver(c, wid, at)
        for actor, key in zip(actors, ["raw-check", "second-check"], strict=True):
            s.accept(
                c,
                actor,
                {
                    "proposal_id": pid,
                    "version": 1,
                    "task_keys": [key],
                    "reason": "本人审阅后同意明确工作",
                },
                at,
            )
        s.start(c, actors[0], {"proposal_id": pid, "version": 1}, at)
        assert (
            c.execute(
                "SELECT COUNT(*) FROM character_life_activities WHERE status='running'"
            ).fetchone()[0]
            == 2
        )
        assert (
            c.execute("SELECT quantity FROM item_instances WHERE id=?", (batches[0],)).fetchone()[0]
            == 99
        )
        assert (
            c.execute("SELECT quantity FROM item_instances WHERE id=?", (batches[1],)).fetchone()[0]
            == 99
        )
    from world_engine.clock import WorldClockService
    from world_engine.collective_work import WorkService
    from world_engine.config import Settings

    WorldClockService(db, Settings(database_path=db.path)).heartbeat(wid, elapsed_seconds=2 * 3600)
    with db.write() as c:
        WorkService.tick(c, wid, at + timedelta(hours=2))
        assert (
            c.execute(
                "SELECT COUNT(*) FROM collective_work_orders WHERE status='completed'"
            ).fetchone()[0]
            == 2
        )
        from world_engine.materials import MaterialService

        assert MaterialService.audit(c, wid)["balanced"]


def test_changed_task_cannot_inherit_resource_permission(lab):
    s = cooperation()
    db, _, actors, batches, _, at = lab
    with db.write() as c:
        body = proposal(actors, batches)
        body["tasks"][1]["batch_id"] = batches[0]
        pid = s.propose(c, actors[0], body, at)
        s.accept(
            c,
            actors[0],
            {
                "proposal_id": pid,
                "version": 1,
                "task_keys": ["raw-check"],
                "reason": "只许可当前方案",
            },
            at,
        )
        body.update(proposal_id=pid, version=1)
        body["tasks"][1]["quantity"] = 2
        s.revise(c, actors[0], body, at)
        assert (
            c.execute(
                "SELECT COUNT(*) FROM collective_resource_permissions WHERE proposal_id=? AND version=2 AND item_id=? AND allowed_actor_id=?",
                (pid, batches[0], actors[1]),
            ).fetchone()[0]
            == 0
        )


def test_task_output_reference_must_declare_its_dependency(lab):
    s = cooperation()
    db, _, actors, batches, _, at = lab
    with db.write() as c:
        body = proposal(actors, batches)
        body["tasks"][1]["batch_id"] = "task:raw-check:0"
        with pytest.raises(ValueError, match="依赖"):
            s.propose(c, actors[0], body, at)


def test_rejection_is_independent_and_prevents_start(lab):
    s = cooperation()
    db, wid, actors, batches, _, at = lab
    from world_engine.collective_messages import MessageService

    with db.write() as c:
        pid = s.propose(c, actors[0], proposal(actors, batches), at)
        c.execute("INSERT INTO collective_contacts VALUES (?,?,?)", (actors[0], actors[1], 0))
        MessageService.send(c, actors[0], actors[1], at, text="评议", proposal_id=pid, version=1)
        MessageService.deliver(c, wid, at)
        s.reject(
            c,
            actors[1],
            {
                "proposal_id": pid,
                "version": 1,
                "task_keys": ["second-check"],
                "reason": "本人认为目前证据不足",
            },
            at,
        )
        assert (
            c.execute(
                "SELECT reason FROM collective_rejections WHERE actor_id=?", (actors[1],)
            ).fetchone()[0]
            == "本人认为目前证据不足"
        )
        with pytest.raises(ValueError, match="同意|拒绝"):
            s.start(c, actors[0], {"proposal_id": pid, "version": 1}, at)


def apparatus(c, wid, actor, *, supplies=None, stations=None):
    tid, iid = str(uuid4()), str(uuid4())
    c.execute(
        "INSERT INTO item_types(id,name,category,stack_limit,slot_size) VALUES (?,'手工处理台','tool',1,1)",
        (tid,),
    )
    c.execute(
        "INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,owner_character_id,quantity,condition) VALUES (?,?,?,'character_inventory',?,?,1,100)",
        (iid, wid, tid, actor, actor),
    )
    c.execute(
        "INSERT INTO collective_apparatus VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            iid,
            wid,
            '["heat","store"]',
            1000,
            100,
            10,
            "检验",
            40,
            json.dumps(supplies or []),
            json.dumps(stations or []),
        ),
    )
    return iid


def start_one(c, actors, batches, at, **changes):
    s = cooperation()
    task = proposal(actors, batches)["tasks"][0]
    task.update(changes)
    pid = s.propose(c, actors[0], {"reason": "本人决定做这项工作", "tasks": [task]}, at)
    s.accept(
        c,
        actors[0],
        {
            "proposal_id": pid,
            "version": 1,
            "task_keys": [task["key"]],
            "reason": "本人已核对该工作",
        },
        at,
    )
    s.start(c, actors[0], {"proposal_id": pid, "version": 1}, at)
    return c.execute("SELECT * FROM collective_work_orders WHERE proposal_id=?", (pid,)).fetchone()


def test_remote_apparatus_cannot_be_operated(lab):
    db, wid, actors, batches, _, at = lab
    with db.write() as c:
        tool = apparatus(c, wid, actors[0])
        c.execute(
            "UPDATE item_instances SET container_type='ground',container_id='remote-unreachable-place' WHERE id=?",
            (tool,),
        )
        order = start_one(c, actors, batches, at, operation="heat", apparatus_id=tool)
        assert order["status"] == "blocked"
        assert (
            c.execute("SELECT quantity FROM item_instances WHERE id=?", (batches[0],)).fetchone()[0]
            == 100
        )


def test_heater_requires_real_consumable_fuel(lab):
    db, wid, actors, batches, _, at = lab
    with db.write() as c:
        tool = apparatus(c, wid, actors[0], supplies=[{"item_type_id": "no-fuel", "quantity": 1}])
        order = start_one(c, actors, batches, at, operation="heat", apparatus_id=tool)
        assert order["status"] == "blocked"
        assert "耗材" in order["result_json"]


def test_interruption_uses_actual_interruption_time_even_when_checked_late(lab):
    db, wid, actors, batches, _, at = lab
    from world_engine.collective_work import WorkService
    from world_engine.life import LifeActivityService

    with db.write() as c:
        order = start_one(c, actors, batches, at, hours=2)
        LifeActivityService.interrupt(c, actors[0], at + timedelta(hours=0.5), "本人停止")
        WorkService.tick(c, wid, at + timedelta(hours=8))
        result = json.loads(
            c.execute(
                "SELECT result_json FROM collective_work_orders WHERE id=?", (order["id"],)
            ).fetchone()[0]
        )
        assert result["actual_hours"] == 0.5


def test_scale_station_requires_distinct_simultaneous_people(lab):
    db, wid, actors, batches, _, at = lab
    with db.write() as c:
        tool = apparatus(
            c,
            wid,
            actors[0],
            stations=[
                {"skill_name": "检验", "min_proficiency": 40, "minimum_load": 10},
                {"skill_name": "检验", "min_proficiency": 40, "minimum_load": 10},
            ],
        )
        order = start_one(c, actors, batches, at, quantity=20, apparatus_id=tool)
        assert order["status"] == "blocked"
        assert "位置" in json.loads(order["result_json"])["error"]


def test_work_assay_records_actual_prediction_and_measurement(lab):
    db, wid, actors, batches, tool, at = lab
    from world_engine.clock import WorldClockService
    from world_engine.collective_work import WorkService
    from world_engine.config import Settings

    with db.write() as c:
        order = start_one(
            c,
            actors,
            batches,
            at,
            operation="assay",
            method="extractable_salt",
            instrument_id=tool,
            prediction={"metric": "extractable_salt", "low": 0.1, "high": 0.3},
        )
        assert order["status"] == "working"
    WorldClockService(db, Settings(database_path=db.path)).heartbeat(wid, elapsed_seconds=3600)
    with db.write() as c:
        WorkService.tick(c, wid, at + timedelta(hours=1))
        result = json.loads(
            c.execute(
                "SELECT result_json FROM collective_work_orders WHERE id=?", (order["id"],)
            ).fetchone()[0]
        )
        assert result["measurement"]["value"] == pytest.approx(0.2)
        assert result["measurement"]["prediction"]["low"] == 0.1


def test_joint_filter_cannot_create_or_refresh_capacity_from_parameters(lab):
    from test_collective_regions import material

    from world_engine.collective_work import WorkService
    from world_engine.life import LifeActivityService
    from world_engine.material_physics import MaterialState
    from world_engine.materials import MaterialService as M

    db, wid, actors, batches, _, at = lab
    with db.write() as c:
        device = apparatus(c, wid, actors[0])
        c.execute(
            "UPDATE collective_apparatus SET operations_json=? WHERE item_id=?",
            ('["filter"]', device),
        )
        absent = start_one(
            c,
            actors,
            batches,
            at,
            operation="filter",
            apparatus_id=device,
            settings={"capacity": 100},
        )
        assert absent["status"] == "blocked" and "滤材" in absent["result_json"]
        medium = material(c, wid, actors[0], MaterialState(carrier="filter_media", mass=0.001), 1)
        for n in range(2):
            now = at + timedelta(hours=n)
            order = start_one(
                c,
                actors,
                batches,
                now,
                operation="filter",
                apparatus_id=device,
                settings={"capacity": 100},
                auxiliary_ids=[medium],
            )
            assert order["status"] == "working"
            activity = LifeActivityService.running(c, actors[0])
            LifeActivityService.finish(
                c, activity, now + timedelta(hours=1), "completed", "过滤劳动完成"
            )
            WorkService.tick(c, wid, now + timedelta(hours=1))
            outputs = json.loads(
                c.execute(
                    "SELECT outputs_json FROM collective_work_orders WHERE id=?", (order["id"],)
                ).fetchone()[0]
            )
            medium = outputs[1]
            assert M.state(c, medium).particles == pytest.approx(0.0001)
            assert M.state(c, outputs[0]).particles == pytest.approx(0.0099 if n == 0 else 0.01)
        assert M.audit(c, wid)["balanced"]


def test_delayed_work_assay_requires_actual_reader_at_the_site(lab):
    db, wid, actors, batches, tool, at = lab
    from world_engine.clock import WorldClockService
    from world_engine.collective_work import WorkService
    from world_engine.config import Settings

    with db.write() as c:
        c.execute("UPDATE collective_instruments SET incubation_hours=2 WHERE item_id=?", (tool,))
        order = start_one(
            c,
            actors,
            batches,
            at,
            operation="assay",
            method="extractable_salt",
            instrument_id=tool,
            prediction={"metric": "extractable_salt", "low": 0.1, "high": 0.3},
        )
    WorldClockService(db, Settings(database_path=db.path)).heartbeat(wid, elapsed_seconds=3600)
    with db.write() as c:
        other = str(uuid4())
        c.execute(
            "INSERT INTO locations(id,world_id,name,kind,longitude,latitude) VALUES (?,?,'读者远方目的地','city',1,1)",
            (other, wid),
        )
        c.execute(
            "UPDATE characters SET current_location_id=?,longitude=1,latitude=1 WHERE id=?",
            (other, actors[0]),
        )
        WorkService.tick(c, wid, at + timedelta(hours=3))
        assert (
            c.execute(
                "SELECT COUNT(*) FROM collective_observations WHERE kind='work_measurement'"
            ).fetchone()[0]
            == 0
        )
        assert (
            c.execute(
                "SELECT status FROM collective_work_orders WHERE id=?", (order["id"],)
            ).fetchone()[0]
            == "working"
        )


def test_unchanged_resource_permission_survives_unrelated_revision(lab):
    s = cooperation()
    db, _, actors, batches, _, at = lab
    with db.write() as c:
        body = proposal(actors, batches)
        pid = s.propose(c, actors[0], body, at)
        s.accept(
            c,
            actors[0],
            {
                "proposal_id": pid,
                "version": 1,
                "task_keys": ["raw-check"],
                "reason": "许可这份原料与任务",
            },
            at,
        )
        body.update(proposal_id=pid, version=1)
        body["tasks"][1]["hours"] = 3
        s.revise(c, actors[0], body, at)
        assert (
            c.execute(
                "SELECT COUNT(*) FROM collective_resource_permissions WHERE proposal_id=? AND version=2 AND item_id=?",
                (pid, batches[0]),
            ).fetchone()[0]
            == 1
        )
