"""哥哥，签字、存档指标和一个笨拙单人计划都不足以证明目标达成。"""

from datetime import timedelta

import pytest
from test_collective_research import lab  # noqa: F401


def test_withdrawal_recomputes_the_same_subsequent_processing_from_original_input():
    from world_engine.material_physics import MaterialState, process

    original = MaterialState(mass=10, particles=1, mobile=0.001, nutrition=200)
    steps = [
        {"operation": "screen", "settings": {"mesh": 0.1}, "hours": 1, "auxiliaries": []},
        {"operation": "store", "settings": {}, "hours": 2, "auxiliaries": []},
    ]
    expected = original
    for step in steps:
        expected = process(expected, step["operation"], step["settings"], step["hours"])[0]
    result = audit().replay_component_chain(original, steps, withdrawn_index=0)
    assert result["actual"] == expected
    assert result["withdrawn"].particles == original.particles
    assert result["withdrawn"].age_hours == original.age_hours + 3
    assert result["withdrawn"].particles > result["actual"].particles
    assert result["subsequent_steps_recomputed"] == 1


def test_processing_keeps_immutable_parameters_linked_to_actual_material_ledger(lab):
    db, wid, actors, batches, _, at = lab
    import sqlite3

    from test_collective_cooperation import cooperation, proposal

    from world_engine.collective_work import WorkService
    from world_engine.life import LifeActivityService

    with db.write() as c:
        body = proposal(actors, batches)
        body["tasks"] = body["tasks"][:1]
        pid = cooperation().propose(c, actors[0], body, at)
        cooperation().accept(
            c,
            actors[0],
            {
                "proposal_id": pid,
                "version": 1,
                "task_keys": ["raw-check"],
                "reason": "本人执行已同意的工作",
            },
            at,
        )
        cooperation().start(c, actors[0], {"proposal_id": pid, "version": 1}, at)
        activity = LifeActivityService.running(c, actors[0])
        LifeActivityService.finish(
            c, activity, at + timedelta(hours=1), "completed", "离线结算完成"
        )
        WorkService.tick(c, wid, at + timedelta(hours=1))
        trace = c.execute("SELECT * FROM collective_process_traces").fetchone()
        assert trace["operation"] == "store" and trace["hours"] == 1
        ledger = c.execute(
            "SELECT * FROM material_ledger WHERE id=?", (trace["ledger_id"],)
        ).fetchone()
        assert ledger["operation"] == "transform"
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            c.execute("UPDATE collective_process_traces SET hours=0")


def test_audit_reads_failed_world_without_modification_or_api(lab, tmp_path):
    db, wid, _, _, _, _ = lab
    import sqlite3

    with db.read() as c:
        with sqlite3.connect(tmp_path / "baseline.db") as target:
            c.backup(target)
    before = audit().database_fingerprint(db.path)
    result = audit().audit_run(db.path, tmp_path / "baseline.db", {"world_id": wid})
    assert result["goal_achieved"] is False
    assert result["api_calls_during_audit"] == 0
    assert result["formal_database_unchanged"] is True
    assert audit().database_fingerprint(db.path) == before


def test_database_logical_fingerprint_ignores_storage_only_checkpoint(lab):
    import sqlite3

    db, _, _, _, _, _ = lab
    before = audit().database_fingerprint(db.path)
    with sqlite3.connect(db.path) as c:
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert audit().database_fingerprint(db.path) == before


def test_future_observation_uses_separate_copy_and_no_new_decisions(database):
    from scripts.demo_collective_disaster import build_baseline

    wid, _ = build_baseline(database)
    with database.read() as c:
        before = c.serialize()
    result = audit().future_maintenance_check(database.path, wid, hours=1)
    assert result["api_calls"] == 0 and result["new_plans"] == 0
    assert result["hours"] == 1 and result["passed"]
    with database.read() as c:
        assert c.serialize() == before


