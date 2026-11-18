"""哥哥，未来请客的承诺、改期、周期和真实用餐分开核验。"""

# ruff: noqa: F811
import hashlib
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from test_appointment_series import current, series
from test_appointments import choose, get, people, set_time
from test_lived_world import town  # noqa: F401
from test_relationship_growth import give_player_food

from world_engine.api_intimacy import NaturalSocialChoice
from world_engine.appointment_meals import AppointmentMealService
from world_engine.appointments import AppointmentIntent, AppointmentService
from world_engine.repository import from_iso, to_iso


def offer(e, host="npc", **extra):
    data = dict(operation="propose", kind="meal", meal_supply="proposer", location_id=e.loc["id"],
        meeting_world_time=to_iso(e.at + timedelta(hours=1)), statement="一小时后到这里一起吃十五分钟，我提供双方各一份自己的食物，你愿意吗？")
    data.update(extra)
    with e.db.write() as c:
        player, npc = people(e, c)
        return AppointmentService.apply(c, npc if host == "npc" else player, player if host == "npc" else npc, e.at, AppointmentIntent(**data))


def terms(e, aid):
    with e.db.read() as c:
        return AppointmentMealService.terms(c, aid)


def units(c, e, cid):
    return c.execute("SELECT COALESCE(SUM(quantity),0) FROM item_instances WHERE item_type_id=? AND owner_character_id=?", (e.food, cid)).fetchone()[0]


def npc_accept(e, aid, **extra):
    with e.db.write() as c:
        player, npc = people(e, c)
        return AppointmentService.apply(c, npc, player, e.at, AppointmentIntent(operation="accept", appointment_id=aid, statement="我接受这份时间地点与供餐安排。", **extra))


def start(e, aid):
    set_time(e, from_iso(get(e, aid)["starts_at"]))
    return choose(e, "attend", aid)


def test_future_npc_hosting_commits_no_food_before_arrival_then_consumes_and_completes(town):
    e = town
    aid = offer(e)["appointment_id"]
    assert terms(e, aid)["meal_provider_id"] == e.npc
    assert choose(e, "accept", aid).status_code == 200
    with e.db.read() as c:
        assert units(c, e, e.npc) == 30 and units(c, e, e.player) == 0
        assert not c.execute("SELECT 1 FROM shared_meal_offers").fetchone()
        row = AppointmentService.context(c, e.wid, e.player, e.npc, e.at)["arrangements"][0]
        assert row["meal_provider_id"] == e.npc
    assert choose(e, "attend", aid).status_code == 409
    response = start(e, aid)
    assert response.status_code == 200, response.text
    assert choose(e, "attend", aid).status_code == 409
    with e.db.read() as c:
        assert units(c, e, e.npc) == 28
        assert c.execute("SELECT COUNT(*) FROM shared_meal_servings").fetchone()[0] == 2
    e.clock.heartbeat(e.wid, elapsed_seconds=900)
    assert get(e, aid)["status"] == "completed"


def test_player_hosting_keeps_actual_provider_when_internal_invitation_changes_direction(town):
    e = town
    give_player_food(e)
    aid = offer(e, "player")["appointment_id"]
    assert npc_accept(e, aid)["status"] == "accepted"
    assert start(e, aid).status_code == 200
    with e.db.read() as c:
        assert units(c, e, e.player) == 0 and units(c, e, e.npc) == 28
        assert {r[0] for r in c.execute("SELECT provider_id FROM shared_meal_servings")} == {e.player}


@pytest.mark.parametrize("new_supply,expected", [(None, "npc"), ("proposer", "player"), ("each_own", None)])
def test_rescheduling_preserves_or_explicitly_revises_provider_only_after_new_consent(town, new_supply, expected):
    e = town
    give_player_food(e)
    aid = offer(e)["appointment_id"]
    choose(e, "accept", aid)
    response = choose(e, "reschedule", aid, kind="meal", meal_supply=new_supply, location_id=e.loc["id"], meeting_world_time=to_iso(e.at + timedelta(hours=2)))
    assert response.status_code == 200, response.text
    new = response.json()["appointment_id"]
    provider = e.npc if expected == "npc" else e.player if expected == "player" else None
    assert terms(e, new)["meal_provider_id"] == provider
    assert get(e, aid)["status"] == "accepted" and terms(e, aid)["meal_provider_id"] == e.npc
    assert npc_accept(e, new)["status"] == "accepted"
    assert get(e, aid)["status"] == "superseded"
    assert start(e, new).status_code == 200
    with e.db.read() as c:
        providers = [r[0] for r in c.execute("SELECT provider_id FROM shared_meal_servings")]
        assert sorted(providers) == sorted([provider, provider] if provider else [e.npc, e.player])


