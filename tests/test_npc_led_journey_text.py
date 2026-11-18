"""哥哥，答应旅行、选择跟随和实际带路分别由本人决定。"""

# ruff: noqa: F811
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_journeys import choose, get, offer
from test_lived_world import town  # noqa: F401

from world_engine.api_dialogue import NpcTextResult
from world_engine.api_intimacy import NaturalSocialChoice, compatible_choice_digests
from world_engine.decisions import NpcReply
from world_engine.domain import CharacterState
from world_engine.engine import WorldEngine
from world_engine.intent_parser import IntentParseResult, IntentParserAgent
from world_engine.journeys import JourneyIntent
from world_engine.npc_performance import settle_npc_reply
from world_engine.outreach import OutreachText


def preview(choice, context, *, apart=False, evidence=None):
    player = CharacterState(id="player", world_id="world", name="旅人", location_id="place", is_player=True,
                            energy=90, satiety=100, money=50, current_room_id="mine" if apart else None)
    npc = player.model_copy(update={"id": "npc", "name": "伙伴", "is_player": False,
                                   "current_room_id": "yours" if apart else None})

    class Backend:
        def complete(self, **request):
            return SimpleNamespace(data=IntentParseResult(
                operation="journey", target_character_id="npc", journey_choice=choice,
                choice_evidence=evidence if evidence is not None else choice.statement,
            ))

    return IntentParserAgent(Backend()).preview(
        intent="我本人明确选择这份旅行安排。", player=player,
        snapshot=SimpleNamespace(characters=[player, npc], world=SimpleNamespace(current_time=datetime(2030, 1, 1, tzinfo=UTC))),
        preferred_target_id="npc", social_context={"npc": context},
    )


def choice_for(operation, **extra):
    fields = dict(journey_id="journey")
    if operation == "propose":
        fields = dict(meeting_location_id="place", deadline_world_time="2030-01-01T04:00:00+00:00",
                      stops=[{"location_id": "known", "minutes": 2}])
    return JourneyIntent(operation=operation, statement="我本人明确选择这份旅行安排。", **fields, **extra)


@pytest.mark.parametrize("leader", [None, "player", "npc", "outsider"])
@pytest.mark.parametrize("known_place", [True, False])
def test_parser_new_journey_only_uses_known_places_and_actual_pair_leader(leader, known_place):
    result = preview(choice_for("propose", leader_id=leader), {
        "journey_places": [{"id": "place"}, {"id": "known" if known_place else "unknown"}],
    })
    assert (result.route_kind == "social_choice") is (known_place and leader != "outsider")


@pytest.mark.parametrize("leader", [None, "player", "npc", "outsider"])
@pytest.mark.parametrize("can_follow", [False, True])
def test_parser_follow_requires_actual_npc_leader_and_current_follow_option(leader, can_follow):
    result = preview(choice_for("follow"), {"journeys": [
        {"id": "journey", "leader_id": leader, "can_follow": can_follow},
    ]})
    assert (result.route_kind == "social_choice") is (leader == "npc" and can_follow)


@pytest.mark.parametrize("operation", ["accept", "follow", "withdraw_follow", "pause", "resume", "end"])
@pytest.mark.parametrize("failure", ["unknown_journey", "forged_evidence", "forged_statement"])
def test_parser_journey_requires_real_shared_id_and_player_words(operation, failure):
    choice = choice_for(operation)
    if failure == "forged_statement":
        choice = choice.model_copy(update={"statement": "另一段并非玩家原话。"})
    result = preview(choice, {"journeys": [{
        "id": "other" if failure == "unknown_journey" else "journey",
        "leader_id": "npc", "can_follow": True,
    }]}, evidence="这并不是玩家原话。" if failure == "forged_evidence" else "我本人明确选择这份旅行安排。")
    assert result.route_kind != "social_choice"


@pytest.mark.parametrize("operation", ["propose", "accept", "decline", "depart", "follow", "withdraw_follow", "pause", "resume", "end"])
def test_apart_journey_only_allows_own_withdraw_pause_and_end(operation):
    result = preview(choice_for(operation), {
        "journey_only": True, "journey_places": [{"id": "place"}, {"id": "known"}],
        "journeys": [{"id": "journey", "leader_id": "npc", "can_follow": True}],
    }, apart=True)
    assert (result.route_kind == "social_choice") is (operation in {"withdraw_follow", "pause", "end"})


@pytest.mark.parametrize("schema", [NpcReply, NpcTextResult, OutreachText])
def test_all_npc_channels_preserve_declared_leader_and_pause(schema):
    raw = {"reply": "我可以带你走这条有限路线。", "journey_intent": choice_for("propose", leader_id="npc").model_dump()}
    if schema is OutreachText:
        raw["should_contact"] = True
    assert schema.model_validate(raw).journey_intent.leader_id == "npc"
    raw["journey_intent"] = choice_for("pause").model_dump()
    assert schema.model_validate(raw).journey_intent.operation == "pause"


