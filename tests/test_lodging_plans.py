"""哥哥，订房筹备在隔离世界核验三方同意、个人钱包和真实限期许可。"""

# ruff: noqa: F811, E501
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_appointments import people
from test_journey_room_appointments import current_time, room_at_stop
from test_lived_world import town  # noqa: F401
from test_overnight_stays import stopped_long_journey

from world_engine.lodging_plans import LODGING_PLAN_SCHEMA, LodgingPlanIntent, LodgingPlanService
from world_engine.repository import from_iso, to_iso, utc_now


def setup(e, **changes):
    with e.db.write() as c:
        c.executescript(LODGING_PLAN_SCHEMA)
    jid = stopped_long_journey(e)
    at = current_time(e)
    with e.db.write() as c:
        loc = c.execute(
            "SELECT l.* FROM journey_stops s JOIN locations l ON l.id=s.location_id WHERE s.journey_id=? ORDER BY s.stop_index LIMIT 1",
            (jid,),
        ).fetchone()
        e.owner = c.execute(
            "SELECT id FROM characters WHERE world_id=? AND is_player=0 AND id<>? ORDER BY id LIMIT 1",
            (e.wid, e.npc),
        ).fetchone()[0]
        # 哥哥，第三方房主是隔离场景的独立角色，绝不拿旅伴接受替代房主住房审议。
        c.execute(
            "UPDATE characters SET longitude=?,latitude=?,current_location_id=?,current_room_id=NULL,current_fixture_id=NULL,money=200,health=100 WHERE id=?",
            (loc["longitude"], loc["latitude"], loc["id"], e.owner),
        )
        c.execute(
            "UPDATE character_life_activities SET status='cancelled' WHERE character_id=? AND status='running'",
            (e.owner,),
        )
        c.execute(
            "UPDATE character_movements SET status='cancelled' WHERE character_id=? AND status='moving'",
            (e.owner,),
        )
        for cid in (e.player, e.npc):
            c.execute(
                "INSERT INTO relationships(world_id,source_character_id,target_character_id,affinity,trust,updated_at) VALUES (?,?,?,40,40,?) ON CONFLICT(world_id,source_character_id,target_character_id) DO UPDATE SET affinity=40,trust=40",
                (e.wid, e.owner, cid, to_iso(utc_now())),
            )
    own = room_at_stop(e, jid, owner=e.owner, rate=6, name="本人要租的东客房")
    other = room_at_stop(e, jid, owner=e.owner, rate=8, name="旅伴要租的西客房")
    fields = dict(
        operation="propose",
        journey_id=jid,
        own_room_id=own,
        other_room_id=other,
        own_budget=20,
        other_budget=20,
        required_until_world_time=to_iso(at + timedelta(hours=12)),
        ready_by_world_time=to_iso(at + timedelta(hours=2)),
        statement="我们各自以本人上限租自己的房间，再回原室外站点核对；我仍会单独确认自己的房费。",
    )
    fields.update(changes)
    return jid, own, other, fields


def choose(e, operation=None, pid=None, *, npc=False, no_other=False, **changes):
    fields = {
        "operation": operation,
        "statement": "这是本人对各自限额订房筹备的明确选择。",
        **changes,
    }
    if pid:
        fields["plan_id"] = pid
    at = current_time(e)
    with e.db.write() as c:
        player, companion = people(e, c)
        return LodgingPlanService.apply(
            c,
            companion if npc else player,
            None if no_other else player if npc else companion,
            at,
            LodgingPlanIntent(**fields),
        )


def accept(e, fields):
    result = choose(e, **fields)
    choose(e, "accept", result["plan_id"], npc=True)
    return result["plan_id"]


def raw(e, pid):
    with e.db.read() as c:
        row = dict(c.execute("SELECT * FROM journey_lodging_plans WHERE id=?", (pid,)).fetchone())
        row["roles"] = {
            r["character_id"]: dict(r)
            for r in c.execute("SELECT * FROM journey_lodging_roles WHERE plan_id=?", (pid,))
        }
        return row


def wallets(e):
    with e.db.read() as c:
        return {
            r["id"]: r["money"]
            for r in c.execute(
                "SELECT id,money FROM characters WHERE id IN (?,?,?)", (e.player, e.npc, e.owner)
            )
        }


