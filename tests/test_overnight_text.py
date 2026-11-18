"""哥哥，住宿文本只能表达本人有原话依据的选择，并引用真正可见的安排。"""

# ruff: noqa: F811

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import TypeAdapter, ValidationError
from test_journey_room_appointments import room_at_stop
from test_lived_world import town  # noqa: F401
from test_overnight_stays import get, npc_offer, stopped_long_journey

from world_engine.api_dialogue import NpcTextResult
from world_engine.decisions import NpcReply
from world_engine.domain import CharacterState
from world_engine.engine import WorldEngine
from world_engine.intent_parser import IntentParserAgent, IntentParseResult
from world_engine.outreach import OutreachText
from world_engine.overnight import OvernightIntent
from world_engine.roleplay import npc_reply_system_prompt, salvage_npc_reply


class ParserBackend:
    def __init__(self, choice):
        self.choice = choice
        self.request = None

    def complete(self, **request):
        self.request = request
        return SimpleNamespace(data=self.choice)


def preview(choice, *, context=None, text=None, evidence=None):
    player = CharacterState(
        id="player",
        world_id="world",
        name="旅人",
        location_id="stop",
        is_player=True,
        energy=80,
        satiety=100,
        money=10,
    )
    npc = player.model_copy(update={"id": "npc", "name": "旅伴", "is_player": False})
    text = text or choice.statement
    parsed = IntentParseResult(
        operation="overnight",
        target_character_id=npc.id,
        overnight_choice=choice,
        choice_evidence=evidence if evidence is not None else text,
    )
    backend = ParserBackend(parsed)
    result = IntentParserAgent(backend).preview(
        intent=text,
        player=player,
        snapshot=SimpleNamespace(
            characters=[player, npc],
            world=SimpleNamespace(current_time=datetime(2030, 1, 1, tzinfo=UTC)),
        ),
        preferred_target_id=npc.id,
        social_context={npc.id: context or {}},
    )
    return result, backend


@pytest.mark.parametrize("operation", ["accept", "decline", "cancel", "check_in"])
def test_existing_stay_response_routes_to_overnight_without_family_fields(operation):
    choice = OvernightIntent(
        operation=operation, stay_id="real-stay", statement="这是我本人对这次住宿的明确选择。"
    )
    result, backend = preview(
        choice, context={"overnights": [{"id": "real-stay", "status": "pending"}]}
    )
    assert result.route_kind == "social_choice"
    assert result.overnight_choice == choice
    assert result.family_choice is None and result.journey_choice is None
    assert "overnight_choice" in backend.request["system_prompt"]
    assert (
        backend.request["schema"]
        .validate_python(
            result.model_dump(
                include={
                    "operation",
                    "target_character_id",
                    "overnight_choice",
                    "choice_evidence",
                }
            )
        )
        .overnight_choice
        == choice
    )


@pytest.mark.parametrize("failure", ["unknown_stay", "forged_evidence", "forged_statement"])
def test_response_without_known_stay_and_exact_player_words_stays_dialogue(failure):
    choice = OvernightIntent(
        operation="accept",
        stay_id="real-stay",
        statement="我明确接受这次八小时住宿。"
        if failure != "forged_statement"
        else "我同意为你打开房门。",
    )
    result, _ = preview(
        choice,
        text="我明确接受这次八小时住宿。",
        evidence="这不是玩家说过的话。" if failure == "forged_evidence" else None,
        context={
            "overnights": [
                {"id": "someone-elses-stay" if failure == "unknown_stay" else "real-stay"}
            ]
        },
    )
    assert result.operation == "none" and result.route_kind == "world"
    assert result.overnight_choice is None


