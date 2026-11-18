"""哥哥，途中主动交谈必须绑定本人正在共同走的这一程，并保持各自实际移动。"""

# ruff: noqa: F811
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_companion_consent import accepted
from test_journeys import advance_to_arrival, choose, get, offer
from test_lived_world import town  # noqa: F401

from world_engine.engine import WorldEngine
from world_engine.movement import MovementService
from world_engine.outreach import process_outreach
from world_engine.repository import WorldRepository, to_iso
from world_engine.society import SocietyService
from world_engine.travel_interaction import TravelInteractionService


def journey(e):
    jid = offer(e, leader_id=e.player)
    assert choose(e, "accept", jid).status_code == 200
    assert choose(e, "depart", jid).status_code == 200
    return jid


def companion(e):
    _, session = accepted(e)
    with e.db.write() as c:
        MovementService().start(
            c,
            world_id=e.wid,
            character_id=e.player,
            destination_longitude=e.loc["longitude"] + 0.003,
            destination_latitude=e.loc["latitude"],
            world_time=e.at,
        )
    return session["session_id"]


def motion(e):
    with e.db.read() as c:
        return TravelInteractionService.shared_motion(c, e.wid, e.npc, e.player, e.at)


def queue(e):
    with e.db.write() as c:
        SocietyService.tick(c, e.wid, e.at)
        c.execute("DELETE FROM npc_outreach_jobs WHERE npc_id!=?", (e.npc,))
        row = c.execute(
            "SELECT * FROM npc_outreach_jobs WHERE npc_id=? AND player_id=?",
            (e.npc, e.player),
        ).fetchone()
        return dict(row) if row else None


def state(e):
    with e.db.read() as c:
        return {
            "people": [
                tuple(r)
                for r in c.execute(
                    "SELECT id,longitude,latitude,money,energy,satiety FROM characters "
                    "WHERE id IN (?,?) ORDER BY id",
                    (e.npc, e.player),
                )
            ],
            "movements": [
                tuple(r)
                for r in c.execute(
                    "SELECT * FROM character_movements ORDER BY id",
                )
            ],
            "activities": [
                tuple(r)
                for r in c.execute(
                    "SELECT * FROM character_life_activities ORDER BY id",
                )
            ],
        }


class ContactModel:
    def __init__(self, should_contact=True, callback=None, **extra):
        self.should_contact = should_contact
        self.callback = callback
        self.extra = extra
        self.context = None

    def complete(self, *, schema, user_payload, **kwargs):
        self.context = user_payload
        if self.callback:
            self.callback()
        return SimpleNamespace(
            data=schema.validate_python(
                {
                    "should_contact": self.should_contact,
                    "reply": "我想边走边和你聊一会，也可以先安静赶路。"
                    if self.should_contact
                    else None,
                    **self.extra,
                }
            )
        )


def run(e, model):
    engine = WorldEngine(e.db, e.settings, e.model)
    engine.agent_model_backend = model
    return process_outreach(engine, e.wid)


def job_status(e, job):
    with e.db.read() as c:
        return c.execute(
            "SELECT status FROM npc_outreach_jobs WHERE id=?", (job["id"],)
        ).fetchone()[0]