def set_time(e, at):
    with e.db.write() as c:
        c.execute("UPDATE worlds SET current_time=? WHERE id=?", (to_iso(at), e.wid))


def book(e, pid, *, npc=False):
    quote = choose(e, "quote", pid, npc=npc)
    assert quote["status"] == "offer_ready", quote
    result = choose(
        e,
        "confirm",
        pid,
        npc=npc,
        quote_id=quote["quote_id"],
        expected_total=quote["expected_total"],
    )
    return quote, result


def test_acceptance_does_not_pay_or_create_lease(town):
    e = town
    _, own, other, fields = setup(e)
    before = wallets(e)
    pid = choose(e, **fields)["plan_id"]
    assert raw(e, pid)["status"] == "pending"
    choose(e, "accept", pid, npc=True)
    assert raw(e, pid)["status"] == "accepted"
    assert wallets(e) == before
    with e.db.read() as c:
        assert not c.execute(
            "SELECT 1 FROM contract_fulfillments WHERE asset_id IN (?,?)", (own, other)
        ).fetchone()
        assert not c.execute(
            "SELECT 1 FROM long_term_operation_requests WHERE operation_type='住房'"
        ).fetchone()
        assert (
            c.execute(
                "SELECT status FROM joint_journeys WHERE id=?", (fields["journey_id"],)
            ).fetchone()[0]
            == "paused"
        )


def test_three_roles_review_then_personal_payment_and_precise_receipt(town):
    e = town
    _, own, _, fields = setup(e)
    pid = accept(e, fields)
    before = wallets(e)
    quote = choose(e, "quote", pid)
    assert quote["expected_total"] == 6 and wallets(e) == before
    with e.db.read() as c:
        req = c.execute(
            "SELECT * FROM long_term_operation_requests WHERE id=?", (quote["request_id"],)
        ).fetchone()
        assert req["requester_id"] == e.player and req["recipient_id"] == e.owner
        assert req["status"] == "npc_accepted" and req["npc_response"] is None
        assert not c.execute(
            "SELECT 1 FROM contract_fulfillments WHERE request_id=?", (req["id"],)
        ).fetchone()
    paid = choose(e, "confirm", pid, quote_id=quote["quote_id"], expected_total=6)
    assert paid["payment"] == 6
    after = wallets(e)
    assert after[e.player] == before[e.player] - 6 and after[e.owner] == before[e.owner] + 6
    assert after[e.npc] == before[e.npc]
    assert raw(e, pid)["roles"][e.player]["spent"] == 6
    replay = choose(e, "confirm", pid, quote_id=quote["quote_id"], expected_total=6)
    assert replay["event_id"] == paid["event_id"] and wallets(e) == after
    with e.db.read() as c:
        contract = c.execute(
            "SELECT * FROM contract_fulfillments WHERE request_id=?", (paid["contract_id"],)
        ).fetchone()
        assert contract["requester_id"] == e.player and contract["asset_id"] == own
        assert from_iso(contract["due_world_time"]) >= from_iso(fields["required_until_world_time"])


def test_npc_pays_only_own_wallet_and_reports_no_private_receipt_to_player(town):
    e = town
    _, _, other, fields = setup(e)
    pid = accept(e, fields)
    before = wallets(e)
    _, paid = book(e, pid, npc=True)
    after = wallets(e)
    assert after[e.player] == before[e.player]
    assert after[e.npc] == before[e.npc] - 8 and after[e.owner] == before[e.owner] + 8
    with e.db.read() as c:
        public = LodgingPlanService.rows(c, e.wid, e.player, current_time(e), e.npc)[0]
        partner = next(r for r in public["roles"] if r["character_id"] == e.npc)
        assert partner["status"] == "unreported" and partner["room_id"] == other
        assert set(partner) == {
            "character_id",
            "room_id",
            "room_name",
            "spending_limit",
            "ready_at",
            "report_event_id",
            "status",
        }
        private = LodgingPlanService.context(c, e.wid, e.npc, e.player, current_time(e))[0]
        own = next(r for r in private["roles"] if r["character_id"] == e.npc)
        assert own["spent"] == 8 and own["status"] == "booked"
        assert (
            c.execute(
                "SELECT requester_id FROM contract_fulfillments WHERE request_id=?",
                (paid["contract_id"],),
            ).fetchone()[0]
            == e.npc
        )


