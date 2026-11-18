"""哥哥，分房文字只表达本人选择，不从台词补出同意或进入对方房间。"""

# ruff: noqa: F811

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError
from test_appointment_rooms import door
from test_appointments import people
from test_journey_room_appointments import current_time, room_at_stop
from test_lived_world import town  # noqa: F401
from test_overnight_stays import stopped_long_journey

from world_engine.api_dialogue import NpcTextResult
from world_engine.decisions import NpcReply
from world_engine.domain import CharacterState
from world_engine.engine import WorldEngine
from world_engine.intent_parser import IntentParserAgent, IntentParseResult
from world_engine.npc_performance import settle_npc_reply
from world_engine.outreach import OutreachText
from world_engine.repository import from_iso
from world_engine.roleplay import npc_reply_system_prompt, salvage_npc_reply
from world_engine.separate_stays import SeparateStayIntent, SeparateStayService


class ParserBackend:
    def __init__(self, choice):
        self.choice = choice
        self.request = None

    def complete(self, **request):
        self.request = request
        return SimpleNamespace(data=self.choice)


def preview(choice, *, context=None, text=None, evidence=None, apart=False):
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
    if apart:
        player = player.model_copy(update={"current_room_id": "own-room"})
        npc = npc.model_copy(update={"current_room_id": "other-room"})
    text = text or choice.statement
    backend = ParserBackend(
        IntentParseResult(
            operation="separate_stay",
            target_character_id=npc.id,
            separate_stay_choice=choice,
            choice_evidence=evidence if evidence is not None else text,
        )
    )
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
def test_existing_separate_stay_response_routes_without_family_or_same_room_choice(operation):
    choice = SeparateStayIntent(
        operation=operation, stay_id="real-stay", statement="这是我本人对此次分房安排的明确选择。"
    )
    result, backend = preview(
        choice, context={"separate_stays": [{"id": "real-stay", "status": "pending"}]}
    )
    assert result.route_kind == "social_choice"
    assert result.separate_stay_choice == choice
    assert result.family_choice is None and result.overnight_choice is None
    assert "separate_stay_choice" in backend.request["system_prompt"]
    assert (
        backend.request["schema"]
        .validate_python(
            result.model_dump(
                include={
                    "operation",
                    "target_character_id",
                    "separate_stay_choice",
                    "choice_evidence",
                }
            )
        )
        .separate_stay_choice
        == choice
    )


@pytest.mark.parametrize("failure", ["unknown_stay", "forged_evidence", "forged_statement"])
def test_response_requires_known_separate_stay_and_exact_player_words(failure):
    text = "我明确接受这次分房休息八小时与醒后会合的安排。"
    choice = SeparateStayIntent(
        operation="accept",
        stay_id="real-stay",
        statement=text if failure != "forged_statement" else "我准许你进入我的房间。",
    )
    result, _ = preview(
        choice,
        text=text,
        evidence="不是原话的选择。" if failure == "forged_evidence" else None,
        context={
            "separate_stays": [{"id": "unknown-stay" if failure == "unknown_stay" else "real-stay"}]
        },
    )
    assert result.operation == "none" and result.separate_stay_choice is None


@pytest.mark.parametrize("operation", ["check_in", "cancel", "accept", "decline"])
def test_apart_participants_can_only_route_their_own_existing_check_in_or_exit(operation):
    choice = SeparateStayIntent(
        operation=operation, stay_id="real-stay", statement="这是我本人对原分房安排的明确选择。"
    )
    result, backend = preview(
        choice,
        apart=True,
        context={
            "separate_stays": [{"id": "real-stay", "status": "accepted"}],
            "separate_stay_only": True,
        },
    )
    assert (result.route_kind == "social_choice") is (operation in {"check_in", "cancel"})
    assert backend.request["user_payload"]["target_options"][0]["name"] == "已有分房安排的参与者"


