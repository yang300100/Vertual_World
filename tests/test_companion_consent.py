"""哥哥，短时同行逐份确认，反提、拒绝和真实行走各自留证。"""

# ruff: noqa: F811
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_appointment_rooms import room
from test_lived_world import town  # noqa: F401
from test_overnight_options import third_person

from world_engine.companion_proposals import CompanionIntent, CompanionProposalService
from world_engine.companions import CompanionService
from world_engine.life import LifeActivityService
from world_engine.repository import from_iso, to_iso


def current(e):
    with e.db.read() as c:
        return from_iso(
            c.execute("SELECT w.current_time FROM worlds w WHERE w.id=?", (e.wid,)).fetchone()[0]
        )


def choose(
    e,
    operation="propose",
    proposal_id=None,
    *,
    npc=False,
    minutes=None,
    session_id=None,
    at=None,
    other_npc=None,
    statement="这是本人明确作出的短时同行选择。",
):
    fields = dict(operation=operation, statement=statement)
    for key, value in (
        ("proposal_id", proposal_id),
        ("minutes", minutes),
        ("session_id", session_id),
    ):
        if value is not None:
            fields[key] = value
    with e.db.write() as c:
        player = c.execute("SELECT * FROM characters WHERE id=?", (e.player,)).fetchone()
        companion = c.execute(
            "SELECT * FROM characters WHERE id=?", (other_npc or e.npc,)
        ).fetchone()
        return CompanionProposalService.apply(
            c,
            companion if npc else player,
            player if npc else companion,
            at or current(e),
            CompanionIntent(**fields),
        )


def raw(e, pid):
    with e.db.read() as c:
        return dict(c.execute("SELECT * FROM companion_proposals WHERE id=?", (pid,)).fetchone())


