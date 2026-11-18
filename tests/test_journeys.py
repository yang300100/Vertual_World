"""哥哥，多站旅行以双方同意、实际路程、停留和可退出性验收。"""

# ruff: noqa: F811
from datetime import timedelta
from uuid import uuid4

import pytest
from test_appointments import people, set_time
from test_lived_world import town  # noqa: F401

from world_engine.actions import ActionService
from world_engine.journeys import JourneyIntent, JourneyService
from world_engine.life import LifeActivityService
from world_engine.movement import MovementService
from world_engine.repository import from_iso, to_iso


def sites(e):
    with e.db.write() as c:
        c.execute("UPDATE characters SET movement_speed_kmh=3 WHERE id=?", (e.npc,))
        places = c.execute(
            "SELECT * FROM locations WHERE world_id=? AND id<>? ORDER BY id LIMIT 2",
            (e.wid, e.loc["id"]),
        ).fetchall()
        for index, place in enumerate(places, 1):
            c.execute(
                "UPDATE locations SET longitude=?,latitude=? WHERE id=?",
                (e.loc["longitude"] + 0.003 * index, e.loc["latitude"], place["id"]),
            )
            # 哥哥，这里明确预置评测双方以前到访过的地点，正式存档不写测试见闻。
            ActionService._record_event(
                c,
                world_id=e.wid,
                tick_id=str(uuid4()),
                occurred_at=e.at - timedelta(days=2),
                event_type="action.socialize",
                actor_id=e.player,
                target_id=e.npc,
                location_id=place["id"],
                summary="隔离评测的双方过去曾到访此处。",
                payload={},
            )
        return [p["id"] for p in places]


def offer(e, stops=None, **extra):
    if stops is None:
        stops = [{"location_id": sites(e)[0], "minutes": 2}]
    fields = dict(
        operation="propose",
        meeting_location_id=e.loc["id"],
        deadline_world_time=to_iso(e.at + timedelta(hours=4)),
        stops=stops,
        return_to_origin=True,
        statement="我们按有限路线徒步，在各站停留，并回到这里，愿意吗？",
    )
    fields.update(extra)
    with e.db.write() as c:
        player, npc = people(e, c)
        return JourneyService.apply(c, npc, player, e.at, JourneyIntent(**fields))["journey_id"]


def choose(e, operation, jid, **extra):
    text = extra.pop("statement", "我明确表达本人对这份旅行的选择。")
    return e.client.post(
        f"/api/worlds/{e.wid}/player/social-choices",
        json={
            "request_id": str(uuid4()),
            "recipient_id": e.npc,
            "intent": text,
            "evidence": text,
            "journey_choice": {
                "operation": operation,
                "journey_id": jid,
                "statement": text,
                **extra,
            },
        },
    )


def get(e, jid):
    with e.db.read() as c:
        return dict(c.execute("SELECT * FROM joint_journeys WHERE id=?", (jid,)).fetchone())


def npc_choice(e, jid, operation, at=None):
    with e.db.write() as c:
        player, npc = people(e, c)
        return JourneyService.apply(
            c,
            npc,
            player,
            at
            or from_iso(
                c.execute("SELECT w.current_time FROM worlds w WHERE id=?", (e.wid,)).fetchone()[0]
            ),
            JourneyIntent(operation=operation, journey_id=jid, statement="这是我本人的决定。"),
        )


def advance_to_arrival(e, jid):
    with e.db.read() as c:
        now = from_iso(
            c.execute("SELECT w.current_time FROM worlds w WHERE id=?", (e.wid,)).fetchone()[0]
        )
        due = max(
            from_iso(r[0])
            for r in c.execute(
                "SELECT estimated_arrival_world FROM character_movements WHERE world_id=? AND status='moving' AND character_id IN (?,?)",
                (e.wid, e.player, e.npc),
            )
        )
    e.clock.heartbeat(e.wid, elapsed_seconds=(due - now).total_seconds() + 0.001)
    return get(e, jid)


