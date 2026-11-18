"""哥哥，现象框架的离线样例核验因果与来源，不计作真实NPC新发现。"""

import json
import sqlite3
from datetime import timedelta

import pytest
from test_principle_research import principle_lab  # noqa: F401
from test_science_programs import craft_program
from test_science_work import finish, queue

from world_engine.collective_evidence import known_evidence
from world_engine.collective_work import WorkService
from world_engine.craft_physics import PROFILE_DEFAULTS, energy_required
from world_engine.material_physics import MaterialState
from world_engine.materials import MaterialService
from world_engine.phenomena import ensure_rule, observations, record_work
from world_engine.phenomena_rules import REGISTRY, RuleRegistry, RuleSpec, canonical
from world_engine.science_knowledge import science_context
from world_engine.science_programs import ScienceService

pytest_plugins = ["science_fixtures"]


def heat(c, wid, actor, assets, at):
    a = assets["actors"][actor]
    pid = queue(
        c,
        actor,
        {
            "batch_id": a["materials"]["clay"],
            "operation": "heat",
            "settings": {"temperature": 1000, "power": 4},
            "apparatus_id": a["apparatus"],
            "energy_id": a["energy"],
        },
        at,
    )
    WorkService.tick(c, wid, at)
    return pid


def test_unsupported_mechanism_does_not_invent_results(science_lab):
    db, wid, _, _, _, at = science_lab
    result = REGISTRY.evaluate("chemistry", "transmute_gold", expected_success=True)
    assert result.status == "unsupported" and not result.outputs and not result.public_facts
    with db.write() as c:
        with pytest.raises(ValueError, match="未支持"):
            ensure_rule(c, wid, result, "unknown", at)
        assert not c.execute("SELECT 1 FROM phenomenon_events").fetchone()


def test_registered_mechanism_is_deterministic_and_composable():
    state = MaterialState(carrier="clay", mass=1, water=0.2)
    settings = {"temperature": 900, "power": 4}
    need = energy_required(state, "heat", settings, 1, PROFILE_DEFAULTS)
    options = dict(
        state=state, settings=settings, hours=1, supplied_energy=need, profile=PROFILE_DEFAULTS
    )
    first = REGISTRY.evaluate("material_processing", "heat", **options)
    second = REGISTRY.evaluate("material_processing", "heat", **options)
    assert first.outputs == second.outputs and canonical(first.audit) == canonical(second.audit)
    cooled = REGISTRY.evaluate(
        "material_processing",
        "cool",
        state=first.outputs[0],
        settings={"temperature": 20},
        hours=1,
        supplied_energy=0,
        profile=PROFILE_DEFAULTS,
    )
    assert cooled.outputs[0].temperature < first.outputs[0].temperature
    assert state.temperature == 20


def test_rule_registration_cannot_replace_an_existing_kernel():
    registry = RuleRegistry()
    spec = RuleSpec("local", "physical", 1, ("observe",), (), (), "离线可信机制注册样例")
    registry.register(spec, lambda *args: None)
    with pytest.raises(ValueError, match="不能在运行中替换"):
        registry.register(spec, lambda *args: "伪造成功")


def test_registry_can_keep_multiple_versions_without_replacing_old_behavior():
    from dataclasses import replace

    registry = RuleRegistry()
    spec, evaluator = REGISTRY.specification("material_measurement", 1)
    registry.register(spec, evaluator)
    registry.register(replace(spec, version=2), evaluator)
    registry.freeze()
    args = dict(
        state=MaterialState(),
        metric="strength",
        instrument={"precision": 0.1, "detection_limit": 0},
        profile=PROFILE_DEFAULTS,
    )
    assert registry.evaluate(spec.key, "assay", **args).version == 2
    assert registry.evaluate(spec.key, "assay", version=1, **args).version == 1
    assert registry.evaluate(spec.key, "assay", version=3, **args).status == "unsupported"
    with pytest.raises(ValueError, match="已冻结"):
        registry.register(replace(spec, version=3), evaluator)


