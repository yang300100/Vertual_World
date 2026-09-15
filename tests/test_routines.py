# ruff: noqa: F811
"""周期作息必须由实际活动履行，跳时、修订和重复心跳不能创造收益。"""

from datetime import timedelta
from uuid import uuid4

import pytest
from test_lived_world import definition, lodging, post, town  # noqa: F401

from scripts.prepare_life_content import export_packet, validate_draft
from world_engine.daily_life import DailyLifeService
from world_engine.life import LifeActivityService
from world_engine.routines import RoutinePlanSpec, RoutineService


def spec(e, **overrides):
    return {
        "name": "居民的生活安排",
        "character_id": e.npc,
        "slots": [
            {
                "key": "morning",
                "name": "上午工作",
                "weekdays": list(range(7)),
                "starts_at": "08:05",
                "duration_minutes": 125,
                "target_minutes": 120,
                "kind": "work",
                "location_id": e.loc["id"],
                "visibility": "public",
            }
        ],
        **overrides,
    }


def register(e, body, expected="applied"):
    pending = post(e, "npc-routines", {"idempotency_key": str(uuid4()), "routine": body}, 201)
    assert pending["registration"]["status"] == "proposed"
    result = post(e, f"registrations/{pending['registration']['id']}/confirm")
    assert result["status"] == expected, result
    return result


def npc(e):
    with e.db.read() as c:
        return dict(c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone())


def test_periodic_work_pays_actual_completed_units_and_does_not_replay(town):
    e = town
    register(e, spec(e))
    before = npc(e)["money"]
    e.clock.heartbeat(e.wid, elapsed_seconds=300)
    assert npc(e)["money"] == before
    e.db.initialize()
    e.db.initialize()
    with e.db.read() as c:
        assert c.execute("SELECT count(*) FROM npc_routine_activities").fetchone()[0] == 1
    e.clock.heartbeat(e.wid, elapsed_seconds=3600)
    assert npc(e)["money"] == before + 12
    e.clock.heartbeat(e.wid, elapsed_seconds=3600)
    e.clock.heartbeat(e.wid, elapsed_seconds=300)
    assert npc(e)["money"] == before + 24
    e.clock.heartbeat(e.wid, elapsed_seconds=0)
    with e.db.read() as c:
        occurrence = c.execute(
            "SELECT * FROM npc_routine_occurrences WHERE character_id=?", (e.npc,)
        ).fetchone()
        assert occurrence["status"] == "completed"
        assert RoutineService.completed_minutes(c, occurrence["id"]) == 120
        assert (
            c.execute(
                "SELECT count(*) FROM npc_routine_activities WHERE occurrence_id=?",
                (occurrence["id"],),
            ).fetchone()[0]
            == 2
        )


def test_skipped_days_record_missed_occurrences_without_backpay(town):
    e = town
    register(e, spec(e))
    before = npc(e)["money"]
    with e.db.write() as c:
        actor = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        RoutineService.context(c, actor, e.at + timedelta(days=3, hours=5))
        count = c.execute(
            "SELECT count(*) FROM npc_routine_occurrences WHERE status='missed'"
        ).fetchone()[0]
        assert count == 4
        RoutineService.context(c, actor, e.at + timedelta(days=3, hours=5))
        assert (
            c.execute(
                "SELECT count(*) FROM npc_routine_occurrences WHERE status='missed'"
            ).fetchone()[0]
            == count
        )
        assert not c.execute("SELECT 1 FROM npc_routine_activities").fetchone()
    assert npc(e)["money"] == before


def test_long_skips_are_bounded_and_repeat_as_next_week_not_retroactive_work(town):
    e = town
    body = spec(e)
    body["slots"][0]["weekdays"] = [e.at.weekday()]
    register(e, body)
    with e.db.write() as c:
        actor = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        current = RoutineService.context(c, actor, e.at + timedelta(days=35, minutes=5))
        assert current["slot"].key == "morning"
        assert current["occurrence"]["status"] == "planned"
        assert c.execute("SELECT count(*) FROM npc_routine_occurrences").fetchone()[0] <= 3
        assert (
            c.execute(
                "SELECT count(*) FROM world_events WHERE event_type='life.routine_gap'"
            ).fetchone()[0]
            == 1
        )