def test_both_consent_then_real_distinct_routes_stops_and_return(town):
    e = town
    jid = offer(e)
    assert choose(e, "depart", jid).status_code == 409
    assert choose(e, "accept", jid).status_code == 200
    with e.db.write() as c:
        c.execute("UPDATE characters SET longitude=longitude+0.0001 WHERE id=?", (e.npc,))
        before = [
            tuple(r)
            for r in c.execute("SELECT id,longitude,latitude,money FROM characters ORDER BY id")
        ]
    assert choose(e, "depart", jid).status_code == 200
    assert choose(e, "depart", jid).status_code == 409
    with e.db.read() as c:
        moves = c.execute("SELECT * FROM character_movements WHERE status='moving'").fetchall()
        assert len(moves) == 2 and len({m["estimated_arrival_world"] for m in moves}) == 1
        assert len({m["origin_longitude"] for m in moves}) == 2
        assert all(m["speed_kmh"] <= 3 for m in moves)
        assert [
            tuple(r)
            for r in c.execute("SELECT id,longitude,latitude,money FROM characters ORDER BY id")
        ] == before
    stopped = advance_to_arrival(e, jid)
    assert (
        stopped["status"] == "active"
        and stopped["phase"] == "stopped"
        and stopped["next_index"] == 1
    )
    assert choose(e, "depart", jid).status_code == 409
    e.clock.heartbeat(e.wid, elapsed_seconds=120)
    assert choose(e, "depart", jid).status_code == 200
    assert advance_to_arrival(e, jid)["status"] == "completed"
    with e.db.read() as c:
        for cid in (e.player, e.npc):
            p = c.execute("SELECT * FROM characters WHERE id=?", (cid,)).fetchone()
            assert p["longitude"] == pytest.approx(e.loc["longitude"])
        assert (
            c.execute("SELECT COUNT(*) FROM journey_stops WHERE arrived_at IS NOT NULL").fetchone()[
                0
            ]
            == 2
        )
        assert c.execute("SELECT COUNT(*) FROM shared_meal_servings").fetchone()[0] == 0


def test_failed_second_route_rolls_back_first_person_and_all_departure_records(town, monkeypatch):
    e = town
    jid = offer(e)
    choose(e, "accept", jid)
    original = MovementService._start_solo

    def fail_npc(self, c, **kwargs):
        if kwargs["character_id"] == e.npc:
            raise ValueError("对方的真实路线不可通行")
        return original(self, c, **kwargs)

    monkeypatch.setattr(MovementService, "_start_solo", fail_npc)
    assert choose(e, "depart", jid).status_code == 409
    with e.db.read() as c:
        assert not c.execute("SELECT 1 FROM journey_legs").fetchone()
        assert not c.execute("SELECT 1 FROM character_movements").fetchone()
    assert get(e, jid)["status"] == "accepted"


def test_pause_keeps_real_midway_positions_and_resume_requires_other_person(town):
    e = town
    jid = offer(e)
    choose(e, "accept", jid)
    choose(e, "depart", jid)
    e.clock.heartbeat(e.wid, elapsed_seconds=60)
    with e.db.read() as c:
        before = [
            tuple(r) for r in c.execute("SELECT id,longitude,latitude FROM characters ORDER BY id")
        ]
    assert choose(e, "pause", jid).status_code == 200
    with e.db.read() as c:
        assert [
            tuple(r) for r in c.execute("SELECT id,longitude,latitude FROM characters ORDER BY id")
        ] == before
        assert not c.execute(
            "SELECT 1 FROM character_movements WHERE status='moving' AND character_id IN (?,?)",
            (e.player, e.npc),
        ).fetchone()
    assert choose(e, "resume", jid).json()["status"] == "resume_pending"
    assert choose(e, "resume", jid).status_code == 409
    assert npc_choice(e, jid, "resume")["status"] == "active"
    assert choose(e, "depart", jid).status_code == 200


