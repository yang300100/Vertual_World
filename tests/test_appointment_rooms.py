# ruff: noqa: F811
"""哥哥，室内约会检验真实进门、接待授权、门锁、租约和私密经历。"""

import hashlib
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from test_appointments import choose, get, people, set_time
from test_lived_world import town  # noqa: F401

from world_engine.actions import ActionService
from world_engine.appointments import AppointmentIntent, AppointmentService
from world_engine.collective_clock import next_boundary
from world_engine.contracts import ContractService
from world_engine.daily_life import DailyLifeService
from world_engine.domain import ActionProposal, ActionType
from world_engine.interiors import InteriorService
from world_engine.repository import to_iso
from world_engine.society import SocietyService


def room(e, *, owner=None, policy="private", parent=None, name="河边会客室", rate=0):
    response = e.client.post(
        f"/api/worlds/{e.wid}/interior-layouts",
        json={
            "idempotency_key": str(uuid4()),
            "room": {
                "name": name,
                "location_id": e.loc["id"],
                "owner_character_id": owner or e.npc,
                "access_policy": policy,
                "parent_room_id": parent,
                "nightly_rate": rate,
                "fixtures": [{"name": "私人木柜", "kind": "container"}],
            },
        },
    )
    assert response.status_code == 201, response.text
    applied = e.client.post(
        f"/api/worlds/{e.wid}/registrations/{response.json()['registration']['id']}/confirm"
    )
    assert applied.status_code == 200, applied.text
    return applied.json()["result_entity_id"]


def invitation(e, rid, *, host=True, at=None):
    with e.db.write() as c:
        player, npc = people(e, c)
        result = AppointmentService.apply(
            c,
            npc,
            player,
            at or e.at,
            AppointmentIntent(
                operation="propose",
                kind="talk",
                location_id=e.loc["id"],
                room_id=rid,
                allow_visit=host,
                meeting_world_time=to_iso((at or e.at) + timedelta(hours=1)),
                statement="我想约你一小时后在这间房间聊天十五分钟。"
                + ("我同意接待你本次来访。" if host else "你愿意接待我吗？"),
            ),
        )
        return result["appointment_id"]


def door(e, rid, op):
    return e.client.post(f"/api/worlds/{e.wid}/player/life/doors/{rid}", json={"operation": op})


def daily(e, at):
    set_time(e, at)
    with e.db.write() as c:
        DailyLifeService.tick(c, e.wid, at, adjudication_due=False)


def test_host_invitation_requires_confirmation_and_real_entry_before_shared_time(town):
    e = town
    rid = room(e)
    aid = invitation(e, rid)
    assert choose(e, "accept", aid).status_code == 200
    options = e.client.get(f"/api/worlds/{e.wid}/appointments/options/{e.npc}").json()
    assert rid in {room["id"] for room in options["room_options"]}
    with e.db.read() as c:
        target = InteriorService.room(c, e.wid, rid)
        assert not InteriorService.allowed(c, target, e.player)
    start = e.at + timedelta(hours=1)
    daily(e, start - timedelta(minutes=15))
    with e.db.read() as c:
        player, npc = people(e, c)
        assert npc["current_room_id"] == rid and player["current_room_id"] is None
        assert c.execute(
            "SELECT 1 FROM world_events WHERE actor_id=? AND event_type='action.interior_enter'",
            (e.npc,),
        ).fetchone()
    set_time(e, start)
    assert choose(e, "attend", aid).status_code == 409
    assert door(e, rid, "enter").status_code == 200
    assert choose(e, "attend", aid).status_code == 200
    e.clock.heartbeat(e.wid, elapsed_seconds=15 * 60)
    assert get(e, aid)["status"] == "completed"


def test_guest_proposal_does_not_implicitly_open_player_room(town):
    e = town
    rid = room(e, owner=e.player)
    aid = invitation(e, rid, host=False)
    assert choose(e, "accept", aid).status_code == 409
    assert get(e, aid)["status"] == "pending" and get(e, aid)["visit_host_id"] is None
    accepted = choose(e, "accept", aid, allow_visit=True)
    assert accepted.status_code == 200, accepted.text
    assert get(e, aid)["visit_host_id"] == e.player


