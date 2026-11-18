"""哥哥，长期住房与借贷只能由提出的玩家确认，NPC 的私人事务保留审计。"""

# ruff: noqa: F811
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from test_appointment_rooms import room
from test_lived_world import town  # noqa: F401
from test_lodging_plans import accept as accept_plan
from test_lodging_plans import choose as choose_plan
from test_lodging_plans import setup as setup_plan

from world_engine.contracts import ContractService
from world_engine.repository import from_iso, to_iso


def add_request(e, c, requester, owner, *, kind="住房", status="npc_accepted", terms=None):
    rid = str(uuid4())
    c.execute(
        """INSERT INTO long_term_operation_requests(
           id,world_id,requester_id,recipient_id,operation_type,terms_json,status,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            rid,
            e.wid,
            requester,
            owner,
            kind,
            json.dumps(terms or {}, ensure_ascii=False),
            status,
            to_iso(e.at),
            to_iso(e.at),
        ),
    )
    return rid


def actors(e, c):
    return [
        r[0]
        for r in c.execute(
            "SELECT id FROM characters WHERE world_id=? AND is_player=0 AND id<>? "
            "ORDER BY id LIMIT 2",
            (e.wid, e.npc),
        )
    ]


def snapshot(e):
    with e.db.read() as c:
        return {
            "money": [
                tuple(row)
                for row in c.execute(
                    "SELECT id,money FROM characters WHERE world_id=? ORDER BY id", (e.wid,)
                )
            ],
            "requests": [
                tuple(row)
                for row in c.execute(
                    "SELECT * FROM long_term_operation_requests WHERE world_id=? ORDER BY id",
                    (e.wid,),
                )
            ],
            "contracts": [
                tuple(row)
                for row in c.execute(
                    "SELECT * FROM contract_fulfillments WHERE world_id=? ORDER BY request_id",
                    (e.wid,),
                )
            ],
            "events": c.execute(
                "SELECT COUNT(*) FROM world_events WHERE world_id=?", (e.wid,)
            ).fetchone()[0],
        }


def test_list_filters_before_pagination_and_only_shows_player_participation(town):
    e = town
    with e.db.write() as c:
        third, fourth = actors(e, c)
        own = add_request(e, c, e.player, e.npc)
        incoming = add_request(e, c, e.npc, e.player)
        private = add_request(e, c, third, fourth)
        c.execute(
            "UPDATE long_term_operation_requests SET created_at='2099-01-01' WHERE id=?", (private,)
        )
    response = e.client.get(f"/api/worlds/{e.wid}/long-term-requests?limit=1")
    assert response.status_code == 200
    assert response.json()[0]["id"] in {own, incoming}
    listed = e.client.get(f"/api/worlds/{e.wid}/long-term-requests").json()
    assert {r["id"] for r in listed} == {own, incoming}
    assert all(r["requester_id"] == e.player or r["recipient_id"] == e.player for r in listed)


def test_world_without_player_has_no_long_term_request_view(town):
    e = town
    world = e.client.post("/api/worlds", json={"name": "无玩家的私人事务"}).json()
    response = e.client.get(f"/api/worlds/{world['world']['id']}/long-term-requests")
    assert response.status_code == 404


def test_player_cannot_confirm_npc_request_even_as_owner(town):
    e = town
    with e.db.write() as c:
        third, _ = actors(e, c)
        unrelated = add_request(e, c, e.npc, third, terms={"payment": 5})
        incoming = add_request(e, c, e.npc, e.player, terms={"payment": 5})
    before = snapshot(e)
    for rid in (unrelated, incoming):
        response = e.client.post(
            f"/api/worlds/{e.wid}/long-term-requests/{rid}/confirm",
            json={"accept_counter_terms": True},
        )
        assert response.status_code == 403, response.text
    assert snapshot(e) == before


def test_npc_lodging_cannot_be_cancelled_or_deleted_through_player_legacy_api(town):
    e = town
    rid = room(e, owner=e.player, rate=2)
    with e.db.write() as c:
        request = add_request(
            e,
            c,
            e.npc,
            e.player,
            status="applied",
            terms={"room_id": rid, "duration_days": 1, "payment": 2},
        )
        row = c.execute(
            "SELECT * FROM long_term_operation_requests WHERE id=?", (request,)
        ).fetchone()
        ContractService.start(c, row, json.loads(row["terms_json"]), to_iso(e.at))
    before = snapshot(e)
    response = e.client.delete(f"/api/worlds/{e.wid}/long-term-requests/{request}")
    assert response.status_code == 403, response.text
    assert snapshot(e) == before


def test_player_cannot_repay_or_cancel_npc_loan_before_permission_check(town):
    e = town
    with e.db.write() as c:
        third, _ = actors(e, c)
        request = add_request(
            e,
            c,
            e.npc,
            third,
            kind="借贷",
            status="applied",
            terms={"direction": "borrow", "payment": 7, "duration_days": 2},
        )
        row = c.execute(
            "SELECT * FROM long_term_operation_requests WHERE id=?", (request,)
        ).fetchone()
        ContractService.start(c, row, json.loads(row["terms_json"]), to_iso(e.at))
    before = snapshot(e)
    repay = e.client.post(f"/api/worlds/{e.wid}/long-term-requests/{request}/repay")
    remove = e.client.delete(f"/api/worlds/{e.wid}/long-term-requests/{request}")
    assert repay.status_code == remove.status_code == 403
    assert snapshot(e) == before


def test_housing_default_quote_and_actual_charge_both_cover_one_day(town):
    e = town
    rid = room(e, rate=2)
    with e.db.read() as c:
        money_before = c.execute("SELECT money FROM characters WHERE id=?", (e.player,)).fetchone()[
            0
        ]
    review = e.client.post(
        f"/api/worlds/{e.wid}/long-term-requests",
        json={
            "recipient_id": e.npc,
            "operation_type": "住房",
            "terms": {"room_id": rid, "payment": 2},
        },
    )
    assert review.status_code == 200, review.text
    saved = next(
        r
        for r in e.client.get(f"/api/worlds/{e.wid}/long-term-requests").json()
        if r["id"] == review.json()["id"]
    )
    assert saved["terms"]["duration_days"] == 1
    terms = saved["counter_terms"] or saved["terms"]
    response = e.client.post(
        f"/api/worlds/{e.wid}/long-term-requests/{saved['id']}/confirm",
        json={"accept_counter_terms": True},
    )
    assert response.status_code == 200, response.text
    with e.db.read() as c:
        contract = c.execute(
            "SELECT * FROM contract_fulfillments WHERE request_id=?", (saved["id"],)
        ).fetchone()
        assert from_iso(contract["due_world_time"]) - from_iso(
            contract["started_world_time"]
        ) == timedelta(days=1)
        assert (
            c.execute("SELECT money FROM characters WHERE id=?", (e.player,)).fetchone()[0]
            == money_before - terms["payment"]
        )
    removed = e.client.delete(f"/api/worlds/{e.wid}/long-term-requests/{saved['id']}")
    assert removed.status_code == 200 and removed.json()["status"] == "removed"


def test_legacy_housing_without_days_is_confirmed_for_quoted_one_day(town):
    e = town
    rid = room(e, rate=2)
    with e.db.write() as c:
        request = add_request(e, c, e.player, e.npc, terms={"room_id": rid, "payment": 2})
    response = e.client.post(
        f"/api/worlds/{e.wid}/long-term-requests/{request}/confirm",
        json={"accept_counter_terms": True},
    )
    assert response.status_code == 200, response.text
    with e.db.read() as c:
        contract = c.execute(
            "SELECT * FROM contract_fulfillments WHERE request_id=?", (request,)
        ).fetchone()
        assert from_iso(contract["due_world_time"]) - from_iso(
            contract["started_world_time"]
        ) == timedelta(days=1)


def test_player_legacy_delete_preserves_newcore_housing_quote_audit(town):
    e = town
    _, _, _, fields = setup_plan(e)
    pid = accept_plan(e, fields)
    quote = choose_plan(e, "quote", pid)
    before = snapshot(e)
    response = e.client.delete(f"/api/worlds/{e.wid}/long-term-requests/{quote['request_id']}")
    assert response.status_code == 409, response.text
    assert snapshot(e) == before
    with e.db.read() as c:
        assert (
            c.execute(
                "SELECT status FROM journey_lodging_quotes WHERE id=?", (quote["quote_id"],)
            ).fetchone()[0]
            == "open"
        )


@pytest.mark.parametrize("budget", [2, 20])
def test_player_legacy_confirm_cannot_bypass_accurate_quote_or_personal_budget(town, budget):
    e = town
    _, _, _, fields = setup_plan(e, own_budget=budget)
    pid = accept_plan(e, fields)
    quote = choose_plan(e, "quote", pid)
    before = snapshot(e)
    response = e.client.post(
        f"/api/worlds/{e.wid}/long-term-requests/{quote['request_id']}/confirm",
        json={"accept_counter_terms": True},
    )
    assert response.status_code == 409, response.text
    assert snapshot(e) == before


def test_restart_keeps_cancelled_npc_and_newcore_request_audit(town):
    e = town
    _, _, _, fields = setup_plan(e)
    pid = accept_plan(e, fields)
    quote = choose_plan(e, "quote", pid)
    with e.db.write() as c:
        third, _ = actors(e, c)
        npc_request = add_request(e, c, e.npc, third, status="cancelled")
        legacy = add_request(e, c, e.player, third, status="cancelled")
        c.execute(
            "UPDATE long_term_operation_requests SET status='cancelled' WHERE id=?",
            (quote["request_id"],),
        )
    e.db.initialize()
    with e.db.read() as c:
        kept = {
            row[0]
            for row in c.execute(
                "SELECT id FROM long_term_operation_requests WHERE world_id=?", (e.wid,)
            )
        }
        assert npc_request in kept and quote["request_id"] in kept
        assert legacy not in kept
