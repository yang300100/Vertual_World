"""哥哥，分房赴约由 NPC 自己逐门完成，身体需要与原有活动仍然优先。"""

# ruff: noqa: F811
from datetime import timedelta
from types import SimpleNamespace

from test_appointment_rooms import door
from test_appointments import people
from test_journey_room_appointments import current_time, room_at_stop
from test_journeys import get as journey
from test_lived_world import town  # noqa: F401
from test_overnight_stays import stopped_long_journey

from world_engine.daily_life import DailyLifeService
from world_engine.life import LifeActivityService
from world_engine.repository import from_iso, to_iso
from world_engine.separate_stays import SeparateStayIntent, SeparateStayService


def arrangement(e, *, nested=False):
    jid = stopped_long_journey(e)
    player_room = room_at_stop(e, jid, owner=e.player, name="旅人自己的客房")
    outer = (
        room_at_stop(e, jid, owner=e.npc, policy="public", name="客房公共前厅") if nested else None
    )
    npc_room = room_at_stop(e, jid, owner=e.npc, parent=outer, name="林澈自己的客房")
    start = current_time(e) + timedelta(minutes=30)
    with e.db.write() as c:
        player, npc = people(e, c)
        result = SeparateStayService.apply(
            c,
            npc,
            player,
            current_time(e),
            SeparateStayIntent(
                operation="propose",
                journey_id=jid,
                own_room_id=npc_room,
                other_room_id=player_room,
                arrival_world_time=to_iso(start),
                minutes=480,
                statement="暂停原旅行，各住自己的指定房间休息八小时，随后回原站点室外按约会合。",
            ),
        )
        SeparateStayService.apply(
            c,
            player,
            npc,
            current_time(e),
            SeparateStayIntent(
                operation="accept",
                stay_id=result["stay_id"],
                statement="我本人同意这份分房休息与会合安排。",
            ),
        )
    return SimpleNamespace(
        id=result["stay_id"],
        jid=jid,
        player_room=player_room,
        npc_room=npc_room,
        outer=outer,
        start=start,
    )


def tick(e, at, *, adjudication_due=True):
    with e.db.write() as c:
        # 哥哥，只控制隔离测试时钟，不借旧辅助函数补满饱食或精力。
        c.execute("UPDATE worlds SET current_time=? WHERE id=?", (to_iso(at), e.wid))
        DailyLifeService.tick(c, e.wid, at, adjudication_due=adjudication_due)


def participant(e, sid, cid):
    with e.db.read() as c:
        return dict(
            c.execute(
                "SELECT * FROM journey_separate_stay_participants "
                "WHERE stay_id=? AND character_id=?",
                (sid, cid),
            ).fetchone()
        )


def daily_state(e):
    with e.db.read() as c:
        return dict(
            c.execute("SELECT * FROM npc_daily_states WHERE character_id=?", (e.npc,)).fetchone()
        )


def test_adjudication_free_time_still_enters_own_room_then_starts_own_rest(town):
    e = town
    stay = arrangement(e)
    tick(e, stay.start - timedelta(minutes=15))
    with e.db.read() as c:
        player, npc = people(e, c)
        assert npc["current_room_id"] == stay.npc_room and player["current_room_id"] is None
        assert not LifeActivityService.running(c, e.npc)
    due = from_iso(daily_state(e)["next_world_time"])
    assert due <= stay.start - timedelta(minutes=10)
    tick(e, due)
    assert from_iso(daily_state(e)["next_world_time"]) <= stay.start
    tick(e, stay.start)
    own = participant(e, stay.id, e.npc)
    assert own["status"] == "active" and from_iso(own["checked_in_at"]) == stay.start
    assert participant(e, stay.id, e.player)["status"] == "waiting"
    with e.db.read() as c:
        assert LifeActivityService.running(c, e.npc)["id"] == own["activity_id"]
        assert not LifeActivityService.running(c, e.player)
    assert journey(e, stay.jid)["status"] == "paused"


def test_each_nested_door_is_a_separate_scheduled_step_and_player_is_not_moved(town):
    e = town
    stay = arrangement(e, nested=True)
    tick(e, stay.start - timedelta(minutes=20))
    with e.db.read() as c:
        assert people(e, c)[1]["current_room_id"] == stay.outer
    second = from_iso(daily_state(e)["next_world_time"])
    assert second <= stay.start - timedelta(minutes=15)
    tick(e, second)
    with e.db.read() as c:
        player, npc = people(e, c)
        assert npc["current_room_id"] == stay.npc_room and player["current_room_id"] is None
    tick(e, stay.start)
    assert participant(e, stay.id, e.npc)["status"] == "active"
    assert participant(e, stay.id, e.player)["status"] == "waiting"