def test_guest_cannot_sign_host_permission_or_mismatch_location(town):
    e = town
    rid = room(e)
    response = choose(
        e,
        "propose",
        kind="talk",
        location_id=e.loc["id"],
        room_id=rid,
        allow_visit=True,
        meeting_world_time=to_iso(e.at + timedelta(hours=1)),
    )
    assert response.status_code == 409
    with e.db.read() as c:
        other = c.execute(
            "SELECT id FROM locations WHERE world_id=? AND id<>? LIMIT 1", (e.wid, e.loc["id"])
        ).fetchone()[0]
    response = choose(
        e,
        "propose",
        kind="talk",
        location_id=other,
        room_id=rid,
        meeting_world_time=to_iso(e.at + timedelta(hours=1)),
    )
    assert response.status_code == 409
    assert e.client.get(f"/api/worlds/{e.wid}/appointments").json() == []


def test_visit_permission_does_not_grant_storage_keys_or_indefinite_residence(town):
    e = town
    rid = room(e)
    aid = invitation(e, rid)
    choose(e, "accept", aid)
    start = e.at + timedelta(hours=1)
    daily(e, start - timedelta(minutes=15))
    set_time(e, start)
    with e.db.read() as c:
        target = InteriorService.room(c, e.wid, rid)
        assert InteriorService.allowed(c, target, e.player)
        assert not InteriorService.storage_allowed(c, target, e.player)
        assert not InteriorService.keyed(c, target, e.player)
        assert not InteriorService.can_unlock(c, target, e.player)
    assert door(e, rid, "enter").status_code == 200
    with e.db.read() as c:
        fid = c.execute("SELECT id FROM life_fixtures WHERE room_id=?", (rid,)).fetchone()[0]
    response = e.client.post(
        f"/api/worlds/{e.wid}/player/life/fixtures/{fid}", json={"operation": "open"}
    )
    assert response.status_code == 409, response.text
    assert choose(e, "cancel", aid).status_code == 200
    with e.db.read() as c:
        assert not InteriorService.allowed(c, InteriorService.room(c, e.wid, rid), e.player)
    # 接待许可撤回后仍能按真实门状态自行离开，不会被困住或被强制传送。
    assert door(e, rid, "exit").status_code == 200


def test_npc_guest_waits_for_host_and_locked_door_without_teleporting(town):
    e = town
    rid = room(e, owner=e.player)
    aid = invitation(e, rid, host=False)
    choose(e, "accept", aid, allow_visit=True)
    start = e.at + timedelta(hours=1)
    daily(e, start)
    with e.db.read() as c:
        assert people(e, c)[1]["current_room_id"] is None
    assert door(e, rid, "open").status_code == 200
    assert door(e, rid, "enter").status_code == 200
    assert door(e, rid, "close").status_code == 200
    assert door(e, rid, "lock").status_code == 200
    daily(e, start + timedelta(minutes=5))
    with e.db.read() as c:
        assert people(e, c)[1]["current_room_id"] is None
        assert InteriorService.room(c, e.wid, rid)["door_locked"] == 1
    assert door(e, rid, "unlock").status_code == 200
    assert door(e, rid, "open").status_code == 200
    daily(e, start + timedelta(minutes=10))
    with e.db.read() as c:
        assert people(e, c)[1]["current_room_id"] == rid


def test_nested_room_route_moves_one_door_per_scheduled_step(town):
    e = town
    outer = room(e, name="前厅")
    inner = room(e, parent=outer, name="内侧会客室")
    aid = invitation(e, inner)
    choose(e, "accept", aid)
    first = e.at + timedelta(minutes=40)
    daily(e, first)
    with e.db.read() as c:
        assert people(e, c)[1]["current_room_id"] == outer
        assert next_boundary(c, e.wid, first) == first + timedelta(minutes=5)
    daily(e, first + timedelta(minutes=5))
    with e.db.read() as c:
        assert people(e, c)[1]["current_room_id"] == inner
    set_time(e, e.at + timedelta(hours=1))
    assert door(e, outer, "enter").status_code == 200
    assert choose(e, "attend", aid).status_code == 409
    assert door(e, inner, "enter").status_code == 200
    assert choose(e, "attend", aid).status_code == 200


def test_parent_room_of_third_party_is_not_opened_by_child_host(town):
    e = town
    outer = room(e, owner=e.player, name="别人的私人前厅")
    inner = room(e, parent=outer)
    with pytest.raises(ValueError, match="通路|进入许可"):
        invitation(e, inner)
    with e.db.read() as c:
        assert not c.execute(
            "SELECT 1 FROM social_appointments WHERE world_id=?", (e.wid,)
        ).fetchone()


