"""哥哥，旅途室内短休分别核验旅行、相处同意、访问许可与真实进门。"""

# ruff: noqa: F811
import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_appointment_rooms import daily, door, lease, room
from test_appointments import choose as appointment_choice
from test_appointments import get as appointment
from test_appointments import people, set_time
from test_journey_breaks import stopped
from test_journeys import choose as journey_choice
from test_journeys import get as journey
from test_journeys import npc_choice, offer
from test_lived_world import town  # noqa: F401

from world_engine.appointments import AppointmentIntent, AppointmentService
from world_engine.interiors import InteriorService
from world_engine.repository import from_iso, to_iso


def current_time(e):
    with e.db.read() as c:
        return from_iso(
            c.execute("SELECT w.current_time FROM worlds w WHERE w.id=?", (e.wid,)).fetchone()[0]
        )


def room_at_stop(e, jid, **changes):
    with e.db.read() as c:
        place = dict(
            c.execute(
                "SELECT l.* FROM journey_stops s JOIN locations l ON l.id=s.location_id "
                "WHERE s.journey_id=? ORDER BY s.stop_index LIMIT 1",
                (jid,),
            ).fetchone()
        )
    # 哥哥，只把登记房间的测试地点设在已到站处，不挪动人物来冒充抵达。
    registration = SimpleNamespace(**{**vars(e), "loc": place})
    return room(registration, **changes)


def invitation_fields(e, jid, rid, **changes):
    with e.db.read() as c:
        lid = c.execute("SELECT location_id FROM life_rooms WHERE id=?", (rid,)).fetchone()[0]
    return {
        "operation": "propose",
        "kind": "rest",
        "journey_id": jid,
        "room_id": rid,
        "location_id": lid,
        "meeting_world_time": to_iso(current_time(e) + timedelta(minutes=30)),
        "minutes": 5,
        "window_minutes": 60,
        "statement": "我选择先暂停原旅行，另行邀请在这间房内休息五分钟，访问许可单独决定。",
        **changes,
    }


def npc_invitation(e, jid, rid, **changes):
    fields = invitation_fields(e, jid, rid, **changes)
    with e.db.write() as c:
        player, npc = people(e, c)
        return AppointmentService.apply(
            c, npc, player, current_time(e), AppointmentIntent(**fields)
        )["appointment_id"]


def player_invitation(e, jid, rid, **changes):
    fields = invitation_fields(e, jid, rid, **changes)
    text = fields["statement"]
    body = {
        "request_id": str(uuid4()),
        "recipient_id": e.npc,
        "intent": text,
        "evidence": text,
        "appointment_choice": fields,
    }
    return e.client.post(f"/api/worlds/{e.wid}/player/social-choices", json=body), body


def npc_accept(e, aid, **changes):
    with e.db.write() as c:
        player, npc = people(e, c)
        return AppointmentService.apply(
            c,
            npc,
            player,
            current_time(e),
            AppointmentIntent(
                operation="accept",
                appointment_id=aid,
                statement="我本人同意这次具体室内短休。",
                **changes,
            ),
        )


def start_npc_hosted_rest(e, jid, rid):
    aid = npc_invitation(e, jid, rid, allow_visit=True)
    accepted = appointment_choice(e, "accept", aid)
    assert accepted.status_code == 200, accepted.text
    start = from_iso(appointment(e, aid)["starts_at"])
    daily(e, start)
    with e.db.read() as c:
        assert people(e, c)[1]["current_room_id"] == rid
        assert people(e, c)[0]["current_room_id"] is None
    entered = door(e, rid, "enter")
    assert entered.status_code == 200, entered.text
    attended = appointment_choice(e, "attend", aid)
    assert attended.status_code == 200, attended.text
    return aid


def assert_unstarted(e, aid):
    assert appointment(e, aid)["moment_id"] is None
    with e.db.read() as c:
        assert not c.execute(
            "SELECT 1 FROM character_life_activities WHERE status='running' "
            "AND character_id IN (?,?)",
            (e.player, e.npc),
        ).fetchone()