def test_future_observation_rejects_nonfatal_new_body_exposure(database):
    wid, at = __import__(
        "scripts.demo_collective_disaster", fromlist=["build_baseline"]
    ).build_baseline(database)
    from world_engine.collective_regions import RegionService

    with database.write() as c:
        actor = c.execute(
            "SELECT character_id FROM collective_people WHERE world_id=? LIMIT 1", (wid,)
        ).fetchone()[0]
        item = c.execute(
            "SELECT id FROM item_instances WHERE owner_character_id=? AND container_type='character_inventory' LIMIT 1",
            (actor,),
        ).fetchone()[0]
        RegionService.ingestion(c, wid, at, batch_id=item, dose=0.049, actor_id=actor)
    result = audit().future_maintenance_check(database.path, wid, hours=49)
    assert result["recorded_deaths"] == 0 and result["unsafe_food_days"] == 0
    assert result["passed"] is False


def test_next_cycle_bound_includes_seed_viability_and_continuing_salt_stress():
    from world_engine.material_physics import MaterialState

    seed = MaterialState(carrier="seed", mass=40, nutrition=800, viability=0.8)
    soil = MaterialState(carrier="soil", mass=1000, mobile=5)
    source = MaterialState(carrier="growth_reserve", mass=5000, nutrition=100000)
    result = audit().next_cycle_yield_lower_bound(seed, soil, source)
    assert result < 18000


def test_actual_withdrawal_replays_later_transform_and_split_before_food_delivery(lab):
    db, wid, actors, _, _, at = lab

    from test_collective_regions import material, region

    from world_engine.collective_production import ProductionService
    from world_engine.material_physics import MaterialState, process
    from world_engine.materials import MaterialService as M

    with db.write() as c:
        rid, *_ = region(c, wid, actors[0], at)
        iid = material(
            c,
            wid,
            actors[0],
            MaterialState(mass=10, particles=1, mobile=0.001, nutrition=200),
            10,
            category="food",
        )
        loc = c.execute(
            "SELECT lab_location_id FROM collective_people WHERE character_id=?", (actors[0],)
        ).fetchone()[0]
        for i, (op, settings) in enumerate([("screen", {"mesh": 0.01}), ("store", {})]):
            item = c.execute("SELECT * FROM item_instances WHERE id=?", (iid,)).fetchone()
            states = process(M.state(c, iid), op, settings, 4)
            outputs = M.transform(
                c,
                [iid],
                [
                    {
                        "item_type_id": item["item_type_id"],
                        "quantity": item["quantity"] if j == 0 else 1,
                        "state": state,
                    }
                    for j, state in enumerate(states)
                ],
                container_type="ground",
                container_id=loc,
                owner_id=actors[0],
            )
            audit().record_processing(
                c, [iid], outputs, "work", f"actual-work-{i}", op, settings, 4
            )
            iid = outputs[0]
        ProductionService.ration(c, actors[0], rid, iid, 5, at + timedelta(hours=8))
        result = audit().component_withdrawals(c, wid)
        first = next(x for x in result if x["source_id"] == "actual-work-0")
        assert first["subsequent_steps_recomputed"] == 1
        assert first["proved_material_effect"]
        assert any(x["actual_safe"] and not x["withdrawn_safe"] for x in first["endpoints"])
        assert not first["unsupported_ledger_ids"]


def audit():
    from world_engine import collective_audit

    return collective_audit