def facts(e):
    with e.db.read() as c:
        return {
            table: [tuple(row) for row in c.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in (
                "characters",
                "companion_proposals",
                "companion_sessions",
                "character_movements",
                "character_life_activities",
                "contract_fulfillments",
                "world_events",
                "character_memories",
            )
        }


def accepted(e, minutes=30):
    pid = choose(e, minutes=minutes)["proposal_id"]
    return pid, choose(e, "accept", pid, npc=True)


@pytest.mark.parametrize("traits,score", [("热情", 100), ("谨慎", -100)])
def test_invitation_never_uses_personality_or_relationship_numbers_as_consent(town, traits, score):
    e = town
    with e.db.write() as c:
        c.execute("UPDATE characters SET traits_json=? WHERE id=?", (json.dumps([traits]), e.npc))
        c.execute(
            "INSERT OR REPLACE INTO relationships VALUES (?,?,?,?,?,?)",
            (e.wid, e.npc, e.player, score, score, to_iso(e.at)),
        )
    pid = choose(e, minutes=30)["proposal_id"]
    assert raw(e, pid)["status"] == "pending"
    with e.db.read() as c:
        assert not c.execute("SELECT 1 FROM companion_sessions").fetchone()
    assert choose(e, "accept", pid, npc=True)["status"] == "active"


def test_legacy_post_only_proposes_even_for_friendly_npc(town):
    e = town
    with e.db.write() as c:
        c.execute("UPDATE characters SET traits_json='[\"热情\"]' WHERE id=?", (e.npc,))
    response = e.client.post(
        f"/api/worlds/{e.wid}/companions", json={"recipient_id": e.npc, "minutes": 30}
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pending"
    with e.db.read() as c:
        assert not c.execute("SELECT 1 FROM companion_sessions").fetchone()


@pytest.mark.parametrize("operation", ["accept", "decline", "counter"])
def test_proposer_cannot_answer_own_invitation(town, operation):
    e = town
    pid = choose(e, minutes=60)["proposal_id"]
    before = facts(e)
    with pytest.raises(ValueError, match="另一方"):
        choose(e, operation, pid, minutes=15 if operation == "counter" else None)
    assert facts(e) == before


def test_npc_can_propose_and_player_must_independently_accept(town):
    e = town
    pid = choose(e, npc=True, minutes=20)["proposal_id"]
    assert raw(e, pid)["proposer_id"] == e.npc
    with e.db.read() as c:
        assert not c.execute("SELECT 1 FROM companion_sessions").fetchone()
    result = choose(e, "accept", pid)
    with e.db.read() as c:
        session = c.execute(
            "SELECT * FROM companion_sessions WHERE id=?", (result["session_id"],)
        ).fetchone()
        assert session["leader_id"] == e.player and session["companion_id"] == e.npc
        event = c.execute("SELECT * FROM world_events WHERE id=?", (result["event_id"],)).fetchone()
        assert event["actor_id"] == e.player and event["target_id"] == e.npc


def test_counter_offer_keeps_original_history_and_needs_the_original_proposer_choice(town):
    e = town
    first = choose(e, minutes=60)["proposal_id"]
    counter = choose(e, "counter", first, npc=True, minutes=15)
    second = counter["proposal_id"]
    assert raw(e, first)["status"] == "countered"
    assert raw(e, second)["parent_proposal_id"] == first
    assert raw(e, second)["proposer_id"] == e.npc and raw(e, second)["recipient_id"] == e.player
    with e.db.read() as c:
        assert not c.execute("SELECT 1 FROM companion_sessions").fetchone()
    with pytest.raises(ValueError):
        choose(e, "accept", second, npc=True)
    result = choose(e, "accept", second)
    with e.db.read() as c:
        session = c.execute(
            "SELECT * FROM companion_sessions WHERE id=?", (result["session_id"],)
        ).fetchone()
        assert from_iso(session["until_world_time"]) - from_iso(
            session["started_world_time"]
        ) == timedelta(minutes=15)


def test_counter_can_be_countered_again_without_becoming_a_decline_cooldown(town):
    e = town
    first = choose(e, minutes=60)["proposal_id"]
    second = choose(e, "counter", first, npc=True, minutes=15)["proposal_id"]
    third = choose(e, "counter", second, minutes=10)["proposal_id"]
    assert raw(e, second)["status"] == "countered" and raw(e, third)["parent_proposal_id"] == second
    assert choose(e, "accept", third, npc=True)["status"] == "active"


def test_pending_request_replay_and_acceptance_replay_do_not_create_second_session(town):
    e = town
    first = choose(e, minutes=20)
    assert choose(e, minutes=20) == first
    response = choose(e, "accept", first["proposal_id"], npc=True)
    again = choose(e, "accept", first["proposal_id"], npc=True)
    assert response["session_id"] == again["session_id"]
    with e.db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM companion_sessions").fetchone()[0] == 1


def test_decline_and_cancellation_are_separate_and_only_decline_has_cooldown(town):
    e = town
    pid = choose(e, minutes=20)["proposal_id"]
    with pytest.raises(ValueError, match="提出者"):
        choose(e, "cancel", pid, npc=True)
    choose(e, "cancel", pid)
    second = choose(e, minutes=15)["proposal_id"]
    choose(e, "decline", second, npc=True)
    with pytest.raises(ValueError, match="世界日"):
        choose(e, minutes=5)
    later = choose(e, minutes=5, at=e.at + timedelta(days=1, seconds=1))
    assert later["status"] == "pending"


def test_pending_expiry_never_starts_companionship_and_records_exact_deadline(town):
    e = town
    pid = choose(e, minutes=20)["proposal_id"]
    end = from_iso(raw(e, pid)["expires_at"])
    with e.db.write() as c:
        CompanionProposalService.maintain(c, e.wid, end + timedelta(hours=1))
        proposal = c.execute("SELECT * FROM companion_proposals WHERE id=?", (pid,)).fetchone()
        event = c.execute(
            "SELECT * FROM world_events WHERE id=?", (proposal["source_event_id"],)
        ).fetchone()
        assert proposal["status"] == "expired" and from_iso(event["occurred_at"]) == end
        assert not c.execute("SELECT 1 FROM companion_sessions").fetchone()
    with pytest.raises(ValueError):
        choose(e, "accept", pid, npc=True, at=end)


def test_acceptance_starts_exact_duration_at_actual_acceptance_and_does_not_move_people(town):
    e = town
    pid = choose(e, minutes=30)["proposal_id"]
    at = e.at + timedelta(minutes=10)
    with e.db.read() as c:
        before = [
            tuple(r)
            for r in c.execute(
                "SELECT id,longitude,latitude,money,energy,current_room_id FROM characters "
                "WHERE id IN (?,?) ORDER BY id",
                (e.player, e.npc),
            )
        ]
    result = choose(e, "accept", pid, npc=True, at=at)
    with e.db.read() as c:
        session = c.execute(
            "SELECT * FROM companion_sessions WHERE id=?", (result["session_id"],)
        ).fetchone()
        assert from_iso(session["started_world_time"]) == at
        assert from_iso(session["until_world_time"]) == at + timedelta(minutes=30)
        assert [
            tuple(r)
            for r in c.execute(
                "SELECT id,longitude,latitude,money,energy,current_room_id FROM characters "
                "WHERE id IN (?,?) ORDER BY id",
                (e.player, e.npc),
            )
        ] == before
        assert not c.execute("SELECT 1 FROM character_movements").fetchone()


def test_world_clock_expires_pending_proposal_at_its_real_boundary(town):
    from world_engine.collective_clock import next_boundary

    e = town
    pid = choose(e, minutes=30)["proposal_id"]
    expires = from_iso(raw(e, pid)["expires_at"])
    with e.db.read() as c:
        assert next_boundary(c, e.wid, e.at) <= expires
    e.clock.heartbeat(e.wid, elapsed_seconds=31 * 60)
    assert raw(e, pid)["status"] == "expired"
    with e.db.read() as c:
        assert not c.execute("SELECT 1 FROM companion_sessions").fetchone()


def test_more_than_twenty_meters_cannot_transmit_proposal_or_create_memories(town):
    e = town
    with e.db.write() as c:
        c.execute("UPDATE characters SET longitude=longitude+0.0004 WHERE id=?", (e.npc,))
    before = facts(e)
    with pytest.raises(ValueError, match="可接触"):
        choose(e, minutes=30)
    assert facts(e) == before


@pytest.mark.parametrize("change", ["room", "layer", "moving", "body", "activity", "sleep"])
def test_acceptance_rechecks_actual_execution_conditions_without_losing_pending_terms(town, change):
    e = town
    pid = choose(e, minutes=30)["proposal_id"]
    at = e.at
    rid = room(e) if change == "room" else None
    with e.db.write() as c:
        if change == "room":
            c.execute("UPDATE characters SET current_room_id=? WHERE id=?", (rid, e.npc))
        elif change == "layer":
            c.execute("UPDATE characters SET space_layer='underground' WHERE id=?", (e.npc,))
        elif change == "body":
            c.execute("UPDATE characters SET satiety=10 WHERE id=?", (e.npc,))
        elif change == "activity":
            actor = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
            LifeActivityService.start(c, actor, at, "rest", 10)
        elif change == "sleep":
            # 哥哥，只推进隔离时钟，原邀请也用同一时刻建立以保留未过期情境。
            at = e.at.replace(hour=21, minute=50)
            c.execute(
                "UPDATE companion_proposals SET proposed_at=?,expires_at=? WHERE id=?",
                (to_iso(at), to_iso(at + timedelta(minutes=30)), pid),
            )
        else:
            from world_engine.movement import MovementService

            MovementService().start(
                c,
                world_id=e.wid,
                character_id=e.npc,
                destination_longitude=e.loc["longitude"] + 0.01,
                destination_latitude=e.loc["latitude"],
                world_time=at,
            )
    before = facts(e)
    with pytest.raises(ValueError):
        choose(e, "accept", pid, npc=True, at=at)
    assert facts(e) == before and raw(e, pid)["status"] == "pending"


def test_long_duration_can_be_countered_without_silently_shortening_accepted_terms(town):
    e = town
    at = e.at.replace(hour=21, minute=50)
    pid = choose(e, minutes=60, at=at)["proposal_id"]
    with pytest.raises(ValueError, match="睡眠"):
        choose(e, "accept", pid, npc=True, at=at)
    shorter = choose(e, "counter", pid, npc=True, minutes=5, at=at)["proposal_id"]
    result = choose(e, "accept", shorter, at=at)
    with e.db.read() as c:
        session = c.execute(
            "SELECT * FROM companion_sessions WHERE id=?", (result["session_id"],)
        ).fetchone()
        assert from_iso(session["until_world_time"]) - at == timedelta(minutes=5)
    assert raw(e, pid)["minutes"] == 60


def test_acceptance_failure_rolls_back_event_memory_proposal_and_session(town, monkeypatch):
    e = town
    pid = choose(e, minutes=30)["proposal_id"]
    before = facts(e)

    def fail(*args, **kwargs):
        raise RuntimeError("隔离故障：执行层创建失败")

    monkeypatch.setattr(CompanionService, "_start_after_consent", fail)
    with pytest.raises(RuntimeError):
        choose(e, "accept", pid, npc=True)
    assert facts(e) == before


def test_player_can_have_multiple_independently_consented_companions(town):
    e = town
    third = third_person(e)
    with e.db.write() as c:
        c.execute(
            "UPDATE characters SET longitude=?,latitude=?,current_location_id=?,location_id=?,"
            "movement_type='land',active_vehicle_id=NULL,current_room_id=NULL,"
            "current_fixture_id=NULL,traits_json='[]' WHERE id=?",
            (e.loc["longitude"], e.loc["latitude"], e.loc["id"], e.loc["id"], third),
        )
    accepted(e, 30)
    pid = choose(e, minutes=20, other_npc=third)["proposal_id"]
    choose(e, "accept", pid, npc=True, other_npc=third)
    with e.db.read() as c:
        assert {s["companion_id"] for s in CompanionService.active(c, e.player)} == {e.npc, third}
    with e.db.write() as c:
        from world_engine.movement import MovementService

        MovementService().start(
            c,
            world_id=e.wid,
            character_id=e.player,
            world_time=e.at,
            destination_longitude=e.loc["longitude"] + 0.001,
            destination_latitude=e.loc["latitude"],
        )
        paths = c.execute(
            "SELECT character_id,estimated_arrival_world FROM character_movements WHERE "
            "status='moving'"
        ).fetchall()
        assert {r["character_id"] for r in paths} == {e.player, e.npc, third}
        assert len({r["estimated_arrival_world"] for r in paths}) == 1


def test_explicit_npc_end_works_remotely_and_does_not_reactivate_on_accept_replay(town):
    e = town
    pid, session = accepted(e)
    with e.db.write() as c:
        c.execute("UPDATE characters SET longitude=longitude+1 WHERE id=?", (e.npc,))
    choose(e, "end", npc=True, session_id=session["session_id"])
    replay = choose(e, "accept", pid, npc=True)
    assert replay["status"] == "ended"
    with e.db.read() as c:
        assert not CompanionService.active(c, e.player)


@pytest.mark.parametrize(
    "bad",
    [
        dict(operation="propose", minutes=True),
        dict(operation="propose"),
        dict(operation="accept", proposal_id="real", minutes=20),
        dict(operation="counter", proposal_id="real"),
        dict(operation="end", proposal_id="real"),
    ],
)
def test_intent_does_not_coerce_or_rewrite_other_person_terms(bad):
    with pytest.raises(ValidationError):
        CompanionIntent(**bad, statement="我本人明确选择。")


def test_old_active_session_migration_does_not_invent_past_consent(town):
    e = town
    sid = str(uuid4())
    with e.db.write() as c:
        source = c.execute(
            "SELECT id FROM world_events WHERE world_id=? LIMIT 1", (e.wid,)
        ).fetchone()[0]
        c.execute("DROP INDEX idx_companion_session_proposal")
        c.execute("ALTER TABLE companion_sessions DROP COLUMN proposal_id")
        c.execute(
            "INSERT INTO companion_sessions(id,world_id,leader_id,companion_id,status,reason,"
            "started_world_time,until_world_time,source_event_id) VALUES (?,?,?,?,'active',"
            "'历史旧同行',?,?,?)",
            (sid, e.wid, e.player, e.npc, to_iso(e.at), to_iso(e.at + timedelta(hours=1)), source),
        )
        before = dict(c.execute("SELECT * FROM companion_sessions WHERE id=?", (sid,)).fetchone())
    e.db.initialize()
    e.db.initialize()
    with e.db.read() as c:
        after = dict(c.execute("SELECT * FROM companion_sessions WHERE id=?", (sid,)).fetchone())
        assert after.pop("proposal_id") is None and after == before
        assert not c.execute("SELECT 1 FROM companion_proposals").fetchone()
        assert any(
            row["from"] == "proposal_id" and row["table"] == "companion_proposals"
            for row in c.execute("PRAGMA foreign_key_list(companion_sessions)")
        )