def test_rest_acceptance_does_not_sign_player_visit_permission_or_enter_anyone(town):
    e = town
    jid = stopped(e)
    rid = room_at_stop(e, jid, owner=e.player)
    aid = npc_invitation(e, jid, rid, allow_visit=False)
    assert journey(e, jid)["status"] == "paused"
    assert appointment_choice(e, "accept", aid).status_code == 409
    assert appointment(e, aid)["status"] == "pending"
    assert appointment(e, aid)["visit_host_id"] is None
    accepted = appointment_choice(e, "accept", aid, allow_visit=True)
    assert accepted.status_code == 200, accepted.text
    assert appointment(e, aid)["visit_host_id"] == e.player
    assert_unstarted(e, aid)
    with e.db.read() as c:
        assert all(p["current_room_id"] is None for p in people(e, c))
        assert not c.execute(
            "SELECT 1 FROM life_room_access WHERE room_id=? AND character_id=?", (rid, e.npc)
        ).fetchone()


def test_npc_guest_waits_for_real_player_entry_and_attend_requires_both_present(town):
    e = town
    jid = stopped(e)
    rid = room_at_stop(e, jid, owner=e.player)
    aid = npc_invitation(e, jid, rid, allow_visit=False)
    assert appointment_choice(e, "accept", aid, allow_visit=True).status_code == 200
    start = from_iso(appointment(e, aid)["starts_at"])
    daily(e, start)
    with e.db.read() as c:
        assert all(p["current_room_id"] is None for p in people(e, c))
    assert appointment_choice(e, "attend", aid).status_code == 409
    assert_unstarted(e, aid)
    assert door(e, rid, "open").status_code == 200
    assert door(e, rid, "enter").status_code == 200
    assert appointment_choice(e, "attend", aid).status_code == 409
    daily(e, start + timedelta(minutes=5))
    with e.db.read() as c:
        assert all(p["current_room_id"] == rid for p in people(e, c))
        assert not InteriorService.storage_allowed(c, InteriorService.room(c, e.wid, rid), e.npc)
    assert appointment_choice(e, "attend", aid).status_code == 200
    assert journey(e, jid)["status"] == "paused"


def test_npc_and_player_use_nested_room_doors_without_automatic_player_entry(town):
    e = town
    jid = stopped(e)
    outer = room_at_stop(e, jid, name="旅行停留前厅")
    inner = room_at_stop(e, jid, parent=outer, name="旅行停留内室")
    aid = npc_invitation(e, jid, inner, allow_visit=True)
    assert appointment_choice(e, "accept", aid).status_code == 200
    start = from_iso(appointment(e, aid)["starts_at"])
    daily(e, start - timedelta(minutes=15))
    with e.db.read() as c:
        player, npc = people(e, c)
        assert player["current_room_id"] is None and npc["current_room_id"] == outer
    daily(e, start - timedelta(minutes=10))
    with e.db.read() as c:
        assert people(e, c)[1]["current_room_id"] == inner
    set_time(e, start)
    assert door(e, inner, "enter").status_code == 409
    assert appointment_choice(e, "attend", aid).status_code == 409
    assert door(e, outer, "enter").status_code == 200
    assert appointment_choice(e, "attend", aid).status_code == 409
    assert door(e, inner, "enter").status_code == 200
    assert appointment_choice(e, "attend", aid).status_code == 200


@pytest.mark.parametrize("phase", ["pending", "accepted", "moving"])
def test_only_actual_arrived_stops_can_link_indoor_rest(town, phase):
    e = town
    jid = offer(e)
    rid = room_at_stop(e, jid)
    if phase != "pending":
        assert journey_choice(e, "accept", jid).status_code == 200
    if phase == "moving":
        assert journey_choice(e, "depart", jid).status_code == 200
    before = journey(e, jid)
    response, _ = player_invitation(e, jid, rid)
    assert response.status_code == 409, response.text
    after = journey(e, jid)
    assert (after["status"], after["source_event_id"]) == (
        before["status"],
        before["source_event_id"],
    )
    with e.db.read() as c:
        assert not c.execute("SELECT 1 FROM journey_room_appointments").fetchone()
        assert not c.execute("SELECT 1 FROM social_appointments").fetchone()
        if phase == "moving":
            assert (
                c.execute(
                    "SELECT COUNT(*) FROM character_movements WHERE status='moving' "
                    "AND character_id IN (?,?)",
                    (e.player, e.npc),
                ).fetchone()[0]
                == 2
            )


def test_other_location_cannot_be_disguised_as_current_stop(town):
    e = town
    jid = stopped(e)
    rid = room(e, owner=e.player)
    before = journey(e, jid)
    response, _ = player_invitation(e, jid, rid, allow_visit=True)
    assert response.status_code == 409, response.text
    assert journey(e, jid)["status"] == "active"
    assert journey(e, jid)["source_event_id"] == before["source_event_id"]