@pytest.mark.parametrize("kind", ["journey", "companion"])
@pytest.mark.parametrize("should_contact", [True, False])
def test_real_shared_movement_can_choose_to_talk_without_stopping_or_moving_people(
    town, kind, should_contact
):
    e = town
    journey(e) if kind == "journey" else companion(e)
    # 哥哥，谨慎和陌生只影响本人要不要说话，不能替代一次真实共同经历。
    with e.db.write() as c:
        c.execute("UPDATE characters SET traits_json='[\"谨慎\"]' WHERE id=?", (e.npc,))
        c.execute(
            "DELETE FROM relationships WHERE source_character_id=? AND target_character_id=?",
            (e.npc, e.player),
        )
    binding = motion(e)
    assert binding and binding["kind"] == kind
    job = queue(e)
    assert job["source_event_id"] == binding["source_event_id"]
    assert job["reason"].startswith(TravelInteractionService.JOB_PREFIX)
    before = state(e)
    model = ContactModel(should_contact)
    assert run(e, model) == int(should_contact)
    after = state(e)
    assert after["movements"] == before["movements"]
    assert after["activities"] == before["activities"]
    for earlier, later in zip(before["people"], after["people"], strict=True):
        assert earlier[:4] == later[:4]
        cost = 2 if should_contact and earlier[0] == e.npc else 0
        assert later[4:] == (earlier[4] - cost, earlier[5] - cost)
    assert job_status(e, job) == ("done" if should_contact else "declined")
    assert model.context["shared_travel"]["has_arrived"] is False
    assert model.context["scene"]["location"] is None
    assert "npc_movement_id" not in model.context["shared_travel"]
    assert "player_movement_id" not in model.context["shared_travel"]
    with e.db.read() as c:
        if should_contact:
            event = c.execute(
                "SELECT payload_json FROM world_events WHERE id="
                "(SELECT result_event_id FROM npc_outreach_jobs WHERE id=?)",
                (job["id"],),
            ).fetchone()
            assert binding["source_event_id"] not in event[0]
        assert (
            c.execute(
                "SELECT COUNT(*) FROM npc_outreach_jobs WHERE npc_id=? "
                "AND player_id=? AND world_day=?",
                (e.npc, e.player, e.at.date().isoformat()),
            ).fetchone()[0]
            == 1
        )


def test_companion_source_is_the_npcs_current_departure_not_the_session_start(town):
    e = town
    sid = companion(e)
    binding = motion(e)
    with e.db.read() as c:
        session = c.execute("SELECT * FROM companion_sessions WHERE id=?", (sid,)).fetchone()
        event = c.execute(
            "SELECT * FROM world_events WHERE id=?", (binding["source_event_id"],)
        ).fetchone()
        assert event["actor_id"] == e.npc and event["event_type"] == "action.movement_started"
        assert json.loads(event["payload_json"])["movement_id"] == binding["npc_movement_id"]
        assert binding["source_event_id"] != session["source_event_id"]


def test_independent_nearby_movers_do_not_gain_a_travel_contact_permission(town):
    e = town
    with e.db.write() as c:
        for cid in (e.npc, e.player):
            MovementService().start(
                c,
                world_id=e.wid,
                character_id=cid,
                destination_longitude=e.loc["longitude"] + 0.003,
                destination_latitude=e.loc["latitude"],
                world_time=e.at,
            )
        c.execute("UPDATE characters SET traits_json='[\"热情\"]' WHERE id=?", (e.npc,))
    assert motion(e) is None
    job = queue(e)
    model = ContactModel()
    before = state(e)
    assert job and run(e, model) == 0
    assert model.context is None and job_status(e, job) == "pending"
    assert state(e) == before


@pytest.mark.parametrize("kind", ["journey", "companion"])
@pytest.mark.parametrize(
    "change", ["distance", "layer", "room", "source", "actor", "event_world", "future"]
)
def test_common_motion_rejects_spatial_and_departure_provenance_changes(town, kind, change):
    e = town
    journey(e) if kind == "journey" else companion(e)
    binding = motion(e)
    assert binding
    rid = None
    other_wid = None
    if change == "room":
        from test_appointment_rooms import room

        rid = room(e)
    if change == "event_world":
        other_wid = e.client.post("/api/worlds", json={"name": "其他来源世界"}).json()["world"][
            "id"
        ]
    with e.db.write() as c:
        if change == "distance":
            c.execute("UPDATE characters SET longitude=longitude+0.001 WHERE id=?", (e.npc,))
        elif change == "layer":
            c.execute("UPDATE characters SET space_layer='underground' WHERE id=?", (e.npc,))
        elif change == "room":
            c.execute("UPDATE characters SET current_room_id=? WHERE id=?", (rid, e.npc))
        else:
            event = c.execute(
                "SELECT * FROM world_events WHERE world_id=? AND actor_id=? "
                "AND event_type='action.movement_started' ORDER BY rowid DESC LIMIT 1",
                (e.wid, e.npc),
            ).fetchone()
            if change == "source":
                c.execute("UPDATE world_events SET payload_json='{}' WHERE id=?", (event["id"],))
            elif change == "actor":
                c.execute("UPDATE world_events SET actor_id=? WHERE id=?", (e.player, event["id"]))
            elif change == "event_world":
                c.execute("UPDATE world_events SET world_id=? WHERE id=?", (other_wid, event["id"]))
            else:
                c.execute(
                    "UPDATE world_events SET occurred_at=? WHERE id=?",
                    (to_iso(e.at + timedelta(seconds=1)), event["id"]),
                )
    assert motion(e) is None