def test_final_dwell_must_be_real_and_is_not_counted_during_pause(town):
    e = town
    place = sites(e)[0]
    jid = offer(e, stops=[{"location_id": place, "minutes": 10}], return_to_origin=False)
    choose(e, "accept", jid)
    choose(e, "depart", jid)
    assert advance_to_arrival(e, jid)["status"] == "active"
    e.clock.heartbeat(e.wid, elapsed_seconds=180)
    assert choose(e, "pause", jid).status_code == 200
    remaining = get(e, jid)["remaining_stop_seconds"]
    assert 419 < remaining <= 420
    later = from_iso(get(e, jid)["next_depart_at"]) + timedelta(minutes=30)
    set_time(e, later)
    choose(e, "resume", jid)
    npc_choice(e, jid, "resume")
    assert get(e, jid)["status"] == "active"
    e.clock.heartbeat(e.wid, elapsed_seconds=remaining - 1)
    assert get(e, jid)["status"] == "active"
    e.clock.heartbeat(e.wid, elapsed_seconds=2)
    assert get(e, jid)["status"] == "completed"


def test_npc_independent_departure_releases_trip_and_is_not_cancelled_by_ending_old_trip(town):
    e = town
    jid = offer(e)
    choose(e, "accept", jid)
    choose(e, "depart", jid)
    with e.db.write() as c:
        movement = MovementService().start(
            c,
            world_id=e.wid,
            character_id=e.npc,
            world_time=e.at,
            destination_longitude=e.loc["longitude"] - 0.003,
            destination_latitude=e.loc["latitude"],
        )
    assert get(e, jid)["status"] == "paused"
    assert choose(e, "end", jid).status_code == 200
    with e.db.read() as c:
        assert (
            c.execute(
                "SELECT status FROM character_movements WHERE id=?", (movement.id,)
            ).fetchone()[0]
            == "moving"
        )


@pytest.mark.parametrize("problem", ["hungry", "tired", "unavailable", "unknown_place"])
def test_readiness_or_unknown_place_never_teleports_or_starts_partial_route(town, problem):
    e = town
    places = sites(e)
    if problem == "unknown_place":
        with e.db.write() as c:
            c.execute(
                "DELETE FROM event_observers WHERE character_id=? AND event_id IN (SELECT id FROM world_events WHERE location_id=?)",
                (e.npc, places[0]),
            )
        with pytest.raises(ValueError, match="到访"):
            offer(e, stops=[{"location_id": places[0], "minutes": 2}])
        return
    jid = offer(e, stops=[{"location_id": places[0], "minutes": 2}])
    choose(e, "accept", jid)
    with e.db.write() as c:
        if problem == "hungry":
            c.execute("UPDATE characters SET satiety=20 WHERE id=?", (e.npc,))
        elif problem == "tired":
            c.execute("UPDATE characters SET energy=20 WHERE id=?", (e.npc,))
        else:
            LifeActivityService.start(c, people(e, c)[1], e.at, "rest", 60)
    assert choose(e, "depart", jid).status_code == 409
    with e.db.read() as c:
        assert not c.execute("SELECT 1 FROM character_movements").fetchone()


def test_external_cancel_stops_only_both_journey_movements_immediately(town):
    e = town
    jid = offer(e)
    choose(e, "accept", jid)
    choose(e, "depart", jid)
    with e.db.write() as c:
        MovementService().cancel(c, world_id=e.wid, character_id=e.player, world_time=e.at)
        assert not c.execute(
            "SELECT 1 FROM character_movements WHERE status='moving' AND character_id IN (?,?)",
            (e.player, e.npc),
        ).fetchone()
    assert get(e, jid)["status"] == "paused"


def test_invalid_independent_npc_move_does_not_pause_valid_trip(town):
    e = town
    jid = offer(e)
    choose(e, "accept", jid)
    choose(e, "depart", jid)
    with e.db.write() as c:
        with pytest.raises(ValueError):
            MovementService().start(
                c,
                world_id=e.wid,
                character_id=e.npc,
                world_time=e.at,
                destination_longitude=999,
                destination_latitude=e.loc["latitude"],
            )
        assert (
            c.execute(
                "SELECT COUNT(*) FROM character_movements WHERE status='moving' AND character_id IN (?,?)",
                (e.player, e.npc),
            ).fetchone()[0]
            == 2
        )
    assert get(e, jid)["status"] == "active"


