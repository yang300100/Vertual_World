"""哥哥，户外约定中的最后接近只由 NPC 真正行走，玩家独立开始相处。"""

from datetime import timedelta

import pytest

from test_appointments import choose, get, offer, set_time
from test_lived_world import town  # noqa: F401
from world_engine.daily_life import DailyLifeService
from world_engine.geo import great_circle_distance_km
from world_engine.life import LifeActivityService
from world_engine.player_steering import PlayerSteeringService
from world_engine.repository import from_iso, to_iso, utc_now


def prepared(e, *, accept=True):
    aid = offer(e)
    if accept:
        assert choose(e, "accept", aid).status_code == 200
    at = e.at + timedelta(hours=1)
    set_time(e, at)
    with e.db.write() as c:
        # 哥哥，这是隔离场景双方各自到场时的初始坐标；测试不会在行程中复制位置。
        c.execute("UPDATE characters SET longitude=?,latitude=? WHERE id=?",
                  (e.loc["longitude"] + 0.0006, e.loc["latitude"], e.player))
        c.execute("UPDATE characters SET longitude=?,latitude=? WHERE id=?",
                  (e.loc["longitude"], e.loc["latitude"], e.npc))
    return aid, at


def test_npc_walks_to_visible_player_and_player_starts_shared_activity(town):
    e = town
    aid, at = prepared(e)
    with e.db.write() as c:
        before_player = dict(c.execute("SELECT * FROM characters WHERE id=?", (e.player,)).fetchone())
        before_npc = dict(c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone())
        assert 0.02 < great_circle_distance_km(before_npc["longitude"], before_npc["latitude"],
                                                before_player["longitude"], before_player["latitude"]) < 0.1
        DailyLifeService.tick(c, e.wid, at, adjudication_due=True)
        movement = c.execute("SELECT * FROM character_movements WHERE character_id=? AND status='moving'", (e.npc,)).fetchone()
        assert movement and movement["destination_longitude"] == before_player["longitude"]
        assert dict(c.execute("SELECT * FROM characters WHERE id=?", (e.player,)).fetchone()) == before_player
        assert c.execute("SELECT status FROM social_appointments WHERE id=?", (aid,)).fetchone()[0] == "accepted"
        event = c.execute("SELECT e.id FROM world_events e WHERE e.world_id=? AND e.actor_id=? "
                          "AND e.event_type='action.movement_started' ORDER BY e.rowid DESC LIMIT 1", (e.wid,e.npc)).fetchone()
        assert event and c.execute("SELECT 1 FROM event_observers WHERE event_id=? AND character_id=? AND channel='witnessed'",
                                   (event["id"],e.player)).fetchone()
        saved = dict(movement)
        DailyLifeService.tick(c, e.wid, at, adjudication_due=True)
        assert c.execute("SELECT COUNT(*) FROM character_movements WHERE character_id=? AND status='moving'", (e.npc,)).fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM shared_life_moments WHERE world_id=?", (e.wid,)).fetchone()[0] == 0
        arrival = from_iso(saved["estimated_arrival_world"])
    # 哥哥，利用正常心跳推进真实路线，不能直接给人物写抵达位置。
    e.clock.heartbeat(e.wid, elapsed_seconds=(arrival-at).total_seconds()+0.1)
    with e.db.read() as c:
        assert c.execute("SELECT status FROM character_movements WHERE id=?", (saved["id"],)).fetchone()[0] == "arrived"
        npc = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        player = c.execute("SELECT * FROM characters WHERE id=?", (e.player,)).fetchone()
        assert great_circle_distance_km(npc["longitude"], npc["latitude"], player["longitude"], player["latitude"]) <= 0.02
        assert player["longitude"] == before_player["longitude"] and player["latitude"] == before_player["latitude"]
    result = choose(e, "attend", aid)
    assert result.status_code == 200, result.text
    assert get(e, aid)["status"] == "active"


