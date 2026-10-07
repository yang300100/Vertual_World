"""哥哥，人物只有自己实际取得的证据，预测不能等结果出来再补。"""

import importlib.util
import json
from datetime import timedelta
from uuid import uuid4

import pytest

from world_engine.material_physics import MaterialState
from world_engine.materials import MaterialService
from world_engine.repository import WorldRepository, to_iso


def research():
    assert importlib.util.find_spec("world_engine.collective_research"), "尚无多人研究能力"
    from world_engine.collective_research import CollectiveResearchService

    return CollectiveResearchService


@pytest.fixture
def lab(database):
    r = research()
    with database.write() as c:
        wid = WorldRepository().create_world(
            c, name="阶段研究", minutes_per_tick=60, seed_demo=False, seed_maps=False
        )
        at = WorldRepository().get_snapshot(c, wid).world.current_time
        loc = str(uuid4())
        c.execute(
            "INSERT INTO locations(id,world_id,name,kind,longitude,latitude) VALUES (?,?,'试验工坊','city',0,0)",
            (loc, wid),
        )
        actors = []
        for name in ("甲工匠", "乙检验者"):
            aid = str(uuid4())
            actors.append(aid)
            c.execute(
                "INSERT INTO characters(id,world_id,name,identity,location_id,current_location_id,longitude,latitude,energy,satiety,money,health,skills_json,goals_json,created_at,updated_at) VALUES (?,?,?,'工坊居民',?,?,0,0,100,100,100,100,'[\"检验\"]','[]',?,?)",
                (aid, wid, name, loc, loc, to_iso(at), to_iso(at)),
            )
            c.execute(
                "INSERT INTO collective_people(character_id,world_id,lab_location_id) VALUES (?,?,?)",
                (aid, wid, loc),
            )
            c.execute("UPDATE characters SET inventory_capacity=12 WHERE id=?", (aid,))
            c.execute(
                "INSERT INTO character_skill_proficiencies(world_id,character_id,skill_name,proficiency,updated_at) VALUES (?,?,?,?,?)",
                (wid, aid, "检验", 80, to_iso(at)),
            )
        tid = str(uuid4())
        c.execute(
            "INSERT INTO item_types(id,name,category,stack_limit,slot_size) VALUES (?,'试验原粮','food',1000,1)",
            (tid,),
        )
        batches = []
        for aid in actors:
            iid = str(uuid4())
            batches.append(iid)
            c.execute(
                "INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,owner_character_id,quantity,condition) VALUES (?,?,?,'character_inventory',?,?,100,100)",
                (iid, wid, tid, aid, aid),
            )
            MaterialService.attach(
                c, iid, MaterialState(mass=100, mobile=20, particles=1, nutrition=2000)
            )
        tool_type, tool = str(uuid4()), str(uuid4())
        c.execute(
            "INSERT INTO item_types(id,name,category,stack_limit,slot_size) VALUES (?,'浸提检验器','tool',1,1)",
            (tool_type,),
        )
        c.execute(
            "INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,owner_character_id,quantity,condition) VALUES (?,?,?,'character_inventory',?,?,1,100)",
            (tool, wid, tool_type, actors[0], actors[0]),
        )
        c.execute(
            "INSERT INTO collective_instruments(item_id,world_id,method,skill_name,min_proficiency,duration_hours,detection_limit,precision) VALUES (?,?,?,?,?,?,?,?)",
            (tool, wid, "extractable_salt", "检验", 40, 1, 0.01, 0.01),
        )
        r.record_observation(
            c,
            actors[0],
            at,
            "看见灰雨落入原粮",
            {"kind": "eyewitness", "appearance": "粮食表面有灰色细粉"},
        )
    return database, wid, actors, batches, tool, at


def test_context_contains_known_observations_and_owned_batches_only(lab):
    db, _, actors, batches, _, at = lab
    with db.read() as c:
        view = research().context(c, actors[0], at)
    text = json.dumps(view)
    assert batches[0] in text and batches[1] not in text
    assert "mobile" not in text and "adsorbed" not in text and "bound" not in text
    assert view["observations"] and view["instruments"]


def test_schema_requires_prediction_before_experiment():
    research()
    from world_engine.collective_types import CollectiveProgram

    with pytest.raises(ValueError):
        CollectiveProgram.model_validate(
            {
                "reason": "测试",
                "commands": [{"op": "experiment", "batch_id": "x", "method": "extractable_salt"}],
            }
        )