@pytest.mark.parametrize(
    "obstacle", ["third_party_room", "third_party_lease", "third_party_outer_room"]
)
def test_room_permission_failure_rolls_back_journey_pause_and_appointment(town, obstacle):
    e = town
    jid = stopped(e)
    with e.db.read() as c:
        third = c.execute(
            "SELECT id FROM characters WHERE world_id=? AND id NOT IN (?,?) LIMIT 1",
            (e.wid, e.player, e.npc),
        ).fetchone()[0]
    if obstacle == "third_party_room":
        rid = room_at_stop(e, jid, owner=third)
    elif obstacle == "third_party_lease":
        rid = room_at_stop(e, jid, rate=1)
        lease(e, rid, third)
    else:
        outer = room_at_stop(e, jid, owner=third, name="第三人的私人前厅")
        rid = room_at_stop(e, jid, parent=outer)
    before = journey(e, jid)
    with pytest.raises(ValueError):
        npc_invitation(e, jid, rid, allow_visit=True)
    after = journey(e, jid)
    assert (after["status"], after["source_event_id"]) == (
        before["status"],
        before["source_event_id"],
    )
    with e.db.read() as c:
        assert not c.execute("SELECT 1 FROM journey_room_appointments").fetchone()
        assert not c.execute("SELECT 1 FROM social_appointments").fetchone()


@pytest.mark.parametrize("state", ["pending", "accepted", "active"])
def test_indoor_arrangement_blocks_both_sides_resuming_travel_until_handled(town, state):
    e = town
    jid = stopped(e)
    rid = room_at_stop(e, jid)
    if state == "active":
        aid = start_npc_hosted_rest(e, jid, rid)
    else:
        aid = npc_invitation(e, jid, rid, allow_visit=True)
        if state == "accepted":
            assert appointment_choice(e, "accept", aid).status_code == 200
    assert appointment(e, aid)["status"] == state
    assert journey_choice(e, "resume", jid).status_code == 409
    with pytest.raises(ValueError):
        npc_choice(e, jid, "resume")
    assert journey(e, jid)["status"] == "paused"


def test_ending_journey_cancels_only_unconfirmed_indoor_invitation(town):
    e = town
    jid = stopped(e)
    rid = room_at_stop(e, jid)
    aid = npc_invitation(e, jid, rid, allow_visit=True)
    assert journey_choice(e, "end", jid).status_code == 200
    assert appointment(e, aid)["status"] == "cancelled"
    assert appointment_choice(e, "accept", aid).status_code == 409
    assert_unstarted(e, aid)
    with e.db.read() as c:
        assert all(p["current_room_id"] is None for p in people(e, c))


@pytest.mark.parametrize("state", ["accepted", "active"])
def test_confirmed_indoor_rest_keeps_independent_consent_after_journey_ends(town, state):
    e = town
    jid = stopped(e)
    rid = room_at_stop(e, jid)
    if state == "active":
        aid = start_npc_hosted_rest(e, jid, rid)
    else:
        aid = npc_invitation(e, jid, rid, allow_visit=True)
        assert appointment_choice(e, "accept", aid).status_code == 200
    assert journey_choice(e, "end", jid).status_code == 200
    assert appointment(e, aid)["status"] == state
    if state == "accepted":
        daily(e, from_iso(appointment(e, aid)["starts_at"]))
        assert door(e, rid, "enter").status_code == 200
        assert appointment_choice(e, "attend", aid).status_code == 200
    e.clock.heartbeat(e.wid, elapsed_seconds=5 * 60)
    assert appointment(e, aid)["status"] == "completed"
    assert journey(e, jid)["status"] == "ended"


