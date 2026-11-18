"""哥哥，真实订房 API 核对本人准确付款、私人回执和重试后的首次授权来源。"""

# ruff: noqa: F811
import hashlib
import json
from uuid import uuid4

import pytest
from test_appointment_rooms import door
from test_lived_world import town  # noqa: F401
from test_lodging_plans import accept, book, choose, raw, set_time, setup, wallets
from test_overnight_options import third_person

from world_engine.api_intimacy import NaturalSocialChoice
from world_engine.repository import from_iso
from world_engine.shared_life import SharedLifeIntent


def body(e, choice, *, request_id=None):
    text = choice["statement"]
    return {
        "request_id": request_id or str(uuid4()),
        "recipient_id": e.npc,
        "intent": "这是我本人此刻的选择：" + text,
        "evidence": text,
        "lodging_plan_choice": choice,
    }


def post(e, payload):
    return e.client.post(f"/api/worlds/{e.wid}/player/social-choices", json=payload)


def action(e, operation, pid, **fields):
    return body(
        e,
        {
            "operation": operation,
            "plan_id": pid,
            "statement": "我明确选择办理本人这份已有订房筹备。",
            **fields,
        },
    )


def rows(e, character_id=None):
    return e.client.get(
        f"/api/worlds/{e.wid}/lodging-plans", params={"character_id": character_id or e.npc}
    )


def contract_count(e, own, other):
    with e.db.read() as c:
        return c.execute(
            "SELECT COUNT(*) FROM contract_fulfillments WHERE asset_id IN (?,?)", (own, other)
        ).fetchone()[0]


def event_source(e, event_id):
    with e.db.read() as c:
        return json.loads(
            c.execute("SELECT payload_json FROM world_events WHERE id=?", (event_id,)).fetchone()[0]
        )


def test_proposal_api_uses_exact_evidence_and_same_receipt_once(town):
    e = town
    _, own, other, fields = setup(e)
    before = wallets(e)
    payload = body(e, fields)
    payload["lodging_plan_choice"]["statement"] = "这段候选不替代玩家实际说出的原话。"
    first = post(e, payload)
    assert first.status_code == 200, first.text
    pid = first.json()["plan_id"]
    assert raw(e, pid)["statement"] == payload["evidence"]
    assert post(e, payload).json() == first.json()
    assert wallets(e) == before and contract_count(e, own, other) == 0
    source = event_source(e, first.json()["event_id"])
    assert source["input_request_id"] == payload["request_id"]
    assert source["player_input"] == payload["intent"]
    changed = {**payload, "intent": "我改变原预算。", "evidence": "我改变原预算。"}
    assert post(e, changed).status_code == 409
    with e.db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM journey_lodging_plans").fetchone()[0] == 1
        assert (
            c.execute(
                "SELECT COUNT(*) FROM social_choice_receipts WHERE request_id=?",
                (payload["request_id"],),
            ).fetchone()[0]
            == 1
        )


def test_npc_proposed_terms_keep_own_other_mapping_after_player_acceptance(town):
    e = town
    _, own, other, fields = setup(e)
    # 哥哥，NPC 自己提议时 own 属于 NPC；玩家接受不会对调原来的两房与上限。
    fields.update(own_room_id=other, other_room_id=own, own_budget=18, other_budget=12)
    pid = choose(e, **fields, npc=True)["plan_id"]
    before = wallets(e)
    result = post(e, action(e, "accept", pid))
    assert result.status_code == 200, result.text
    plan = raw(e, pid)
    assert plan["status"] == "accepted"
    assert (plan["roles"][e.player]["room_id"], plan["roles"][e.player]["spending_limit"]) == (
        own,
        12,
    )
    assert (plan["roles"][e.npc]["room_id"], plan["roles"][e.npc]["spending_limit"]) == (other, 18)
    assert wallets(e) == before and contract_count(e, own, other) == 0