def test_production_waits_for_real_output_before_following_commands(lab):
    db, wid, actors, batches, _, at = lab
    with db.write() as c:
        pid = research().submit(
            c,
            actors[0],
            {
                "reason": "处理后才能收取真实产物",
                "commands": [
                    {
                        "op": "produce",
                        "operation": "store",
                        "batch_id": batches[0],
                        "quantity": 1,
                        "hours": 1,
                        "result_key": "product",
                    },
                    {"op": "collect", "batch_id": "$product:0", "quantity": 1},
                ],
            },
            at,
        )
        assert (
            c.execute("SELECT status FROM collective_programs WHERE id=?", (pid,)).fetchone()[0]
            == "active"
        )
    from world_engine.clock import WorldClockService
    from world_engine.config import Settings

    WorldClockService(db, Settings(database_path=db.path)).heartbeat(wid, elapsed_seconds=3600)
    from world_engine.collective_clock import settle

    with db.write() as c:
        settle(c, wid, at + timedelta(hours=1))
        row = c.execute("SELECT * FROM collective_programs WHERE id=?", (pid,)).fetchone()
        assert row["status"] == "completed"
        output = json.loads(row["bindings_json"])["product"][0]
        item = c.execute("SELECT * FROM item_instances WHERE id=?", (output,)).fetchone()
        assert item["container_type"] == "character_inventory" and item["container_id"] == actors[0]


def test_unknown_evidence_reference_is_rejected_without_program(lab):
    db, _, actors, _, _, at = lab
    with db.write() as c:
        with pytest.raises(ValueError, match="证据|已知"):
            research().submit(
                c,
                actors[0],
                {
                    "reason": "推断",
                    "hypotheses": [
                        {
                            "explanation": "污染可以检测",
                            "evidence_ids": ["unreceived"],
                            "predictions": [{"metric": "extractable_salt", "low": 0.1, "high": 1}],
                        }
                    ],
                    "commands": [],
                },
                at,
            )
        assert c.execute("SELECT COUNT(*) FROM collective_programs").fetchone()[0] == 0


def experiment(batch, tool):
    return {
        "reason": "检验亲见原粮，不把灰色外观直接当作盐含量",
        "hypotheses": [
            {
                "explanation": "原粮存在可浸出的污染",
                "evidence_ids": [],
                "predictions": [{"metric": "extractable_salt", "low": 0.1, "high": 0.4}],
            }
        ],
        "commands": [
            {
                "op": "experiment",
                "batch_id": batch,
                "quantity": 1,
                "method": "extractable_salt",
                "instrument_id": tool,
                "hypothesis_index": 0,
            }
        ],
    }


def test_experiment_consumes_real_sample_and_result_appears_after_work(lab):
    db, wid, actors, batches, tool, at = lab
    with db.write() as c:
        pid = research().submit(c, actors[0], experiment(batches[0], tool), at)
        assert (
            c.execute(
                "SELECT COUNT(*) FROM collective_observations WHERE kind='measurement'"
            ).fetchone()[0]
            == 0
        )
        activity = c.execute(
            "SELECT * FROM character_life_activities WHERE character_id=? AND status='running'",
            (actors[0],),
        ).fetchone()
        assert activity is not None and activity["kind"] == "research"
        assert (
            c.execute("SELECT quantity FROM item_instances WHERE id=?", (batches[0],)).fetchone()[0]
            == 99
        )
        assert MaterialService.audit(c, wid)["balanced"]
    from world_engine.clock import WorldClockService
    from world_engine.config import Settings

    clock = WorldClockService(db, Settings(database_path=db.path))
    clock.heartbeat(wid, elapsed_seconds=3600)
    with db.write() as c:
        research().tick(c, wid, at + timedelta(hours=1))
        rows = c.execute(
            "SELECT * FROM collective_observations WHERE kind='measurement'"
        ).fetchall()
        assert len(rows) == 1
        measured = json.loads(rows[0]["content_json"])
        assert measured["value"] == pytest.approx(0.2, abs=0.01)
        assert measured["prediction"]["low"] == 0.1
        assert measured["prediction_recorded_at"] <= measured["measured_at"]
        assert (
            c.execute("SELECT status FROM collective_programs WHERE id=?", (pid,)).fetchone()[0]
            == "completed"
        )
        assert MaterialService.audit(c, wid)["balanced"]