@pytest.mark.parametrize("condition", ["pending", "invisible", "different_layer", "player_busy", "npc_busy", "player_steering", "unknown_acceptance", "premature_knowledge", "outside_window", "too_late"])
def test_no_approach_without_current_visible_available_and_confirmed_context(town, condition):
    e = town
    aid, at = prepared(e, accept=condition != "pending")
    with e.db.write() as c:
        if condition == "invisible":
            c.execute("UPDATE characters SET longitude=? WHERE id=?", (e.loc["longitude"] - 0.0006, e.npc))
        elif condition == "different_layer":
            c.execute("UPDATE characters SET space_layer='sky' WHERE id=?", (e.player,))
        elif condition == "player_busy":
            player = c.execute("SELECT * FROM characters WHERE id=?", (e.player,)).fetchone()
            LifeActivityService.start(c, player, at, "rest", 30)
        elif condition == "npc_busy":
            npc = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
            LifeActivityService.start(c, npc, at, "rest", 30)
        elif condition == "player_steering":
            player = c.execute("SELECT * FROM characters WHERE id=?", (e.player,)).fetchone()
            PlayerSteeringService.set_direction(c, player, (1, 0), 1, at, utc_now())
        elif condition == "unknown_acceptance":
            event = c.execute("SELECT source_event_id FROM social_appointments WHERE id=?", (aid,)).fetchone()[0]
            c.execute("DELETE FROM character_event_knowledge WHERE event_id=? AND character_id=?", (event,e.npc))
        elif condition == "premature_knowledge":
            event = c.execute("SELECT source_event_id FROM social_appointments WHERE id=?", (aid,)).fetchone()[0]
            c.execute("UPDATE character_event_knowledge SET learned_world_time=? WHERE event_id=? AND character_id=?", (to_iso(e.at-timedelta(seconds=1)),event,e.npc))
        elif condition == "outside_window":
            at += timedelta(hours=3)
            c.execute("UPDATE worlds SET current_time=? WHERE id=?", (to_iso(at),e.wid))
        elif condition == "too_late":
            date = c.execute("SELECT ends_at,minutes FROM social_appointments WHERE id=?", (aid,)).fetchone()
            at = from_iso(date["ends_at"]) - timedelta(minutes=date["minutes"]) + timedelta(seconds=1)
            c.execute("UPDATE worlds SET current_time=? WHERE id=?", (to_iso(at),e.wid))
        before = dict(c.execute("SELECT * FROM characters WHERE id=?", (e.player,)).fetchone())
        DailyLifeService.tick(c,e.wid,at,adjudication_due=True)
        assert not c.execute("SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'", (e.npc,)).fetchone()
        assert dict(c.execute("SELECT * FROM characters WHERE id=?", (e.player,)).fetchone()) == before
        assert not c.execute("SELECT 1 FROM shared_life_moments WHERE world_id=?", (e.wid,)).fetchone()


def test_arrival_of_player_triggers_approach_before_next_hourly_check(town):
    e = town
    aid, at = prepared(e)
    with e.db.write() as c:
        # 哥哥，双方都在地点范围内但最初相距超过视距，这是隔离场景初始状态。
        c.execute("UPDATE characters SET longitude=? WHERE id=?", (e.loc["longitude"] - 0.0006,e.npc))
        DailyLifeService.tick(c,e.wid,at,adjudication_due=False)
        state = c.execute("SELECT next_world_time FROM npc_daily_states WHERE character_id=?", (e.npc,)).fetchone()
        assert state and from_iso(state[0]) > at + timedelta(minutes=10)
        assert not c.execute("SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'", (e.npc,)).fetchone()
    # 哥哥，玩家使用真实移动入口走到场地中心，不能直接修改其运行中坐标。
    response=e.client.post(f"/api/worlds/{e.wid}/player/move",json={
        "destination_longitude":e.loc["longitude"],"destination_latitude":e.loc["latitude"]})
    assert response.status_code==201,response.text
    player_move=response.json()["movement"]
    end=from_iso(player_move["estimated_arrival_world"])
    e.clock.heartbeat(e.wid,elapsed_seconds=(end-at).total_seconds()+0.1)
    with e.db.read() as c:
        assert c.execute("SELECT status FROM character_movements WHERE id=?",(player_move["id"],)).fetchone()[0]=="arrived"
        assert c.execute("SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",(e.npc,)).fetchone()
        assert c.execute("SELECT status FROM social_appointments WHERE id=?",(aid,)).fetchone()[0]=="accepted"


