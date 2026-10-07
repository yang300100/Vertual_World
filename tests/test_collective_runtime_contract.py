"""哥哥，复现正式轮次暴露的通用契约缺陷，全部使用离线实物与时钟。"""

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from test_collective_research import experiment, lab, research  # noqa: F401

from world_engine.clock import WorldClockService
from world_engine.config import Settings
from world_engine.materials import MaterialService as M


@pytest.mark.parametrize("ground_tool", [False, True])
def test_owned_local_ground_samples_and_control_have_actual_labor(ground_tool, lab):
    db, wid, actors, batches, tool, at = lab
    with db.write() as c:
        loc = research().actor(c, actors[0])["lab_location_id"]
        c.execute(
            "UPDATE item_instances SET container_type='ground',container_id=? WHERE id=?",
            (loc, batches[0]),
        )
        if ground_tool:
            c.execute(
                "UPDATE item_instances SET container_type='ground',container_id=? WHERE id=?",
                (loc, tool),
            )
        spec = experiment(batches[0], tool)
        spec["commands"][0]["control_batch_id"] = batches[0]
        pid = research().submit(c, actors[0], spec, at)
        assert (
            c.execute("SELECT quantity FROM item_instances WHERE id=?", (batches[0],)).fetchone()[0]
            == 98
        )
        assert (
            c.execute(
                "SELECT count(*) FROM collective_observations WHERE kind='measurement'"
            ).fetchone()[0]
            == 0
        )
        assert M.audit(c, wid)["balanced"]
    WorldClockService(db, Settings(database_path=db.path)).heartbeat(wid, elapsed_seconds=7200)
    with db.write() as c:
        research().tick(c, wid, at + timedelta(hours=2))
        result = json.loads(
            c.execute(
                "SELECT content_json FROM collective_observations WHERE kind='measurement'"
            ).fetchone()[0]
        )
        assert result["value"] == pytest.approx(0.2)
        assert result["control"]["value"] == pytest.approx(0.2)
        assert result["prediction_recorded_at"] < result["measured_at"]
        assert (
            c.execute("SELECT status FROM collective_programs WHERE id=?", (pid,)).fetchone()[0]
            == "completed"
        )
        actual = c.execute(
            "SELECT container_type,container_id FROM item_instances WHERE id=?", (tool,)
        ).fetchone()
        assert (
            tuple(actual) == ("ground", loc)
            if ground_tool
            else tuple(actual) == ("character_inventory", actors[0])
        )
        assert M.audit(c, wid)["balanced"]