def test_other_persons_batch_cannot_be_sampled_without_permission(lab):
    db, _, actors, batches, tool, at = lab
    with db.write() as c:
        with pytest.raises(ValueError, match="材料|许可|批次"):
            research().submit(c, actors[0], experiment(batches[1], tool), at)
        assert (
            c.execute("SELECT quantity FROM item_instances WHERE id=?", (batches[1],)).fetchone()[0]
            == 100
        )


def test_insufficient_skill_cannot_be_replaced_by_role_description(lab):
    db, _, actors, batches, tool, at = lab
    with db.write() as c:
        c.execute(
            "UPDATE character_skill_proficiencies SET proficiency=0 WHERE character_id=?",
            (actors[0],),
        )
        with pytest.raises(ValueError, match="熟练|检验|能力"):
            research().submit(c, actors[0], experiment(batches[0], tool), at)
        assert (
            c.execute("SELECT quantity FROM item_instances WHERE id=?", (batches[0],)).fetchone()[0]
            == 100
        )


def test_detection_limit_is_reported_without_revealing_hidden_exact_value(lab):
    db, wid, actors, batches, tool, at = lab
    with db.write() as c:
        iid, tid = (
            str(uuid4()),
            c.execute(
                "SELECT item_type_id FROM item_instances WHERE id=?", (batches[0],)
            ).fetchone()[0],
        )
        c.execute(
            "INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,owner_character_id,quantity,condition) VALUES (?,?,?,'character_inventory',?,?,10,100)",
            (iid, wid, tid, actors[0], actors[0]),
        )
        MaterialService.attach(c, iid, MaterialState(mass=10, mobile=0.001))
        research().submit(c, actors[0], experiment(iid, tool), at)
    from world_engine.clock import WorldClockService
    from world_engine.config import Settings

    WorldClockService(db, Settings(database_path=db.path)).heartbeat(wid, elapsed_seconds=3600)
    with db.write() as c:
        research().tick(c, wid, at + timedelta(hours=1))
        data = json.loads(
            c.execute(
                "SELECT content_json FROM collective_observations WHERE kind='measurement'"
            ).fetchone()[0]
        )
        assert data["value"] is None
        assert data["detection_limit"] == 0.01
        assert data["label"] == "低于检出限"


def test_consumable_shortage_rejects_before_sample_is_taken(lab):
    db, _, actors, batches, tool, at = lab
    with db.write() as c:
        c.execute(
            "UPDATE collective_instruments SET supplies_json=? WHERE item_id=?",
            (json.dumps([{"item_type_id": "missing-solvent", "quantity": 1}]), tool),
        )
        with pytest.raises(ValueError, match="耗材|材料"):
            research().submit(c, actors[0], experiment(batches[0], tool), at)
        assert (
            c.execute("SELECT quantity FROM item_instances WHERE id=?", (batches[0],)).fetchone()[0]
            == 100
        )


def test_incubation_reader_cannot_observe_while_traveling(lab):
    db, wid, actors, batches, tool, at = lab
    from world_engine.clock import WorldClockService
    from world_engine.config import Settings
    from world_engine.movement import MovementService

    with db.write() as c:
        c.execute("UPDATE collective_instruments SET incubation_hours=2 WHERE item_id=?", (tool,))
        research().submit(c, actors[0], experiment(batches[0], tool), at)
    clock = WorldClockService(db, Settings(database_path=db.path))
    clock.heartbeat(wid, elapsed_seconds=3600)
    with db.write() as c:
        research().tick(c, wid, at + timedelta(hours=1))
        MovementService().start(
            c,
            world_id=wid,
            character_id=actors[0],
            destination_longitude=2,
            destination_latitude=0,
            world_time=at + timedelta(hours=1),
        )
        c.execute("UPDATE characters SET current_location_id=NULL WHERE id=?", (actors[0],))
        research().tick(c, wid, at + timedelta(hours=3))
        assert not c.execute(
            "SELECT 1 FROM collective_observations WHERE kind='measurement'"
        ).fetchone()


