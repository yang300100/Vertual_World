"""哥哥，离线魔法样例只核验守恒与权限，不计作真实NPC新研发。"""

import json
from uuid import uuid4

import pytest
from test_principle_research import call, principle_lab  # noqa: F401
from test_principle_tree import foundation

from world_engine.event_log import record_event
from world_engine.magic_research import public_context, settle, validate
from world_engine.principle_applications import PrincipleApplications
from world_engine.principle_inventions import resolve
from world_engine.principle_service import PrincipleTechnologyService as T
from world_engine.principle_types import CapabilityEffect
from world_engine.repository import to_iso


def setup(c, wid, actor_id, at, operation="transfer"):
    event = record_event(
        c,
        world_id=wid,
        tick_id=str(uuid4()),
        occurred_at=at,
        event_type="world.magic_profile_registered",
        actor_id=actor_id,
        summary="离线作者登记的有限器物与已知单纹",
        payload={},
    )
    pattern = str(uuid4())
    c.execute(
        "INSERT INTO magic_patterns VALUES (?,?,?,?,'private-band',0.8,0.5,0.7,0.9,?)",
        (pattern, wid, "离线完整单纹", operation, event),
    )
    c.execute(
        "INSERT INTO magic_known_patterns VALUES (?,?,?,?,?)",
        (wid, actor_id, pattern, to_iso(at), event),
    )
    c.execute("INSERT INTO magic_attunements VALUES (?,?,?,?)", (actor_id, wid, to_iso(at), event))
    items = []
    for stored in (3, 0):
        tid, iid = str(uuid4()), str(uuid4())
        c.execute(
            "INSERT INTO item_types(id,name,category,stack_limit,slot_size) "
            "VALUES (?,?,'tool',1,1)",
            (tid, "离线登记储能器物"),
        )
        c.execute(
            "INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,"
            "owner_character_id,quantity,condition) VALUES (?,?,?,'character_inventory',?,?,1,100)",
            (iid, wid, tid, actor_id, actor_id),
        )
        c.execute(
            "INSERT INTO magic_energy_devices VALUES (?,?,'private-band',3,?,0.1,?)",
            (iid, wid, stored, event),
        )
        items.append(iid)
    plan = {
        "pattern_id": pattern,
        "source_item_id": items[0],
        "receiver_item_id": items[1] if operation == "transfer" else None,
    }
    effect = {
        "kind": "magic_transfer_efficiency" if operation == "transfer" else "magic_energy_cost",
        "activity": "magic",
        "factor": 1.2 if operation == "transfer" else 0.8,
        "explanation": "只改进该已知完整单纹的能量利用，输入和损耗仍遵守真实器物边界。",
        "required_skill": None,
        "carrier": "knowledge",
        "materials": [],
        "magic_plan": plan,
    }
    return items, plan, effect


def register(c, wid, sid, actor, at, operation="transfer", materials=(), legacy=False):
    items, plan, effect = setup(c, wid, actor, at, operation)
    effect["materials"] = list(materials)
    spec = foundation("离线魔法损耗研究")
    spec.update(domain="magic", inventions=[spec["inventions"][0] | {"magic_plan": plan}])
    batch = {"reason": "离线核验已有单纹的有限能源结算", "ideas": [spec]}
    cid = call(c, sid, actor, "propose", batch, at)
    pid = T.propose(c, sid, actor, cid, batch, at)[0]
    review = {
        "proposal_id": pid,
        "approved": True,
        "reasoning": "既有单纹和实际器物相容，有限损耗改进不产生新魔纹或无源能量。",
        "effect": None,
        "connections": [],
        "invention_reviews": [
            {
                "index": 0,
                "approved": True,
                "reasoning": "应用绑定已知纹路与自有器物的有限供能，不改变固定核心效果。",
                "effect": effect,
            }
        ],
    }
    if legacy:
        review.update(effect=effect, invention_reviews=[])
    rid = call(c, sid, None, "review", {"decisions": [review]}, at)
    tech = T.review(c, pid, rid, review, at)
    inv = c.execute(
        "SELECT id FROM principle_inventions WHERE technology_id=?", (tech,)
    ).fetchone()[0]
    return tech, inv, items, effect