@pytest.mark.parametrize("problem", ["one_left", "spoiled", "broken", "reserve", "guest_avoid", "full_bag"])
def test_failed_arrival_has_no_ghost_invitation_or_partial_meal(town, problem):
    e = town
    aid = offer(e)["appointment_id"]
    choose(e, "accept", aid)
    set_time(e, e.at + timedelta(hours=1))
    with e.db.write() as c:
        iid = c.execute("SELECT id FROM item_instances WHERE item_type_id=? AND owner_character_id=?", (e.food, e.npc)).fetchone()[0]
        if problem == "one_left":
            c.execute("UPDATE item_instances SET quantity=1 WHERE id=?", (iid,))
        elif problem == "spoiled":
            c.execute("UPDATE item_instances SET spoils_world_time=? WHERE id=?", (to_iso(e.at), iid))
        elif problem == "broken":
            c.execute("UPDATE item_instances SET condition=0 WHERE id=?", (iid,))
        elif problem in {"reserve", "guest_avoid"}:
            c.execute("INSERT INTO collective_food_decisions VALUES (?,?,?,?,?)", (e.npc if problem == "reserve" else e.player, iid, "reserve" if problem == "reserve" else "avoid", "本人饮食安排", to_iso(e.at)))
        else:
            c.execute("UPDATE characters SET inventory_capacity=0 WHERE id=?", (e.player,))
        player, npc = people(e, c)
        before = [tuple(r) for r in c.execute("SELECT * FROM item_instances ORDER BY id")]
        with pytest.raises(ValueError):
            AppointmentService.apply(c, player, npc, e.at + timedelta(hours=1), AppointmentIntent(operation="attend", appointment_id=aid, statement="我到了，现在开始这次安排。"))
        assert not c.execute("SELECT 1 FROM shared_life_moments").fetchone()
        assert not c.execute("SELECT 1 FROM shared_meal_servings").fetchone()
        assert not c.execute("SELECT 1 FROM character_life_activities WHERE status='running'").fetchone()
        assert [tuple(r) for r in c.execute("SELECT * FROM item_instances ORDER BY id")] == before
    assert get(e, aid)["status"] == "accepted"


@pytest.mark.parametrize("resolution", ["decline", "cancel", "missed", "expired"])
def test_unstarted_future_hosting_never_consumes_or_pays(town, resolution):
    e = town
    aid = offer(e)["appointment_id"]
    if resolution in {"cancel", "missed"}:
        choose(e, "accept", aid)
    if resolution in {"decline", "cancel"}:
        assert choose(e, resolution, aid).status_code == 200
    else:
        with e.db.write() as c:
            AppointmentService.maintain(c, e.wid, e.at + timedelta(hours=3))
    assert get(e, aid)["status"] == {"decline": "declined", "cancel": "cancelled", "missed": "missed", "expired": "expired"}[resolution]
    with e.db.read() as c:
        assert units(c, e, e.npc) == 30
        assert not c.execute("SELECT 1 FROM shared_meal_servings").fetchone()
        assert c.execute("SELECT money FROM characters WHERE id=?", (e.player,)).fetchone()[0] == 200


def test_hosted_cycle_requires_whole_consent_and_rechecks_food_every_occurrence(town):
    e = town
    proposal = offer(e, repeat_count=2, repeat_every_days=1)
    aid, sid = proposal["appointment_id"], proposal["series_id"]
    assert choose(e, "accept", aid, accept_recurrence=True).status_code == 200
    assert series(e, sid)["meal_provider_id"] == e.npc
    for _ in range(2):
        active = current(e, sid)
        assert terms(e, active)["meal_provider_id"] == e.npc
        with e.db.read() as c:
            before = units(c, e, e.npc)
        assert start(e, active).status_code == 200
        with e.db.read() as c:
            assert units(c, e, e.npc) == before - 2
        e.clock.heartbeat(e.wid, elapsed_seconds=900)
        assert get(e, active)["status"] == "completed"
    with e.db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM shared_meal_servings").fetchone()[0] == 4
    assert series(e, sid)["status"] == "completed"