def test_incubation_releases_operator_after_setup_but_not_result(lab):
    db, wid, actors, batches, tool, at = lab
    with db.write() as c:
        c.execute("UPDATE collective_instruments SET incubation_hours=24 WHERE item_id=?", (tool,))
        research().submit(c, actors[0], experiment(batches[0], tool), at)
    from world_engine.clock import WorldClockService
    from world_engine.config import Settings

    clock = WorldClockService(db, Settings(database_path=db.path))
    clock.heartbeat(wid, elapsed_seconds=3600)
    with db.write() as c:
        research().tick(c, wid, at + timedelta(hours=1))
        assert (
            c.execute(
                "SELECT COUNT(*) FROM collective_observations WHERE kind='measurement'"
            ).fetchone()[0]
            == 0
        )
        assert (
            c.execute(
                "SELECT COUNT(*) FROM character_life_activities WHERE character_id=? AND status='running'",
                (actors[0],),
            ).fetchone()[0]
            == 0
        )
    clock.heartbeat(wid, elapsed_seconds=24 * 3600)
    with db.write() as c:
        research().tick(c, wid, at + timedelta(hours=25))
        assert (
            c.execute(
                "SELECT COUNT(*) FROM collective_observations WHERE kind='measurement'"
            ).fetchone()[0]
            == 1
        )


def test_unmeasured_prediction_cannot_be_rewritten_after_submission(lab):
    db, _, actors, batches, tool, at = lab
    with db.write() as c:
        research().submit(c, actors[0], experiment(batches[0], tool), at)
        import sqlite3

        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            c.execute("UPDATE collective_hypotheses SET spec_json='{}'")


def test_cancelled_work_returns_sample_without_fabricating_measurement(lab):
    db, wid, actors, batches, tool, at = lab
    with db.write() as c:
        research().submit(c, actors[0], experiment(batches[0], tool), at)
        from world_engine.life import LifeActivityService

        LifeActivityService.interrupt(c, actors[0], at, "本人选择停止")
        research().tick(c, wid, at)
        assert (
            c.execute(
                "SELECT COUNT(*) FROM collective_observations WHERE kind='measurement'"
            ).fetchone()[0]
            == 0
        )
        assert MaterialService.audit(c, wid)["balanced"]
        item = c.execute("SELECT * FROM item_instances WHERE id=?", (tool,)).fetchone()
        assert item["container_id"] == actors[0] and item["condition"] == 100


def test_interrupting_measurement_does_not_repeat_completed_processing(lab):
    db, wid, actors, batches, tool, at = lab
    spec = experiment(batches[0], tool)
    spec["commands"][0].update(operation="store", settings={}, hours=1)
    with db.write() as c:
        research().submit(c, actors[0], spec, at)
    from world_engine.clock import WorldClockService
    from world_engine.config import Settings
    from world_engine.life import LifeActivityService

    WorldClockService(db, Settings(database_path=db.path)).heartbeat(wid, elapsed_seconds=3600)
    with db.write() as c:
        research().tick(c, wid, at + timedelta(hours=1))
        assert c.execute("SELECT COUNT(*) FROM collective_process_traces").fetchone()[0] == 1
        LifeActivityService.interrupt(
            c, actors[0], at + timedelta(hours=1.5), "只停止尚未完成的测量"
        )
        research().tick(c, wid, at + timedelta(hours=1.5))
        assert c.execute("SELECT COUNT(*) FROM collective_process_traces").fetchone()[0] == 1


def test_processing_experiment_has_control_and_real_polluted_liquid(lab):
    db, wid, actors, batches, tool, at = lab
    with db.write() as c:
        water_type, water = str(uuid4()), str(uuid4())
        c.execute(
            "INSERT INTO item_types(id,name,category,stack_limit,slot_size) VALUES (?,'清水瓶','material',100,1)",
            (water_type,),
        )
        c.execute(
            "INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,owner_character_id,quantity,condition) VALUES (?,?,?,'character_inventory',?,?,20,100)",
            (water, wid, water_type, actors[0], actors[0]),
        )
        MaterialService.attach(
            c, water, MaterialState(carrier="water", mass=0, water=20, viability=0)
        )
        spec = experiment(batches[0], tool)
        spec["commands"][0].update(
            operation="wash",
            settings={"water": 2},
            hours=1,
            auxiliary_ids=[water],
            auxiliary_quantities={water: 2},
            control_batch_id=batches[0],
        )
        research().submit(c, actors[0], spec, at)
    from world_engine.clock import WorldClockService
    from world_engine.config import Settings

    clock = WorldClockService(db, Settings(database_path=db.path))
    clock.heartbeat(wid, elapsed_seconds=3 * 3600)
    with db.write() as c:
        research().tick(c, wid, at + timedelta(hours=3))
        data = json.loads(
            c.execute(
                "SELECT content_json FROM collective_observations WHERE kind='measurement'"
            ).fetchone()[0]
        )
        assert data["value"] < data["control"]["value"]
        assert data["control"]["value"] == pytest.approx(0.2)
        assert data["outputs"]
        residuals = [MaterialService.state(c, iid) for iid in data["outputs"]]
        assert any(x.carrier == "water" and x.mobile > 0 for x in residuals)
        assert MaterialService.audit(c, wid)["balanced"]


