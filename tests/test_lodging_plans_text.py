"""哥哥，订房文字保持本人预算、准确报价及旧安排操作的边界。"""

# ruff: noqa: F811

import copy
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import TypeAdapter
from test_appointments import people
from test_journey_room_appointments import current_time, room_at_stop
from test_lived_world import town  # noqa: F401
from test_lodging_plans import accept as accept_lodging
from test_lodging_plans import choose as choose_lodging
from test_lodging_plans import setup as setup_lodging
from test_overnight_stays import stopped_long_journey

from world_engine.api_dialogue import NpcTextResult
from world_engine.decisions import NpcReply
from world_engine.domain import CharacterState
from world_engine.engine import WorldEngine
from world_engine.intent_parser import IntentParserAgent, IntentParseResult
from world_engine.lodging_plans import LodgingPlanIntent, LodgingPlanService
from world_engine.npc_performance import settle_npc_reply
from world_engine.outreach import OutreachText
from world_engine.repository import to_iso
from world_engine.roleplay import npc_reply_system_prompt, salvage_npc_reply
from world_engine.roleplay_pipeline import build_npc_prompt


class Backend:
    def __init__(self, choice):
        self.choice = choice

    def complete(self, **request):
        self.request = request
        return SimpleNamespace(data=self.choice)