def test_exact_payment_api_retries_keep_first_authorization_and_one_charge(town):
    e = town
    _, own, other, fields = setup(e)
    pid = accept(e, fields)
    before = wallets(e)
    quote = post(e, action(e, "quote", pid))
    assert quote.status_code == 200, quote.text
    quoted = quote.json()
    assert quoted["expected_total"] == 6 and wallets(e) == before
    wrong = action(e, "confirm", pid, quote_id=quoted["quote_id"], expected_total=20)
    assert post(e, wrong).status_code == 409
    assert wallets(e) == before and contract_count(e, own, other) == 0
    first_payload = action(
        e,
        "confirm",
        pid,
        quote_id=quoted["quote_id"],
        expected_total=6,
        statement="我已核对本人房间的准确报价，明确确认本人支付六枚。",
    )
    paid = post(e, first_payload)
    assert paid.status_code == 200, paid.text
    paid_result = paid.json()
    assert paid_result["payment"] == 6
    after = wallets(e)
    assert after[e.player] == before[e.player] - 6 and after[e.npc] == before[e.npc]
    assert after[e.owner] == before[e.owner] + 6
    assert post(e, first_payload).json() == paid_result
    # 哥哥，新网络请求编号也可以重取同一笔付款回执，但不能改写首次授权事件。
    second_payload = {**first_payload, "request_id": str(uuid4())}
    second_payload["intent"] = "我再次核对这笔已付款回执。" + second_payload["evidence"]
    replay = post(e, second_payload)
    assert replay.status_code == 200, replay.text
    assert replay.json()["event_id"] == paid_result["event_id"]
    assert wallets(e) == after and contract_count(e, own, other) == 1
    source = event_source(e, paid_result["event_id"])
    assert source["input_request_id"] == first_payload["request_id"]
    assert source["player_input"] == first_payload["intent"]
    with e.db.read() as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM social_choice_receipts WHERE request_id IN (?,?)",
                (first_payload["request_id"], second_payload["request_id"]),
            ).fetchone()[0]
            == 2
        )


def test_player_api_never_confirms_companion_quote_or_injected_actor(town):
    e = town
    _, own, other, fields = setup(e)
    pid = accept(e, fields)
    private = choose(e, "quote", pid, npc=True)
    before = wallets(e)
    payload = action(
        e, "confirm", pid, quote_id=private["quote_id"], expected_total=private["expected_total"]
    )
    assert post(e, payload).status_code == 409
    assert post(e, {**payload, "actor_id": e.npc}).status_code == 422
    assert wallets(e) == before and contract_count(e, own, other) == 0


def test_get_only_exposes_own_quote_and_keeps_companion_booking_private(town):
    e = town
    _, _, _, fields = setup(e)
    pid = accept(e, fields)
    own_quote = post(e, action(e, "quote", pid)).json()
    companion_quote, companion_paid = book(e, pid, npc=True)
    response = rows(e)
    assert response.status_code == 200, response.text
    plan = next(row for row in response.json() if row["id"] == pid)
    roles = {role["character_id"]: role for role in plan["roles"]}
    assert roles[e.player]["quote_id"] == own_quote["quote_id"]
    assert roles[e.player]["quote"]["total"] == 6
    assert roles[e.npc]["status"] == "unreported"
    assert not {
        "quote",
        "quote_id",
        "spent",
        "private_reason",
        "request_id",
        "contract_id",
        "retry_at",
    }.intersection(roles[e.npc])
    assert companion_quote["quote_id"] not in response.text
    assert companion_paid["contract_id"] not in response.text
    assert e.client.get(f"/api/worlds/{e.wid}/lodging-plans").json() == response.json()


def test_quote_expires_before_confirm_without_payment_or_contract(town):
    e = town
    _, own, other, fields = setup(e)
    pid = accept(e, fields)
    quote = post(e, action(e, "quote", pid)).json()
    with e.db.read() as c:
        expires = from_iso(
            c.execute(
                "SELECT expires_at FROM journey_lodging_quotes WHERE id=?", (quote["quote_id"],)
            ).fetchone()[0]
        )
    before = wallets(e)
    set_time(e, expires)
    response = post(e, action(e, "confirm", pid, quote_id=quote["quote_id"], expected_total=6))
    assert response.status_code == 409, response.text
    assert wallets(e) == before and contract_count(e, own, other) == 0