def test_unsupported_processing_cannot_create_water_from_settings(lab):
    db, _, actors, batches, tool, at = lab
    with db.write() as c:
        spec = experiment(batches[0], tool)
        spec["commands"][0].update(operation="wash", settings={"water": 100})
        with pytest.raises(ValueError, match="实际|材料|水"):
            research().submit(c, actors[0], spec, at)
        assert (
            c.execute("SELECT quantity FROM item_instances WHERE id=?", (batches[0],)).fetchone()[0]
            == 100
        )


def test_program_can_collect_local_output_and_remeasure_it(lab):
    db, _, actors, batches, tool, at = lab
    with db.write() as c:
        loc = c.execute(
            "SELECT lab_location_id FROM collective_people WHERE character_id=?", (actors[0],)
        ).fetchone()[0]
        item = c.execute("SELECT * FROM item_instances WHERE id=?", (batches[0],)).fetchone()
        output = MaterialService.move(c, item, 2, "ground", loc, actors[0])
        assert output in json.dumps(research().context(c, actors[0], at))
        spec = experiment(output, tool)
        spec["commands"].insert(0, {"op": "collect", "batch_id": output, "quantity": 2})
        research().submit(c, actors[0], spec, at)
        assert c.execute("SELECT COUNT(*) FROM collective_jobs").fetchone()[0] == 1
        assert (
            c.execute("SELECT container_type FROM item_instances WHERE id=?", (output,)).fetchone()[
                0
            ]
            == "character_inventory"
        )


def test_observe_with_invalid_hypothesis_is_rejected_by_contract():
    from world_engine.collective_types import CollectiveProgram

    with pytest.raises(ValueError, match="假说|预测"):
        CollectiveProgram.model_validate(
            {"reason": "延迟观察", "commands": [{"op": "observe", "hypothesis_index": 9}]}
        )


def test_program_can_propose_and_send_bound_proposal_without_assistant(lab):
    db, wid, actors, batches, _, at = lab
    with db.write() as c:
        c.execute("INSERT INTO collective_contacts VALUES (?,?,?)", (actors[0], actors[1], 0))
        spec = {
            "reason": "提出并请同伴独立审阅",
            "commands": [
                {
                    "op": "propose",
                    "result_key": "idea",
                    "text": "本人建议做留存对照",
                    "tasks": [
                        {
                            "key": "check",
                            "worker_id": actors[1],
                            "operation": "store",
                            "batch_id": batches[1],
                            "quantity": 1,
                            "hours": 1,
                        }
                    ],
                },
                {
                    "op": "send",
                    "proposal_id": "$idea",
                    "version": 1,
                    "recipients": [actors[1]],
                    "text": "请独立评议",
                },
            ],
        }
        pid = research().submit(c, actors[0], spec, at)
        assert (
            c.execute("SELECT status FROM collective_programs WHERE id=?", (pid,)).fetchone()[0]
            == "completed"
        )
        from world_engine.collective_messages import MessageService

        MessageService.deliver(c, wid, at)
        received = json.loads(
            c.execute(
                "SELECT content_json FROM collective_observations WHERE character_id=?",
                (actors[1],),
            ).fetchone()[0]
        )
        assert received["proposal"]["tasks"][0]["worker_id"] == actors[1]


def test_processing_reserves_apparatus_until_actual_completion(lab):
    from test_collective_cooperation import apparatus

    db, wid, actors, batches, tool, at = lab
    with db.write() as c:
        heater = apparatus(c, wid, actors[0])
        spec = experiment(batches[0], tool)
        spec["commands"][0].update(
            operation="heat", apparatus_id=heater, settings={"temperature": 60, "power": 1}, hours=1
        )
        research().submit(c, actors[0], spec, at)
        assert (
            c.execute("SELECT container_type FROM item_instances WHERE id=?", (heater,)).fetchone()[
                0
            ]
            == "collective_instrument"
        )