@pytest.mark.parametrize(
    "failure",
    [None, "moving", "ended", "unknown_own_room", "unknown_other_room", "unknown_journey"],
)
def test_proposal_requires_both_known_rooms_and_an_actually_stopped_journey(failure):
    choice = SeparateStayIntent(
        operation="propose",
        journey_id="trip",
        own_room_id="my-room",
        other_room_id="your-room",
        arrival_world_time="2030-01-01T22:00:00Z",
        minutes=480,
        statement="我们暂停旅行，今晚十点各住已许可的两间客房休息八小时，醒后在到站室外会合。",
    )
    result, _ = preview(
        choice,
        context={
            "journeys": [
                {
                    "id": "unknown-trip" if failure == "unknown_journey" else "trip",
                    "status": "ended" if failure == "ended" else "active",
                    "phase": "moving" if failure == "moving" else "stopped",
                }
            ],
            "separate_stay_rooms": [
                {"id": "unknown-my-room" if failure == "unknown_own_room" else "my-room"},
                {"id": "unknown-your-room" if failure == "unknown_other_room" else "your-room"},
            ],
        },
    )
    assert (result.route_kind == "social_choice") is (failure is None)
    if failure is None:
        assert result.separate_stay_choice.own_room_id == "my-room"
        assert result.separate_stay_choice.other_room_id == "your-room"
        assert result.separate_stay_choice.reunion_window_minutes == 60


@pytest.mark.parametrize("schema", [NpcReply, NpcTextResult, OutreachText])
def test_dialogue_letter_and_outreach_preserve_explicit_separate_stay_intent(schema):
    raw = {
        "reply": "我接受这次分房安排，各自入自己的房间休息，醒后再会合。",
        "separate_stay_intent": {
            "operation": "accept",
            "stay_id": "actual-stay",
            "statement": "我接受这次分房安排。",
        },
    }
    result = (
        schema.model_validate(raw | {"should_contact": True})
        if schema is OutreachText
        else salvage_npc_reply(json.dumps(raw, ensure_ascii=False), TypeAdapter(schema))
    )
    assert result.separate_stay_intent.stay_id == "actual-stay"
    assert result.separate_stay_intent.operation == "accept"
    assert result.journey_intent is None and result.overnight_intent is None


@pytest.mark.parametrize("schema", [NpcReply, NpcTextResult])
def test_bare_acceptance_words_never_create_structured_separate_stay_consent(schema):
    result = salvage_npc_reply("我愿意分别住两间房，醒后会合。", TypeAdapter(schema))
    assert result.separate_stay_intent is None


@pytest.mark.parametrize(
    "patch",
    [
        {"minutes": 239},
        {"minutes": 721},
        {"minutes": True},
        {"window_minutes": 14},
        {"reunion_window_minutes": 121},
        {"allow_visit": True},
        {"operation": "accept", "stay_id": "old-stay"},
    ],
)
def test_schema_rejects_bad_duration_access_grants_or_rewritten_response(patch):
    with pytest.raises(ValidationError):
        IntentParseResult.model_validate(
            {
                "operation": "separate_stay",
                "separate_stay_choice": {
                    "operation": "propose",
                    "journey_id": "trip",
                    "own_room_id": "mine",
                    "other_room_id": "yours",
                    "arrival_world_time": "2030-01-01T22:00:00Z",
                    "statement": "我提出本人这次分房安排。",
                    **patch,
                },
            }
        )