def test_two_real_leases_and_individual_reports_do_not_auto_rest_or_resume(town):
    e = town
    jid, _, _, fields = setup(e)
    pid = accept(e, fields)
    book(e, pid)
    book(e, pid, npc=True)
    assert choose(e, "report_ready", pid)["status"] == "ready"
    assert raw(e, pid)["status"] == "accepted"
    assert choose(e, "report_ready", pid, npc=True)["status"] == "completed"
    with e.db.read() as c:
        assert not c.execute("SELECT 1 FROM journey_separate_stays").fetchone()
        assert not c.execute(
            "SELECT 1 FROM character_life_activities WHERE character_id IN (?,?) AND status='running'",
            (e.player, e.npc),
        ).fetchone()
        assert (
            c.execute("SELECT status FROM joint_journeys WHERE id=?", (jid,)).fetchone()[0]
            == "paused"
        )
        assert not LodgingPlanService.blocks_journey(c, jid, current_time(e))


def test_second_room_failure_preserves_first_lease_and_cancellation_keeps_history(town):
    e = town
    _, _, other, fields = setup(e)
    pid = accept(e, fields)
    _, paid = book(e, pid)
    before = wallets(e)
    with e.db.write() as c:
        c.execute("DELETE FROM room_rental_rates WHERE room_id=?", (other,))
    with pytest.raises(ValueError, match="出租"):
        choose(e, "quote", pid, npc=True)
    assert wallets(e) == before
    choose(e, "cancel", pid, npc=True)
    assert wallets(e) == before and raw(e, pid)["status"] == "cancelled"
    with e.db.read() as c:
        assert (
            c.execute(
                "SELECT status FROM contract_fulfillments WHERE request_id=?",
                (paid["contract_id"],),
            ).fetchone()[0]
            == "active"
        )
        assert c.execute(
            "SELECT 1 FROM long_term_operation_requests WHERE id=?", (paid["request_id"],)
        ).fetchone()
        assert c.execute("SELECT 1 FROM world_events WHERE id=?", (paid["event_id"],)).fetchone()


@pytest.mark.parametrize(
    "field,value",
    [("own_budget", True), ("other_budget", "8"), ("own_budget", -1), ("other_budget", 1001)],
)
def test_budget_strict_bounds(town, field, value):
    _, _, _, fields = setup(town)
    with pytest.raises(ValidationError):
        LodgingPlanIntent(**{**fields, field: value})


@pytest.mark.parametrize(
    "change",
    [
        dict(own_budget=1),
        dict(other_room_id="changed"),
        dict(ready_by_world_time="2030-01-01T12:00:00+00:00"),
    ],
)
def test_responding_cannot_replace_accepted_terms(change):
    with pytest.raises(ValidationError):
        LodgingPlanIntent(
            operation="accept", plan_id="real-plan", statement="我只回应原计划。", **change
        )


@pytest.mark.parametrize(
    "change", [dict(quote_id="q"), dict(expected_total=6), dict(quote_id="q", expected_total=True)]
)
def test_confirm_requires_exact_quote_and_strict_total(change):
    with pytest.raises(ValidationError):
        LodgingPlanIntent(
            operation="confirm", plan_id="real-plan", statement="我明确确认本人总价。", **change
        )


def test_wrong_party_cannot_accept_or_use_another_person_quote(town):
    e = town
    _, _, _, fields = setup(e)
    pid = choose(e, **fields)["plan_id"]
    with pytest.raises(ValueError, match="只有收到"):
        choose(e, "accept", pid)
    choose(e, "accept", pid, npc=True)
    quote = choose(e, "quote", pid, npc=True)
    with pytest.raises(ValueError, match="准确报价"):
        choose(
            e, "confirm", pid, quote_id=quote["quote_id"], expected_total=quote["expected_total"]
        )