def test_transfer_changes_real_energy_with_loss_and_no_double_settlement(principle_lab, settings):  # noqa: F811
    db, wid, sid, actors, at = principle_lab
    with db.write() as c:
        tech, inv, items, _ = register(c, wid, sid, actors[0], at)
    result = PrincipleApplications(db, settings, wid).apply(tech, inv)
    assert result["status"] == "applied" and result["loss"] > 0
    with db.write() as c:
        energy = [
            c.execute("SELECT stored FROM magic_energy_devices WHERE item_id=?", (i,)).fetchone()[0]
            for i in items
        ]
        assert 0 <= energy[0] < 3 and energy[1] > 0 and sum(energy) < 3
        actor = c.execute("SELECT * FROM characters WHERE id=?", (actors[0],)).fetchone()
        finished = c.execute(
            "SELECT ends_world_time FROM character_life_activities WHERE id=?",
            (result["activity_id"],),
        ).fetchone()[0]
        from world_engine.repository import from_iso

        settle(c, resolve(c, tech, inv), actor, result["activity_id"], from_iso(finished))
        assert [
            c.execute("SELECT stored FROM magic_energy_devices WHERE item_id=?", (i,)).fetchone()[0]
            for i in items
        ] == energy
        assert c.execute("SELECT count(*) FROM magic_applications").fetchone()[0] == 1


def test_cast_cost_draws_only_registered_storage_and_keeps_output(principle_lab, settings):  # noqa: F811
    db, wid, sid, actors, at = principle_lab
    with db.write() as c:
        tech, inv, items, _ = register(c, wid, sid, actors[0], at, "cast")
    result = PrincipleApplications(db, settings, wid).apply(tech, inv)
    assert result["output"] == 0.5 and result["source_consumed"] == pytest.approx(0.64)
    assert result["loss"] > 0
    with db.read() as c:
        assert c.execute(
            "SELECT stored FROM magic_energy_devices WHERE item_id=?", (items[0],)
        ).fetchone()[0] == pytest.approx(2.36)


def test_identity_does_not_substitute_for_guidance_and_known_pattern(principle_lab):  # noqa: F811
    db, wid, _, actors, at = principle_lab
    with db.write() as c:
        _, plan, effect = setup(c, wid, actors[0], at, "cast")
        c.execute("DELETE FROM magic_attunements")
        actor = c.execute("SELECT * FROM characters WHERE id=?", (actors[0],)).fetchone()
        with pytest.raises(ValueError, match="引导与激活"):
            validate(c, actor, CapabilityEffect.model_validate(effect), at)
        c.execute("DELETE FROM magic_known_patterns")
        with pytest.raises(ValueError, match="未实际掌握"):
            validate(c, actor, CapabilityEffect.model_validate(effect), at)


def test_incompatible_or_foreign_device_never_becomes_available(principle_lab):  # noqa: F811
    db, wid, _, actors, at = principle_lab
    with db.write() as c:
        items, _, effect = setup(c, wid, actors[0], at)
        actor = c.execute("SELECT * FROM characters WHERE id=?", (actors[0],)).fetchone()
        c.execute(
            "UPDATE item_instances SET owner_character_id=? WHERE id=?", (actors[1], items[0])
        )
        with pytest.raises(ValueError, match="现场自有"):
            validate(c, actor, CapabilityEffect.model_validate(effect), at)
        c.execute(
            "UPDATE item_instances SET owner_character_id=? WHERE id=?", (actors[0], items[0])
        )
        c.execute(
            "UPDATE magic_energy_devices SET coupling_key='other' WHERE item_id=?", (items[0],)
        )
        with pytest.raises(ValueError, match="不相容"):
            validate(c, actor, CapabilityEffect.model_validate(effect), at)


def test_actor_context_omits_author_measurements_and_private_patterns(principle_lab):  # noqa: F811
    db, wid, _, actors, at = principle_lab
    with db.write() as c:
        setup(c, wid, actors[0], at)
        actor = c.execute("SELECT * FROM characters WHERE id=?", (actors[0],)).fetchone()
        text = json.dumps(public_context(c, actor, at), ensure_ascii=False)
        assert "private-band" not in text and "base_cost" not in text and "capacity" not in text
        other = c.execute("SELECT * FROM characters WHERE id=?", (actors[1],)).fetchone()
        assert public_context(c, other, at)["known_patterns"] == []


