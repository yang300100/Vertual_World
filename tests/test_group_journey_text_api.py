"""哥哥，真实接口只在隔离存档验收，每名成员的结构化选择分别留下证据。"""

# ruff: noqa: F811

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_journeys import sites
from test_lived_world import town  # noqa: F401

from world_engine.actions import ActionService
from world_engine.api_dialogue import NpcTextResult
from world_engine.api_intimacy import NaturalSocialChoice, compatible_choice_digests
from world_engine.decisions import NpcReply
from world_engine.domain import CharacterState
from world_engine.engine import WorldEngine
from world_engine.group_journeys import GroupJourneyIntent, GroupJourneyService
from world_engine.intent_parser import IntentParserAgent, IntentParseResult
from world_engine.npc_performance import settle_npc_reply
from world_engine.outreach import OutreachText, _group_opportunity_current, process_outreach
from world_engine.repository import to_iso

WORDS = "我本人明确选择这份完整有限多人行程。"


@pytest.fixture
def trio(town):
    e = town
    e.route = sites(e)
    with e.db.write() as c:
        e.third = c.execute(
            "SELECT id FROM characters WHERE world_id=? AND is_player=0 AND id<>? ORDER BY rowid LIMIT 1",
            (e.wid, e.npc),
        ).fetchone()[0]
        c.execute(
            "UPDATE characters SET longitude=?,latitude=?,location_id=?,current_location_id=?,current_room_id=NULL,current_fixture_id=NULL,movement_speed_kmh=4,health=100,energy=95,satiety=95 WHERE id=?",
            (e.loc["longitude"], e.loc["latitude"], e.loc["id"], e.loc["id"], e.third),
        )
        for lid in e.route:
            ActionService._record_event(
                c,
                world_id=e.wid,
                tick_id=str(uuid4()),
                occurred_at=e.at - timedelta(days=2),
                event_type="action.socialize",
                actor_id=e.third,
                target_id=None,
                location_id=lid,
                summary="隔离验收本人过去到访此站。",
                payload={},
            )
    e.ids = [e.player, e.npc, e.third]
    return e


def proposal(e, **extra):
    return dict(
        operation="propose",
        member_ids=e.ids,
        leader_id=e.npc,
        meeting_location_id=e.loc["id"],
        deadline_world_time=to_iso(e.at + timedelta(hours=5)),
        stops=[{"location_id": e.route[0], "minutes": 2}],
        return_to_origin=True,
        separation_policy="pause",
        statement=WORDS,
        **extra,
    )


def post(e, choice, **extra):
    payload = dict(
        request_id=str(uuid4()),
        recipient_id=e.npc,
        intent=WORDS,
        evidence=WORDS,
        group_journey_choice=choice,
    )
    payload.update(extra)
    return e.client.post(f"/api/worlds/{e.wid}/player/social-choices", json=payload)


def rows(e):
    response = e.client.get(f"/api/worlds/{e.wid}/group-journeys")
    assert response.status_code == 200, response.text
    return response.json()


def answer(e, cid, jid, operation="accept"):
    e.model.respond_to_player = lambda **kwargs: NpcReply(
        reply="这是我本人对原完整名单、领队和路线的答复。",
        group_journey_intent=GroupJourneyIntent(
            operation=operation, journey_id=jid, statement="本人明确回应原完整条款。"
        ),
    )
    response = e.client.post(
        f"/api/worlds/{e.wid}/player/act",
        json={"intent": "说：请你本人独立回应这份完整行程。", "target_character_id": cid},
    )
    assert response.status_code == 201, response.text
    return response


def test_real_api_three_independent_choices_and_no_implicit_follow(trio):
    e = trio
    request_id = str(uuid4())
    response = post(e, proposal(e), request_id=request_id)
    assert response.status_code == 200, response.text
    jid = response.json()["journey_id"]
    assert [m["decision"] for m in rows(e)[0]["members"]] == ["accepted", "pending", "pending"]
    answer(e, e.npc, jid)
    assert rows(e)[0]["status"] == "pending"
    assert [m["decision"] for m in rows(e)[0]["members"]] == ["accepted", "accepted", "pending"]
    answer(e, e.third, jid)
    shared = rows(e)[0]
    assert shared["status"] == "accepted" and shared["can_follow"] and not shared["following"]
    assert post(e, proposal(e), request_id=request_id).json() == response.json()
    with e.db.read() as c:
        assert not c.execute(
            "SELECT 1 FROM character_movements WHERE world_id=? AND status='moving'", (e.wid,)
        ).fetchone()
        event = json.loads(
            c.execute(
                "SELECT payload_json FROM world_events WHERE id=?", (response.json()["event_id"],)
            ).fetchone()[0]
        )
        assert event["player_input"] == event["choice_evidence"] == WORDS
        assert (
            len(
                {
                    m[0]
                    for m in c.execute(
                        "SELECT decision_event_id FROM group_journey_members WHERE journey_id=?",
                        (jid,),
                    )
                }
            )
            == 3
        )