@pytest.mark.parametrize("change", ["pause", "end", "arrive"])
def test_expired_leg_cancels_queued_travel_contact_without_calling_model(town, change):
    e = town
    jid = journey(e)
    job = queue(e)
    if change == "arrive":
        advance_to_arrival(e, jid)
    else:
        assert choose(e, change, jid).status_code == 200
    model = ContactModel()
    before = state(e)
    assert run(e, model) == 0 and model.context is None
    assert job_status(e, job) == "cancelled" and state(e) == before


@pytest.mark.parametrize("cid_key", ["npc", "player"])
def test_model_response_cannot_cross_a_changed_movement_even_without_version_bump(town, cid_key):
    e = town
    companion(e)
    job = queue(e)

    def callback():
        with e.db.write() as c:
            MovementService().cancel(
                c, world_id=e.wid, character_id=getattr(e, cid_key), world_time=e.at
            )

    model = ContactModel(callback=callback)
    assert run(e, model) == 0 and model.context
    assert job_status(e, job) == "cancelled"
    with e.db.read() as c:
        assert not c.execute(
            "SELECT 1 FROM player_notifications WHERE id=?", (job["id"],)
        ).fetchone()


def test_nonwait_activity_still_delays_a_real_travel_contact(town):
    e = town
    journey(e)
    job = queue(e)
    with e.db.write() as c:
        # 哥哥，隔离构造并发冲突的负面状态，确认已有非等待活动仍优先。
        c.execute(
            "INSERT INTO character_life_activities(id,world_id,character_id,kind,status,"
            "location_id,longitude,latitude,initial_health,started_world_time,ends_world_time,"
            "last_processed_world_time,created_at) SELECT 'travel-conflict',world_id,id,"
            "'rest','running',current_location_id,longitude,latitude,health,?,?,?,? "
            "FROM characters WHERE id=?",
            (to_iso(e.at), to_iso(e.at + timedelta(minutes=10)), to_iso(e.at), to_iso(e.at), e.npc),
        )
    model = ContactModel()
    assert run(e, model) == 0 and model.context is None
    assert job_status(e, job) == "pending"


def test_only_explicit_pause_intent_stops_the_bound_leg(town):
    e = town
    jid = journey(e)
    job = queue(e)
    model = ContactModel(
        journey_intent={"operation": "pause", "journey_id": jid, "statement": "我想先停下歇一会。"}
    )
    before = state(e)["people"]
    assert run(e, model) == 1
    assert job_status(e, job) == "done" and get(e, jid)["status"] == "paused"
    with e.db.read() as c:
        assert not c.execute(
            "SELECT 1 FROM character_movements WHERE character_id IN (?,?) AND status='moving'",
            (e.player, e.npc),
        ).fetchone()
    assert [p[:4] for p in state(e)["people"]] == [p[:4] for p in before]


def test_shared_dialogue_context_only_carries_the_current_leg_without_destination_coordinates(town):
    e = town
    journey(e)
    with e.db.read() as c:
        snapshot = WorldRepository().get_snapshot(c, e.wid)
        engine = WorldEngine(e.db, e.settings, e.model)
        context = engine.dialogue_context.build(
            c,
            snapshot=snapshot,
            npc=snapshot.character_by_id(e.npc),
            player=snapshot.character_by_id(e.player),
            player_text="我们边走边说说吧。",
            conversation=None,
            channel="in_person",
            interaction="共同移动中当面交谈",
        )
        shared = context["shared_travel"]
        assert shared["in_motion"] and not shared["has_arrived"]
        assert not any(
            "destination" in key or "coordinate" in key or "location" in key for key in shared
        )
        assert set(shared) <= {
            "kind",
            "journey_id",
            "leg_id",
            "source_event_id",
            "in_motion",
            "has_arrived",
            "talk_range_m",
            "scope",
        }
        remote = engine.dialogue_context.build(
            c,
            snapshot=snapshot,
            npc=snapshot.character_by_id(e.npc),
            player=snapshot.character_by_id(e.player),
            player_text="远程来信。",
            conversation=None,
            channel="letter",
            interaction="远程来信",
        )
        assert "shared_travel" not in remote