def test_task_sources_preserve_unchanged_research_and_revert_only_changed_parameters(lab):
    import json

    from test_collective_cooperation import proposal
    from test_collective_research import research

    from world_engine.collective_messages import MessageService

    db, wid, actors, batches, _, at = lab
    with db.write() as c:
        body = proposal(actors, batches)
        body["tasks"][0].update(operation="screen", settings={"mesh": 0.01}, hours=4)
        first = research().submit(
            c,
            actors[0],
            {
                "reason": "提出研究",
                "commands": [{"op": "propose", "text": body["reason"], "tasks": body["tasks"]}],
            },
            at,
        )
        pid = c.execute("SELECT id FROM collective_proposals").fetchone()[0]
        c.execute("INSERT INTO collective_contacts VALUES (?,?,0)", (actors[0], actors[1]))
        MessageService.send(c, actors[0], actors[1], at, text="请评议", proposal_id=pid, version=1)
        MessageService.deliver(c, wid, at)
        body["tasks"][0]["settings"]["mesh"] = 0.010000001
        second = research().submit(
            c,
            actors[1],
            {
                "reason": "修订",
                "commands": [
                    {
                        "op": "revise",
                        "proposal_id": pid,
                        "version": 1,
                        "text": body["reason"],
                        "tasks": body["tasks"],
                    }
                ],
            },
            at,
        )
        sources = {
            r["task_key"]: dict(r)
            for r in c.execute(
                "SELECT * FROM collective_task_sources WHERE proposal_id=? AND version=2", (pid,)
            )
        }
        assert sources["second-check"]["program_id"] == first
        changed = sources["raw-check"]
        assert changed["program_id"] == second
        assert json.loads(changed["previous_task_json"])["settings"] == {"mesh": 0.01}
        original = __import__(
            "world_engine.material_physics", fromlist=["MaterialState"]
        ).MaterialState(mass=10, particles=1)
        counterfactual = audit().component_counterfactual(
            original, (), "screen", {"mesh": 0.010000001}, 4, changed
        )
        assert counterfactual[0].particles < original.particles
        actual = __import__("world_engine.material_physics", fromlist=["process"]).process(
            original, "screen", {"mesh": 0.010000001}, 4
        )[0]
        assert counterfactual[0].particles == pytest.approx(actual.particles, abs=1e-8)


def test_joint_assay_counts_its_designers_prediction_and_actual_delegated_measurement(lab):
    from uuid import uuid4

    from test_collective_cooperation import cooperation
    from test_collective_research import research

    from world_engine.collective_messages import MessageService
    from world_engine.collective_work import WorkService
    from world_engine.life import LifeActivityService

    db, wid, actors, batches, tool, at = lab
    with db.write() as c:
        task = {
            "key": "assay",
            "worker_id": actors[1],
            "operation": "assay",
            "batch_id": batches[1],
            "quantity": 1,
            "hours": 1,
            "method": "extractable_salt",
            "instrument_id": tool,
            "prediction": {"metric": "extractable_salt", "low": 0.1, "high": 0.3},
        }
        program = research().submit(
            c,
            actors[0],
            {
                "reason": "设计后委托有技能的同伴测量",
                "hypotheses": [
                    {"explanation": "预测浸提盐可检出", "predictions": [task["prediction"]]}
                ],
                "commands": [{"op": "propose", "tasks": [task]}],
            },
            at,
        )
        spec = c.execute(
            "SELECT spec_json FROM collective_programs WHERE id=?", (program,)
        ).fetchone()[0]
        call = str(uuid4())
        c.execute(
            "INSERT INTO collective_model_calls(id,world_id,actor_id,fingerprint,status,reason,context_json,system_prompt,response_json,reserved_tokens,created_at) VALUES (?,?,?,'test-fixture','succeeded','离线来源验证','{}','离线',?,0,?)",
            (call, wid, actors[0], spec, at.isoformat()),
        )
        c.execute("UPDATE collective_programs SET call_id=? WHERE id=?", (call, program))
        pid = c.execute("SELECT id FROM collective_proposals").fetchone()[0]
        c.execute("INSERT INTO collective_contacts VALUES (?,?,0)", (actors[0], actors[1]))
        MessageService.send(
            c, actors[0], actors[1], at, text="请独立审阅", proposal_id=pid, version=1
        )
        MessageService.deliver(c, wid, at)
        cooperation().accept(
            c,
            actors[0],
            {
                "proposal_id": pid,
                "version": 1,
                "resource_task_keys": ["assay"],
                "reason": "许可借器具",
            },
            at,
        )
        cooperation().accept(
            c,
            actors[1],
            {"proposal_id": pid, "version": 1, "task_keys": ["assay"], "reason": "本人确认测量"},
            at,
        )
        cooperation().start(c, actors[0], {"proposal_id": pid, "version": 1}, at)
        activity = LifeActivityService.running(c, actors[1])
        LifeActivityService.finish(
            c, activity, at + timedelta(hours=1), "completed", "实际检验完成"
        )
        WorkService.tick(c, wid, at + timedelta(hours=1))
        result = audit().contribution_candidates(c, wid)
        assert len(result["candidates"]) == 1
        measurement = result["candidates"][0]["measurements"][0]
        assert measurement["execution_actor_id"] == actors[1]
        assert measurement["design_actor_id"] == actors[0]
        assert measurement["result"]["value"] == pytest.approx(0.2)