def test_api_receipt_locks_complete_members_leader_route_and_original_words(trio):
    e = trio
    rid = str(uuid4())
    choice = proposal(e)
    response = post(e, choice, request_id=rid)
    assert response.status_code == 200
    for field, value in (
        ("member_ids", [e.player, e.npc]),
        ("leader_id", e.player),
        ("separation_policy", "continue"),
        ("stops", [{"location_id": e.route[1], "minutes": 2}]),
    ):
        assert post(e, {**choice, field: value}, request_id=rid).status_code == 409
    assert post(e, choice, evidence="不是玩家本次原话").status_code == 422
    with e.db.read() as c:
        assert (
            c.execute("SELECT COUNT(*) FROM group_journeys WHERE world_id=?", (e.wid,)).fetchone()[
                0
            ]
            == 1
        )


def test_npc_actual_dialogue_can_propose_complete_group_without_player_consent(trio):
    e = trio
    e.model.respond_to_player = lambda **kwargs: NpcReply(
        reply="我想邀请你们按这份有限路线出行，你们各自考虑。",
        group_journey_intent=GroupJourneyIntent(**proposal(e)),
    )
    response = e.client.post(
        f"/api/worlds/{e.wid}/player/act",
        json={"intent": "说：请提出你本人愿意的完整旅行建议。", "target_character_id": e.npc},
    )
    assert response.status_code == 201, response.text
    shared = rows(e)[0]
    assert [m["decision"] for m in shared["members"]] == ["pending", "accepted", "pending"]
    jid = shared["id"]
    assert post(e, dict(operation="accept", journey_id=jid, statement=WORDS)).status_code == 200
    assert rows(e)[0]["status"] == "pending"
    answer(e, e.third, jid)
    assert rows(e)[0]["status"] == "accepted" and not rows(e)[0]["following"]


def test_actual_preview_preserves_pending_self_and_masks_target_names(trio):
    e = trio
    with e.db.write() as c:
        proposer = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        jid = GroupJourneyService.apply(c, proposer, e.at, GroupJourneyIntent(**proposal(e)))[
            "journey_id"
        ]
        c.execute(
            "DELETE FROM character_acquaintances WHERE observer_id=? AND subject_id=?",
            (e.player, e.third),
        )
        c.execute("UPDATE characters SET name='不应进入解析器的真名乙' WHERE id=?", (e.third,))
    captured = []

    def complete(*, schema, **kwargs):
        captured.append(kwargs["user_payload"])
        return SimpleNamespace(
            data=schema.validate_python(
                {
                    "operation": "group_journey",
                    "target_character_id": e.npc,
                    "group_journey_choice": {
                        "operation": "accept",
                        "journey_id": jid,
                        "statement": WORDS,
                    },
                    "choice_evidence": WORDS,
                }
            )
        )

    e.model.complete = complete
    response = e.client.post(
        f"/api/worlds/{e.wid}/player/intents/preview",
        json={"intent": WORDS, "target_character_id": e.npc},
    )
    assert response.status_code == 200, response.text
    parsed = response.json()
    assert (
        parsed["route_kind"] == "social_choice"
        and parsed["group_journey_choice"]["journey_id"] == jid
    )
    assert "不应进入解析器的真名乙" not in json.dumps(captured, ensure_ascii=False)
    assert (
        next(m for m in rows(e)[0]["members"] if m["character_id"] == e.player)["decision"]
        == "pending"
    )
    assert post(e, parsed["group_journey_choice"]).status_code == 200
    assert (
        next(m for m in rows(e)[0]["members"] if m["character_id"] == e.third)["decision"]
        == "pending"
    )