@pytest.mark.parametrize("change", ["total", "rate", "owner", "money", "expiry", "relation"])
def test_stale_quote_never_partially_pays(town, change):
    e = town
    _, own, _, fields = setup(e)
    pid = accept(e, fields)
    quote = choose(e, "quote", pid)
    with e.db.write() as c:
        if change == "rate":
            c.execute("UPDATE room_rental_rates SET nightly_rate=7 WHERE room_id=?", (own,))
        elif change == "owner":
            c.execute("UPDATE life_rooms SET owner_character_id=? WHERE id=?", (e.npc, own))
        elif change == "money":
            c.execute("UPDATE characters SET money=2 WHERE id=?", (e.player,))
        elif change == "relation":
            c.execute(
                "UPDATE relationships SET affinity=-50,trust=-50 WHERE source_character_id=? AND target_character_id=?",
                (e.owner, e.player),
            )
    if change == "expiry":
        set_time(e, current_time(e) + timedelta(minutes=30))
    before = wallets(e)
    with pytest.raises(ValueError):
        choose(
            e,
            "confirm",
            pid,
            quote_id=quote["quote_id"],
            expected_total=7 if change == "total" else 6,
        )
    assert wallets(e) == before
    with e.db.read() as c:
        assert not c.execute(
            "SELECT 1 FROM contract_fulfillments WHERE request_id=?", (quote["request_id"],)
        ).fetchone()
    assert raw(e, pid)["roles"][e.player]["spent"] == 0


def test_low_relationship_counter_is_not_doubled_again_at_confirmation(town):
    e = town
    _, _, _, fields = setup(e)
    with e.db.write() as c:
        c.execute(
            "DELETE FROM relationships WHERE (source_character_id=? AND target_character_id=?) OR (source_character_id=? AND target_character_id=?)",
            (e.owner, e.player, e.player, e.owner),
        )
    pid = accept(e, fields)
    quote, paid = book(e, pid)
    assert quote["expected_total"] == paid["payment"] == 12
    with e.db.read() as c:
        request = c.execute(
            "SELECT * FROM long_term_operation_requests WHERE id=?", (quote["request_id"],)
        ).fetchone()
        assert json.loads(request["terms_json"])["payment"] == 6
        assert json.loads(request["counter_terms_json"])["payment"] == 12


def test_over_budget_counter_keeps_real_rejection_and_no_payment(town):
    e = town
    _, _, _, fields = setup(e, own_budget=7)
    with e.db.write() as c:
        c.execute(
            "DELETE FROM relationships WHERE (source_character_id=? AND target_character_id=?) OR (source_character_id=? AND target_character_id=?)",
            (e.owner, e.player, e.player, e.owner),
        )
    pid = accept(e, fields)
    before = wallets(e)
    quote = choose(e, "quote", pid)
    assert quote["status"] == "blocked" and quote["expected_total"] == 12
    assert wallets(e) == before
    assert raw(e, pid)["roles"][e.player]["status"] == "blocked"
    with pytest.raises(ValueError, match="报价"):
        choose(e, "confirm", pid, quote_id=quote["quote_id"], expected_total=12)


def test_npc_reserve_blocks_a_nominally_affordable_quote(town):
    e = town
    _, _, _, fields = setup(e)
    pid = accept(e, fields)
    with e.db.write() as c:
        c.execute("UPDATE characters SET money=15 WHERE id=?", (e.npc,))
    quote = choose(e, "quote", pid, npc=True)
    assert quote["status"] == "blocked"
    assert raw(e, pid)["roles"][e.npc]["spent"] == 0


def test_unknown_or_uncontactable_owner_not_tracked_by_true_location(town):
    e = town
    _, _, _, fields = setup(e)
    pid = accept(e, fields)
    with e.db.write() as c:
        c.execute("UPDATE characters SET longitude=longitude+1 WHERE id=?", (e.owner,))
    with pytest.raises(ValueError, match="实际联系房主"):
        choose(e, "quote", pid)
    with e.db.write() as c:
        _, npc = people(e, c)
        before = (npc["longitude"], npc["latitude"])
        assert LodgingPlanService.perform(c, npc, current_time(e)) is None
        after = c.execute(
            "SELECT longitude,latitude FROM characters WHERE id=?", (e.npc,)
        ).fetchone()
        assert tuple(after) == before
        assert not c.execute(
            "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'", (e.npc,)
        ).fetchone()