def test_material_effect_rejects_negligible_parameter_change_even_at_a_threshold():
    from world_engine.material_physics import MaterialState

    actual = MaterialState(mass=10, mobile=0.05)
    negligible = MaterialState(mass=10, mobile=0.050000001)
    assert not audit().materially_changed(actual, negligible, "food")


def test_leak_counterfactual_recomputes_capacity_overflow_from_changed_waste_mass():
    from world_engine.material_physics import MaterialState

    actual = MaterialState(carrier="sludge", mass=20, mobile=1)
    smaller = MaterialState(carrier="sludge", mass=5, mobile=1)
    water = MaterialState(carrier="water", mass=0, water=100)
    settings = {
        "integrated_rate": 0.0,
        "capacity": 10,
        "loads": {"waste": {"mass": 20, "water": 0, "particles": 0}},
    }
    outputs = audit().replay_waste_leak([smaller, water], settings, {"waste": smaller})
    assert outputs[0].mobile == 1 and outputs[1].mobile == 0
    outputs = audit().replay_waste_leak([actual, water], settings, {})
    assert outputs[0].mobile == 0.5 and outputs[1].mobile == 0.5


def test_soil_component_withdrawal_recomputes_real_crop_growth_and_harvest(lab):
    from test_collective_regions import material, region

    from world_engine.collective_production import ProductionService as P
    from world_engine.collective_types import CollectiveCommand
    from world_engine.life import LifeActivityService
    from world_engine.material_physics import MaterialState, process
    from world_engine.materials import MaterialService as M

    db, wid, actors, _, _, at = lab
    with db.write() as c:
        rid, _, _, _ = region(c, wid, actors[0], at)
        loc = c.execute("SELECT location_id FROM collective_regions WHERE id=?", (rid,)).fetchone()[
            0
        ]
        soil = material(
            c,
            wid,
            actors[0],
            MaterialState(carrier="soil", mass=1000, mobile=20),
            1000,
            location=loc,
        )
        medium = material(
            c, wid, actors[0], MaterialState(carrier="charcoal", mass=100), 100, location=loc
        )
        states = process(M.state(c, soil), "adsorb", {}, 12, (M.state(c, medium),))
        soil_item = c.execute("SELECT * FROM item_instances WHERE id=?", (soil,)).fetchone()
        media_item = c.execute("SELECT * FROM item_instances WHERE id=?", (medium,)).fetchone()
        outputs = M.transform(
            c,
            [soil, medium],
            [
                {"item_type_id": soil_item["item_type_id"], "quantity": 1000, "state": states[0]},
                {"item_type_id": media_item["item_type_id"], "quantity": 100, "state": states[1]},
            ],
            container_type="ground",
            container_id=loc,
            owner_id=actors[0],
        )
        audit().record_processing(
            c, [soil, medium], outputs, "work", "test-adsorb", "adsorb", {}, 12
        )
        c.execute(
            "INSERT INTO character_skill_proficiencies(world_id,character_id,skill_name,proficiency,updated_at) VALUES (?,?,'农作',80,?)",
            (wid, actors[0], at.isoformat()),
        )

        def finish(command, started):
            P.start(c, actors[0], command, started)
            activity = LifeActivityService.running(c, actors[0])
            ended = started + timedelta(hours=1)
            LifeActivityService.finish(c, activity, ended, "completed", "真实劳动完成")
            P.tick(c, wid, ended)
            return ended

        started = finish(
            CollectiveCommand(
                op="land", region_id=rid, batch_id=outputs[0], quantity=1000, hours=1
            ),
            at,
        )
        seed = material(
            c, wid, actors[0], MaterialState(carrier="seed", mass=10, nutrition=200), 10
        )
        planted = finish(
            CollectiveCommand(op="plant", region_id=rid, batch_id=seed, quantity=10, hours=1),
            started,
        )
        P.tick(c, wid, planted + timedelta(days=90))
        crop = c.execute("SELECT * FROM collective_crops").fetchone()
        finish(
            CollectiveCommand(op="harvest", crop_id=crop["id"], hours=1),
            planted + timedelta(days=90),
        )
        result = next(
            r for r in audit().component_withdrawals(c, wid) if r["source_id"] == "test-adsorb"
        )
        assert result["subsequent_steps_recomputed"] >= 2
        assert not result["unsupported_ledger_ids"]
        assert result["proved_material_effect"]
        assert M.audit(c, wid)["balanced"]