def test_letter_typed_own_leave_survives_remote_channel_without_new_invitation(trio):
    e = trio
    jid = post(e, proposal(e)).json()["journey_id"]
    answer(e, e.npc, jid)
    answer(e, e.third, jid)
    with e.db.write() as c:
        c.execute(
            "INSERT INTO character_contacts(id,world_id,requester_id,recipient_id,status,created_at,updated_at) VALUES ('g6-letters',?,?,?,'accepted',?,?)",
            (e.wid, e.player, e.npc, to_iso(e.at), to_iso(e.at)),
        )
        c.execute("UPDATE characters SET longitude=longitude+1 WHERE id=?", (e.npc,))
    e.model.complete = lambda *, schema, **kwargs: SimpleNamespace(
        data=schema.validate_python(
            {
                "reply": "我决定退出这份原行程。",
                "group_journey_intent": {
                    "operation": "leave",
                    "journey_id": jid,
                    "statement": "我本人退出原完整行程。",
                },
            }
        )
    )
    response = e.client.post(
        f"/api/worlds/{e.wid}/messages",
        json={"recipient_id": e.npc, "content": "请告诉我你本人对旧行程的选择。"},
    )
    assert response.status_code == 200, response.text
    assert next(m for m in rows(e)[0]["members"] if m["character_id"] == e.npc)["left_at"]


@pytest.mark.parametrize("finish_at", [None, "before_claim", "during_model", "accept"])
def test_group_outreach_checks_real_pending_member_before_and_after_model(trio, finish_at):
    e = trio
    response = post(e, proposal(e)).json()
    jid, source = response["journey_id"], response["event_id"]
    with e.db.write() as c:
        c.execute("DELETE FROM npc_outreach_jobs WHERE world_id=?", (e.wid,))
        c.execute(
            "INSERT INTO npc_outreach_jobs(id,world_id,npc_id,player_id,channel,reason,source_event_id,world_day) VALUES ('g6-job',?,?,?,'in_person','本人考虑原完整多人提议',?,?)",
            (e.wid, e.npc, e.player, source, e.at.date().isoformat()),
        )
        third = c.execute("SELECT * FROM characters WHERE id=?", (e.third,)).fetchone()
        GroupJourneyService.apply(
            c,
            third,
            e.at,
            GroupJourneyIntent(
                operation="accept", journey_id=jid, statement="我本人接受原完整条款。"
            ),
        )
        job = dict(c.execute("SELECT * FROM npc_outreach_jobs WHERE id='g6-job'").fetchone())
        assert _group_opportunity_current(c, job, e.at)
        assert (
            c.execute("SELECT source_event_id FROM group_journeys WHERE id=?", (jid,)).fetchone()[0]
            != source
        )
        if finish_at == "before_claim":
            npc = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
            GroupJourneyService.apply(
                c,
                npc,
                e.at,
                GroupJourneyIntent(
                    operation="decline", journey_id=jid, statement="我婉拒原完整行程。"
                ),
            )

    class Backend:
        calls = 0

        def complete(self, **request):
            self.calls += 1
            if finish_at == "during_model":
                with e.db.write() as c:
                    npc = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
                    GroupJourneyService.apply(
                        c,
                        npc,
                        e.at,
                        GroupJourneyIntent(
                            operation="decline", journey_id=jid, statement="我婉拒原完整行程。"
                        ),
                    )
            if finish_at == "accept":
                return SimpleNamespace(
                    data=OutreachText(
                        should_contact=True,
                        reply="我本人接受这份原完整多人行程。",
                        group_journey_intent=GroupJourneyIntent(
                            operation="accept", journey_id=jid, statement="本人独立接受原条款。"
                        ),
                    )
                )
            return SimpleNamespace(data=OutreachText(should_contact=False))

    backend = Backend()
    engine = WorldEngine(e.db, e.settings, e.model)
    engine.agent_model_backend = backend
    try:
        assert process_outreach(engine, e.wid) == (1 if finish_at == "accept" else 0)
        assert backend.calls == (0 if finish_at == "before_claim" else 1)
        with e.db.read() as c:
            assert c.execute("SELECT status FROM npc_outreach_jobs WHERE id='g6-job'").fetchone()[
                0
            ] == (
                "declined"
                if finish_at is None
                else "done"
                if finish_at == "accept"
                else "cancelled"
            )
            if finish_at == "accept":
                assert (
                    c.execute("SELECT status FROM group_journeys WHERE id=?", (jid,)).fetchone()[0]
                    == "accepted"
                )
    finally:
        engine.close()


@pytest.mark.parametrize("operation", ["leave", "pause", "end", "withdraw_follow"])
def test_remote_player_existing_group_exit_and_no_remote_new_consent(trio, operation):
    e = trio
    response = post(e, proposal(e))
    jid = response.json()["journey_id"]
    answer(e, e.npc, jid)
    answer(e, e.third, jid)
    assert post(e, dict(operation="follow", journey_id=jid, statement=WORDS)).status_code == 200
    with e.db.write() as c:
        c.execute(
            "UPDATE characters SET longitude=longitude+1 WHERE world_id=? AND is_player=0", (e.wid,)
        )
    assert post(e, dict(operation="accept", journey_id=jid, statement=WORDS)).status_code == 409
    assert post(e, proposal(e)).status_code == 409
    response = post(e, dict(operation=operation, journey_id=jid, statement=WORDS))
    assert response.status_code == 200, response.text
    assert rows(e)[0]["status"] == (
        "left" if operation == "leave" else "ended" if operation == "end" else "paused"
    )