def test_invalid_executor_source_does_not_leave_partial_registration():
    registry = RuleRegistry()
    spec = RuleSpec("invalid", "physical", 1, ("observe",), (), (), "离线注册失败的边界验证")
    with pytest.raises(ValueError, match="半注册"):
        registry.register(spec, len)
    assert registry.capabilities() == []


def test_actual_processing_creates_only_sourced_private_observation(science_lab):
    db, wid, _, actors, assets, at = science_lab
    with db.write() as c:
        pid = heat(c, wid, actors[0], assets, at)
        assert not c.execute("SELECT 1 FROM phenomenon_events").fetchone()
        finish(c, wid, at + timedelta(hours=1))
        records = observations(c, actors[0], at + timedelta(hours=1))
        assert len(records) == 1 and records[0]["source_kind"] == "work_order"
        assert records[0]["rule_version"] == 1 and records[0]["operation"] == "heat"
        assert not observations(c, actors[1], at + timedelta(hours=1))
        assert not observations(c, actors[0], at)
        assert records[0]["id"] in known_evidence(c, actors[0], at + timedelta(hours=1))
        assert records[0]["id"] not in known_evidence(c, actors[1], at + timedelta(hours=1))
        public = canonical(records)
        assert (
            "structural_bond" not in public
            and "strength_scale" not in public
            and "profile" not in public
        )
        audit = json.loads(c.execute("SELECT audit_json FROM phenomenon_events").fetchone()[0])
        assert audit["calculation"]["after"][0]["temperature"] > 20
        order = c.execute(
            "SELECT * FROM collective_work_orders WHERE proposal_id=?", (pid,)
        ).fetchone()
        after = MaterialService.state(c, json.loads(order["outputs_json"])[0])
        assert after.temperature == audit["calculation"]["after"][0]["temperature"]
        assert MaterialService.audit(c, wid)["balanced"]
        WorkService.tick(c, wid, at + timedelta(hours=2))
        assert c.execute("SELECT count(*) FROM phenomenon_events").fetchone()[0] == 1


def test_preview_does_not_create_rules_phenomena_or_observation(science_lab):
    db, _, sid, actors, assets, at = science_lab
    with db.write() as c:
        before = list(c.iterdump())
        result = ScienceService.preview(
            c, sid, actors[0], craft_program(assets["actors"][actors[0]]), at
        )
        assert result["status"] == "ready_to_start" and list(c.iterdump()) == before
        assert not c.execute("SELECT 1 FROM phenomenon_rules").fetchone()


def test_chained_work_and_assay_expose_reading_but_not_internal_sample_conditions(science_lab):
    db, wid, sid, actors, assets, at = science_lab
    with db.write() as c:
        ScienceService.submit(c, sid, actors[0], craft_program(assets["actors"][actors[0]]), at)
        ScienceService.tick(c, wid, at)
        finish(c, wid, at + timedelta(hours=1))
        ScienceService.tick(c, wid, at + timedelta(hours=1))
        finish(c, wid, at + timedelta(hours=1.5))
        ScienceService.tick(c, wid, at + timedelta(hours=1.5))
        records = observations(c, actors[0], at + timedelta(hours=1.5))
        facts = [f for r in records for f in r["facts"] if f["channel"] == "instrument"]
        assert len(facts) == 1 and facts[0]["metric"] == "strength" and facts[0]["precision"] == 0.1
        context = science_context(c, actors[0], at + timedelta(hours=1.5), max_chars=80000)
        raw_measurement = c.execute(
            "SELECT content_json FROM collective_observations WHERE kind='work_measurement'"
        ).fetchone()[0]
        assert "temperature" in json.loads(raw_measurement)["measurement"]["sample_conditions"]
        for packet in context["observations"]:
            measurement = packet["content"].get("measurement")
            if measurement:
                assert set(measurement["sample_conditions"]) == {"carrier"}
        assert not any("properties" in m for m in context["phenomenon_mechanisms"])