def test_real_baseline_germination_uses_finite_local_ground_supplies(database):
    from scripts.demo_collective_disaster import build_baseline
    from world_engine.engine import WorldEngine

    db = database
    wid, at = build_baseline(db)
    with db.write() as c:
        tool = c.execute(
            "SELECT s.*,i.owner_character_id FROM collective_instruments s JOIN item_instances i ON i.id=s.item_id WHERE s.method='germination' LIMIT 1"
        ).fetchone()
        actor = tool["owner_character_id"]
        seed = c.execute(
            "SELECT i.id FROM item_instances i JOIN material_batches b ON b.item_id=i.id WHERE i.owner_character_id=? AND json_extract(b.state_json,'$.carrier')='seed'",
            (actor,),
        ).fetchone()[0]
        supply = json.loads(tool["supplies_json"])[0]
        stock = c.execute(
            "SELECT id,quantity FROM item_instances WHERE owner_character_id=? AND item_type_id=?",
            (actor, supply["item_type_id"]),
        ).fetchone()
        spec = {
            "reason": "本人用自己的现场种子和器具登记一次活力检验",
            "hypotheses": [
                {
                    "explanation": "待检验的种子活力",
                    "predictions": [{"metric": "germination", "low": 0, "high": 1}],
                }
            ],
            "commands": [
                {
                    "op": "experiment",
                    "batch_id": seed,
                    "quantity": 5,
                    "method": "germination",
                    "instrument_id": tool["item_id"],
                    "hypothesis_index": 0,
                }
            ],
        }
        pid = research().submit(c, actor, spec, at)
        assert (
            c.execute("SELECT quantity FROM item_instances WHERE id=?", (stock["id"],)).fetchone()[
                0
            ]
            == stock["quantity"] - 1
        )
        sample = c.execute(
            "SELECT sample_id FROM collective_jobs WHERE program_id=?", (pid,)
        ).fetchone()[0]
        assert M.state(c, sample).water > 0
        assert M.audit(c, wid)["balanced"]
    engine = WorldEngine(
        db,
        Settings(
            database_path=db.path,
            collective_api_enabled=False,
            knowledge_enabled=False,
            history_logging_enabled=False,
            active_character_limit=100,
        ),
    )
    try:
        engine.heartbeat(wid, elapsed_seconds=3600)
        with db.read() as c:
            assert (
                c.execute(
                    "SELECT count(*) FROM collective_observations WHERE kind='measurement'"
                ).fetchone()[0]
                == 0
            )
        from world_engine.collective_clock import next_boundary

        endpoint = at + timedelta(hours=73)
        current = at + timedelta(hours=1)
        while current < endpoint:
            with db.read() as c:
                boundary = min(next_boundary(c, wid, current), endpoint)
                scale = c.execute(
                    "SELECT time_scale FROM world_clock WHERE world_id=?", (wid,)
                ).fetchone()[0]
            current = engine.heartbeat(
                wid, elapsed_seconds=(boundary - current).total_seconds() / scale
            ).current_time
        with db.read() as c:
            result = json.loads(
                c.execute(
                    "SELECT content_json FROM collective_observations WHERE character_id=? AND kind='measurement'",
                    (actor,),
                ).fetchone()[0]
            )
            assert result["metric"] == "germination"
            assert result["prediction_recorded_at"] < result["measured_at"]
            assert M.audit(c, wid)["balanced"]
            assert c.execute("SELECT count(*) FROM collective_model_calls").fetchone()[0] == 0
    finally:
        engine.close()


def test_future_measured_evidence_is_resolved_only_after_the_experiment(lab):
    from world_engine.collective_messages import MessageService

    db, wid, actors, batches, tool, at = lab
    with db.write() as c:
        c.execute("INSERT INTO collective_contacts VALUES (?,?,.25)", (actors[0], actors[1]))
        spec = experiment(batches[0], tool)
        spec["commands"][0]["result_key"] = "actual_reading"
        spec["commands"].append(
            {
                "op": "send",
                "recipients": [actors[1]],
                "text": "请独立阅读真实读数",
                "evidence_ids": ["$actual_reading"],
            }
        )
        research().submit(c, actors[0], spec, at)
        assert c.execute("SELECT count(*) FROM collective_messages").fetchone()[0] == 0
    clock = WorldClockService(db, Settings(database_path=db.path))
    clock.heartbeat(wid, elapsed_seconds=3600)
    with db.write() as c:
        research().tick(c, wid, at + timedelta(hours=1))
        message = json.loads(
            c.execute("SELECT content_json FROM collective_messages").fetchone()[0]
        )
        reading = c.execute(
            "SELECT id FROM collective_observations WHERE kind='measurement'"
        ).fetchone()[0]
        assert message["evidence"][0]["id"] == reading
        assert message["evidence"][0]["content"]["value"] == pytest.approx(0.2)
        assert not c.execute(
            "SELECT 1 FROM collective_observations WHERE character_id=? AND kind='received_report'",
            (actors[1],),
        ).fetchone()
        MessageService.deliver(c, wid, at + timedelta(hours=1.25))
        assert c.execute(
            "SELECT 1 FROM collective_observations WHERE character_id=? AND kind='received_report'",
            (actors[1],),
        ).fetchone()


def test_forward_result_and_future_hypothesis_evidence_are_rejected(lab):
    db, _, actors, batches, tool, at = lab
    with db.write() as c:
        with pytest.raises(ValueError):
            research().submit(
                c,
                actors[0],
                {
                    "reason": "不能引用尚未执行的后置动作",
                    "commands": [
                        {"op": "send", "recipients": [actors[1]], "evidence_ids": ["$later"]},
                        {"op": "inspect", "result_key": "later"},
                    ],
                },
                at,
            )
        spec = experiment(batches[0], tool)
        spec["hypotheses"][0]["evidence_ids"] = ["$future"]
        with pytest.raises(ValueError):
            research().submit(c, actors[0], spec, at)
        assert c.execute("SELECT count(*) FROM collective_programs").fetchone()[0] == 0