def test_options_hide_unknown_names_private_places_and_other_world_members(trio):
    e = trio
    with e.db.write() as c:
        c.execute(
            "DELETE FROM character_acquaintances WHERE observer_id=? AND subject_id=?",
            (e.player, e.third),
        )
        c.execute("UPDATE characters SET name='不可泄露的真名甲' WHERE id=?", (e.third,))
        hidden = c.execute(
            "SELECT id FROM locations WHERE world_id=? AND id NOT IN (?,?,?) LIMIT 1",
            (e.wid, e.loc["id"], *e.route),
        ).fetchone()
    result = e.client.get(f"/api/worlds/{e.wid}/group-journeys/options")
    assert result.status_code == 200
    assert "不可泄露的真名甲" not in result.text
    assert {e.player, e.npc, e.third}.issubset({r["id"] for r in result.json()["members"]})
    if hidden:
        assert hidden[0] not in {p["id"] for p in result.json()["places"]}
    assert (
        post(
            e, {**proposal(e), "member_ids": [e.player, e.npc, "other-world-character"]}
        ).status_code
        == 409
    )
    assert post(e, proposal(e), recipient_id="other-world-character").status_code == 409
    assert (
        e.client.get(
            f"/api/worlds/{e.wid}/group-journeys?character_id=other-world-character"
        ).status_code
        == 404
    )
    assert not rows(e)


@pytest.mark.parametrize("schema", [NpcReply, NpcTextResult, OutreachText])
def test_npc_typed_channels_keep_one_members_own_choice(schema):
    raw = dict(
        reply="我本人答复。",
        group_journey_intent={
            "operation": "accept",
            "journey_id": "real-pending",
            "statement": "本人明确接受原完整条款。",
        },
    )
    if schema is OutreachText:
        raw["should_contact"] = True
    parsed = schema.model_validate(raw)
    restored = schema.model_validate(parsed.model_dump())
    assert restored.group_journey_intent.operation == "accept"
    assert not restored.group_journey_intent.member_ids


def parser_preview(choice, row, *, apart=False, evidence=WORDS):
    player = CharacterState(
        id="player",
        world_id="world",
        name="旅人",
        location_id="place",
        is_player=True,
        energy=95,
        satiety=95,
        money=30,
        current_room_id="mine" if apart else None,
    )
    npc = player.model_copy(
        update={"id": "npc", "is_player": False, "current_room_id": "other" if apart else None}
    )

    class Backend:
        def complete(self, **request):
            return SimpleNamespace(
                data=IntentParseResult(
                    operation="group_journey",
                    target_character_id="npc",
                    group_journey_choice=choice,
                    choice_evidence=evidence,
                )
            )

    return IntentParserAgent(Backend()).preview(
        intent=WORDS,
        player=player,
        snapshot=SimpleNamespace(
            characters=[player, npc],
            world=SimpleNamespace(current_time=datetime(2030, 1, 1, tzinfo=UTC)),
        ),
        preferred_target_id="npc",
        social_context={"npc": {"group_journeys": [row], "group_journey_only": apart}},
    )


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "wrong_id",
        "wrong_world",
        "wrong_person",
        "already_accepted",
        "wrong_words",
        "missing_evidence",
    ],
)
def test_parser_accept_only_real_pending_self_and_player_evidence(failure):
    row = dict(
        id="real-pending",
        world_id="other" if failure == "wrong_world" else "world",
        status="pending",
        members=[
            {
                "character_id": "other" if failure == "wrong_person" else "player",
                "decision": "accepted" if failure == "already_accepted" else "pending",
            },
            {"character_id": "npc", "decision": "accepted"},
        ],
    )
    choice = GroupJourneyIntent(
        operation="accept",
        journey_id="fake" if failure == "wrong_id" else "real-pending",
        statement="伪造原话" if failure == "wrong_words" else WORDS,
    )
    result = parser_preview(choice, row, evidence="" if failure == "missing_evidence" else WORDS)
    assert (result.route_kind == "social_choice") is (failure is None)
    if failure is None:
        assert result.group_journey_choice.member_ids == []


