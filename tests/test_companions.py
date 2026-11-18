"""哥哥，隔离存档验证同意边界、真实路程与自主退出。"""

# ruff: noqa: F811

from datetime import timedelta

import pytest
from test_lived_world import town  # noqa: F401

from world_engine.companion_proposals import CompanionIntent, CompanionProposalService
from world_engine.companions import CompanionService
from world_engine.geo import great_circle_distance_km
from world_engine.movement import MovementService


def willing(e, *, speed=3):
    with e.db.write() as c:
        c.execute(
            "UPDATE characters SET traits_json='[\"热情\"]',"
            "movement_speed_kmh=?,species='elf' WHERE id=?",
            (speed, e.npc),
        )


def propose(e, minutes=60):
    r = e.client.post(
        f"/api/worlds/{e.wid}/companions", json={"recipient_id": e.npc, "minutes": minutes}
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending"
    return r.json()


def accept_proposal(e, proposal_id):
    # 哥哥，移动回归的前置明确给出另一人的结构化选择，不用性格代替同意。
    with e.db.write() as c:
        npc = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        player = c.execute("SELECT * FROM characters WHERE id=?", (e.player,)).fetchone()
        result = CompanionProposalService.apply(
            c,
            npc,
            player,
            e.at,
            CompanionIntent(
                operation="accept",
                proposal_id=proposal_id,
                statement="我明确接受这份既定时长的短时同行。",
            ),
        )
        return dict(
            c.execute(
                "SELECT * FROM companion_sessions WHERE id=?", (result["session_id"],)
            ).fetchone()
        )


def invite(e, minutes=60):
    return accept_proposal(e, propose(e, minutes)["proposal_id"])


def test_personality_and_directional_trust_never_replace_actual_consent(town):
    e = town
    with e.db.write() as c:
        c.execute("UPDATE characters SET traits_json='[\"谨慎\"]' WHERE id=?", (e.npc,))
        c.execute(
            "INSERT OR REPLACE INTO relationships(world_id,source_character_id,"
            "target_character_id,affinity,trust,updated_at) VALUES (?,?,?,?,?,?)",
            (e.wid, e.npc, e.player, -50, -50, e.at.isoformat()),
        )
    proposal = propose(e)
    with e.db.read() as c:
        assert not c.execute("SELECT 1 FROM companion_sessions").fetchone()
    assert accept_proposal(e, proposal["proposal_id"])["status"] == "active"


def test_invitation_idempotent_and_audited_for_both_people(town):
    e = town
    willing(e)
    proposal = propose(e)
    assert propose(e)["proposal_id"] == proposal["proposal_id"]
    first = accept_proposal(e, proposal["proposal_id"])
    assert accept_proposal(e, proposal["proposal_id"])["id"] == first["id"]
    with e.db.read() as c:
        memories = c.execute(
            "SELECT character_id FROM character_memories WHERE event_id=?",
            (first["source_event_id"],),
        ).fetchall()
        assert {r[0] for r in memories} == {e.player, e.npc}
        assert (
            c.execute(
                "SELECT count(*) FROM companion_sessions WHERE companion_id=?", (e.npc,)
            ).fetchone()[0]
            == 1
        )


def test_group_walk_from_distinct_origins_and_stop_without_teleport(town):
    e = town
    willing(e)
    with e.db.write() as c:
        c.execute("UPDATE characters SET longitude=longitude+0.0001 WHERE id=?", (e.npc,))
    invite(e)
    with e.db.write() as c:
        before = {
            r["id"]: dict(r)
            for r in c.execute("SELECT * FROM characters WHERE id IN (?,?)", (e.player, e.npc))
        }
        movement = MovementService().start(
            c,
            world_id=e.wid,
            character_id=e.player,
            destination_longitude=e.loc["longitude"] + 0.01,
            destination_latitude=e.loc["latitude"],
            world_time=e.at,
        )
        rows = c.execute(
            "SELECT * FROM character_movements WHERE status='moving' AND character_id IN (?,?)",
            (e.player, e.npc),
        ).fetchall()
        assert len(rows) == 2
        assert len({r["estimated_arrival_world"] for r in rows}) == 1
        assert all(r["speed_kmh"] <= 3 for r in rows)
        assert (
            next(r for r in rows if r["character_id"] == e.npc)["origin_longitude"]
            == before[e.npc]["longitude"]
        )
        assert all(
            c.execute("SELECT longitude FROM characters WHERE id=?", (cid,)).fetchone()[0]
            == row["longitude"]
            for cid, row in before.items()
        )
        MovementService().advance(
            c,
            world_id=e.wid,
            previous_time=e.at,
            current_time=e.at + timedelta(minutes=1),
            created_at=e.at,
        )
        after = {
            r["id"]: dict(r)
            for r in c.execute("SELECT * FROM characters WHERE id IN (?,?)", (e.player, e.npc))
        }
        for cid in before:
            distance = great_circle_distance_km(
                before[cid]["longitude"],
                before[cid]["latitude"],
                after[cid]["longitude"],
                after[cid]["latitude"],
            )
            assert 0 < distance <= 3 / 60 + 0.00001
        MovementService().cancel(
            c, world_id=e.wid, character_id=e.player, world_time=e.at + timedelta(minutes=1)
        )
        assert (
            c.execute(
                "SELECT count(*) FROM character_movements WHERE status='moving' "
                "AND character_id IN (?,?)",
                (e.player, e.npc),
            ).fetchone()[0]
            == 0
        )
        assert movement.estimated_arrival_world > e.at


def test_long_route_rolls_back_entire_group(town):
    e = town
    willing(e)
    invite(e, minutes=5)
    with e.db.write() as c:
        with pytest.raises(ValueError, match="超过同行"):
            MovementService().start(
                c,
                world_id=e.wid,
                character_id=e.player,
                destination_longitude=e.loc["longitude"] + 0.1,
                destination_latitude=e.loc["latitude"],
                world_time=e.at,
            )
        assert (
            c.execute(
                "SELECT count(*) FROM character_movements WHERE world_id=?", (e.wid,)
            ).fetchone()[0]
            == 0
        )


def test_steering_walks_both_people_and_uses_slowest_speed(town):
    e = town
    willing(e)
    session = invite(e)
    start = e.client.post(
        f"/api/worlds/{e.wid}/player/steer", json={"dx": 1, "dy": 0, "sequence": 100}
    )
    assert start.status_code == 200, start.text
    with e.db.read() as c:
        before = {
            r["id"]: dict(r)
            for r in c.execute("SELECT * FROM characters WHERE id IN (?,?)", (e.player, e.npc))
        }
    e.clock.heartbeat(e.wid, elapsed_seconds=1)
    with e.db.read() as c:
        for cid, row in before.items():
            after = c.execute("SELECT * FROM characters WHERE id=?", (cid,)).fetchone()
            assert after["longitude"] > row["longitude"]
            assert (
                great_circle_distance_km(
                    row["longitude"], row["latitude"], after["longitude"], after["latitude"]
                )
                <= 3 / 3600 + 0.00001
            )
        assert (
            c.execute(
                "SELECT status FROM companion_sessions WHERE id=?", (session["id"],)
            ).fetchone()[0]
            == "active"
        )


@pytest.mark.parametrize("condition", ["energy=10", "satiety=10", "health=0"])
def test_companion_leaves_when_own_needs_change(town, condition):
    e = town
    willing(e)
    session = invite(e)
    with e.db.write() as c:
        c.execute(f"UPDATE characters SET {condition} WHERE id=?", (e.npc,))
        CompanionService.maintain(c, e.wid, e.at)
        assert (
            c.execute(
                "SELECT status FROM companion_sessions WHERE id=?", (session["id"],)
            ).fetchone()[0]
            == "ended"
        )


def test_expiry_and_manual_end_preserve_position(town):
    e = town
    willing(e)
    session = invite(e, 5)
    with e.db.write() as c:
        CompanionService.maintain(c, e.wid, e.at + timedelta(minutes=5))
        assert (
            c.execute(
                "SELECT status FROM companion_sessions WHERE id=?", (session["id"],)
            ).fetchone()[0]
            == "ended"
        )
    second = invite(e)
    r = e.client.delete(f"/api/worlds/{e.wid}/companions/{second['id']}")
    assert r.status_code == 200 and r.json()["status"] == "ended"


def test_invite_checks_real_scene_and_duration(town):
    e = town
    willing(e)
    with e.db.write() as c:
        c.execute("UPDATE characters SET space_layer='underground' WHERE id=?", (e.npc,))
    assert (
        e.client.post(f"/api/worlds/{e.wid}/companions", json={"recipient_id": e.npc}).status_code
        == 409
    )
    assert (
        e.client.post(
            f"/api/worlds/{e.wid}/companions", json={"recipient_id": e.npc, "minutes": True}
        ).status_code
        == 422
    )


def test_species_reaches_snapshot_and_visible_people(town):
    e = town
    willing(e)
    snapshot = e.client.get(f"/api/worlds/{e.wid}").json()
    assert next(p for p in snapshot["characters"] if p["id"] == e.npc)["species"] == "elf"
    view = e.client.get(f"/api/worlds/{e.wid}/player/experience").json()
    person = next(p for p in view["people"] if p["id"] == e.npc)
    assert person["species_name"] == "精灵"
    assert "traits" not in person


def test_travelers_are_formally_applied_and_not_duplicated(town):
    from scripts.seed_encounter_people import register_encounter_people

    e = town
    with e.db.write() as c:
        first = register_encounter_people(c, e.wid, e.loc["id"])
        second = register_encounter_people(c, e.wid, e.loc["id"])
        assert {r["id"] for r in first} == {r["id"] for r in second}
        assert all(r["status"] == "applied" for r in first)
        assert {r["species"] for r in first} == {"human", "elf", "dwarf", "goblin", "orc"}
        assert (
            c.execute(
                "SELECT count(*) FROM npc_routine_plans WHERE world_id=?", (e.wid,)
            ).fetchone()[0]
            == 5
        )
        assert (
            c.execute(
                "SELECT count(*) FROM element_registration_requests WHERE world_id=? "
                "AND idempotency_key LIKE 'encounter-life-v1:%' AND status='applied'",
                (e.wid,),
            ).fetchone()[0]
            == 5
        )


def test_dialogue_knows_own_species_and_actual_companion_agreement(town):
    from world_engine.conversations import ConversationService
    from world_engine.repository import WorldRepository
    from world_engine.roleplay import npc_reply_system_prompt
    from world_engine.roleplay_pipeline import build_npc_prompt

    e = town
    willing(e)
    session = invite(e)
    with e.db.read() as c:
        snapshot = WorldRepository().get_snapshot(c, e.wid)
        context = ConversationService().build_npc_reply_context(
            c,
            snapshot=snapshot,
            npc=snapshot.character_by_id(e.npc),
            player=snapshot.character_by_id(e.player),
            player_text="你为什么愿意一起走？",
            conversation=None,
        )
    assert context["npc"]["species_name"] == "精灵"
    assert context["companionship"][0]["id"] == session["id"]
    prompt = build_npc_prompt(npc_reply_system_prompt(), context)
    assert "精灵" in str(prompt.messages)
    assert session["id"] in str(prompt.messages)


def test_model_intent_preview_is_read_only_and_offline_does_not_invite(town):
    from types import SimpleNamespace

    from world_engine.intent_parser import IntentParserAgent
    from world_engine.repository import WorldRepository

    e = town

    class Parser:
        def complete(self, *, schema, **kwargs):
            return SimpleNamespace(
                data=schema.validate_python(
                    {
                        "operation": "companion",
                        "target_character_id": e.npc,
                        "minutes": 30,
                    }
                )
            )

    with e.db.read() as c:
        snapshot = WorldRepository().get_snapshot(c, e.wid)
        player = snapshot.character_by_id(e.player)
        preview = IntentParserAgent(Parser()).preview(
            intent="和我在附近一起走半小时吧",
            player=player,
            snapshot=snapshot,
        )
        assert preview.requires_form and preview.minutes == 30
        assert preview.operation == "companion"
        assert (
            not IntentParserAgent(None)
            .preview(
                intent="一起走吧",
                player=player,
                snapshot=snapshot,
            )
            .requires_form
        )
        assert (
            c.execute(
                "SELECT count(*) FROM companion_sessions WHERE world_id=?", (e.wid,)
            ).fetchone()[0]
            == 0
        )


def test_worker_stops_at_companion_expiry_boundary(town):
    from world_engine.collective_clock import next_boundary
    from world_engine.repository import from_iso

    e = town
    willing(e)
    session = invite(e, 5)
    with e.db.read() as c:
        assert next_boundary(c, e.wid, e.at) == from_iso(session["until_world_time"])


def test_npc_own_departure_keeps_leader_steering_and_ends_own_companionship_atomically(town):
    from world_engine.player_steering import PlayerSteeringService

    e = town
    willing(e)
    session = invite(e)
    response = e.client.post(
        f"/api/worlds/{e.wid}/player/steer",
        json={"dx": 1, "dy": 0, "sequence": 100},
    )
    assert response.status_code == 200, response.text
    with e.db.write() as c:
        MovementService().start(
            c,
            world_id=e.wid,
            character_id=e.npc,
            destination_longitude=e.loc["longitude"] + 0.01,
            destination_latitude=e.loc["latitude"],
            world_time=e.at,
        )
        assert (
            c.execute(
                "SELECT status FROM companion_sessions WHERE id=?",
                (session["id"],),
            ).fetchone()[0]
            == "ended"
        )
        assert PlayerSteeringService.active(c, e.player) is not None
        assert (
            c.execute(
                "SELECT count(*) FROM character_movements WHERE character_id=? AND status='moving'",
                (e.npc,),
            ).fetchone()[0]
            == 1
        )


def test_invalid_npc_departure_keeps_existing_agreement_and_direction(town):
    from world_engine.player_steering import PlayerSteeringService

    e = town
    willing(e)
    session = invite(e)
    e.client.post(f"/api/worlds/{e.wid}/player/steer", json={"dx": 1, "dy": 0, "sequence": 100})
    with e.db.write() as c:
        npc = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        with pytest.raises(ValueError):
            MovementService().start(
                c,
                world_id=e.wid,
                character_id=e.npc,
                destination_longitude=npc["longitude"],
                destination_latitude=npc["latitude"],
                world_time=e.at,
            )
        assert (
            c.execute(
                "SELECT status FROM companion_sessions WHERE id=?",
                (session["id"],),
            ).fetchone()[0]
            == "active"
        )
        assert PlayerSteeringService.active(c, e.player) is not None


def test_group_movement_is_visible_in_player_lived_event_history(town):
    e = town
    willing(e)
    invite(e)
    with e.db.write() as c:
        MovementService().start(
            c,
            world_id=e.wid,
            character_id=e.player,
            destination_longitude=e.loc["longitude"] + 0.01,
            destination_latitude=e.loc["latitude"],
            world_time=e.at,
        )
        MovementService().cancel(c, world_id=e.wid, character_id=e.player, world_time=e.at)
    view = e.client.get(f"/api/worlds/{e.wid}/player/experience").json()
    types = {event["event_type"] for event in view["events"]}
    assert "action.movement_started" in types
    assert "action.movement_cancelled" in types


def test_relationship_numbers_do_not_withdraw_person_own_explicit_consent(town):
    e = town
    willing(e)
    session = invite(e)
    with e.db.write() as c:
        c.execute(
            "INSERT OR REPLACE INTO relationships(world_id,source_character_id,"
            "target_character_id,affinity,trust,updated_at) VALUES (?,?,?,?,?,?)",
            (e.wid, e.npc, e.player, -15, -20, e.at.isoformat()),
        )
        CompanionService.maintain(c, e.wid, e.at)
        assert (
            c.execute(
                "SELECT status FROM companion_sessions WHERE id=?",
                (session["id"],),
            ).fetchone()[0]
            == "active"
        )
        npc = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        player = c.execute("SELECT * FROM characters WHERE id=?", (e.player,)).fetchone()
        CompanionProposalService.apply(
            c,
            npc,
            player,
            e.at,
            CompanionIntent(
                operation="end", session_id=session["id"], statement="我自己决定结束这份同行。"
            ),
        )
        assert (
            c.execute(
                "SELECT status FROM companion_sessions WHERE id=?", (session["id"],)
            ).fetchone()[0]
            == "ended"
        )