def test_engine_preview_supplies_only_known_separate_stay_rooms_and_does_not_execute(town):
    e = town
    jid = stopped_long_journey(e)
    mine = room_at_stop(e, jid, owner=e.player, name="玩家独立客房")
    yours = room_at_stop(e, jid, owner=e.npc, name="旅伴独立客房")
    hidden = room_at_stop(e, jid, parent=yours, name="尚未知晓的深处私室")
    text = (
        "我们暂停旅行，今晚十点我住玩家独立客房，你住旅伴独立客房，各自休息八小时后在到站室外会合。"
    )
    backend = ParserBackend(
        IntentParseResult(
            operation="separate_stay",
            target_character_id=e.npc,
            choice_evidence=text,
            separate_stay_choice=SeparateStayIntent(
                operation="propose",
                journey_id=jid,
                own_room_id=mine,
                other_room_id=yours,
                arrival_world_time="2030-01-01T22:00:00Z",
                statement=text,
            ),
        )
    )
    engine = WorldEngine(e.db, e.settings)
    engine.intent_parser = IntentParserAgent(backend)
    result = engine.preview_player_intent(e.wid, "说话：" + text, target_character_id=e.npc)
    assert result.route_kind == "social_choice" and result.separate_stay_choice.own_room_id == mine
    context = backend.request["user_payload"]["social_context"][e.npc]
    assert {mine, yours}.issubset({r["id"] for r in context["separate_stay_rooms"]})
    assert hidden not in {r["id"] for r in context["separate_stay_rooms"]}
    with e.db.read() as c:
        assert not c.execute(
            "SELECT 1 FROM journey_separate_stays WHERE world_id=?", (e.wid,)
        ).fetchone()


def test_npc_settlement_routes_only_explicit_intent_to_service(town, monkeypatch):
    e = town
    calls = []

    def apply(c, actor, other, at, choice):
        calls.append((actor["id"], other["id"], choice))
        return {"status": "accepted", "event_id": None, "summary": "本人已接受分房提议。"}

    monkeypatch.setattr(SeparateStayService, "apply", apply)
    with e.db.write() as c:
        npc = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        base = dict(npc=npc, player_id=e.player, at=current_time(e), channel="letter")
        settle_npc_reply(c, **base, reply=NpcReply(reply="我愿意分房休息。"))
        assert not calls
        choice = SeparateStayIntent(
            operation="accept", stay_id="actual-stay", statement="我接受这次分房安排。"
        )
        settle_npc_reply(
            c, **base, reply=NpcReply(reply=choice.statement, separate_stay_intent=choice)
        )
    assert calls == [(e.npc, e.player, choice)]


def test_engine_self_check_in_uses_known_arrangement_when_partner_is_in_another_room(town):
    e = town
    jid = stopped_long_journey(e)
    mine = room_at_stop(e, jid, owner=e.player, name="本人单独客房")
    yours = room_at_stop(e, jid, owner=e.npc, name="伙伴单独客房")
    with e.db.write() as c:
        player, npc = people(e, c)
        sid = SeparateStayService.apply(
            c,
            player,
            npc,
            current_time(e),
            SeparateStayIntent(
                operation="propose",
                journey_id=jid,
                own_room_id=mine,
                other_room_id=yours,
                arrival_world_time="2030-01-01T22:00:00Z",
                statement="我们今晚十点各住自己的客房八小时，醒后会合。",
            ),
        )["stay_id"]
        SeparateStayService.apply(
            c,
            npc,
            player,
            current_time(e),
            SeparateStayIntent(operation="accept", stay_id=sid, statement="我接受这次分房安排。"),
        )
    assert door(e, mine, "open").status_code == 200
    assert door(e, mine, "enter").status_code == 200
    text = "我已经进入自己约定的房间，现在明确开始这次分房休息。"
    backend = ParserBackend(
        IntentParseResult(
            operation="separate_stay",
            target_character_id=e.npc,
            separate_stay_choice=SeparateStayIntent(
                operation="check_in", stay_id=sid, statement=text
            ),
            choice_evidence=text,
        )
    )
    engine = WorldEngine(e.db, e.settings)
    engine.intent_parser = IntentParserAgent(backend)
    try:
        result = engine.preview_player_intent(e.wid, text, target_character_id=e.npc)
        assert result.route_kind == "social_choice" and result.separate_stay_choice.stay_id == sid
        context = backend.request["user_payload"]["social_context"][e.npc]
        assert set(context) == {"separate_stays", "separate_stay_only", "journeys", "journey_only"}
        assert context["journey_only"] is True
        assert context["journeys"] and all(
            {row["proposer_id"], row["recipient_id"]} == {e.player, e.npc}
            for row in context["journeys"]
        )
        assert context["separate_stay_only"] is True
        assert context["separate_stays"][0]["id"] == sid
    finally:
        engine.close()