def test_cross_midnight_weekly_clock_and_offset_are_stable(town):
    e = town
    body = spec(e, utc_offset_minutes=480)
    body["slots"] = [
        {
            "key": "night",
            "name": "跨日休息",
            "weekdays": [6],
            "starts_at": "23:30",
            "duration_minutes": 120,
            "kind": "rest",
            "location_id": e.loc["id"],
        }
    ]
    register(e, body)
    # 2030-01-06 16:15 UTC 对应作息当地周一00:15，属于周日开始的夜间时段。
    at = e.at.replace(day=6, hour=16, minute=15)
    with e.db.write() as c:
        actor = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        context = RoutineService.context(c, actor, at)
        assert context["start"] == at.replace(hour=15, minute=30)
        assert context["end"] == at.replace(hour=17, minute=30)


@pytest.mark.parametrize(
    "slots",
    [
        [
            {"key": "one", "weekdays": [6], "starts_at": "23:30", "duration_minutes": 120},
            {"key": "two", "weekdays": [0], "starts_at": "00:00", "duration_minutes": 60},
        ],
        [
            {"key": "same", "weekdays": [1], "starts_at": "08:00", "duration_minutes": 60},
            {"key": "same", "weekdays": [2], "starts_at": "08:00", "duration_minutes": 60},
        ],
        [{"key": "one", "weekdays": [1, 1], "starts_at": "08:00", "duration_minutes": 60}],
    ],
)
def test_overlapping_or_ambiguous_weekly_definitions_are_rejected(slots):
    with pytest.raises(ValueError):
        RoutinePlanSpec.model_validate(
            {
                "name": "无效安排",
                "character_id": "npc",
                "slots": [
                    {"name": "休息", "kind": "rest", "location_id": "place", **slot}
                    for slot in slots
                ],
            }
        )


def test_night_work_is_not_overridden_by_the_old_default_bedtime(town):
    e = town
    body = spec(e)
    body["slots"][0].update(starts_at="23:55", duration_minutes=65, target_minutes=60)
    register(e, body)
    e.clock.heartbeat(e.wid, elapsed_seconds=(15 * 60 + 55) * 60)
    with e.db.read() as c:
        assert LifeActivityService.running(c, e.npc)["kind"] == "work"


def test_registered_shift_starts_even_at_an_adjudication_boundary(town):
    from world_engine.repository import to_iso

    e = town
    body = spec(e)
    body["slots"][0].update(starts_at="12:00", duration_minutes=60, target_minutes=60)
    register(e, body)
    with e.db.write() as c:
        c.execute(
            "UPDATE world_clock SET next_adjudication_world_time=? WHERE world_id=?",
            (to_iso(e.at.replace(hour=12)), e.wid),
        )
    result = e.clock.heartbeat(e.wid, elapsed_seconds=4 * 3600)
    assert result.adjudication_due
    with e.db.read() as c:
        activity = LifeActivityService.running(c, e.npc)
        assert activity["kind"] == "work"
        assert activity["started_world_time"] == to_iso(e.at.replace(hour=12))


def test_daily_work_allowance_prevents_more_automatic_units(town):
    e = town
    register(e, spec(e, max_work_minutes_per_day=60))
    before = npc(e)["money"]
    for elapsed in (300, 3600, 3900):
        e.clock.heartbeat(e.wid, elapsed_seconds=elapsed)
    assert npc(e)["money"] == before + 12
    with e.db.read() as c:
        assert (
            c.execute(
                "SELECT status FROM npc_routine_occurrences WHERE character_id=?", (e.npc,)
            ).fetchone()[0]
            == "partial"
        )


def test_reserve_goal_skips_optional_work_without_pretending_completion(town):
    e = town
    register(e, spec(e, goal="maintain_reserve", income_reserve=100))
    before = npc(e)["money"]
    e.clock.heartbeat(e.wid, elapsed_seconds=300)
    with e.db.read() as c:
        assert (
            c.execute(
                "SELECT status FROM npc_routine_occurrences WHERE character_id=?", (e.npc,)
            ).fetchone()[0]
            == "skipped"
        )
        assert not c.execute("SELECT 1 FROM npc_routine_activities").fetchone()
    assert npc(e)["money"] == before