@pytest.mark.parametrize(
    "operation",
    ["accept", "decline", "follow", "depart", "resume", "leave", "pause", "end", "withdraw_follow"],
)
def test_remote_parser_only_offers_self_exit_operations(operation):
    row = dict(
        id="real-pending",
        status="pending" if operation in {"accept", "decline"} else "accepted",
        proposer_id="player",
        leader_id="npc",
        can_follow=True,
        members=[
            {"character_id": "player", "decision": "pending"},
            {"character_id": "npc", "decision": "accepted"},
        ],
    )
    result = parser_preview(
        GroupJourneyIntent(operation=operation, journey_id="real-pending", statement=WORDS),
        row,
        apart=True,
    )
    assert (result.route_kind == "social_choice") is (
        operation in {"leave", "pause", "end", "withdraw_follow"}
    )


def test_npc_bare_words_and_competing_candidate_never_leave_partial_consent(trio):
    e = trio
    jid = post(e, proposal(e)).json()["journey_id"]
    with e.db.write() as c:
        npc = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        settle_npc_reply(
            c,
            npc=npc,
            player_id=e.player,
            at=e.at,
            channel="in_person",
            reply=NpcReply(reply="我们全员都同意，就一起走吧。"),
        )
        result = settle_npc_reply(
            c,
            npc=npc,
            player_id=e.player,
            at=e.at,
            channel="in_person",
            reply=NpcReply(
                reply="我同意，也请你收下东西。",
                group_journey_intent=GroupJourneyIntent(
                    operation="accept", journey_id=jid, statement="本人接受原条款。"
                ),
                action_intent={"kind": "give_item", "target_id": "fake-item"},
            ),
        )
        assert result.action_status == "rejected"
        assert (
            c.execute(
                "SELECT decision FROM group_journey_members WHERE journey_id=? AND character_id=?",
                (jid, e.npc),
            ).fetchone()[0]
            == "pending"
        )
        assert not c.execute(
            "SELECT 1 FROM world_events WHERE world_id=? AND event_type='social.group_journey_accepted'",
            (e.wid,),
        ).fetchone()


def test_group_choice_keeps_orthogonal_private_reflection(trio):
    e = trio
    jid = post(e, proposal(e)).json()["journey_id"]
    e.model.respond_to_player = lambda **kwargs: NpcReply(
        reply="我本人接受这份完整行程。",
        group_journey_intent=GroupJourneyIntent(
            operation="accept", journey_id=jid, statement="本人接受原完整条款。"
        ),
        relationship_reflection={
            "feeling": "愿意合作",
            "need": "各自决定",
            "concern": "组团私密忧虑丙",
        },
    )
    response = e.client.post(
        f"/api/worlds/{e.wid}/player/act",
        json={"intent": "说：你本人如何考虑这份行程？", "target_character_id": e.npc},
    )
    assert response.status_code == 201, response.text
    assert "组团私密忧虑丙" not in response.text
    with e.db.read() as c:
        assert (
            c.execute(
                "SELECT concern FROM intimate_perspectives WHERE character_id=? AND other_id=?",
                (e.npc, e.player),
            ).fetchone()[0]
            == "组团私密忧虑丙"
        )
        assert (
            c.execute(
                "SELECT decision FROM group_journey_members WHERE journey_id=? AND character_id=?",
                (jid, e.npc),
            ).fetchone()[0]
            == "accepted"
        )


def test_legacy_receipt_drops_only_empty_new_group_field():
    payload = NaturalSocialChoice(
        request_id=str(uuid4()),
        recipient_id="npc",
        intent=WORDS,
        evidence=WORDS,
        journey_choice={"operation": "pause", "journey_id": "old-pair", "statement": WORDS},
    )
    old = payload.model_dump(mode="json")
    old.pop("group_journey_choice")
    digest = hashlib.sha256(
        json.dumps(old, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert digest in compatible_choice_digests(payload)


def test_actual_api_replays_saved_legacy_hash_without_new_null_field(trio):
    e = trio
    payload = NaturalSocialChoice(
        request_id=str(uuid4()),
        recipient_id=e.npc,
        intent=WORDS,
        evidence=WORDS,
        journey_choice={"operation": "pause", "journey_id": "old-pair", "statement": WORDS},
    )
    old = payload.model_dump(mode="json")
    old.pop("group_journey_choice")
    digest = hashlib.sha256(
        json.dumps(old, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    saved = {"status": "paused", "journey_id": "old-pair", "event_id": "first-real-event"}
    with e.db.write() as c:
        c.execute(
            "INSERT INTO social_choice_receipts VALUES (?,?,?,?,?)",
            (e.wid, payload.request_id, e.player, digest, json.dumps(saved)),
        )
    response = e.client.post(f"/api/worlds/{e.wid}/player/social-choices", json=old)
    assert response.status_code == 200, response.text
    assert response.json() == saved
