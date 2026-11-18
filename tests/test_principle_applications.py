"""哥哥，科技应用必须完成正常劳动，不能只增加一行影响文字。"""

from datetime import timedelta

from test_principle_research import certify, principle_lab  # noqa: F401

from world_engine.principle_applications import PrincipleApplications


def test_personal_work_really_finishes_with_technology_savings(principle_lab, settings):  # noqa: F811
    db, wid, sid, actors, at = principle_lab
    with db.write() as c:
        # 哥哥，测试只保留普通生活模式，不用旧集体实验的逐事件边界。
        c.execute("DELETE FROM collective_people")
        c.execute("UPDATE world_clock SET time_scale=1 WHERE world_id=?", (wid,))
        tid, *_ = certify(c, sid, actors[0], at)
    app = PrincipleApplications(db, settings, wid)
    result = app.apply(tid)
    with db.read() as c:
        activity = c.execute(
            "SELECT * FROM character_life_activities WHERE id=?", (result["activity_id"],)
        ).fetchone()
        assert activity["status"] == "completed"
        assert activity["ends_world_time"] == (at + timedelta(minutes=48)).isoformat()
        detail = c.execute(
            "SELECT result_json FROM activity_task_details WHERE activity_id=?",
            (result["activity_id"],),
        ).fetchone()
        assert detail[0]
        assert c.execute("SELECT 1 FROM principle_impacts WHERE technology_id=?", (tid,)).fetchone()


def test_speed_setting_is_respected_without_overshooting(principle_lab, settings):  # noqa: F811
    from world_engine.repository import from_iso

    db, wid, _, _, at = principle_lab
    with db.write() as c:
        c.execute("DELETE FROM collective_people")
        c.execute("UPDATE world_clock SET time_scale=60 WHERE world_id=?", (wid,))
    app = PrincipleApplications(db, settings, wid)
    app.advance_to(at + timedelta(minutes=30))
    with db.read() as c:
        after = from_iso(
            c.execute("SELECT w.current_time FROM worlds w WHERE id=?", (wid,)).fetchone()[0]
        )
        assert after == at + timedelta(minutes=30)


def test_tool_is_manufactured_from_real_material_before_specific_use(principle_lab, settings):  # noqa: F811
    from world_engine.principle_workshop import seed_inputs

    db, wid, sid, actors, at = principle_lab
    with db.write() as c:
        c.execute("DELETE FROM collective_people")
        c.execute("UPDATE world_clock SET time_scale=1 WHERE world_id=?", (wid,))
        seed_inputs(c, wid)
        tid, *_ = certify(
            c, sid, actors[0], at, carrier="tool", materials=[{"kind": "wood", "quantity": 2}]
        )
    result = PrincipleApplications(db, settings, wid).apply(tid)
    assert result["status"] == "applied"
    with db.read() as c:
        proto = c.execute(
            "SELECT * FROM principle_prototypes WHERE technology_id=?", (tid,)
        ).fetchone()
        assert proto is not None
        assert (
            c.execute(
                "SELECT status FROM character_life_activities WHERE id=?", (proto["activity_id"],)
            ).fetchone()[0]
            == "completed"
        )
        assert (
            c.execute(
                "SELECT condition FROM item_instances WHERE id=?", (proto["item_id"],)
            ).fetchone()[0]
            == 99
        )
        wood = c.execute(
            "SELECT i.quantity FROM item_instances i JOIN principle_material_catalogue m "
            "ON m.item_type_id=i.item_type_id WHERE m.kind='wood' "
            "AND i.owner_character_id=?",
            (actors[0],),
        ).fetchone()[0]
        assert wood == 22


def test_missing_skill_does_not_claim_an_application(principle_lab, settings):  # noqa: F811
    db, wid, sid, actors, at = principle_lab
    with db.write() as c:
        tid, *_ = certify(c, sid, actors[0], at, required_skill="未知职业技能")
    assert PrincipleApplications(db, settings, wid).apply(tid)["status"] == "needs_skill"
    with db.read() as c:
        assert not c.execute("SELECT 1 FROM principle_impacts").fetchone()


def test_nutrition_uses_actual_food_and_records_satiety_change(principle_lab):  # noqa: F811
    from world_engine.food import FoodService

    db, _, sid, actors, at = principle_lab
    with db.write() as c:
        tid, *_ = certify(
            c,
            sid,
            actors[0],
            at,
            factor=1.2,
            kind="food_nutrition",
            carrier="food_process",
            required_skill=None,
        )
        c.execute("UPDATE characters SET satiety=50 WHERE id=?", (actors[0],))
        actor = c.execute("SELECT * FROM characters WHERE id=?", (actors[0],)).fetchone()
        item = c.execute(
            "SELECT i.*,20 nutrition FROM item_instances i JOIN item_types t "
            "ON t.id=i.item_type_id WHERE i.owner_character_id=? AND t.category='food' "
            "ORDER BY i.id LIMIT 1",
            (actors[0],),
        ).fetchone()
        before = item["quantity"]
        FoodService.eat(c, actor, item, at, technology_ids=(tid,))
        assert (
            c.execute("SELECT satiety FROM characters WHERE id=?", (actors[0],)).fetchone()[0] == 74
        )
        assert (
            c.execute("SELECT quantity FROM item_instances WHERE id=?", (item["id"],)).fetchone()[0]
            == before - 1
        )
        meal = c.execute("SELECT * FROM principle_meals").fetchone()
        assert meal["quantity_before"] - meal["quantity_after"] == 1
        assert meal["satiety_after"] > meal["satiety_before"]


def test_energy_effect_is_actually_paid_by_completed_work(principle_lab, settings):  # noqa: F811
    import json

    db, wid, sid, actors, at = principle_lab
    with db.write() as c:
        c.execute("DELETE FROM collective_people")
        c.execute("UPDATE world_clock SET time_scale=1 WHERE world_id=?", (wid,))
        tid, *_ = certify(c, sid, actors[0], at, kind="labor_energy")
    result = PrincipleApplications(db, settings, wid).apply(tid)
    with db.read() as c:
        detail = c.execute(
            "SELECT spec_json FROM activity_task_details WHERE activity_id=?",
            (result["activity_id"],),
        ).fetchone()[0]
        spec = json.loads(detail)
        assert spec["energy_cost"] == 13
        assert (
            c.execute(
                "SELECT energy_paid FROM activity_task_details WHERE activity_id=?",
                (result["activity_id"],),
            ).fetchone()[0]
            == 13
        )