def test_existing_accepted_letter_contact_can_reach_absent_owner(town):
    e = town
    _, _, _, fields = setup(e)
    pid = accept(e, fields)
    with e.db.write() as c:
        c.execute("UPDATE characters SET longitude=longitude+1 WHERE id=?", (e.owner,))
        stamp = to_iso(utc_now())
        c.execute(
            "INSERT INTO character_contacts(id,world_id,requester_id,recipient_id,status,created_at,updated_at) VALUES (?,?,?,?,'accepted',?,?)",
            (str(uuid4()), e.wid, e.player, e.owner, stamp, stamp),
        )
    assert choose(e, "quote", pid)["status"] == "offer_ready"


def test_landlord_can_be_companion_but_travel_accept_is_not_rental_accept(town):
    e = town
    _, own, _, fields = setup(e)
    with e.db.write() as c:
        c.execute("UPDATE life_rooms SET owner_character_id=? WHERE id=?", (e.npc, own))
    pid = accept(e, fields)
    assert raw(e, pid)["roles"][e.player]["request_id"] is None
    quote = choose(e, "quote", pid)
    with e.db.read() as c:
        request = c.execute(
            "SELECT * FROM long_term_operation_requests WHERE id=?", (quote["request_id"],)
        ).fetchone()
        assert request["recipient_id"] == e.npc and request["status"] in {
            "npc_accepted",
            "npc_countered",
        }


def test_owned_rooms_need_no_money_but_each_person_still_reports(town):
    e = town
    _, own, other, fields = setup(e, own_budget=0, other_budget=0)
    with e.db.write() as c:
        c.execute("UPDATE life_rooms SET owner_character_id=? WHERE id=?", (e.player, own))
        c.execute("UPDATE life_rooms SET owner_character_id=? WHERE id=?", (e.npc, other))
    pid = accept(e, fields)
    before = wallets(e)
    with pytest.raises(ValueError, match="已有覆盖"):
        choose(e, "quote", pid)
    choose(e, "report_ready", pid, no_other=True)
    assert choose(e, "report_ready", pid, npc=True)["status"] == "completed"
    assert wallets(e) == before
    with e.db.read() as c:
        assert not c.execute(
            "SELECT 1 FROM long_term_operation_requests WHERE operation_type='住房'"
        ).fetchone()


def test_complete_existing_lease_is_not_bought_again(town):
    e = town
    _, _, _, fields = setup(e)
    pid = accept(e, fields)
    _, paid = book(e, pid)
    before = wallets(e)
    with pytest.raises(ValueError, match="已有覆盖"):
        choose(e, "quote", pid)
    assert choose(e, "report_ready", pid)["status"] == "ready"
    assert raw(e, pid)["roles"][e.player]["contract_id"] == paid["contract_id"]
    assert wallets(e) == before


@pytest.mark.parametrize("change", ["inside", "far", "busy"])
def test_ready_report_requires_real_idle_outdoor_reunion(town, change):
    e = town
    _, own, _, fields = setup(e)
    pid = accept(e, fields)
    book(e, pid)
    with e.db.write() as c:
        if change == "inside":
            c.execute("UPDATE characters SET current_room_id=? WHERE id=?", (own, e.player))
        elif change == "far":
            c.execute("UPDATE characters SET longitude=longitude+0.001 WHERE id=?", (e.npc,))
        else:
            from world_engine.life import LifeActivityService

            actor = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
            LifeActivityService.start(c, actor, current_time(e), "rest", 5)
    with pytest.raises(ValueError, match="室外站点"):
        choose(e, "report_ready", pid)
    assert raw(e, pid)["roles"][e.player]["ready_at"] is None


def test_reported_readiness_is_withdrawn_when_lease_loses_coverage(town):
    e = town
    _, _, _, fields = setup(e)
    pid = accept(e, fields)
    _, paid = book(e, pid)
    choose(e, "report_ready", pid)
    with e.db.write() as c:
        c.execute(
            "UPDATE contract_fulfillments SET due_world_time=? WHERE request_id=?",
            (to_iso(current_time(e) + timedelta(hours=1)), paid["contract_id"]),
        )
        LodgingPlanService.maintain(c, e.wid, current_time(e))
    assert raw(e, pid)["roles"][e.player]["ready_at"] is None
    assert raw(e, pid)["roles"][e.player]["status"] == "blocked"