def test_revision_and_disable_preserve_the_activity_already_started(town):
    e = town
    register(e, spec(e))
    e.clock.heartbeat(e.wid, elapsed_seconds=300)
    with e.db.read() as c:
        aid = LifeActivityService.running(c, e.npc)["id"]
    revised = spec(e, expected_revision=1, enabled=False)
    with e.db.write() as c:
        c.execute("UPDATE locations SET is_active=0 WHERE id=?", (e.loc["id"],))
    register(e, revised)
    register(e, revised, expected="rejected")
    with e.db.read() as c:
        assert (
            c.execute("SELECT status FROM character_life_activities WHERE id=?", (aid,)).fetchone()[
                0
            ]
            == "running"
        )
        assert (
            c.execute(
                "SELECT status FROM npc_routine_occurrences WHERE character_id=?", (e.npc,)
            ).fetchone()[0]
            == "superseded"
        )
        assert (
            c.execute(
                "SELECT revision FROM npc_routine_plans WHERE character_id=?", (e.npc,)
            ).fetchone()[0]
            == 2
        )


def test_private_rest_and_internal_reserve_are_not_public_schedule(town):
    e = town
    body = spec(e)
    body["slots"].append(
        {
            "key": "private",
            "name": "私人休息",
            "starts_at": "22:00",
            "duration_minutes": 120,
            "kind": "rest",
            "location_id": e.loc["id"],
        }
    )
    register(e, body)
    post(e, "contacts", {"recipient_id": e.npc})
    public = e.client.get(f"/api/worlds/{e.wid}/player/experience").json()["public_routines"]
    schedule = next(item for item in public if item["character_id"] == e.npc)
    assert len(schedule["slots"]) == 1
    assert "income_reserve" not in schedule
    assert "recipe_id" not in schedule["slots"][0]
    register(e, {**body, "character_id": e.player}, expected="rejected")


def test_lack_of_wages_leads_to_real_travel_and_alternative_pay(town):
    e = town
    with e.db.write() as c:
        alternate = c.execute(
            "SELECT * FROM locations WHERE world_id=? AND id!=? LIMIT 1", (e.wid, e.loc["id"])
        ).fetchone()
        c.execute(
            "UPDATE locations SET longitude=?,latitude=?,area_radius_km=.05,area_priority=30 WHERE id=?",
            (e.loc["longitude"] + 0.002, e.loc["latitude"], alternate["id"]),
        )
        c.execute("UPDATE workplace_accounts SET balance=0 WHERE location_id=?", (e.loc["id"],))
    definition(
        e,
        {
            "element_type": "workplace_budget",
            "location_id": alternate["id"],
            "initial_funds": 500,
            "wage": 7,
        },
    )
    register(e, spec(e))
    before = npc(e)["money"]
    e.clock.heartbeat(e.wid, elapsed_seconds=300)
    assert npc(e)["money"] == before
    with e.db.read() as c:
        assert c.execute(
            "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'", (e.npc,)
        ).fetchone()
    e.clock.heartbeat(e.wid, elapsed_seconds=300)
    e.clock.heartbeat(e.wid, elapsed_seconds=3600)
    assert npc(e)["money"] == before + 7
    with e.db.read() as c:
        assert (
            c.execute(
                "SELECT location_id FROM npc_work_preferences WHERE character_id=?", (e.npc,)
            ).fetchone()[0]
            == alternate["id"]
        )


def test_npc_leaves_room_rented_to_someone_else_before_resting(town):
    e = town
    room = lodging(e)
    with e.db.write() as c:
        c.execute("UPDATE characters SET current_room_id=? WHERE id=?", (room, e.npc))
        actor = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        assert DailyLifeService._rest_at_home(c, actor, e.at, 60) == "离开房间"
        assert (
            c.execute("SELECT current_room_id FROM characters WHERE id=?", (e.npc,)).fetchone()[0]
            is None
        )
        assert LifeActivityService.running(c, e.npc) is None


def test_content_packet_exports_fifth_schema_and_checks_routine_revisions(town, tmp_path):
    e = town
    register(e, spec(e))
    packet = export_packet(e.settings.database_path, e.wid, [e.loc["id"]], tmp_path / "packet")
    assert (tmp_path / "packet/schema-npc_routine.json").is_file()
    assert packet["npc_routines"][0]["revision"] == 1
    draft = {
        "format_version": 1,
        "world_id": e.wid,
        "entries": [
            {
                "key": "routine",
                "source_note": "待作者核对",
                "payload": {"element_type": "npc_routine", **spec(e, expected_revision=1)},
            }
        ],
    }
    assert validate_draft(draft, packet) == 1
    draft["entries"][0]["payload"]["expected_revision"] = 2
    with pytest.raises(ValueError):
        validate_draft(draft, packet)