def test_npc_start_is_not_repeated_on_later_daily_tick(town):
    e = town
    stay = arrangement(e)
    tick(e, stay.start - timedelta(minutes=15))
    tick(e, stay.start)
    initial = participant(e, stay.id, e.npc)
    tick(e, stay.start + timedelta(minutes=5))
    assert participant(e, stay.id, e.npc)["activity_id"] == initial["activity_id"]
    with e.db.read() as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM character_life_activities WHERE id=?",
                (initial["activity_id"],),
            ).fetchone()[0]
            == 1
        )
        assert (
            c.execute(
                "SELECT COUNT(*) FROM world_events WHERE actor_id=? "
                "AND event_type='social.appointment_separate_stay_checked_in'",
                (e.npc,),
            ).fetchone()[0]
            == 1
        )


def test_hunger_is_handled_before_own_check_in_and_due_stay_is_rechecked(town):
    e = town
    stay = arrangement(e)
    tick(e, stay.start - timedelta(minutes=15))
    with e.db.write() as c:
        c.execute("UPDATE characters SET satiety=20 WHERE id=?", (e.npc,))
    tick(e, stay.start)
    with e.db.read() as c:
        assert people(e, c)[1]["satiety"] > 30
        assert not LifeActivityService.running(c, e.npc)
    assert participant(e, stay.id, e.npc)["status"] == "waiting"
    retry = from_iso(daily_state(e)["next_world_time"])
    assert stay.start < retry <= stay.start + timedelta(minutes=5)
    tick(e, retry)
    assert participant(e, stay.id, e.npc)["status"] == "active"


def test_existing_activity_is_not_replaced_to_begin_separate_rest(town):
    e = town
    stay = arrangement(e)
    tick(e, stay.start - timedelta(minutes=15))
    with e.db.write() as c:
        npc = people(e, c)[1]
        previous = LifeActivityService.start(c, npc, stay.start - timedelta(minutes=1), "rest", 120)
    tick(e, stay.start)
    with e.db.read() as c:
        assert LifeActivityService.running(c, e.npc)["id"] == previous
    assert participant(e, stay.id, e.npc)["status"] == "waiting"


def test_completed_rest_leaves_room_and_waits_at_reunion_without_resuming_trip(town):
    e = town
    stay = arrangement(e)
    tick(e, stay.start - timedelta(minutes=15))
    tick(e, stay.start)
    assert door(e, stay.player_room, "open").status_code == 200
    entered = door(e, stay.player_room, "enter")
    assert entered.status_code == 200, entered.text
    with e.db.write() as c:
        player, npc = people(e, c)
        SeparateStayService.apply(
            c,
            player,
            npc,
            stay.start,
            SeparateStayIntent(
                operation="check_in",
                stay_id=stay.id,
                statement="我在自己的约定客房开始自己的休息。",
            ),
        )
    e.clock.heartbeat(e.wid, elapsed_seconds=8 * 3600)
    with e.db.read() as c:
        row = c.execute("SELECT * FROM journey_separate_stays WHERE id=?", (stay.id,)).fetchone()
        assert row["status"] == "completed" and row["reunion_status"] == "waiting"
        assert people(e, c)[0]["current_room_id"] == stay.player_room
    tick(e, current_time(e) + timedelta(minutes=5))
    with e.db.read() as c:
        player, npc = people(e, c)
        assert npc["current_room_id"] is None and player["current_room_id"] == stay.player_room
        assert not LifeActivityService.running(c, e.npc)
    assert journey(e, stay.jid)["status"] == "paused"
    # 哥哥，NPC 自己守约等候，不能把同意住宿当作玩家已经离屋或同意恢复。
    tick(e, current_time(e) + timedelta(minutes=5))
    assert "等候约定会合" in daily_state(e)["intention"]
    with e.db.read() as c:
        assert (
            c.execute(
                "SELECT reunion_status FROM journey_separate_stays WHERE id=?", (stay.id,)
            ).fetchone()[0]
            == "waiting"
        )
    exited = door(e, stay.player_room, "exit")
    assert exited.status_code == 200, exited.text
    with e.db.read() as c:
        reunion = from_iso(
            c.execute(
                "SELECT reunion_starts_at FROM journey_separate_stays WHERE id=?",
                (stay.id,),
            ).fetchone()[0]
        )
    tick(e, reunion)
    with e.db.read() as c:
        assert (
            c.execute(
                "SELECT reunion_status FROM journey_separate_stays WHERE id=?",
                (stay.id,),
            ).fetchone()[0]
            == "met"
        )
        assert not LifeActivityService.running(c, e.npc)
    assert "等候约定会合" in daily_state(e)["intention"]
    assert journey(e, stay.jid)["status"] == "paused"