def test_context_retains_only_the_authors_own_submitted_program_and_block(lab):
    from test_collective_runner import ExternalBackend

    from world_engine.collective_runner import CollectiveRunner

    db, wid, actors, _, _, at = lab
    raw = experiment("unavailable", "unavailable")
    runner = CollectiveRunner(db, ExternalBackend(program=raw))
    assert runner.decide(wid, at)
    with db.read() as c:
        own = research().context(c, actors[0], at)["own_submissions"]
        other = research().context(c, actors[1], at)["own_submissions"]
        assert len(own) == 1 and other == []
        assert own[0]["submitted_program"]["reason"] == raw["reason"]
        assert own[0]["status"] == "rejected" and own[0]["error"]


@pytest.mark.parametrize("place", ["foreign_owner", "remote_ground", "other_backpack", "transit"])
def test_site_sampling_does_not_open_other_owners_places_or_transport(place, lab):
    db, _, actors, batches, tool, at = lab
    with db.write() as c:
        actor = research().actor(c, actors[0])
        loc = actor["lab_location_id"]
        container, holder, owner = "ground", loc, actors[0]
        if place == "foreign_owner":
            owner = actors[1]
        if place == "remote_ground":
            holder = "not-this-place"
        if place == "other_backpack":
            container, holder = "character_inventory", actors[1]
        if place == "transit":
            container, holder = "collective_transit", "mail-route"
        c.execute(
            "UPDATE item_instances SET container_type=?,container_id=?,owner_character_id=? WHERE id=?",
            (container, holder, owner, batches[0]),
        )
        with pytest.raises(ValueError, match="材料|许可|批次"):
            research().submit(c, actors[0], experiment(batches[0], tool), at)
        assert (
            c.execute("SELECT quantity FROM item_instances WHERE id=?", (batches[0],)).fetchone()[0]
            == 100
        )


def test_pure_assay_keeps_registered_sample_capacity(lab):
    db, _, actors, batches, tool, at = lab
    with db.write() as c:
        spec = experiment(batches[0], tool)
        spec["commands"][0]["quantity"] = 6
        with pytest.raises(ValueError, match="容量"):
            research().submit(c, actors[0], spec, at)
        assert (
            c.execute("SELECT quantity FROM item_instances WHERE id=?", (batches[0],)).fetchone()[0]
            == 100
        )


def test_ground_processing_uses_real_auxiliary_and_returns_station_to_site(lab):

    db, wid, actors, batches, tool, at = lab
    with db.write() as c:
        loc = research().actor(c, actors[0])["lab_location_id"]
        c.execute(
            "UPDATE item_instances SET container_type='ground',container_id=? WHERE id=?",
            (loc, batches[0]),
        )
        tid, apparatus = str(uuid4()), str(uuid4())
        c.execute(
            "INSERT INTO item_types(id,name,category,stack_limit,slot_size) VALUES (?,'小样筛','tool',1,1)",
            (tid,),
        )
        c.execute(
            "INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,owner_character_id,quantity,condition) VALUES (?,?,?,'ground',?,?,1,100)",
            (apparatus, wid, tid, loc, actors[0]),
        )
        c.execute(
            "INSERT INTO collective_apparatus(item_id,world_id,operations_json,capacity,throughput,max_power,skill_name,min_proficiency) VALUES (?,?,'[\"screen\"]',10,10,1,'检验',40)",
            (apparatus, wid),
        )
        spec = experiment(batches[0], tool)
        spec["commands"][0].update(
            operation="screen", settings={"mesh": 0.5}, apparatus_id=apparatus
        )
        research().submit(c, actors[0], spec, at)
        assert (
            c.execute(
                "SELECT container_type FROM item_instances WHERE id=?", (apparatus,)
            ).fetchone()[0]
            == "collective_instrument"
        )
    WorldClockService(db, Settings(database_path=db.path)).heartbeat(wid, elapsed_seconds=7200)
    with db.write() as c:
        research().tick(c, wid, at + timedelta(hours=2))
        assert tuple(
            c.execute(
                "SELECT container_type,container_id FROM item_instances WHERE id=?", (apparatus,)
            ).fetchone()
        ) == ("ground", loc)
        assert c.execute("SELECT count(*) FROM collective_process_traces").fetchone()[0] == 1
        assert M.audit(c, wid)["balanced"]