@pytest.mark.parametrize("change", ["deadline", "trip", "nested", "different_stop"])
def test_invalid_plan_does_not_pause_or_write_plan(town, change):
    e = town
    jid, own, other, fields = setup(e)
    if change == "deadline":
        fields["required_until_world_time"] = to_iso(current_time(e) + timedelta(days=3))
    elif change == "trip":
        with e.db.write() as c:
            c.execute("UPDATE joint_journeys SET phase='ready' WHERE id=?", (jid,))
    elif change == "nested":
        with e.db.write() as c:
            c.execute("UPDATE life_rooms SET parent_room_id=? WHERE id=?", (own, other))
    else:
        with e.db.write() as c:
            c.execute("UPDATE life_rooms SET location_id=? WHERE id=?", (e.loc["id"], other))
    with e.db.read() as c:
        before = dict(c.execute("SELECT * FROM joint_journeys WHERE id=?", (jid,)).fetchone())
    with pytest.raises(ValueError):
        choose(e, **fields)
    with e.db.read() as c:
        assert (
            dict(c.execute("SELECT * FROM joint_journeys WHERE id=?", (jid,)).fetchone()) == before
        )
        assert not c.execute("SELECT 1 FROM journey_lodging_plans").fetchone()


def test_private_outer_room_cannot_be_included_in_rent_without_its_permission(town):
    e = town
    jid, own, _, fields = setup(e)
    outer = room_at_stop(e, jid, owner=e.owner, name="不开放的外层私厅")
    pid = accept(e, fields)
    with e.db.write() as c:
        c.execute("UPDATE life_rooms SET parent_room_id=? WHERE id=?", (outer, own))
        c.execute(
            "INSERT INTO life_room_access(room_id,character_id) VALUES (?,?)", (outer, e.player)
        )
    before = wallets(e)
    with pytest.raises(ValueError, match="外层"):
        choose(e, "quote", pid)
    assert wallets(e) == before


def test_locked_keyed_room_requires_real_key_before_payment(town):
    e = town
    _, own, _, fields = setup(e)
    pid = accept(e, fields)
    with e.db.write() as c:
        c.execute("UPDATE life_door_policies SET key_item_type_id=? WHERE room_id=?", (e.food, own))
        c.execute("UPDATE life_rooms SET door_locked=1 WHERE id=?", (own,))
    with pytest.raises(ValueError, match="实体钥匙"):
        choose(e, "quote", pid)


def test_future_full_duration_is_rounded_from_actual_confirmation(town):
    e = town
    _, _, _, fields = setup(e)
    at = current_time(e)
    fields["required_until_world_time"] = to_iso(at + timedelta(hours=25))
    pid = accept(e, fields)
    quote, paid = book(e, pid)
    assert quote["expected_total"] == 12
    with e.db.read() as c:
        assert (
            c.execute(
                "SELECT duration_days FROM journey_lodging_quotes WHERE id=?", (quote["quote_id"],)
            ).fetchone()[0]
            == 2
        )
        contract = c.execute(
            "SELECT * FROM contract_fulfillments WHERE request_id=?", (paid["contract_id"],)
        ).fetchone()
        assert from_iso(contract["due_world_time"]) == at + timedelta(days=2)


def test_npc_autonomous_steps_are_separate_and_never_operate_player(town):
    e = town
    _, _, _, fields = setup(e)
    pid = accept(e, fields)
    before = wallets(e)
    at = current_time(e)
    with e.db.write() as c:
        _, npc = people(e, c)
        assert LodgingPlanService.perform(c, npc, at)
    assert raw(e, pid)["roles"][e.npc]["status"] == "offer_ready" and wallets(e) == before
    set_time(e, at + timedelta(minutes=1))
    with e.db.write() as c:
        _, npc = people(e, c)
        assert LodgingPlanService.perform(c, npc, at + timedelta(minutes=1))
    assert raw(e, pid)["roles"][e.npc]["status"] == "booked"
    assert wallets(e)[e.player] == before[e.player]
    with e.db.write() as c:
        _, npc = people(e, c)
        assert LodgingPlanService.perform(c, npc, at + timedelta(minutes=1))
    assert raw(e, pid)["roles"][e.npc]["status"] == "ready"
    assert raw(e, pid)["roles"][e.player]["status"] == "awaiting"