@pytest.mark.parametrize("operation", ["cancel", "accept"])
def test_departed_player_can_end_met_reunion_wait_without_reopening_remote_dialogue(
    town, operation
):
    from test_separate_stays import begin, get, npc_perform, settle, setup

    e = town
    _, own, other, fields = setup(e)
    sid, start = begin(e, fields, own, other)
    settle(e, start + timedelta(hours=8))
    assert npc_perform(e, start + timedelta(hours=8, minutes=5)) == "离开房间"
    assert door(e, own, "exit").status_code == 200
    reunion = from_iso(get(e, sid)["reunion_starts_at"])
    settle(e, reunion)
    assert get(e, sid)["reunion_status"] == "met"
    # 哥哥，玩家真实离开室外会合点进自己的房间，伙伴仍留在室外等候。
    assert door(e, own, "enter").status_code == 200
    with e.db.read() as c:
        player, npc = people(e, c)
        assert player["current_room_id"] == own and npc["current_room_id"] is None
    text = "我们已实际会合，我现在明确结束本次会合之后的剩余等候。"
    backend = ParserBackend(
        IntentParseResult(
            operation="separate_stay",
            target_character_id=e.npc,
            separate_stay_choice=SeparateStayIntent(
                operation=operation, stay_id=sid, statement=text
            ),
            choice_evidence=text,
        )
    )
    engine = WorldEngine(e.db, e.settings)
    engine.intent_parser = IntentParserAgent(backend)
    try:
        result = engine.preview_player_intent(e.wid, text, target_character_id=e.npc)
        context = backend.request["user_payload"]["social_context"][e.npc]
        assert set(context) == {"separate_stays", "separate_stay_only", "journeys", "journey_only"}
        assert context["journey_only"] is True
        assert context["journeys"] and all(
            {row["proposer_id"], row["recipient_id"]} == {e.player, e.npc}
            for row in context["journeys"]
        )
        assert context["separate_stay_only"] is True
        stay = next(row for row in context["separate_stays"] if row["id"] == sid)
        assert stay["status"] == "completed" and stay["reunion_status"] == "met"
        assert stay["can_end_reunion_wait"] is True
        assert (result.route_kind == "social_choice") is (operation == "cancel")
        if operation == "cancel":
            response = e.client.post(
                f"/api/worlds/{e.wid}/player/social-choices",
                json={
                    "request_id": str(uuid4()),
                    "recipient_id": e.npc,
                    "intent": text,
                    "evidence": result.choice_evidence,
                    "separate_stay_choice": result.separate_stay_choice.model_dump(mode="json"),
                },
            )
            assert response.status_code == 200, response.text
            row = get(e, sid)
            assert row["status"] == "completed" and row["reunion_status"] == "met"
            assert row["reunion_wait_ended_at"]
        else:
            assert result.separate_stay_choice is None
            assert get(e, sid)["reunion_wait_ended_at"] is None
    finally:
        engine.close()


def test_npc_prompt_explains_room_assignment_self_check_in_and_actual_reunion():
    prompt = npc_reply_system_prompt()
    assert "separate_stay_intent" in prompt and "own分给本人、other分给对方" in prompt
    assert "NPC自主开始本人休息，玩家check_in只管本人" in prompt
    assert "会合窗内各自离屋到原到站室外会合才完成" in prompt
    assert "不自动订房付费，恢复旅行另议" in prompt