def test_processed_output_binding_drives_next_real_collection(lab):
    db, wid, actors, batches, tool, at = lab
    with db.write() as c:
        spec = experiment(batches[0], tool)
        spec["commands"][0].update(operation="store", result_key="sample", hours=1)
        spec["commands"].append({"op": "collect", "batch_id": "$sample:0", "quantity": 1})
        pid = research().submit(c, actors[0], spec, at)
    from world_engine.clock import WorldClockService
    from world_engine.config import Settings

    WorldClockService(db, Settings(database_path=db.path)).heartbeat(wid, elapsed_seconds=7200)
    with db.write() as c:
        research().tick(c, wid, at + timedelta(hours=2))
        row = c.execute("SELECT * FROM collective_programs WHERE id=?", (pid,)).fetchone()
        assert row["status"] == "completed"
        output = json.loads(row["bindings_json"])["sample"][0]
        assert (
            c.execute("SELECT container_type FROM item_instances WHERE id=?", (output,)).fetchone()[
                0
            ]
            == "character_inventory"
        )


def test_unknown_method_rejected_before_spending_sample(lab):
    db, _, actors, batches, tool, at = lab
    with db.write() as c:
        c.execute(
            "UPDATE collective_instruments SET method='unavailable-metric' WHERE item_id=?", (tool,)
        )
        spec = experiment(batches[0], tool)
        spec["hypotheses"][0]["predictions"][0]["metric"] = "unavailable-metric"
        spec["commands"][0]["method"] = "unavailable-metric"
        with pytest.raises(ValueError, match="测量|方法"):
            research().submit(c, actors[0], spec, at)


def test_boolean_quantity_does_not_become_actual_material():
    from world_engine.collective_types import CollectiveProgram

    with pytest.raises(ValueError):
        CollectiveProgram.model_validate(
            {
                "reason": "材料计数",
                "commands": [{"op": "wait", "auxiliary_quantities": {"water": True}}],
            }
        )


def test_germination_consumes_real_hydration_and_observes_ionic_damage(lab):
    from test_collective_regions import material

    db, wid, actors, batches, tool, at = lab
    from world_engine.clock import WorldClockService
    from world_engine.collective_clock import next_boundary
    from world_engine.config import Settings

    with db.write() as c:
        loc = c.execute(
            "SELECT lab_location_id FROM collective_people WHERE character_id=?", (actors[0],)
        ).fetchone()[0]
        old_food = c.execute("SELECT * FROM item_instances WHERE id=?", (batches[0],)).fetchone()
        MaterialService.move(c, old_food, old_food["quantity"], "ground", loc, actors[0])
        material(
            c,
            wid,
            actors[0],
            MaterialState(carrier="grain", mass=100, nutrition=6000),
            100,
            category="food",
        )
        seed = material(
            c, wid, actors[0], MaterialState(carrier="seed", mass=5, mobile=0.2, nutrition=100), 5
        )
        water = material(
            c, wid, actors[0], MaterialState(carrier="water", mass=0, water=10, viability=0), 10
        )
        tid = c.execute("SELECT item_type_id FROM item_instances WHERE id=?", (water,)).fetchone()[
            0
        ]
        c.execute(
            "UPDATE collective_instruments SET method=?,incubation_hours=72,supplies_json=? WHERE item_id=?",
            (
                "germination",
                json.dumps([{"item_type_id": tid, "quantity": 1, "role": "hydration"}]),
                tool,
            ),
        )
        c.execute("INSERT INTO material_modes VALUES (?,?)", (wid, '{"mode":"collective"}'))
        spec = experiment(seed, tool)
        spec["commands"][0]["method"] = "germination"
        spec["hypotheses"][0]["predictions"][0].update(metric="germination", low=0, high=0.5)
        research().submit(c, actors[0], spec, at)
        sample = c.execute("SELECT sample_id FROM collective_jobs").fetchone()[0]
        assert MaterialService.state(c, sample).water == 1
    clock = WorldClockService(db, Settings(database_path=db.path))
    current = at
    while current < at + timedelta(hours=73):
        with db.read() as c:
            boundary = min(next_boundary(c, wid, current), at + timedelta(hours=73))
        clock.heartbeat(wid, elapsed_seconds=(boundary - current).total_seconds())
        current = boundary
    with db.read() as c:
        result = json.loads(
            c.execute(
                "SELECT content_json FROM collective_observations WHERE kind='measurement'"
            ).fetchone()[0]
        )
        assert result["value"] < 0.5
        assert MaterialService.audit(c, wid)["balanced"]