def test_same_rule_version_cannot_change_while_work_is_pending(science_lab):
    db, wid, _, actors, assets, at = science_lab
    with db.write() as c:
        heat(c, wid, actors[0], assets, at)
        rules = json.loads(
            c.execute(
                "SELECT rules_json FROM science_workshops WHERE world_id=?", (wid,)
            ).fetchone()[0]
        )
        rules["clay"]["strength_scale"] += 10
        c.execute(
            "UPDATE science_workshops SET rules_json=? WHERE world_id=?", (canonical(rules), wid)
        )
        # 哥哥，整个结算保存点回滚；不能把变更后的规则用于已开始的试验。
        with pytest.raises(ValueError, match="同一规则版本"):
            finish(c, wid, at + timedelta(hours=1))
        assert not c.execute("SELECT 1 FROM phenomenon_events").fetchone()


def test_observation_requires_actually_settled_source(science_lab):
    db, wid, _, actors, assets, at = science_lab
    with db.write() as c:
        pid = heat(c, wid, actors[0], assets, at)
        order = c.execute(
            "SELECT id FROM collective_work_orders WHERE proposal_id=?", (pid,)
        ).fetchone()[0]
        with pytest.raises(ValueError, match="已经实际结算"):
            record_work(c, order, MaterialState(), (), (), 1, at)


def test_zero_time_interruption_releases_work_without_inventing_observation(science_lab):
    from world_engine.life import LifeActivityService

    db, wid, _, actors, assets, at = science_lab
    with db.write() as c:
        pid = heat(c, wid, actors[0], assets, at)
        LifeActivityService.interrupt(c, actors[0], at, "离线零工时中断验证")
        WorkService.tick(c, wid, at)
        row = c.execute(
            "SELECT * FROM collective_work_orders WHERE proposal_id=?", (pid,)
        ).fetchone()
        assert row["status"] == "interrupted"
        assert not c.execute("SELECT 1 FROM phenomenon_events").fetchone()
        assert not c.execute(
            "SELECT 1 FROM item_instances WHERE container_type='collective_work_tool'"
        ).fetchone()
        assert MaterialService.audit(c, wid)["balanced"]


def test_injury_before_first_work_progress_does_not_block_interruption(science_lab):
    db, wid, _, actors, assets, at = science_lab
    with db.write() as c:
        pid = heat(c, wid, actors[0], assets, at)
        c.execute("UPDATE characters SET health=health-1 WHERE id=?", (actors[0],))
        finish(c, wid, at + timedelta(minutes=30))
        row = c.execute(
            "SELECT * FROM collective_work_orders WHERE proposal_id=?", (pid,)
        ).fetchone()
        assert row["status"] == "interrupted"
        assert not c.execute("SELECT 1 FROM phenomenon_events").fetchone()
        assert MaterialService.audit(c, wid)["balanced"]


def test_first_magic_activity_seals_parameters_before_work_finishes(
    principle_lab,  # noqa: F811
    settings,
    monkeypatch,
):
    from test_magic_research import register

    from world_engine.principle_applications import PrincipleApplications

    db, wid, sid, actors, at = principle_lab
    with db.write() as c:
        tech, inv, items, _ = register(c, wid, sid, actors[0], at)
    app = PrincipleApplications(db, settings, wid)
    original = app.work

    def change_parameters(*args, **kwargs):
        result = original(*args, **kwargs)
        with db.write() as c:
            assert c.execute("SELECT count(*) FROM phenomenon_rules").fetchone()[0] == 1
            c.execute("UPDATE magic_patterns SET base_efficiency=.8")
        return result

    monkeypatch.setattr(app, "work", change_parameters)
    with pytest.raises(ValueError, match="同一规则版本"):
        app.apply(tech, inv)
    with db.read() as c:
        assert not c.execute("SELECT 1 FROM magic_applications").fetchone()
        assert not c.execute("SELECT 1 FROM phenomenon_events").fetchone()
        assert (
            c.execute(
                "SELECT stored FROM magic_energy_devices WHERE item_id=?", (items[0],)
            ).fetchone()[0]
            == 3
        )