def test_expiry_and_original_trip_exit_keep_paid_contracts(town):
    e = town
    jid, _, _, fields = setup(e)
    pid = accept(e, fields)
    _, paid = book(e, pid)
    before = wallets(e)
    set_time(e, from_iso(fields["ready_by_world_time"]) + timedelta(microseconds=1))
    with e.db.write() as c:
        LodgingPlanService.maintain(c, e.wid, current_time(e))
        assert not LodgingPlanService.blocks_journey(c, jid, current_time(e))
        assert (
            c.execute(
                "SELECT status FROM contract_fulfillments WHERE request_id=?",
                (paid["contract_id"],),
            ).fetchone()[0]
            == "active"
        )
    assert wallets(e) == before and raw(e, pid)["status"] == "expired"


def test_clock_boundary_stops_before_ready_deadline_and_quote_expiry(town):
    e = town
    jid, _, _, fields = setup(e)
    pid = accept(e, fields)
    quote = choose(e, "quote", pid)
    at = current_time(e)
    with e.db.read() as c:
        assert LodgingPlanService.has_live(c, e.wid)
        assert LodgingPlanService.blocks_journey(c, jid, at)
        assert LodgingPlanService.boundary(c, e.wid, at, at + timedelta(days=1)) <= at + timedelta(
            minutes=5
        )
        assert LodgingPlanService.planned_minutes(c, people(e, c)[1], at) <= 120
        assert (
            c.execute(
                "SELECT status FROM journey_lodging_quotes WHERE id=?", (quote["quote_id"],)
            ).fetchone()[0]
            == "open"
        )


def test_post_payment_validation_failure_rolls_back_money_contract_and_receipt(town, monkeypatch):
    e = town
    _, _, _, fields = setup(e)
    pid = accept(e, fields)
    quote = choose(e, "quote", pid)
    before = wallets(e)
    original = LodgingPlanService._access

    def changed_permission(c, row, actor, at):
        if c.execute(
            "SELECT 1 FROM contract_fulfillments WHERE request_id=?", (quote["request_id"],)
        ).fetchone():
            raise ValueError("隔离并发验证：付款事务内房间许可改变")
        return original(c, row, actor, at)

    monkeypatch.setattr(LodgingPlanService, "_access", staticmethod(changed_permission))
    with pytest.raises(ValueError, match="付款事务内"):
        choose(e, "confirm", pid, quote_id=quote["quote_id"], expected_total=6)
    assert wallets(e) == before
    with e.db.read() as c:
        assert not c.execute(
            "SELECT 1 FROM contract_fulfillments WHERE request_id=?", (quote["request_id"],)
        ).fetchone()
        assert (
            c.execute(
                "SELECT status FROM long_term_operation_requests WHERE id=?", (quote["request_id"],)
            ).fetchone()[0]
            == "npc_accepted"
        )
        assert (
            c.execute(
                "SELECT status FROM journey_lodging_quotes WHERE id=?", (quote["quote_id"],)
            ).fetchone()[0]
            == "open"
        )
    assert raw(e, pid)["roles"][e.player]["spent"] == 0


def test_competing_renter_after_quote_prevents_second_payment(town):
    from world_engine.contracts import ContractService

    e = town
    _, own, _, fields = setup(e)
    pid = accept(e, fields)
    quote = choose(e, "quote", pid)
    at = current_time(e)
    with e.db.write() as c:
        competing = str(uuid4())
        stamp = to_iso(utc_now())
        c.execute(
            "INSERT INTO long_term_operation_requests(id,world_id,requester_id,recipient_id,operation_type,status,created_at,updated_at) VALUES (?,?,?,?,'住房','applied',?,?)",
            (competing, e.wid, e.npc, e.owner, stamp, stamp),
        )
        request = c.execute(
            "SELECT * FROM long_term_operation_requests WHERE id=?", (competing,)
        ).fetchone()
        ContractService.start(
            c, request, {"room_id": own, "payment": 6, "duration_days": 1}, to_iso(at)
        )
    before = wallets(e)
    with pytest.raises(ValueError, match="租约"):
        choose(e, "confirm", pid, quote_id=quote["quote_id"], expected_total=6)
    assert wallets(e) == before
    with e.db.read() as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM contract_fulfillments WHERE asset_id=? AND status='active'",
                (own,),
            ).fetchone()[0]
            == 1
        )