def preview(choice, context, *, apart=False, text=None, evidence=None):
    player = CharacterState(
        id="player",
        world_id="world",
        name="旅人",
        location_id="stop",
        is_player=True,
        energy=80,
        satiety=100,
        money=30,
    )
    npc = player.model_copy(update={"id": "npc", "is_player": False})
    if apart:
        player = player.model_copy(update={"current_room_id": "mine"})
        npc = npc.model_copy(update={"current_room_id": "yours"})
    text = text or choice.statement
    backend = Backend(
        IntentParseResult(
            operation="lodging_plan",
            target_character_id="npc",
            lodging_plan_choice=choice,
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
        preferred_target_id="npc",
        social_context={"npc": context},
    )
    return result, backend


@pytest.mark.parametrize("operation", ["accept", "decline", "cancel", "quote", "report_ready"])
def test_known_plan_routes_own_exact_choice_without_family_fallback(operation):
    choice = LodgingPlanIntent(
        operation=operation, plan_id="plan", statement="这是我本人对这份订房筹备的明确选择。"
    )
    result, backend = preview(choice, {"lodging_plans": [{"id": "plan"}]})
    assert result.route_kind == "social_choice" and result.lodging_plan_choice == choice
    assert result.family_choice is None and result.separate_stay_choice is None
    assert "预算当价格" in backend.request["system_prompt"]


@pytest.mark.parametrize("failure", ["unknown", "statement", "evidence"])
def test_plan_response_cannot_invent_known_id_or_player_authorization(failure):
    choice = LodgingPlanIntent(
        operation="quote",
        plan_id="plan",
        statement="我授权本人询价。"
        if failure == "statement"
        else "我明确询问自己房间的准确报价。",
    )
    result, _ = preview(
        choice,
        {"lodging_plans": [{"id": "unknown" if failure == "unknown" else "plan"}]},
        text="我明确询问自己房间的准确报价。",
        evidence="替玩家改写" if failure == "evidence" else None,
    )
    assert result.operation == "none"


@pytest.mark.parametrize(
    "operation", ["accept", "decline", "cancel", "quote", "confirm", "report_ready"]
)
def test_apart_only_routes_personal_old_plan_operations(operation):
    fields = {"quote_id": "own-quote", "expected_total": 6} if operation == "confirm" else {}
    choice = LodgingPlanIntent(
        operation=operation,
        plan_id="plan",
        statement="我本人确认这份已有订房选择，准确报价六枚。",
        **fields,
    )
    result, _ = preview(
        choice,
        {
            "lodging_plan_only": True,
            "lodging_plans": [
                {
                    "id": "plan",
                    "roles": [
                        {"character_id": "player", "quote_id": "own-quote", "quote": {"total": 6}}
                    ],
                }
            ],
        },
        apart=True,
    )
    assert (result.route_kind == "social_choice") is (
        operation in {"quote", "confirm", "report_ready", "cancel"}
    )


@pytest.mark.parametrize("failure", [None, "partner_quote", "budget_as_total", "missing_quote"])
def test_payment_preview_binds_own_visible_accurate_quote(failure):
    choice = LodgingPlanIntent(
        operation="confirm",
        plan_id="plan",
        quote_id="partner-quote" if failure == "partner_quote" else "own-quote",
        expected_total=30 if failure == "budget_as_total" else 6,
        statement="我已核对自己房间六枚的报价，明确确认付款。",
    )
    own = {"character_id": "player", "quote_id": "own-quote", "quote": {"total": 6}}
    if failure == "missing_quote":
        own.pop("quote")
    result, _ = preview(
        choice,
        {
            "lodging_plans": [
                {
                    "id": "plan",
                    "roles": [
                        own,
                        {"character_id": "npc", "quote_id": "partner-quote", "quote": {"total": 6}},
                    ],
                }
            ]
        },
    )
    assert (result.route_kind == "social_choice") is (failure is None)


@pytest.mark.parametrize("failure", [None, "unknown_room", "moving", "unknown_trip"])
def test_new_plan_requires_both_known_rooms_and_stopped_actual_journey(failure):
    choice = LodgingPlanIntent(
        operation="propose",
        journey_id="trip",
        own_room_id="mine",
        other_room_id="yours",
        own_budget=12,
        other_budget=18,
        ready_by_world_time="2030-01-01T21:00:00Z",
        required_until_world_time="2030-01-02T10:00:00Z",
        statement="我提议我负责甲房预算十二枚，你负责乙房预算十八枚，今晚九点回原站准备好，两房许可到明天十点。",
    )
    result, _ = preview(
        choice,
        {
            "journeys": [
                {
                    "id": "other-trip" if failure == "unknown_trip" else "trip",
                    "status": "paused",
                    "phase": "moving" if failure == "moving" else "stopped",
                }
            ],
            "lodging_room_options": [
                {"id": "mine"},
                {"id": "hidden" if failure == "unknown_room" else "yours"},
            ],
        },
    )
    assert (result.route_kind == "social_choice") is (failure is None)


@pytest.mark.parametrize("schema", [NpcReply, NpcTextResult, OutreachText])
def test_dialogue_letter_outreach_keep_structured_plan_consent(schema):
    raw = {
        "reply": "我接受自己十八枚的预算和房间分工，保留生活储备，各自订房。",
        "lodging_plan_intent": {
            "operation": "accept",
            "plan_id": "plan",
            "statement": "我接受自己这份预算和订房分工。",
        },
    }
    result = (
        schema.model_validate(raw | {"should_contact": True})
        if schema is OutreachText
        else salvage_npc_reply(json.dumps(raw, ensure_ascii=False), TypeAdapter(schema))
    )
    assert result.lodging_plan_intent.plan_id == "plan"
    assert result.separate_stay_intent is None


@pytest.mark.parametrize("schema", [NpcReply, NpcTextResult])
def test_acceptance_words_alone_never_become_authorized_booking(schema):
    assert (
        salvage_npc_reply("我愿意用自己的预算订房。", TypeAdapter(schema)).lodging_plan_intent
        is None
    )


def test_prompt_retains_current_budgets_own_quote_and_two_rooms_in_original_budget():
    plan = {
        "id": "plan",
        "status": "accepted",
        "roles": [
            {
                "character_id": "npc",
                "room_id": "mine",
                "spending_limit": 18,
                "spent": 0,
                "status": "offer_ready",
                "quote_id": "quote",
                "quote": {
                    "id": "quote",
                    "total": 6,
                    "duration_days": 1,
                    "status": "open",
                    "expires_at": "2030-01-01T22:00:00Z",
                },
            },
            {
                "character_id": "player",
                "room_id": "yours",
                "spending_limit": 12,
                "status": "unreported",
            },
        ],
    }
    context = {
        "player_text": "我们各自的订房准备怎么样？",
        "intimacy": {
            "lodging_plans": [plan],
            "lodging_room_options": [
                {"id": "mine", "name": "本人房间"},
                {"id": "yours", "name": "旅伴房间"},
            ],
            "journeys": [],
            "shared_plans": [],
            "overnights": [],
            "separate_stays": [
                {
                    "id": "old",
                    "status": "completed",
                    "reunion_status": "met",
                    "statement": "过去已完成的原话。" * 6000,
                }
            ],
        },
    }
    before = copy.deepcopy(context)
    prompt = build_npc_prompt(npc_reply_system_prompt(), context, max_input_tokens=8192)
    sent = json.loads(prompt.messages[1]["content"])["intimacy"]
    assert prompt.input_tokens <= 8192
    assert sent["lodging_plans"] == [plan]
    assert {r["id"] for r in sent["lodging_room_options"]} == {"mine", "yours"}
    assert not prompt.trace["intimacy.separate_stays:0"]["included"]
    assert context == before


def test_terminal_budget_selection_preserves_pending_reunion_obligation():
    stay = {"id": "stay", "status": "completed", "reunion_status": "waiting"}
    context = {"intimacy": {"separate_stays": [stay], "lodging_plans": []}}
    prompt = build_npc_prompt("保留当前义务。", context, max_input_tokens=200)
    assert json.loads(prompt.messages[1]["content"])["intimacy"]["separate_stays"] == [stay]


def test_new_journey_can_budget_terminal_stay_history_before_booking_plan_exists():
    journey = {"id": "new-trip", "status": "pending", "phase": "gathering"}
    context = {
        "player_text": "我们是否一起出发？",
        "intimacy": {
            "journeys": [journey],
            "lodging_plans": [],
            "separate_stays": [],
            "overnights": [
                {
                    "id": "past-stay",
                    "status": "completed",
                    "statement": "过去已经完成的住宿原话。" * 1000,
                }
            ],
        },
    }
    before = copy.deepcopy(context)
    prompt = build_npc_prompt("只引用真实承诺。", context, max_input_tokens=300)
    sent = json.loads(prompt.messages[1]["content"])["intimacy"]
    assert sent["journeys"] == [journey]
    assert sent.get("overnights", []) == []
    assert not prompt.trace["intimacy.overnights:0"]["included"]
    assert before == context


def test_settlement_never_books_from_plain_words(monkeypatch):
    calls = []
    monkeypatch.setattr(
        LodgingPlanService,
        "apply",
        lambda *args: (
            calls.append(args)
            or {"status": "accepted", "event_id": None, "summary": "本人同意预算。"}
        ),
    )

    class Connection:
        def execute(self, *args):
            return SimpleNamespace(fetchone=lambda: {"id": "player", "world_id": "world"})

    base = dict(
        npc={"id": "npc", "world_id": "world"},
        player_id="player",
        at=datetime(2030, 1, 1, tzinfo=UTC),
        channel="letter",
    )
    settle_npc_reply(Connection(), **base, reply=NpcReply(reply="我愿意自己订房。"))
    assert not calls
    choice = LodgingPlanIntent(operation="accept", plan_id="plan", statement="我同意自己的预算。")
    settle_npc_reply(
        Connection(), **base, reply=NpcReply(reply=choice.statement, lodging_plan_intent=choice)
    )
    assert len(calls) == 1 and calls[0][-1] == choice


@pytest.mark.parametrize("failed", [False, True])
def test_npc_private_booking_receipt_never_leaks_into_partner_beat(monkeypatch, failed):
    def apply(*args):
        if failed:
            raise ValueError("本人钱包只剩十三枚，因孩子照护支出不足。")
        return {
            "status": "blocked",
            "event_id": "private-event",
            "summary": "本人实际报价十一枚，生活储备不足。",
        }

    monkeypatch.setattr(LodgingPlanService, "apply", apply)

    class Connection:
        def execute(self, *args):
            return SimpleNamespace(fetchone=lambda: {"id": "player", "world_id": "world"})

    result = settle_npc_reply(
        Connection(),
        npc={"id": "npc", "world_id": "world"},
        player_id="player",
        at=datetime(2030, 1, 1, tzinfo=UTC),
        channel="letter",
        reply=NpcReply(
            reply="我去问问自己的房间条件。",
            lodging_plan_intent=LodgingPlanIntent(
                operation="quote", plan_id="plan", statement="我自己询价。"
            ),
        ),
    )
    assert not result.stage_direction
    assert result.action_event_id is None
    assert result.action_status == "recorded"


@pytest.mark.parametrize("accepted", [False, True])
def test_actual_stopped_journey_keeps_budget_and_room_terms_in_8192(town, accepted):
    e = town
    jid = stopped_long_journey(e)
    mine = room_at_stop(e, jid, owner=e.player, name="旅人订房客室")
    yours = room_at_stop(e, jid, owner=e.npc, name="旅伴订房客室")
    hidden = room_at_stop(e, jid, owner=e.player, parent=mine, name="未经认识的深处房间")
    at = current_time(e)
    with e.db.write() as c:
        player, npc = people(e, c)
        result = LodgingPlanService.apply(
            c,
            player,
            npc,
            at,
            LodgingPlanIntent(
                operation="propose",
                journey_id=jid,
                own_room_id=mine,
                other_room_id=yours,
                own_budget=12,
                other_budget=18,
                required_until_world_time=to_iso(at + timedelta(hours=16)),
                ready_by_world_time=to_iso(at + timedelta(hours=2)),
                statement="我们各自负责自己的房间，我十二枚、你十八枚上限，保留生活储备，两小时内回到原站，房间许可覆盖十六小时后，后续休息另议。",
            ),
        )
        if accepted:
            LodgingPlanService.apply(
                c,
                npc,
                player,
                at,
                LodgingPlanIntent(
                    operation="accept",
                    plan_id=result["plan_id"],
                    statement="我接受自己十八枚的订房预算和分工。",
                ),
            )
    engine = WorldEngine(e.db, e.settings)
    try:
        with e.db.read() as c:
            snapshot = engine.repository.get_snapshot(c, e.wid)
            context = engine.dialogue_context.build(
                c,
                snapshot=snapshot,
                npc=snapshot.character_by_id(e.npc),
                player=snapshot.character_by_id(e.player),
                player_text="我们各自预算的订房准备，你有什么想法？",
                conversation=None,
                channel="in_person",
                interaction="只读核验真实住宿筹备上下文",
            )
        before = copy.deepcopy(context)
        prompt = build_npc_prompt(npc_reply_system_prompt(), context, max_input_tokens=8192)
        sent = json.loads(prompt.messages[1]["content"])["intimacy"]
        assert prompt.input_tokens <= 8192
        assert sent["lodging_plans"] == before["intimacy"]["lodging_plans"]
        assert sent["journeys"] == before["intimacy"]["journeys"]
        assert {mine, yours} <= {room["id"] for room in sent["lodging_room_options"]}
        assert hidden not in {room["id"] for room in sent["lodging_room_options"]}
        assert {role["spending_limit"] for role in sent["lodging_plans"][0]["roles"]} == {12, 18}
        assert context == before
    finally:
        engine.close()


def test_actual_private_accurate_quote_budget_and_room_allocation_remain_complete(town):
    e = town
    _, own, other, fields = setup_lodging(e)
    pid = accept_lodging(e, fields)
    quoted = choose_lodging(e, "quote", pid, npc=True)
    assert quoted["expected_total"] == 8
    engine = WorldEngine(e.db, e.settings)
    try:
        with e.db.read() as c:
            snapshot = engine.repository.get_snapshot(c, e.wid)
            context = engine.dialogue_context.build(
                c,
                snapshot=snapshot,
                npc=snapshot.character_by_id(e.npc),
                player=snapshot.character_by_id(e.player),
                player_text="你按自己确认的预算处理自己的房间，准备好后回站告诉我。",
                conversation=None,
                channel="in_person",
                interaction="只读核验本人准确报价与承诺预算",
            )
        before = copy.deepcopy(context)
        prompt = build_npc_prompt(npc_reply_system_prompt(), context, max_input_tokens=8192)
        sent = json.loads(prompt.messages[1]["content"])["intimacy"]
        assert sent["lodging_plans"] == before["intimacy"]["lodging_plans"]
        assert sent["journeys"] == before["intimacy"]["journeys"]
        assert {own, other} <= {room["id"] for room in sent["lodging_room_options"]}
        role = next(
            role for role in sent["lodging_plans"][0]["roles"] if role["character_id"] == e.npc
        )
        assert role["quote"]["id"] == quoted["quote_id"]
        assert role["quote"]["total"] == 8 and role["quote"]["status"] == "open"
        assert role["spending_limit"] == fields["other_budget"]
        assert context == before
    finally:
        engine.close()