@pytest.mark.parametrize("failure", [None, "moving", "ended", "unknown_room", "unknown_journey"])
def test_proposal_requires_a_known_room_and_actually_stopped_journey(failure):
    choice = OvernightIntent(
        operation="propose",
        journey_id="trip",
        room_id="visible-room",
        arrival_world_time="2030-01-01T22:00:00Z",
        minutes=480,
        statement="我们暂停这次旅行，今晚十点在这间客房休息八小时，各自处理进屋和房租。",
    )
    result, _ = preview(
        choice,
        context={
            "journeys": [
                {
                    "id": "other-trip" if failure == "unknown_journey" else "trip",
                    "status": "ended" if failure == "ended" else "active",
                    "phase": "moving" if failure == "moving" else "stopped",
                }
            ],
            "overnight_rooms": [
                {"id": "hidden-room" if failure == "unknown_room" else "visible-room"}
            ],
        },
    )
    assert (result.route_kind == "social_choice") is (failure is None)
    if failure is None:
        assert result.overnight_choice.allow_visit is False
        assert result.overnight_choice.minutes == 480


@pytest.mark.parametrize("schema", [NpcReply, NpcTextResult, OutreachText])
def test_dialogue_letter_and_outreach_keep_independent_overnight_response(schema):
    raw = {
        "reply": "我接受这次八小时住宿，仍等我们各自走进房间后再开始。",
        "overnight_intent": {
            "operation": "accept",
            "stay_id": "actual-stay",
            "statement": "我接受这次八小时住宿。",
        },
    }
    if schema is OutreachText:
        result = schema.model_validate(raw | {"should_contact": True})
    else:
        result = salvage_npc_reply(json.dumps(raw, ensure_ascii=False), TypeAdapter(schema))
    assert result.overnight_intent.operation == "accept"
    assert result.overnight_intent.stay_id == "actual-stay"
    assert result.overnight_intent.allow_visit is False
    assert result.journey_intent is None and result.appointment_intent is None


@pytest.mark.parametrize(
    "patch",
    [
        {"minutes": 180},
        {"minutes": 721},
        {"minutes": True},
        {"window_minutes": 14},
        {"allow_visit": "true"},
        {"operation": "accept", "stay_id": "old-stay"},
    ],
)
def test_parser_schema_rejects_invalid_duration_or_rewritten_stay_response(patch):
    with pytest.raises(ValidationError):
        IntentParseResult.model_validate(
            {
                "operation": "overnight",
                "overnight_choice": {
                    "operation": "propose",
                    "journey_id": "trip",
                    "room_id": "room",
                    "arrival_world_time": "2030-01-01T22:00:00Z",
                    "statement": "我邀请本次住宿。",
                    **patch,
                },
            }
        )


def test_engine_preview_supplies_real_stay_and_visible_room_without_accepting_it(town):
    e = town
    jid = stopped_long_journey(e)
    rid = room_at_stop(e, jid)
    hidden_rid = room_at_stop(e, jid, parent=rid, name="尚未走到的内室")
    sid = npc_offer(e, jid, rid)
    text = "我明确接受这次八小时住宿，房间访问许可仍按原约定。"
    backend = ParserBackend(
        IntentParseResult(
            operation="overnight",
            target_character_id=e.npc,
            choice_evidence=text,
            overnight_choice=OvernightIntent(operation="accept", stay_id=sid, statement=text),
        )
    )
    engine = WorldEngine(e.db, e.settings)
    engine.intent_parser = IntentParserAgent(backend)
    result = engine.preview_player_intent(e.wid, "说话：" + text, target_character_id=e.npc)
    assert result.route_kind == "social_choice"
    assert result.overnight_choice.stay_id == sid
    context = backend.request["user_payload"]["social_context"][e.npc]
    assert any(row["id"] == sid for row in context["overnights"])
    assert any(row["id"] == rid for row in context["overnight_rooms"])
    assert not any(row["id"] == hidden_rid for row in context["overnight_rooms"])
    assert get(e, sid)["status"] == "pending"


def test_npc_prompt_explains_long_rest_as_independent_consent_and_real_check_in():
    prompt = npc_reply_system_prompt()
    assert "overnight_intent" in prompt and "240至720分钟" in prompt
    assert "仅玩家check_in" in prompt and "房租、进门和恢复旅行另行处理" in prompt
    assert "同屋不授予同床或接触权" in prompt