def test_owner_rejection_is_audited_and_no_lease_or_hidden_player_notification(town):
    e = town
    _, _, _, fields = setup(e)
    pid = accept(e, fields)
    with e.db.write() as c:
        c.execute(
            "UPDATE relationships SET affinity=-50,trust=-50 WHERE source_character_id=? AND target_character_id=?",
            (e.owner, e.npc),
        )
    before = wallets(e)
    result = choose(e, "quote", pid, npc=True)
    assert result["status"] == "blocked" and wallets(e) == before
    with e.db.read() as c:
        request = c.execute(
            "SELECT * FROM long_term_operation_requests WHERE id=?", (result["request_id"],)
        ).fetchone()
        assert request["status"] == "npc_rejected"
        assert not c.execute(
            "SELECT 1 FROM event_observers WHERE event_id=? AND character_id=?",
            (result["event_id"], e.player),
        ).fetchone()
        assert not c.execute(
            "SELECT 1 FROM player_notifications WHERE event_id=?", (result["event_id"],)
        ).fetchone()
        assert not c.execute(
            "SELECT 1 FROM contract_fulfillments WHERE request_id=?", (result["request_id"],)
        ).fetchone()
        partner = next(
            r
            for r in LodgingPlanService.rows(c, e.wid, e.player, current_time(e))[0]["roles"]
            if r["character_id"] == e.npc
        )
        assert partner["status"] == "unreported" and "private_reason" not in partner


def test_paid_receipt_replay_after_cancel_does_not_reopen_preparation(town):
    e = town
    _, _, _, fields = setup(e)
    pid = accept(e, fields)
    quote, paid = book(e, pid)
    choose(e, "cancel", pid)
    before = wallets(e)
    replay = choose(e, "confirm", pid, quote_id=quote["quote_id"], expected_total=6)
    assert replay["event_id"] == paid["event_id"] and wallets(e) == before
    assert raw(e, pid)["status"] == "cancelled"


def test_exact_lease_end_is_sufficient_for_preparation_and_report_actor_is_actual_person(town):
    e = town
    _, _, _, fields = setup(e)
    pid = accept(e, fields)
    _, paid = book(e, pid, npc=True)
    with e.db.write() as c:
        c.execute(
            "UPDATE contract_fulfillments SET due_world_time=? WHERE request_id=?",
            (fields["required_until_world_time"], paid["contract_id"]),
        )
    report = choose(e, "report_ready", pid, npc=True)
    assert report["status"] == "ready"
    with e.db.read() as c:
        event = c.execute("SELECT * FROM world_events WHERE id=?", (report["event_id"],)).fetchone()
        assert event["actor_id"] == e.npc and event["target_id"] == e.player


def test_pending_invitation_cannot_quote_and_original_trip_end_cancels_preparation(town):
    e = town
    jid, _, _, fields = setup(e)
    pid = choose(e, **fields)["plan_id"]
    with pytest.raises(ValueError, match="已确认"):
        choose(e, "quote", pid)
    with e.db.write() as c:
        c.execute("UPDATE joint_journeys SET status='ended' WHERE id=?", (jid,))
        LodgingPlanService.maintain(c, e.wid, current_time(e))
    assert raw(e, pid)["status"] == "cancelled"


def test_boundary_handles_already_elapsed_pending_deadline_without_restarting_clock(town):
    e = town
    _, _, _, fields = setup(e)
    choose(e, **fields)
    at = from_iso(fields["ready_by_world_time"]) + timedelta(minutes=1)
    with e.db.read() as c:
        limit = at + timedelta(hours=1)
        assert LodgingPlanService.boundary(c, e.wid, at, limit) == limit