def test_next_occurrence_cannot_reuse_previous_food_receipts(town):
    e = town
    proposal = offer(e, repeat_count=2, repeat_every_days=1)
    aid, sid = proposal["appointment_id"], proposal["series_id"]
    choose(e, "accept", aid, accept_recurrence=True)
    assert start(e, aid).status_code == 200
    e.clock.heartbeat(e.wid, elapsed_seconds=900)
    next_date = current(e, sid)
    with e.db.write() as c:
        c.execute("UPDATE item_instances SET quantity=1 WHERE item_type_id=? AND owner_character_id=?", (e.food, e.npc))
    response = start(e, next_date)
    assert response.status_code == 409
    with e.db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM shared_meal_servings").fetchone()[0] == 2
    assert get(e, next_date)["status"] == "accepted"


def test_accepting_only_one_hosted_date_does_not_accept_future_food_provision(town):
    e = town
    proposal = offer(e, repeat_count=3, repeat_every_days=1)
    aid, sid = proposal["appointment_id"], proposal["series_id"]
    assert choose(e, "accept", aid).status_code == 200
    assert series(e, sid)["status"] == "ended"
    assert start(e, aid).status_code == 200
    e.clock.heartbeat(e.wid, elapsed_seconds=900)
    assert len(series(e, sid)["occurrences"]) == 1
    with e.db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM appointment_meal_offers").fetchone()[0] == 1


def test_pause_and_resume_restores_changed_single_date_provider_then_template_provider(town):
    e = town
    give_player_food(e)
    proposal = offer(e, repeat_count=3, repeat_every_days=1)
    aid, sid = proposal["appointment_id"], proposal["series_id"]
    choose(e, "accept", aid, accept_recurrence=True)
    revised = choose(e, "reschedule", aid, kind="meal", meal_supply="proposer", location_id=e.loc["id"], meeting_world_time=to_iso(e.at + timedelta(hours=2))).json()["appointment_id"]
    npc_accept(e, revised)
    choose(e, "pause_series", series_id=sid)
    choose(e, "resume_series", series_id=sid)
    with e.db.write() as c:
        player, npc = people(e, c)
        AppointmentService.apply(c, npc, player, e.at, AppointmentIntent(operation="resume_series", series_id=sid, statement="我也同意恢复原来的周期安排。"))
    restored = current(e, sid)
    assert restored != revised and terms(e, restored)["meal_provider_id"] == e.player
    assert "具体供餐方以本次记录为准" in get(e, restored)["statement"]
    assert start(e, restored).status_code == 200
    e.clock.heartbeat(e.wid, elapsed_seconds=900)
    next_date = current(e, sid)
    assert terms(e, next_date)["meal_provider_id"] == e.npc


@pytest.mark.parametrize("operation,kind", [("accept", None), ("attend", None), ("propose", "talk"), ("resume_series", None)])
def test_meal_terms_cannot_be_smuggled_into_responses_or_other_kinds(operation, kind):
    fields = {"operation": operation, "kind": kind, "meal_supply": "proposer", "statement": "我愿意。"}
    with pytest.raises(ValueError):
        AppointmentIntent(**fields)


@pytest.mark.parametrize("strip_older_defaults", [False, True])
def test_legacy_appointment_receipts_survive_new_optional_food_field(town, strip_older_defaults):
    e = town
    text = "我邀请你一小时后在这里聊天十五分钟。"
    payload = NaturalSocialChoice(request_id=str(uuid4()), recipient_id=e.npc, intent=text, evidence=text,
        appointment_choice=AppointmentIntent(operation="propose", kind="talk", location_id=e.loc["id"], meeting_world_time=to_iso(e.at + timedelta(hours=1)), statement=text)).model_dump(mode="json")
    payload["appointment_choice"].pop("meal_supply")
    if strip_older_defaults:
        for field in ("room_id", "allow_visit", "series_id", "repeat_count", "repeat_every_days", "accept_recurrence"):
            payload["appointment_choice"].pop(field)
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    saved = {"status": "pending", "appointment_id": "historical", "event_id": "historical", "summary": "原邀请已保存"}
    with e.db.write() as c:
        c.execute("INSERT INTO social_choice_receipts VALUES (?,?,?,?,?)", (e.wid, payload["request_id"], e.player, digest, json.dumps(saved)))
    assert e.client.post(f"/api/worlds/{e.wid}/player/social-choices", json=payload).json() == saved