def test_npc_context_knows_its_own_weekly_preferences(town):
    from world_engine.engine import WorldEngine

    e = town
    register(e, spec(e))
    engine = WorldEngine(e.db, e.settings)
    with e.db.read() as c:
        snapshot = engine.repository.get_snapshot(c, e.wid)
        context = engine.dialogue_context.build(
            c,
            snapshot=snapshot,
            npc=snapshot.character_by_id(e.npc),
            player=snapshot.character_by_id(e.player),
            player_text="你平时几点工作？",
            conversation=None,
            channel="in_person",
            interaction="询问日常习惯",
        )
    routine = context["personal_experience"]["routine"]
    assert routine["enabled"]
    assert any("上午工作" in entry and "08:05" in entry for entry in routine["weekly_slots"])


def test_appointment_takes_priority_over_starting_periodic_work(town):
    from test_schedules import meeting

    e = town
    register(e, spec(e))
    meeting(e, hours=1)
    before = npc(e)["money"]
    e.clock.heartbeat(e.wid, elapsed_seconds=300)
    e.clock.heartbeat(e.wid, elapsed_seconds=3300)
    with e.db.read() as c:
        assert LifeActivityService.running(c, e.npc) is None
        assert (
            c.execute(
                "SELECT intention FROM npc_daily_states WHERE character_id=?", (e.npc,)
            ).fetchone()[0]
            == "已到约定地点，等候对方"
        )
    assert npc(e)["money"] == before


def test_alternative_commute_also_reserves_the_return_to_an_appointment(town):
    from test_schedules import meeting

    e = town
    with e.db.write() as c:
        alternate = c.execute(
            "SELECT id FROM locations WHERE world_id=? AND kind='workplace' LIMIT 1", (e.wid,)
        ).fetchone()[0]
        c.execute(
            "UPDATE locations SET longitude=?,latitude=?,area_radius_km=.1 WHERE id=?",
            (e.loc["longitude"] + 0.05, e.loc["latitude"], alternate),
        )
        c.execute("UPDATE workplace_accounts SET balance=0 WHERE location_id=?", (e.loc["id"],))
    body = spec(e)
    body["slots"][0].update(starts_at="09:00", target_minutes=60)
    register(e, body)
    meeting(e, hours=2)
    e.clock.heartbeat(e.wid, elapsed_seconds=55 * 60)
    with e.db.read() as c:
        assert not c.execute(
            "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'", (e.npc,)
        ).fetchone()
        assert LifeActivityService.running(c, e.npc) is None


def test_periodic_crafting_uses_real_materials_and_collects_once(town):
    e = town
    proposed = post(
        e,
        "activity-recipes",
        {
            "idempotency_key": str(uuid4()),
            "recipe": {
                "name": "整理补给",
                "kind": "craft",
                "location_id": e.loc["id"],
                "duration_minutes": 30,
                "energy_cost": 5,
                "ingredients": [{"item_type_id": e.food, "quantity": 1}],
                "output_item_type_id": e.food,
                "output_quantity": 1,
            },
        },
        201,
    )
    recipe = post(e, f"registrations/{proposed['registration']['id']}/confirm")["result_entity_id"]
    body = spec(e)
    body["slots"][0].update(kind="craft", recipe_id=recipe, duration_minutes=35, target_minutes=30)
    register(e, body)
    e.clock.heartbeat(e.wid, elapsed_seconds=300)
    with e.db.read() as c:
        assert (
            c.execute(
                "SELECT sum(quantity) FROM item_instances WHERE container_type='activity_escrow' AND owner_character_id=?",
                (e.npc,),
            ).fetchone()[0]
            == 1
        )
    e.clock.heartbeat(e.wid, elapsed_seconds=1800)
    e.db.initialize()
    e.clock.heartbeat(e.wid, elapsed_seconds=0)
    with e.db.read() as c:
        assert (
            c.execute(
                "SELECT status FROM npc_routine_occurrences WHERE character_id=?", (e.npc,)
            ).fetchone()[0]
            == "completed"
        )
        assert (
            c.execute(
                "SELECT sum(quantity) FROM item_instances WHERE container_type='character_inventory' AND container_id=? AND item_type_id=?",
                (e.npc, e.food),
            ).fetchone()[0]
            == 30
        )