def test_cancel_api_keeps_paid_lease_and_original_trip_paused(town):
    e = town
    jid, own, other, fields = setup(e)
    pid = accept(e, fields)
    book(e, pid)
    before = wallets(e)
    response = post(e, action(e, "cancel", pid))
    assert response.status_code == 200, response.text
    assert raw(e, pid)["status"] == "cancelled"
    assert wallets(e) == before and contract_count(e, own, other) == 1
    with e.db.read() as c:
        assert (
            c.execute("SELECT status FROM joint_journeys WHERE id=?", (jid,)).fetchone()[0]
            == "paused"
        )
        assert (
            c.execute(
                "SELECT status FROM contract_fulfillments WHERE asset_id=?", (own,)
            ).fetchone()[0]
            == "active"
        )


def test_player_reports_only_after_own_lease_then_companion_reports_independently(town):
    e = town
    _, _, _, fields = setup(e)
    pid = accept(e, fields)
    assert post(e, action(e, "report_ready", pid)).status_code == 409
    book(e, pid)
    response = post(e, action(e, "report_ready", pid))
    assert response.status_code == 200, response.text
    assert raw(e, pid)["roles"][e.player]["status"] == "ready"
    assert raw(e, pid)["status"] == "accepted"
    book(e, pid, npc=True)
    assert raw(e, pid)["status"] == "accepted"
    choose(e, "report_ready", pid, npc=True)
    assert raw(e, pid)["status"] == "completed"


def test_personal_booking_can_be_read_in_own_room_but_ready_requires_real_outdoor_station(town):
    e = town
    _, own, _, fields = setup(e)
    pid = accept(e, fields)
    book(e, pid)
    assert door(e, own, "open").status_code == 200
    assert door(e, own, "enter").status_code == 200
    assert rows(e).status_code == 200
    assert post(e, action(e, "report_ready", pid)).status_code == 409
    assert post(e, action(e, "cancel", pid)).status_code == 200


def test_known_options_do_not_enumerate_unrelated_or_foreign_people(town):
    e = town
    setup(e)
    stranger = third_person(e)
    with e.db.write() as c:
        c.execute(
            "DELETE FROM character_acquaintances WHERE observer_id=? AND subject_id=?",
            (e.player, stranger),
        )
        c.execute(
            "UPDATE characters SET longitude=?,current_room_id=NULL WHERE id=?",
            (e.loc["longitude"] + 5, stranger),
        )
    foreign = e.client.post("/api/worlds", json={"name": "别的隔离世界"}).json()["characters"][0][
        "id"
    ]
    for cid in (stranger, foreign, str(uuid4()), e.player):
        response = e.client.get(f"/api/worlds/{e.wid}/lodging-plans/options/{cid}")
        assert response.status_code == 404, response.text
    for cid in (foreign, str(uuid4()), e.player):
        assert rows(e, cid).status_code == 404


def test_one_natural_social_choice_cannot_include_two_independent_decisions(town):
    e = town
    _, _, _, fields = setup(e)
    payload = body(e, fields)
    payload["shared_life_choice"] = {
        "operation": "invite",
        "kind": "rest",
        "minutes": 5,
        "statement": fields["statement"],
    }
    assert post(e, payload).status_code == 422
    with e.db.read() as c:
        assert not c.execute("SELECT 1 FROM journey_lodging_plans").fetchone()


@pytest.mark.parametrize("explicit_null", [False, True])
def test_legacy_receipt_without_lodging_null_replays_original_result(town, explicit_null):
    e = town
    text = "我邀请你在这里短休五分钟。"
    payload = NaturalSocialChoice(
        request_id=str(uuid4()),
        recipient_id=e.npc,
        intent=text,
        evidence=text,
        shared_life_choice=SharedLifeIntent(
            operation="invite", kind="rest", minutes=5, statement=text
        ),
    ).model_dump(mode="json")
    assert payload.pop("lodging_plan_choice") is None
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    saved = {"status": "pending", "event_id": "old-receipt", "summary": "此前选择已结算。"}
    with e.db.write() as c:
        c.execute(
            "INSERT INTO social_choice_receipts VALUES (?,?,?,?,?)",
            (e.wid, payload["request_id"], e.player, digest, json.dumps(saved)),
        )
    if explicit_null:
        payload["lodging_plan_choice"] = None
    response = post(e, payload)
    assert response.status_code == 200 and response.json() == saved
    with e.db.read() as c:
        assert not c.execute("SELECT 1 FROM shared_life_moments").fetchone()
        assert not c.execute("SELECT 1 FROM journey_lodging_plans").fetchone()