def test_inactive_destination_ends_trip_without_breaking_world_heartbeat(town):
    e = town
    place = sites(e)[0]
    jid = offer(e, stops=[{"location_id": place, "minutes": 2}])
    choose(e, "accept", jid)
    choose(e, "depart", jid)
    with e.db.write() as c:
        c.execute("UPDATE locations SET is_active=0 WHERE id=?", (place,))
    e.clock.heartbeat(e.wid, elapsed_seconds=10)
    assert get(e, jid)["status"] == "ended"


def test_body_need_pauses_before_any_automatic_food_spending(town):
    e = town
    jid = offer(e)
    choose(e, "accept", jid)
    choose(e, "depart", jid)
    with e.db.write() as c:
        c.execute("UPDATE characters SET satiety=30 WHERE id=?", (e.player,))
        before = c.execute("SELECT money FROM characters WHERE id=?", (e.player,)).fetchone()[0]
        JourneyService.maintain(c, e.wid, e.at)
        assert (
            c.execute("SELECT money FROM characters WHERE id=?", (e.player,)).fetchone()[0]
            == before
        )
    assert get(e, jid)["status"] == "paused"


def test_decline_and_deadline_do_not_create_completion_or_return_teleport(town):
    e = town
    jid = offer(e)
    assert choose(e, "decline", jid).status_code == 200
    assert choose(e, "depart", jid).status_code == 409
    other = offer(e)
    choose(e, "accept", other)
    choose(e, "depart", other)
    advance_to_arrival(e, other)
    with e.db.write() as c:
        JourneyService.maintain(c, e.wid, from_iso(get(e, other)["deadline_at"]))
    assert get(e, other)["status"] == "expired"
    with e.db.read() as c:
        assert (
            c.execute("SELECT longitude FROM characters WHERE id=?", (e.player,)).fetchone()[0]
            != e.loc["longitude"]
        )
        assert not c.execute(
            "SELECT 1 FROM world_events WHERE event_type='social.appointment_journey_completed'"
        ).fetchone()


def test_pause_contact_and_attack_end_cooperation_without_returning_people(town):
    from world_engine.intimacy import IntimacyService, RelationshipIntent

    e = town
    jid = offer(e)
    choose(e, "accept", jid)
    choose(e, "depart", jid)
    with e.db.write() as c:
        player, npc = people(e, c)
        IntimacyService.apply(
            c,
            player,
            npc,
            e.at,
            RelationshipIntent(
                operation="boundary", pause_contact=True, statement="我想先留出自己的空间。"
            ),
        )
        JourneyService.maintain(c, e.wid, e.at)
    assert get(e, jid)["status"] == "paused"


@pytest.mark.parametrize(
    "fields",
    [
        {"deadline_world_time": "2030-01-01T09:00:00"},
        {"deadline_world_time": "2040-01-01T09:00:00Z"},
        {"stops": []},
        {"return_to_origin": 1},
    ],
)
def test_invalid_itinerary_has_no_partial_proposal(town, fields):
    e = town
    sites(e)
    with pytest.raises(ValueError):
        offer(e, **fields)
    with e.db.read() as c:
        assert not c.execute("SELECT 1 FROM joint_journeys").fetchone()


def test_legacy_social_receipt_survives_new_null_journey_field(town):
    import hashlib
    import json

    from world_engine.api_intimacy import NaturalSocialChoice
    from world_engine.shared_life import SharedLifeIntent

    e = town
    text = "我想邀请你留十五分钟聊聊。"
    payload = NaturalSocialChoice(
        request_id=str(uuid4()),
        recipient_id=e.npc,
        intent=text,
        evidence=text,
        shared_life_choice=SharedLifeIntent(operation="invite", kind="talk", statement=text),
    ).model_dump(mode="json")
    payload.pop("journey_choice")
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    saved = {"status": "pending", "event_id": "historical", "summary": "旧邀请已经保存"}
    with e.db.write() as c:
        c.execute(
            "INSERT INTO social_choice_receipts VALUES (?,?,?,?,?)",
            (e.wid, payload["request_id"], e.player, digest, json.dumps(saved)),
        )
    assert e.client.post(f"/api/worlds/{e.wid}/player/social-choices", json=payload).json() == saved