def test_local_public_food_account_is_visible_without_hidden_safety_state(database):
    from scripts.demo_collective_disaster import build_baseline

    wid, at = build_baseline(database)
    with database.read() as c:
        region = c.execute("SELECT * FROM collective_regions LIMIT 1").fetchone()
        stock = c.execute(
            "SELECT id,quantity FROM item_instances WHERE container_type='region_food' AND container_id=?",
            (region["id"],),
        ).fetchone()
        context = research().context(c, region["manager_id"], at)
        account = next(r for r in context["regions"] if r["id"] == region["id"])["food_account"]
        assert account["status"] == "observed_here"
        assert any(
            x["id"] == stock["id"] and x["quantity"] == stock["quantity"] for x in account["stock"]
        )
        encoded = json.dumps(account)
        assert (
            "mobile" not in encoded
            and "safe_nutrition" not in encoded
            and "exposure" not in encoded
        )
    with database.write() as c:
        from world_engine.collective_regions import RegionService

        RegionService._consume_day(c, region, at + timedelta(days=1))
        account = next(
            r
            for r in research().context(c, region["manager_id"], at + timedelta(days=1))["regions"]
            if r["id"] == region["id"]
        )["food_account"]
        assert account["issue_records"] and account["issue_records"][0]["issued"]
        encoded = json.dumps(account)
        for hidden in ("nutrition", "mobile", "particles", "exposure", "mass"):
            assert hidden not in encoded
        other = c.execute(
            "SELECT location_id FROM collective_regions WHERE id!=? LIMIT 1", (region["id"],)
        ).fetchone()[0]
        c.execute(
            "UPDATE characters SET current_location_id=? WHERE id=?", (other, region["manager_id"])
        )
        distant = research().context(c, region["manager_id"], at)["regions"][0]["food_account"]
        assert distant == {"status": "unobserved_here"}


@pytest.mark.parametrize("capacity,throughput", [(1, 10), (10, 1)])
def test_water_processing_respects_actual_apparatus_load(capacity, throughput, lab):
    from world_engine.material_physics import MaterialState

    db, wid, actors, _, tool, at = lab
    with db.write() as c:
        loc = research().actor(c, actors[0])["lab_location_id"]
        tid, batch, apparatus = str(uuid4()), str(uuid4()), str(uuid4())
        c.execute(
            "INSERT INTO item_types(id,name,category,stack_limit,slot_size) VALUES (?,'试验水与沉降槽','tool',100,1)",
            (tid,),
        )
        for iid, count in [(batch, 5), (apparatus, 1)]:
            c.execute(
                "INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,owner_character_id,quantity,condition) VALUES (?,?,?,'ground',?,?,?,100)",
                (iid, wid, tid, loc, actors[0], count),
            )
        M.attach(c, batch, MaterialState(carrier="water", mass=0, water=5, mobile=1))
        c.execute(
            "INSERT INTO collective_apparatus(item_id,world_id,operations_json,capacity,throughput,max_power,skill_name,min_proficiency) VALUES (?,?,'[\"settle\"]',?,?,1,'检验',40)",
            (apparatus, wid, capacity, throughput),
        )
        spec = experiment(batch, tool)
        spec["commands"][0].update(operation="settle", quantity=5, hours=1, apparatus_id=apparatus)
        with pytest.raises(ValueError, match="容量|吞吐量"):
            research().submit(c, actors[0], spec, at)
        assert (
            c.execute("SELECT quantity FROM item_instances WHERE id=?", (batch,)).fetchone()[0] == 5
        )
        assert M.audit(c, wid)["balanced"]