def test_public_room_requires_no_host_consent_and_own_room_needs_no_exit_time(town):
    e = town
    rid = room(e, policy="public")
    aid = invitation(e, rid, host=False)
    assert choose(e, "accept", aid).status_code == 200
    with e.db.write() as c:
        c.execute("UPDATE characters SET current_room_id=? WHERE id=?", (rid, e.npc))
        from world_engine.schedules import ScheduleService

        target = c.execute("SELECT * FROM locations WHERE id=?", (e.loc["id"],)).fetchone()
        assert (
            ScheduleService.earliest_arrival(c, people(e, c)[1], target, e.at, room_id=rid) == e.at
        )


def test_private_room_speech_is_not_heard_outside_or_automatically_spread(town):
    e = town
    rid = room(e, owner=e.player)
    with e.db.write() as c:
        bystander = c.execute(
            "SELECT id FROM characters WHERE world_id=? AND id NOT IN (?,?) LIMIT 1",
            (e.wid, e.player, e.npc),
        ).fetchone()[0]
        c.execute(
            "UPDATE characters SET current_room_id=?,longitude=?,latitude=? WHERE id IN (?,?)",
            (rid, e.loc["longitude"], e.loc["latitude"], e.player, e.npc),
        )
        c.execute(
            "UPDATE characters SET current_room_id=NULL,longitude=?,latitude=? WHERE id=?",
            (e.loc["longitude"], e.loc["latitude"], bystander),
        )
        outcome = ActionService().execute(
            c,
            world_id=e.wid,
            tick_id=str(uuid4()),
            occurred_at=e.at,
            proposal=ActionProposal(
                actor_id=e.player,
                target_id=e.npc,
                action=ActionType.SOCIALIZE,
                reason="室内私下聊天",
                dialogue="这份顾虑我只想当面告诉你。",
            ),
        )
        assert outcome.accepted
        payload = json.loads(
            c.execute(
                "SELECT payload_json FROM world_events WHERE id=?", (outcome.event_id,)
            ).fetchone()[0]
        )
        assert payload["information_scope"] == "private" and payload["perception_room_id"] == rid
        assert not c.execute(
            "SELECT 1 FROM character_event_knowledge WHERE event_id=? AND character_id=?",
            (outcome.event_id, bystander),
        ).fetchone()
        c.execute("UPDATE characters SET current_room_id=NULL WHERE id=?", (e.npc,))
        SocietyService.exchange_news(
            c,
            e.wid,
            e.npc,
            bystander,
            e.at + timedelta(minutes=1),
            preferred_event_id=outcome.event_id,
        )
        assert not c.execute(
            "SELECT 1 FROM character_event_knowledge WHERE event_id=? AND character_id=?",
            (outcome.event_id, bystander),
        ).fetchone()


def test_foreign_private_room_is_absent_from_options(town):
    e = town
    with e.db.read() as c:
        third = c.execute(
            "SELECT id FROM characters WHERE world_id=? AND id NOT IN (?,?) LIMIT 1",
            (e.wid, e.player, e.npc),
        ).fetchone()[0]
    secret = room(e, owner=third, name="外人不知的房间")
    result = e.client.get(f"/api/worlds/{e.wid}/appointments/options/{e.npc}").json()
    assert secret not in {r["id"] for r in result["room_options"]}