def test_room_rest_never_pays_rent_grants_storage_or_auto_exits_or_resumes(town):
    e = town
    jid = stopped(e)
    rid = room_at_stop(e, jid, rate=8)
    with e.db.read() as c:
        money = {p["id"]: p["money"] for p in people(e, c)}
    aid = start_npc_hosted_rest(e, jid, rid)
    with e.db.read() as c:
        target = InteriorService.room(c, e.wid, rid)
        assert InteriorService.allowed(c, target, e.player)
        assert not InteriorService.keyed(c, target, e.player)
        assert not InteriorService.storage_allowed(c, target, e.player)
        assert {p["id"]: p["money"] for p in people(e, c)} == money
        assert not c.execute("SELECT 1 FROM contract_fulfillments WHERE kind='lodging'").fetchone()
    e.clock.heartbeat(e.wid, elapsed_seconds=5 * 60)
    assert appointment(e, aid)["status"] == "completed"
    assert journey(e, jid)["status"] == "paused"
    with e.db.read() as c:
        assert people(e, c)[0]["current_room_id"] == rid
        assert not InteriorService.allowed(c, InteriorService.room(c, e.wid, rid), e.player)
        assert not c.execute(
            "SELECT 1 FROM character_movements WHERE status='moving' AND character_id=?",
            (e.player,),
        ).fetchone()
    assert door(e, rid, "exit").status_code == 200


def test_link_and_real_arrival_pause_evidence_survive_reinitialize_and_are_exposed(town):
    e = town
    jid = stopped(e)
    rid = room_at_stop(e, jid)
    aid = npc_invitation(e, jid, rid, allow_visit=True)
    e.db.initialize()
    with e.db.read() as c:
        link = c.execute(
            "SELECT * FROM journey_room_appointments WHERE appointment_id=?", (aid,)
        ).fetchone()
        assert link["journey_id"] == jid and link["stop_index"] == 0
        for key, event_type in (
            ("arrival_event_id", "social.appointment_journey_arrived"),
            ("pause_event_id", "social.appointment_journey_paused"),
        ):
            assert (
                c.execute(
                    "SELECT event_type FROM world_events WHERE id=?", (link[key],)
                ).fetchone()[0]
                == event_type
            )
        row = next(
            r
            for r in AppointmentService.rows(c, e.wid, e.player, current_time(e))
            if r["id"] == aid
        )
        assert row["journey_id"] == jid and row["journey_stop_index"] == 0


def test_same_player_request_reuses_pause_invitation_and_source_link(town):
    e = town
    jid = stopped(e)
    rid = room_at_stop(e, jid, owner=e.player)
    response, body = player_invitation(e, jid, rid, allow_visit=True)
    assert response.status_code == 200, response.text
    first = response.json()
    repeated = e.client.post(f"/api/worlds/{e.wid}/player/social-choices", json=body)
    assert repeated.status_code == 200 and repeated.json() == first
    with e.db.read() as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM journey_room_appointments WHERE journey_id=?", (jid,)
            ).fetchone()[0]
            == 1
        )
        assert (
            c.execute(
                "SELECT COUNT(*) FROM social_appointments WHERE id=?", (first["appointment_id"],)
            ).fetchone()[0]
            == 1
        )
    changed = json.loads(json.dumps(body))
    changed["appointment_choice"]["journey_id"] = str(uuid4())
    assert (
        e.client.post(f"/api/worlds/{e.wid}/player/social-choices", json=changed).status_code == 409
    )


def test_legacy_receipt_ignores_only_new_null_journey_reference(town):
    from world_engine.api_intimacy import NaturalSocialChoice

    e = town
    text = "我邀请你一小时后在这里休息五分钟。"
    payload = NaturalSocialChoice(
        request_id=str(uuid4()),
        recipient_id=e.npc,
        intent=text,
        evidence=text,
        appointment_choice=AppointmentIntent(
            operation="propose",
            kind="rest",
            location_id=e.loc["id"],
            meeting_world_time=to_iso(e.at + timedelta(hours=1)),
            minutes=5,
            statement=text,
        ),
    ).model_dump(mode="json")
    payload["appointment_choice"].pop("journey_id")
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    saved = {"status": "pending", "event_id": "historical", "summary": "旧室内安排已经记录"}
    with e.db.write() as c:
        c.execute(
            "INSERT INTO social_choice_receipts VALUES (?,?,?,?,?)",
            (e.wid, payload["request_id"], e.player, digest, json.dumps(saved)),
        )
    assert e.client.post(f"/api/worlds/{e.wid}/player/social-choices", json=payload).json() == saved


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "meal"},
        {"room_id": None},
        {"repeat_count": 2, "repeat_every_days": 1},
        {"operation": "accept", "appointment_id": "old"},
        {"operation": "reschedule", "appointment_id": "old"},
    ],
)
def test_journey_reference_only_belongs_to_new_single_indoor_rest(changes):
    with pytest.raises(ValueError):
        AppointmentIntent(
            **(
                {
                    "operation": "propose",
                    "kind": "rest",
                    "journey_id": "journey",
                    "room_id": "room",
                    "location_id": "location",
                    "meeting_world_time": "2030-01-01T09:00:00+00:00",
                    "statement": "我只提出本次室内短休，其他权限分别商量。",
                }
                | changes
            )
        )