def test_full_audit_requires_related_citation_and_qualifies_real_experiment_used_in_food(
    lab, tmp_path
):
    import json
    from uuid import uuid4

    from test_collective_cooperation import apparatus
    from test_collective_regions import material, region
    from test_collective_research import research

    from world_engine.collective_production import ProductionService
    from world_engine.collective_work import WorkService
    from world_engine.life import LifeActivityService
    from world_engine.material_physics import MaterialState

    db, wid, actors, _, tool, at = lab
    with db.write() as c:
        rid, *_ = region(c, wid, actors[0], at)
        raw = material(
            c,
            wid,
            actors[0],
            MaterialState(mass=10, particles=1, mobile=0.001, nutrition=200),
            10,
            category="food",
        )
        device = apparatus(c, wid, actors[0])
        c.execute(
            "UPDATE collective_apparatus SET operations_json=? WHERE item_id=?",
            ('["screen"]', device),
        )
        c.execute(
            "UPDATE collective_instruments SET method='particles',precision=.0001,detection_limit=.0001 WHERE item_id=?",
            (tool,),
        )

        def attest(pid):
            spec = c.execute(
                "SELECT spec_json FROM collective_programs WHERE id=?", (pid,)
            ).fetchone()[0]
            call = str(uuid4())
            c.execute(
                "INSERT INTO collective_model_calls(id,world_id,actor_id,fingerprint,status,reason,context_json,system_prompt,response_json,reserved_tokens,created_at) VALUES (?,?,?,'offline-test','succeeded','离线正例','{}','离线',?,0,?)",
                (call, wid, actors[0], spec, at.isoformat()),
            )
            c.execute("UPDATE collective_programs SET call_id=? WHERE id=?", (call, pid))

        pid = research().submit(
            c,
            actors[0],
            {
                "reason": "实测筛除细粉",
                "hypotheses": [
                    {
                        "explanation": "筛分比原样减少颗粒",
                        "predictions": [{"metric": "particles", "low": 0, "high": 0.01}],
                    }
                ],
                "commands": [
                    {
                        "op": "experiment",
                        "batch_id": raw,
                        "quantity": 1,
                        "operation": "screen",
                        "settings": {"mesh": 0.01},
                        "method": "particles",
                        "instrument_id": tool,
                        "apparatus_id": device,
                        "hypothesis_index": 0,
                        "control_batch_id": raw,
                        "hours": 4,
                    }
                ],
            },
            at,
        )
        attest(pid)
        activity = LifeActivityService.running(c, actors[0])
        LifeActivityService.finish(
            c, activity, at + timedelta(hours=6), "completed", "处理并测量完成"
        )
        research().tick(c, wid, at + timedelta(hours=6))
        oid = c.execute(
            "SELECT observation_id FROM collective_jobs WHERE program_id=?", (pid,)
        ).fetchone()[0]
        remaining = c.execute("SELECT quantity FROM item_instances WHERE id=?", (raw,)).fetchone()[
            0
        ]
        scale = research().submit(
            c,
            actors[0],
            {
                "reason": "按实测推广",
                "commands": [
                    {
                        "op": "produce",
                        "batch_id": raw,
                        "quantity": remaining,
                        "operation": "screen",
                        "settings": {"mesh": 0.01},
                        "apparatus_id": device,
                        "hours": 4,
                        "evidence_ids": [oid],
                    }
                ],
            },
            at + timedelta(hours=6),
        )
        attest(scale)
        activity = LifeActivityService.running(c, actors[0])
        LifeActivityService.finish(c, activity, at + timedelta(hours=10), "completed", "生产完成")
        WorkService.tick(c, wid, at + timedelta(hours=10))
        product = json.loads(
            c.execute("SELECT outputs_json FROM collective_work_orders").fetchone()[0]
        )[0]
        ProductionService.ration(c, actors[0], rid, product, remaining, at + timedelta(hours=10))
    with db.read() as c:
        import sqlite3

        with sqlite3.connect(tmp_path / "baseline.db") as target:
            c.backup(target)
    result = audit().audit_run(db.path, tmp_path / "baseline.db", {"world_id": wid})
    assert result["contribution"]["qualified_count"] == 1
    assert result["contribution"]["qualified"][0]["measurements"][0]["result"]["control"]
    assert result["goal_achieved"] is False
    qualified = result["contribution"]["qualified"][0]
    effect = qualified["causal_effects"][0]
    with db.read() as c:
        assert not audit().supports_component(c, qualified, effect, {}, set())
        irrelevant = {
            **qualified,
            "measurements": [{**qualified["measurements"][0], "method": "germination"}],
        }
        oid = qualified["measurements"][0]["observation_id"]
        assert not audit().supports_component(c, irrelevant, effect, {}, {oid})


