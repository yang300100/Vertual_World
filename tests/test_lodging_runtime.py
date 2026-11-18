"""哥哥，真实订房回执和逐步日程不能绕过个人限额与已发生的事实。"""

# ruff: noqa: F811
import json

from test_lived_world import town  # noqa: F401
from test_lodging_plans import accept, choose, raw, setup, wallets
from test_journey_room_appointments import room_at_stop

from world_engine.lodging_plans import LodgingPlanService
from world_engine.repository import from_iso


def test_zero_budget_can_report_existing_home_without_creating_or_paying_a_lease(town):
    e = town
    jid, _, _, fields = setup(e, own_budget=0)
    fields["own_room_id"] = room_at_stop(e, jid, owner=e.player, name="本人已有住所")
    pid = accept(e, fields)
    before = wallets(e)
    result = choose(e, "report_ready", pid)
    assert result["status"] == "ready" and wallets(e) == before
    role = raw(e, pid)["roles"][e.player]
    assert role["status"] == "ready" and role["spent"] == 0 and role["contract_id"] is None
    with e.db.read() as c:
        assert not c.execute("SELECT 1 FROM contract_fulfillments WHERE kind='lodging'").fetchone()


def test_rechecking_quote_records_new_request_without_rewriting_owner_review(town):
    e = town
    _, _, _, fields = setup(e)
    pid = accept(e, fields)
    first = choose(e, "quote", pid)
    with e.db.read() as c:
        original = dict(c.execute(
            "SELECT * FROM world_events WHERE id=?", (first["event_id"],)
        ).fetchone())
    second = choose(e, "quote", pid)
    assert first["quote_id"] == second["quote_id"]
    assert first["request_id"] == second["request_id"]
    assert first["event_id"] != second["event_id"]
    with e.db.read() as c:
        assert dict(c.execute(
            "SELECT * FROM world_events WHERE id=?", (first["event_id"],)
        ).fetchone()) == original
        payload = json.loads(c.execute(
            "SELECT payload_json FROM world_events WHERE id=?", (second["event_id"],)
        ).fetchone()[0])
        assert payload["quote_id"] == first["quote_id"]


def test_daily_life_quotes_pays_reports_then_waits_for_player_without_spending_player_money(town):
    e = town
    _, _, _, fields = setup(e)
    pid = accept(e, fields)
    before = wallets(e)
    states = []
    for _ in range(3):
        e.clock.heartbeat(e.wid, elapsed_seconds=61)
        states.append(raw(e, pid)["roles"][e.npc]["status"])
    assert states == ["offer_ready", "booked", "ready"]
    after = wallets(e)
    spent = raw(e, pid)["roles"][e.npc]["spent"]
    assert spent > 0
    assert after[e.npc] == before[e.npc] - spent
    assert after[e.owner] == before[e.owner] + spent
    assert after[e.player] == before[e.player]
    e.clock.heartbeat(e.wid, elapsed_seconds=61)
    with e.db.read() as c:
        actor = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        at = from_iso(
            c.execute("SELECT w.current_time FROM worlds w WHERE w.id=?", (e.wid,)).fetchone()[0]
        )
        assert LodgingPlanService.planned_minutes(c, actor, at) == 0
        assert not c.execute(
            "SELECT 1 FROM character_life_activities WHERE character_id=? AND status='running'",
            (e.npc,),
        ).fetchone()
    assert raw(e, pid)["status"] == "accepted"