def test_multiple_stops_complete_only_after_last_dwell(town):
    e = town
    first, second = sites(e)
    jid = offer(
        e,
        stops=[{"location_id": first, "minutes": 1}, {"location_id": second, "minutes": 3}],
        return_to_origin=False,
    )
    assert choose(e, "accept", jid).status_code == 200
    assert choose(e, "depart", jid).status_code == 200
    assert advance_to_arrival(e, jid)["next_index"] == 1
    e.clock.heartbeat(e.wid, elapsed_seconds=60)
    assert choose(e, "depart", jid).status_code == 200
    assert advance_to_arrival(e, jid)["status"] == "active"
    e.clock.heartbeat(e.wid, elapsed_seconds=180)
    assert get(e, jid)["status"] == "completed"
    with e.db.read() as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM journey_legs WHERE journey_id=? AND status='arrived'", (jid,)
            ).fetchone()[0]
            == 2
        )


def test_confirmed_appointment_created_during_trip_pauses_at_departure_boundary(town):
    from test_appointments import choose as appointment_choose
    from test_appointments import offer as appointment_offer

    from world_engine.collective_clock import next_boundary
    from world_engine.schedules import ScheduleService

    e = town
    jid = offer(e)
    choose(e, "accept", jid)
    choose(e, "depart", jid)
    advance_to_arrival(e, jid)
    # 哥哥，这里保留新约定的双方同意；旅行不能覆盖后来确认的日程。
    with e.db.read() as c:
        at = from_iso(
            c.execute("SELECT w.current_time FROM worlds w WHERE w.id=?", (e.wid,)).fetchone()[0]
        )
    aid = appointment_offer(e, at=at)
    assert appointment_choose(e, "accept", aid).status_code == 200
    with e.db.write() as c:
        player, npc = people(e, c)
        assert ScheduleService.assess(c, player, e.at) is not None
        plans = [ScheduleService.assess(c, p, at) for p in (player, npc)]
        due = min(p["depart_at"] for p in plans)
        assert next_boundary(c, e.wid, due - timedelta(seconds=1)) <= due
        JourneyService.maintain(c, e.wid, due)
    assert get(e, jid)["status"] == "paused"


def test_same_person_cannot_accept_and_request_retries_do_not_duplicate_legs(town):
    e = town
    jid = offer(e)
    with pytest.raises(ValueError, match="收到"):
        npc_choice(e, jid, "accept")
    choose(e, "accept", jid)
    text = "我已实际会合，选择按这份已确认行程出发。"
    payload = {
        "request_id": str(uuid4()),
        "recipient_id": e.npc,
        "intent": text,
        "evidence": text,
        "journey_choice": {"operation": "depart", "journey_id": jid, "statement": text},
    }
    first = e.client.post(f"/api/worlds/{e.wid}/player/social-choices", json=payload)
    assert first.status_code == 200
    assert (
        e.client.post(f"/api/worlds/{e.wid}/player/social-choices", json=payload).json()
        == first.json()
    )
    with e.db.read() as c:
        assert (
            c.execute("SELECT COUNT(*) FROM journey_legs WHERE journey_id=?", (jid,)).fetchone()[0]
            == 1
        )


def test_waiting_for_departure_expires_without_forcing_npc_to_stay(town):
    e = town
    jid = offer(e)
    choose(e, "accept", jid)
    with e.db.write() as c:
        JourneyService.maintain(c, e.wid, e.at)
        assert "meet_wait_until" not in JourneyService.rows(c, e.wid, e.player, e.at)[0]
        JourneyService.maintain(c, e.wid, e.at + timedelta(hours=1))
    assert get(e, jid)["status"] == "paused"