def test_api_acceptance_and_bare_npc_reply_do_not_authorize_follow(town):
    e = town
    jid = offer(e, leader_id=e.npc)
    assert choose(e, "accept", jid).status_code == 200
    with e.db.write() as c:
        npc = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        settle_npc_reply(c, npc=npc, player_id=e.player, at=e.at, channel="in_person",
                        reply=NpcReply(reply="那就跟我走吧。"))
        assert not c.execute("SELECT 1 FROM character_movements WHERE status='moving'").fetchone()
        assert get(e, jid)["follow_authorized_by"] is None
    assert choose(e, "depart", jid).status_code == 409
    result = e.client.get(f"/api/worlds/{e.wid}/journeys?character_id={e.npc}")
    assert result.status_code == 200
    shared = next(row for row in result.json() if row["id"] == jid)
    assert shared["can_follow"] and not shared["following"]
    assert "follow_terms_json" not in shared


def test_api_follow_has_original_words_stable_receipt_and_no_implicit_movement(town):
    e = town
    jid = offer(e, leader_id=e.npc)
    assert choose(e, "accept", jid).status_code == 200
    text = "我本人选择跟随你带领这份已确认的有限路线，暂停后重新选择。"
    payload = dict(request_id=str(uuid4()), recipient_id=e.npc, intent=text, evidence=text,
                   journey_choice=dict(operation="follow", journey_id=jid, statement="候选改写不作为授权原话。"))
    path = f"/api/worlds/{e.wid}/player/social-choices"
    response = e.client.post(path, json=payload)
    assert response.status_code == 200, response.text
    result = response.json()
    assert e.client.post(path, json=payload).json() == result
    with e.db.read() as c:
        row = get(e, jid)
        assert row["follow_authorized_by"] == e.player
        event = c.execute("SELECT * FROM world_events WHERE id=?", (result["event_id"],)).fetchone()
        recorded = json.loads(event["payload_json"])
        assert recorded["choice_evidence"] == recorded["player_input"] == text
        assert recorded["follow_terms"]["leader_id"] == e.npc
        assert not c.execute("SELECT 1 FROM character_movements WHERE status='moving'").fetchone()
        assert c.execute("SELECT COUNT(*) FROM world_events WHERE event_type='social.appointment_journey_follow'").fetchone()[0] == 1
    assert choose(e, "withdraw_follow", jid).status_code == 200
    assert e.client.post(path, json=payload).json() == result
    assert get(e, jid)["follow_authorized_by"] is None
    assert get(e, jid)["status"] == "paused"


def test_legacy_receipt_empty_leader_compatible_but_real_leader_change_conflicts():
    raw = dict(request_id=str(uuid4()), recipient_id="npc", intent="我选择这份行程。", evidence="我选择这份行程。",
               journey_choice=dict(operation="propose", meeting_location_id="place", deadline_world_time="2030-01-01T04:00:00+00:00",
                                   stops=[{"location_id": "known", "minutes": 2}], statement="我选择这份行程。"))
    payload = NaturalSocialChoice.model_validate(raw)
    legacy = payload.model_dump(mode="json")
    legacy["journey_choice"].pop("leader_id")
    legacy.pop("companion_choice")
    digest = hashlib.sha256(json.dumps(legacy, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    assert digest in compatible_choice_digests(payload)
    raw["journey_choice"]["leader_id"] = "npc"
    assert digest not in compatible_choice_digests(NaturalSocialChoice.model_validate(raw))


def test_remote_engine_preview_can_withdraw_existing_follow_without_remote_talk(town):
    e = town
    jid = offer(e, leader_id=e.npc)
    choose(e, "accept", jid)
    assert choose(e, "follow", jid).status_code == 200
    with e.db.write() as c:
        c.execute("UPDATE characters SET longitude=longitude+0.01 WHERE id=?", (e.npc,))
    text = "我本人撤回这份已有行程的跟随授权。"

    class Backend:
        def complete(self, **request):
            return SimpleNamespace(data=IntentParseResult(
                operation="journey", target_character_id=e.npc, choice_evidence=text,
                journey_choice=JourneyIntent(operation="withdraw_follow", journey_id=jid, statement=text),
            ))

    engine = WorldEngine(e.db, e.settings, e.model)
    engine.intent_parser = IntentParserAgent(Backend())
    try:
        result = engine.preview_player_intent(e.wid, text, target_character_id=e.npc)
        assert result.route_kind == "social_choice"
        assert result.journey_choice.operation == "withdraw_follow"
        assert get(e, jid)["follow_authorized_by"] == e.player
    finally:
        engine.close()