def test_self_charging_cycle_is_rejected(principle_lab):  # noqa: F811
    db, wid, _, actors, at = principle_lab
    with db.write() as c:
        items, _, effect = setup(c, wid, actors[0], at)
        effect["magic_plan"]["receiver_item_id"] = items[0]
        actor = c.execute("SELECT * FROM characters WHERE id=?", (actors[0],)).fetchone()
        with pytest.raises(ValueError, match="自充循环"):
            validate(c, actor, CapabilityEffect.model_validate(effect), at)


def test_magic_knowledge_cannot_skip_declared_materials(principle_lab, settings):  # noqa: F811
    db, wid, sid, actors, at = principle_lab
    with db.write() as c:
        tech, inv, items, _ = register(
            c, wid, sid, actors[0], at, materials=[{"kind": "metal", "quantity": 2}]
        )
    with pytest.raises(ValueError, match="现实物资"):
        PrincipleApplications(db, settings, wid).apply(tech, inv)
    with db.read() as c:
        assert (
            c.execute(
                "SELECT stored FROM magic_energy_devices WHERE item_id=?", (items[0],)
            ).fetchone()[0]
            == 3
        )
        assert not c.execute("SELECT 1 FROM magic_applications").fetchone()


def test_magic_knowledge_records_actual_auxiliary_material_consumption(principle_lab, settings):  # noqa: F811
    from world_engine.principle_workshop import seed_inputs

    db, wid, sid, actors, at = principle_lab
    with db.write() as c:
        seed_inputs(c, wid)
        tech, inv, _, _ = register(
            c, wid, sid, actors[0], at, materials=[{"kind": "metal", "quantity": 2}]
        )
    assert PrincipleApplications(db, settings, wid).apply(tech, inv)["status"] == "applied"
    with db.read() as c:
        count = c.execute(
            "SELECT i.quantity FROM item_instances i JOIN principle_material_catalogue m "
            "ON m.item_type_id=i.item_type_id WHERE m.kind='metal' AND i.owner_character_id=?",
            (actors[0],),
        ).fetchone()[0]
        assert count == 22
        assert (
            c.execute(
                "SELECT count(*) FROM principle_material_uses WHERE technology_id=?", (tech,)
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("reuse_receipt", [False, True])
def test_direct_settlement_cannot_skip_or_reuse_material_receipt(
    principle_lab,  # noqa: F811
    settings,
    reuse_receipt,
):
    from world_engine.principle_workshop import seed_inputs
    from world_engine.repository import from_iso

    db, wid, sid, actors, at = principle_lab
    with db.write() as c:
        seed_inputs(c, wid)
        tech, inv, items, _ = register(
            c, wid, sid, actors[0], at, materials=[{"kind": "metal", "quantity": 2}]
        )
    app = PrincipleApplications(db, settings, wid)
    receipt = None
    if reuse_receipt:
        app.apply(tech, inv)
        with db.read() as c:
            receipt = c.execute("SELECT auxiliary_use_id FROM magic_applications").fetchone()[0]
    activity = app.work(
        actors[0],
        "离线核验伪造耗材引用不能取得魔法效果",
        technology_ids=(),
        application_reference={
            "technology_id": tech,
            "invention_id": inv,
            "auxiliary_use_id": receipt,
        },
    )
    with db.write() as c:
        energy_before = [
            c.execute("SELECT stored FROM magic_energy_devices WHERE item_id=?", (i,)).fetchone()[0]
            for i in items
        ]
        actor = c.execute("SELECT * FROM characters WHERE id=?", (actors[0],)).fetchone()
        end = from_iso(
            c.execute(
                "SELECT ends_world_time FROM character_life_activities WHERE id=?", (activity,)
            ).fetchone()[0]
        )
        with pytest.raises(ValueError, match="不能跳过或复用旧凭证"):
            settle(c, resolve(c, tech, inv), actor, activity, end)
        assert [
            c.execute("SELECT stored FROM magic_energy_devices WHERE item_id=?", (i,)).fetchone()[0]
            for i in items
        ] == energy_before


def test_legacy_single_effect_cannot_bypass_magic_invention_review(principle_lab):  # noqa: F811
    db, wid, sid, actors, at = principle_lab
    with db.write() as c:
        with pytest.raises(ValueError, match="不能使用旧版单项契约"):
            register(c, wid, sid, actors[0], at, legacy=True)
        assert c.execute("SELECT count(*) FROM principle_technologies").fetchone()[0] == 0