def test_future_visit_cannot_be_used_as_current_route_knowledge(town):
    e = town
    place = sites(e)[0]
    with e.db.write() as c:
        c.execute(
            "UPDATE world_events SET occurred_at=? WHERE location_id=?",
            (to_iso(e.at + timedelta(days=1)), place),
        )
        assert place not in {p["id"] for p in JourneyService.places(c, e.wid, e.npc)}
    with pytest.raises(ValueError, match="到访"):
        offer(e, stops=[{"location_id": place, "minutes": 2}])


@pytest.mark.parametrize("health", [100, 20])
def test_attack_ends_active_journey_and_cancels_bound_movements(town, health):
    e = town
    jid = offer(e)
    choose(e, "accept", jid)
    choose(e, "depart", jid)
    with e.db.write() as c:
        c.execute("UPDATE characters SET health=? WHERE id=?", (health, e.npc))
        ActionService._record_event(
            c,
            world_id=e.wid,
            tick_id=str(uuid4()),
            occurred_at=e.at,
            event_type="action.attack",
            actor_id=e.player,
            target_id=e.npc,
            location_id=e.loc["id"],
            summary="隔离评测中已经发生主动攻击。",
            payload={},
        )
        JourneyService.maintain(c, e.wid, e.at)
        assert not c.execute(
            "SELECT 1 FROM character_movements WHERE status='moving' AND character_id IN (?,?)",
            (e.player, e.npc),
        ).fetchone()
    assert get(e, jid)["status"] == "ended"


def test_npc_work_at_meeting_place_is_not_counted_as_waiting(town):
    e = town
    jid = offer(e)
    choose(e, "accept", jid)
    with e.db.write() as c:
        LifeActivityService.start(c, people(e, c)[1], e.at, "rest", 120)
        JourneyService.maintain(c, e.wid, e.at)
        assert (
            c.execute("SELECT meet_wait_until FROM joint_journeys WHERE id=?", (jid,)).fetchone()[0]
            is None
        )


def test_fulfilled_memory_requires_real_completed_journey_record(town):
    from world_engine.relationship_continuity import RelationshipContinuityService

    e = town
    place = sites(e)[0]
    jid = offer(e, stops=[{"location_id": place, "minutes": 0}], return_to_origin=False)
    choose(e, "accept", jid)
    choose(e, "depart", jid)
    assert advance_to_arrival(e, jid)["status"] == "completed"
    with e.db.write() as c:
        row = c.execute(
            "SELECT * FROM world_events WHERE id=?", (get(e, jid)["source_event_id"],)
        ).fetchone()
        assert RelationshipContinuityService._fulfilled(c, row)
        fake = ActionService._record_event(
            c,
            world_id=e.wid,
            tick_id=str(uuid4()),
            occurred_at=e.at,
            event_type="social.appointment_journey_completed",
            actor_id=e.player,
            target_id=e.npc,
            location_id=e.loc["id"],
            summary="仅有相似事件不代表旅行已经完成。",
            payload={"journey_id": jid},
        )
        assert not RelationshipContinuityService._fulfilled(
            c, c.execute("SELECT * FROM world_events WHERE id=?", (fake,)).fetchone()
        )


def test_dialogue_letter_and_outreach_keep_journey_candidate():
    import json

    from pydantic import TypeAdapter

    from world_engine.api_dialogue import NpcTextResult
    from world_engine.decisions import NpcReply
    from world_engine.outreach import OutreachText
    from world_engine.roleplay import salvage_npc_reply

    raw = {
        "reply": "我同意恢复原行程，下一程仍等你自己选择。",
        "journey_intent": {
            "operation": "resume",
            "journey_id": "真实行程编号",
            "statement": "这是我本人的恢复意愿。",
        },
    }
    for schema in (NpcReply, NpcTextResult, OutreachText):
        data = raw | {"should_contact": True} if schema is OutreachText else raw
        reply = (
            schema.model_validate(data)
            if schema is OutreachText
            else salvage_npc_reply(json.dumps(data, ensure_ascii=False), TypeAdapter(schema))
        )
        assert reply.journey_intent.operation == "resume"
        assert reply.journey_intent.journey_id == "真实行程编号"