def test_receipt_created_before_room_fields_stays_idempotent(town):
    from world_engine.api_intimacy import NaturalSocialChoice

    e = town
    text = "我明确提出一小时后在室外聊十五分钟。"
    payload = {
        "request_id": str(uuid4()),
        "recipient_id": e.npc,
        "intent": text,
        "evidence": text,
        "appointment_choice": {
            "operation": "propose",
            "kind": "talk",
            "location_id": e.loc["id"],
            "meeting_world_time": to_iso(e.at + timedelta(hours=1)),
            "statement": text,
        },
    }
    url = f"/api/worlds/{e.wid}/player/social-choices"
    first = e.client.post(url, json=payload)
    assert first.status_code == 200, first.text
    legacy = NaturalSocialChoice.model_validate(payload).model_dump(mode="json")
    legacy["appointment_choice"].pop("room_id")
    legacy["appointment_choice"].pop("allow_visit")
    digest = hashlib.sha256(
        json.dumps(legacy, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    with e.db.write() as c:
        c.execute(
            "UPDATE social_choice_receipts SET payload_hash=? WHERE world_id=?", (digest, e.wid)
        )
    assert e.client.post(url, json=payload).json() == first.json()


def lease(e, rid, renter):
    with e.db.write() as c:
        cid = str(uuid4())
        stamp = to_iso(e.at)
        c.execute(
            "INSERT INTO long_term_operation_requests(id,world_id,requester_id,recipient_id,operation_type,status,created_at,updated_at) VALUES (?,?,?,?,'住房','applied',?,?)",
            (cid, e.wid, renter, e.npc, stamp, stamp),
        )
        request = c.execute(
            "SELECT * FROM long_term_operation_requests WHERE id=?", (cid,)
        ).fetchone()
        ContractService.start(c, request, {"room_id": rid, "payment": 1, "duration_days": 1}, stamp)


def test_owner_cannot_host_over_third_party_active_lease(town):
    e = town
    rid = room(e, rate=1)
    with e.db.read() as c:
        tenant = c.execute(
            "SELECT id FROM characters WHERE world_id=? AND id NOT IN (?,?) LIMIT 1",
            (e.wid, e.player, e.npc),
        ).fetchone()[0]
    lease(e, rid, tenant)
    with pytest.raises(ValueError, match="接待权"):
        invitation(e, rid)


def test_future_room_arrangement_cannot_extend_past_lease_end(town):
    e = town
    rid = room(e, rate=1)
    lease(e, rid, e.player)
    # 租客可以在有效租期内明确接待所有者；租期末不能替之后的接待权作承诺。
    response = choose(
        e,
        "propose",
        kind="talk",
        location_id=e.loc["id"],
        room_id=rid,
        allow_visit=True,
        meeting_world_time=to_iso(e.at + timedelta(hours=23, minutes=45)),
    )
    assert response.status_code == 409, response.text


def test_room_controller_change_interrupts_active_time_and_revokes_guest_entry(town):
    e = town
    rid = room(e)
    aid = invitation(e, rid)
    choose(e, "accept", aid)
    start = e.at + timedelta(hours=1)
    daily(e, start - timedelta(minutes=15))
    set_time(e, start)
    assert door(e, rid, "enter").status_code == 200
    assert choose(e, "attend", aid).status_code == 200
    with e.db.write() as c:
        owner = c.execute(
            "SELECT id FROM characters WHERE world_id=? AND id NOT IN (?,?) LIMIT 1",
            (e.wid, e.player, e.npc),
        ).fetchone()[0]
        c.execute("UPDATE life_rooms SET owner_character_id=? WHERE id=?", (owner, rid))
        AppointmentService.maintain(c, e.wid, start + timedelta(minutes=2))
        assert not InteriorService.allowed(c, InteriorService.room(c, e.wid, rid), e.player)
    assert get(e, aid)["status"] == "interrupted"


def test_public_child_room_in_private_home_keeps_speech_private(town):
    e = town
    outer = room(e)
    inner = room(e, parent=outer, policy="public")
    with e.db.write() as c:
        c.execute(
            "UPDATE characters SET current_room_id=? WHERE id IN (?,?)", (inner, e.player, e.npc)
        )
        outcome = ActionService().execute(
            c,
            world_id=e.wid,
            tick_id=str(uuid4()),
            occurred_at=e.at,
            proposal=ActionProposal(
                actor_id=e.player,
                target_id=e.npc,
                action=ActionType.SOCIALIZE,
                reason="在私人住所内聊天",
                dialogue="这仍是我们私下谈的事。",
            ),
        )
        assert outcome.accepted
        payload = json.loads(
            c.execute(
                "SELECT payload_json FROM world_events WHERE id=?", (outcome.event_id,)
            ).fetchone()[0]
        )
        assert payload["information_scope"] == "private"


def test_indoor_reschedule_requires_host_to_confirm_the_new_visit_window(town):
    e = town
    rid = room(e)
    aid = invitation(e, rid)
    choose(e, "accept", aid)
    revised = choose(
        e,
        "reschedule",
        aid,
        kind="talk",
        location_id=e.loc["id"],
        room_id=rid,
        meeting_world_time=to_iso(e.at + timedelta(hours=3)),
    )
    assert revised.status_code == 200, revised.text
    revised_id = revised.json()["appointment_id"]
    assert get(e, aid)["status"] == "accepted" and get(e, revised_id)["visit_host_id"] is None
    with e.db.write() as c:
        player, npc = people(e, c)
        with pytest.raises(ValueError, match="进入许可"):
            AppointmentService.apply(
                c,
                npc,
                player,
                e.at,
                AppointmentIntent(
                    operation="accept", appointment_id=revised_id, statement="我同意聊天。"
                ),
            )
        AppointmentService.apply(
            c,
            npc,
            player,
            e.at,
            AppointmentIntent(
                operation="accept",
                appointment_id=revised_id,
                allow_visit=True,
                statement="我同意改期，并愿意在新的时间窗内接待你。",
            ),
        )
    assert get(e, aid)["status"] == "superseded" and get(e, revised_id)["visit_host_id"] == e.npc