def test_magic_keeps_started_version_when_new_version_becomes_available(
    principle_lab,  # noqa: F811
    settings,
    monkeypatch,
):
    from dataclasses import replace

    from test_magic_research import register

    from world_engine import phenomena, phenomena_rules
    from world_engine.principle_applications import PrincipleApplications

    db, wid, sid, actors, at = principle_lab
    with db.write() as c:
        tech, inv, _, _ = register(c, wid, sid, actors[0], at)
    app = PrincipleApplications(db, settings, wid)
    original = app.work

    def load_new_version(*args, **kwargs):
        activity = original(*args, **kwargs)
        registry = RuleRegistry()
        spec, evaluator = REGISTRY.specification("magic_energy", 1)
        registry.register(spec, evaluator)
        registry.register(replace(spec, version=2), evaluator)
        registry.freeze()
        monkeypatch.setattr(phenomena_rules, "REGISTRY", registry)
        monkeypatch.setattr(phenomena, "REGISTRY", registry)
        return activity

    monkeypatch.setattr(app, "work", load_new_version)
    assert app.apply(tech, inv)["status"] == "applied"
    with db.read() as c:
        assert [r[0] for r in c.execute("SELECT version FROM phenomenon_rules")] == [1]


def test_rule_and_phenomenon_records_are_immutable(science_lab):
    db, wid, _, actors, assets, at = science_lab
    with db.write() as c:
        heat(c, wid, actors[0], assets, at)
        finish(c, wid, at + timedelta(hours=1))
        for table, field in (
            ("phenomenon_rules", "definition_json"),
            ("phenomenon_events", "audit_json"),
            ("phenomenon_observations", "facts_json"),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(f"UPDATE {table} SET {field}='伪造结果'")


def test_instrument_detection_limit_is_not_a_negative_result():
    result = REGISTRY.evaluate(
        "material_measurement",
        "assay",
        state=MaterialState(),
        metric="strength",
        instrument={"precision": 0.1, "detection_limit": 999},
        profile=PROFILE_DEFAULTS,
    )
    assert (
        result.public_facts[0]["value"] is None and result.public_facts[0]["detection_limit"] == 999
    )


def test_magic_observation_has_real_energy_source_and_only_charge_bands(principle_lab, settings):  # noqa: F811
    from test_magic_research import register

    from world_engine.principle_applications import PrincipleApplications
    from world_engine.repository import from_iso

    db, wid, sid, actors, at = principle_lab
    with db.write() as c:
        tech, inv, _, _ = register(c, wid, sid, actors[0], at)
    result = PrincipleApplications(db, settings, wid).apply(tech, inv)
    with db.read() as c:
        end = from_iso(
            c.execute(
                "SELECT ends_world_time FROM character_life_activities WHERE id=?",
                (result["activity_id"],),
            ).fetchone()[0]
        )
        seen = observations(c, actors[0], end)
        assert len(seen) == 1 and seen[0]["source_kind"] == "magic_application"
        assert seen[0]["facts"][0]["after"] == "耗尽"
        assert seen[0]["facts"][1]["before"] == "耗尽"
        assert not observations(c, actors[1], end)
        public = canonical(seen)
        assert (
            "private-band" not in public
            and "capacity" not in public
            and "base_efficiency" not in public
        )


def test_observation_api_is_read_only_and_scoped(science_lab):
    from test_science_api import client

    db, wid, _, actors, assets, at = science_lab
    with db.write() as c:
        heat(c, wid, actors[0], assets, at)
        finish(c, wid, at + timedelta(hours=1))
        before = list(c.iterdump())
    api = client(db)
    response = api.get(f"/api/worlds/{wid}/science/phenomena", params={"observer_id": actors[0]})
    assert response.status_code == 200 and len(response.json()) == 1
    assert "audit_json" not in response.text and "definition_json" not in response.text
    assert (
        api.get(f"/api/worlds/{wid}/science/phenomena", params={"observer_id": actors[1]}).json()
        == []
    )
    assert (
        api.get(
            f"/api/worlds/{wid}/science/phenomena", params={"observer_id": "missing"}
        ).status_code
        == 404
    )
    assert len(api.get(f"/api/worlds/{wid}/science/mechanisms").json()["mechanisms"]) == 3
    with db.read() as c:
        assert list(c.iterdump()) == before