def test_forged_safe_day_counter_without_fourteen_real_consumption_days_is_rejected(lab):
    db, wid, actors, _, _, at = lab
    from test_collective_regions import region

    with db.write() as c:
        rid, *_ = region(c, wid, actors[0], at)
        c.execute("UPDATE collective_regions SET safe_days=99 WHERE id=?", (rid,))
        result = audit().recovery_check(c, wid, at + timedelta(days=99), expected_regions=1)
        assert not result["passed"]
        assert not result["regions"][0]["continuous_food"]


def test_signature_only_cooperation_does_not_count_as_independent_research(lab):
    db, wid, _, _, _, _ = lab
    with db.read() as c:
        result = audit().contribution_candidates(c, wid)
        assert result["qualified_count"] == 0 and result["passed"] is False


def test_single_person_audit_accepts_zero_travel_lower_bound_at_one_location(lab):
    db, wid, _, _, _, _ = lab
    with db.read() as c:
        result = audit().single_person_bound(c, wid, horizon_hours=5760)
        assert result["travel_hours_lower_bound"] == 0
        assert result["proved_impossible"] is False


def test_global_single_person_bound_is_optimistic_even_with_every_permission_and_skill(database):
    from scripts.demo_collective_disaster import build_baseline

    wid, _ = build_baseline(database)
    with database.read() as c:
        result = audit().single_person_bound(c, wid, horizon_hours=5760)
        assert result["travel_hours_lower_bound"] > 5760
        assert result["proved_impossible"] is True
        assert result["permissions_relaxed"] and result["skills_relaxed"]
        assert result["allows_sequential_and_small_batches"]
        assert len(result["edges"]) == 20