def test_npc_waits_for_player_to_finish_existing_route(town):
    e = town
    aid, at = prepared(e)
    with e.db.write() as c:
        # 哥哥，双方位于可见范围内，玩家走完原路线后仍离 NPC 足够远。
        c.execute("UPDATE characters SET latitude=? WHERE id=?", (e.loc["latitude"] - 0.0002, e.npc))
        c.execute("UPDATE characters SET longitude=?,latitude=? WHERE id=?",
                  (e.loc["longitude"], e.loc["latitude"] + 0.0007, e.player))
    response = e.client.post(f"/api/worlds/{e.wid}/player/move", json={
        "destination_longitude": e.loc["longitude"],
        "destination_latitude": e.loc["latitude"] + 0.0001,
    })
    assert response.status_code == 201, response.text
    route = response.json()["movement"]
    with e.db.write() as c:
        DailyLifeService.tick(c, e.wid, at, adjudication_due=True)
        assert c.execute("SELECT status FROM character_movements WHERE id=?", (route["id"],)).fetchone()[0] == "moving"
        assert not c.execute("SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'", (e.npc,)).fetchone()
        assert c.execute("SELECT status FROM social_appointments WHERE id=?", (aid,)).fetchone()[0] == "accepted"
    arrival = from_iso(route["estimated_arrival_world"])
    e.clock.heartbeat(e.wid, elapsed_seconds=(arrival - at).total_seconds() + 0.1)
    with e.db.read() as c:
        assert c.execute("SELECT status FROM character_movements WHERE id=?", (route["id"],)).fetchone()[0] == "arrived"
        assert c.execute("SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'", (e.npc,)).fetchone()
        assert c.execute("SELECT status FROM social_appointments WHERE id=?", (aid,)).fetchone()[0] == "accepted"


def test_too_late_after_approach_route_started_rolls_back_every_effect(town):
    e = town
    aid, _ = prepared(e)
    with e.db.read() as c:
        date = c.execute("SELECT ends_at,minutes FROM social_appointments WHERE id=?", (aid,)).fetchone()
        at = from_iso(date["ends_at"]) - timedelta(minutes=date["minutes"], seconds=1)
    set_time(e, at)
    with e.db.write() as c:
        before_people = [dict(c.execute("SELECT * FROM characters WHERE id=?", (cid,)).fetchone())
                         for cid in (e.player, e.npc)]
        before_movements = c.execute("SELECT COUNT(*) FROM character_movements WHERE character_id=?", (e.npc,)).fetchone()[0]
        before_events = c.execute("SELECT COUNT(*) FROM world_events WHERE world_id=? AND actor_id=? "
                                  "AND event_type='action.movement_started'", (e.wid, e.npc)).fetchone()[0]
        DailyLifeService.tick(c, e.wid, at, adjudication_due=True)
        assert c.execute("SELECT last_error FROM npc_daily_states WHERE character_id=?", (e.npc,)).fetchone()[0] == "剩余时间不足以完成真实靠近和约定相处"
        assert c.execute("SELECT COUNT(*) FROM character_movements WHERE character_id=?", (e.npc,)).fetchone()[0] == before_movements
        assert c.execute("SELECT COUNT(*) FROM world_events WHERE world_id=? AND actor_id=? "
                         "AND event_type='action.movement_started'", (e.wid, e.npc)).fetchone()[0] == before_events
        assert [dict(c.execute("SELECT * FROM characters WHERE id=?", (cid,)).fetchone())
                for cid in (e.player, e.npc)] == before_people
        assert c.execute("SELECT status FROM social_appointments WHERE id=?", (aid,)).fetchone()[0] == "accepted"