def test_changed_room_controller_is_checked_before_elapsed_rest_is_completed(town):
    e = town
    jid = stopped(e)
    rid = room_at_stop(e, jid, rate=1)
    aid = start_npc_hosted_rest(e, jid, rid)
    e.clock.heartbeat(e.wid, elapsed_seconds=60)
    assert appointment(e, aid)["status"] == "active"
    with e.db.read() as c:
        renter = c.execute(
            "SELECT id FROM characters WHERE world_id=? AND id NOT IN (?,?) LIMIT 1",
            (e.wid, e.player, e.npc),
        ).fetchone()[0]
    # 哥哥，活动期间真实产生第三人的租约；下一次心跳直接到结束，仍须先核验接待权。
    rental = SimpleNamespace(**{**vars(e), "at": current_time(e)})
    lease(rental, rid, renter)
    e.clock.heartbeat(e.wid, elapsed_seconds=4 * 60)
    row = appointment(e, aid)
    assert row["status"] == "interrupted"
    assert journey(e, jid)["status"] == "paused"
    with e.db.read() as c:
        moment = c.execute(
            "SELECT status FROM shared_life_moments WHERE id=?", (row["moment_id"],)
        ).fetchone()
        assert moment["status"] == "interrupted"


def test_changed_room_controller_stops_npc_before_entering_new_tenants_room(town):
    e = town
    jid = stopped(e)
    rid = room_at_stop(e, jid, rate=1)
    aid = npc_invitation(e, jid, rid, allow_visit=True)
    assert appointment_choice(e, "accept", aid).status_code == 200
    with e.db.read() as c:
        renter = c.execute(
            "SELECT id FROM characters WHERE world_id=? AND id NOT IN (?,?) LIMIT 1",
            (e.wid, e.player, e.npc),
        ).fetchone()[0]
        initial = InteriorService.room(c, e.wid, rid)
        before = (initial["door_open"], initial["door_locked"])
    lease(SimpleNamespace(**{**vars(e), "at": current_time(e)}), rid, renter)
    daily(e, from_iso(appointment(e, aid)["starts_at"]))
    with e.db.read() as c:
        assert all(p["current_room_id"] is None for p in people(e, c))
        target = InteriorService.room(c, e.wid, rid)
        assert (target["door_open"], target["door_locked"]) == before
        assert not c.execute(
            "SELECT 1 FROM world_events WHERE world_id=? AND actor_id=? "
            "AND event_type='action.interior_enter' AND json_extract(payload_json,'$.room_id')=?",
            (e.wid, e.npc, rid),
        ).fetchone()
    assert_unstarted(e, aid)


@pytest.mark.parametrize("accessor", ["owner", "old_access"])
def test_current_tenant_overrides_owner_and_previous_room_access(accessor, town):
    e = town
    jid = stopped(e)
    rid = room_at_stop(e, jid, rate=1)
    with e.db.write() as c:
        # 哥哥，这里预置房主此前授予的常规使用权，再核验租约能否真正限制旧权限。
        c.execute("INSERT INTO life_room_access VALUES (?,?)", (rid, e.player))
        renter = c.execute(
            "SELECT id FROM characters WHERE world_id=? AND id NOT IN (?,?) LIMIT 1",
            (e.wid, e.player, e.npc),
        ).fetchone()[0]
        former_user = e.npc if accessor == "owner" else e.player
        target = InteriorService.room(c, e.wid, rid)
        assert InteriorService.allowed(c, target, former_user)
        assert InteriorService.keyed(c, target, former_user)
        assert InteriorService.storage_allowed(c, target, former_user)
    lease(SimpleNamespace(**{**vars(e), "at": current_time(e)}), rid, renter)
    with e.db.read() as c:
        target = InteriorService.room(c, e.wid, rid)
        assert not InteriorService.allowed(c, target, former_user)
        assert not InteriorService.keyed(c, target, former_user)
        assert not InteriorService.storage_allowed(c, target, former_user)
        assert not InteriorService.can_unlock(c, target, former_user)
        assert InteriorService.allowed(c, target, renter)
        assert InteriorService.keyed(c, target, renter)
        assert InteriorService.storage_allowed(c, target, renter)
