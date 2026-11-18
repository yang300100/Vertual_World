"""哥哥，这些验收只新建 C:/tmp 下的隔离世界，不调用模型或正式存档。"""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from world_engine.group_journeys import GroupJourneyIntent as Intent
from world_engine.group_journeys import GroupJourneyService as Groups
from world_engine.actions import ActionService
from world_engine.clock import WorldClockService
from world_engine.config import Settings
from world_engine.daily_life import DailyLifeService
from world_engine.database import Database
from world_engine.movement import MovementService
from world_engine.repository import WorldRepository, from_iso, to_iso


@pytest.fixture
def group(tmp_path, monkeypatch):
    settings = Settings(database_path=tmp_path / "group.db", minutes_per_tick=60)
    db = Database(settings.database_path)
    db.initialize()
    at = datetime(2030, 1, 1, 8, tzinfo=UTC)
    repo = WorldRepository()
    with db.write() as c:
        wid = repo.create_world(c, name="隔离有限三人旅行", minutes_per_tick=60, start_time=at, seed_maps=False)
        loc = dict(c.execute("SELECT * FROM locations WHERE world_id=? AND name='晨星广场'", (wid,)).fetchone())
        player = repo.create_player_character(c, world_id=wid, name="验收旅人", identity="旅人", location_id=loc["id"], traits=[], goal="真实有限同行")
        npcs = c.execute("SELECT id FROM characters WHERE world_id=? AND is_player=0 ORDER BY name", (wid,)).fetchall()
        npc, leader, outside = (n[0] for n in npcs)
        ids = [player.id, npc, leader]
        places = c.execute("SELECT id FROM locations WHERE world_id=? AND id<>? ORDER BY name", (wid, loc["id"])).fetchall()
        sites = [p[0] for p in places]
        # 哥哥，以下坐标、身体和历史只是独立验收世界的明确初始场景，不是执行时补状态。
        for index, lid in enumerate(sites, 1):
            c.execute("UPDATE locations SET longitude=?,latitude=?,area_radius_km=0.06 WHERE id=?", (loc["longitude"] + 0.003 * index, loc["latitude"], lid))
        c.execute("UPDATE locations SET area_radius_km=0.06 WHERE id=?", (loc["id"],))
        for index, cid in enumerate(ids):
            c.execute("UPDATE characters SET longitude=?,latitude=?,location_id=?,current_location_id=?,current_room_id=NULL,current_fixture_id=NULL,active_vehicle_id=NULL,movement_type='land',movement_speed_kmh=?,energy=95,satiety=95,health=100,identity='旅人',traits_json='[]' WHERE id=?", (loc["longitude"] + index * 0.00004, loc["latitude"], loc["id"], loc["id"], 5 - index, cid))
            for lid in [loc["id"], *sites]:
                ActionService._record_event(c, world_id=wid, tick_id=str(uuid4()), occurred_at=at - timedelta(days=2), event_type="action.socialize", actor_id=cid, target_id=None, location_id=lid, summary="隔离验收的初始历史：本人两日前曾实际到访该地点。", payload={"fixture_initial_history": True})
        c.execute("UPDATE world_clock SET time_scale=1,next_adjudication_world_time=? WHERE world_id=?", (to_iso(at + timedelta(days=10)), wid))
        c.execute("UPDATE worlds SET current_time=? WHERE id=?", (to_iso(at), wid))
        Groups.install(c)
    e = SimpleNamespace(db=db, settings=settings, clock=WorldClockService(db, settings), wid=wid, at=at, player=player.id, npc=npc, leader=leader, outsider=outside, ids=ids, sites=sites, loc=loc)

    def group_dispatch(c, wid, now, **kwargs):
        # 哥哥，这个适配器只补未来正式调度的三处调用，身体和移动仍由真实 WorldClock 结算。
        Groups.maintain(c, wid, now)
        for person in c.execute("SELECT * FROM characters WHERE world_id=? AND is_player=0", (wid,)).fetchall():
            Groups.perform(c, person, now)

    monkeypatch.setattr(DailyLifeService, "tick", group_dispatch)
    return e


def now(e, c):
    return from_iso(c.execute("SELECT w.current_time FROM worlds w WHERE w.id=?", (e.wid,)).fetchone()[0])


def choose(e, cid, operation, jid=None, **extra):
    with e.db.write() as c:
        actor = c.execute("SELECT * FROM characters WHERE id=?", (cid,)).fetchone()
        return Groups.apply(c, actor, now(e, c), Intent(operation=operation, journey_id=jid, statement="这是我本人对原完整有限条款的明确选择。", **extra))


def offer(e, **extra):
    args = dict(member_ids=e.ids, leader_id=e.leader, meeting_location_id=e.loc["id"], deadline_world_time=to_iso(e.at + timedelta(hours=5)), stops=[{"location_id": e.sites[0], "minutes": 2}, {"location_id": e.sites[1], "minutes": 3}], return_to_origin=True)
    args.update(extra)
    return choose(e, e.player, "propose", **args)["journey_id"]


def accept(e, jid):
    for cid in (e.npc, e.leader):
        result = choose(e, cid, "accept", jid)
    assert result["status"] == "accepted"
    return jid


def get(e, jid):
    with e.db.read() as c:
        return dict(Groups._row(c, jid))


def members(e, jid):
    with e.db.read() as c:
        return [dict(m) for m in Groups._members(c, Groups._row(c, jid), remaining=False)]


def moves(e):
    with e.db.read() as c:
        return [dict(m) for m in c.execute("SELECT * FROM character_movements WHERE world_id=? ORDER BY rowid", (e.wid,))]


def start(e, jid):
    choose(e, e.player, "follow", jid)
    return choose(e, e.leader, "depart", jid)


def step(e, seconds):
    # 哥哥，真实心跳按新的边界逐段调用；不伪造抵达、不直接写人物坐标。
    remaining = seconds
    while remaining > 0.0000001:
        with e.db.read() as c:
            at = now(e, c)
            boundary = Groups.boundary(c, e.wid, at, at + timedelta(seconds=min(remaining, 3600)))
        duration = min(remaining, max(0.000001, (boundary - at).total_seconds()))
        e.clock.heartbeat(e.wid, elapsed_seconds=duration)
        remaining -= duration


def arrive(e):
    with e.db.read() as c:
        at = now(e, c)
        due = max(from_iso(r[0]) for r in c.execute("SELECT estimated_arrival_world FROM character_movements WHERE world_id=? AND status='moving'", (e.wid,)))
    step(e, (due - at).total_seconds())


def test_proposal_is_only_proposers_consent_and_other_two_answer_independently(group):
    e = group
    jid = offer(e)
    assert [m["decision"] for m in members(e, jid)] == ["accepted", "pending", "pending"]
    assert choose(e, e.npc, "accept", jid)["status"] == "pending"
    with pytest.raises(ValueError, match="必须全体"):
        choose(e, e.player, "follow", jid)
    assert choose(e, e.leader, "accept", jid)["status"] == "accepted"
    assert not moves(e)
    assert len({m["decision_event_id"] for m in members(e, jid)}) == 3


def test_one_decline_preserves_others_answers_and_never_shrinks_roster(group):
    e = group
    jid = offer(e)
    first = choose(e, e.npc, "decline", jid)
    choose(e, e.leader, "accept", jid)
    assert get(e, jid)["status"] == "pending"
    assert [m["decision"] for m in members(e, jid)] == ["accepted", "declined", "accepted"]
    assert choose(e, e.npc, "decline", jid)["event_id"] == first["event_id"]
    with pytest.raises(ValueError):
        choose(e, e.leader, "depart", jid)
    assert choose(e, e.player, "end", jid)["status"] == "ended"
    assert len(members(e, jid)) == 3 and not moves(e)


def test_outsider_and_other_world_cannot_respond(group):
    e = group
    jid = offer(e)
    with pytest.raises(ValueError, match="原名单"):
        choose(e, e.outsider, "accept", jid)
    with e.db.write() as c:
        wid = WorldRepository().create_world(c, name="另一个世界", minutes_per_tick=60, seed_maps=False)
        actor = c.execute("SELECT * FROM characters WHERE world_id=? LIMIT 1", (wid,)).fetchone()
        with pytest.raises(ValueError, match="不属于本世界"):
            Groups.apply(c, actor, e.at, Intent(operation="accept", journey_id=jid, statement="我来自别的世界。"))


@pytest.mark.parametrize("change", ["duplicate", "missing_player", "leader_outside", "proposer_outside", "return_omitted", "too_short", "naive", "stops7"])
def test_strict_proposal_boundaries(group, change):
    e = group
    args = {}
    if change == "duplicate": args["member_ids"] = [e.player, e.npc, e.npc]
    if change == "missing_player": args["member_ids"] = [e.npc, e.leader]
    if change == "leader_outside": args["leader_id"] = e.outsider
    if change == "proposer_outside": args["member_ids"] = [e.npc, e.leader]
    if change == "return_omitted": args["return_to_origin"] = None
    if change == "too_short": args["deadline_world_time"] = to_iso(e.at + timedelta(minutes=29))
    if change == "naive": args["deadline_world_time"] = "2030-01-01T12:00:00"
    if change == "stops7": args["stops"] = [{"location_id": e.sites[0], "minutes": 0}] * 7
    with pytest.raises((ValueError, ValidationError)):
        offer(e, **args)
    with e.db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM group_journeys").fetchone()[0] == 0


def test_third_npc_leader_accepts_and_requires_separate_player_follow(group):
    e = group
    jid = accept(e, offer(e))
    step(e, 20)
    assert not moves(e)
    with pytest.raises(ValueError, match="有效跟随"):
        choose(e, e.leader, "depart", jid)
    with pytest.raises(ValueError, match="只有仍参加的玩家"):
        choose(e, e.npc, "follow", jid)
    with pytest.raises(ValueError, match="原领队本人"):
        choose(e, e.player, "depart", jid)
    assert start(e, jid)["status"] == "active"


def test_npc_proposal_counts_only_its_own_consent(group):
    e = group
    with e.db.write() as c:
        actor = c.execute("SELECT * FROM characters WHERE id=?", (e.leader,)).fetchone()
        jid = Groups.apply(c, actor, e.at, Intent(operation="propose", member_ids=e.ids, leader_id=e.leader, meeting_location_id=e.loc["id"], deadline_world_time=to_iso(e.at + timedelta(hours=4)), stops=[{"location_id": e.sites[0], "minutes": 0}], return_to_origin=False, statement="我本人提议带领这份有限路线。"))["journey_id"]
    assert [m["decision"] for m in members(e, jid)] == ["pending", "pending", "accepted"]
    choose(e, e.player, "accept", jid)
    assert choose(e, e.npc, "accept", jid)["status"] == "accepted"


def test_unknown_npc_route_is_not_probed_by_proposal_but_leader_cannot_accept(group):
    e = group
    with e.db.write() as c:
        c.execute("DELETE FROM event_observers WHERE character_id=? AND event_id IN (SELECT id FROM world_events WHERE location_id=?)", (e.leader, e.sites[1]))
    jid = offer(e)
    choose(e, e.npc, "accept", jid)
    with pytest.raises(ValueError, match="本人不能确认"):
        choose(e, e.leader, "accept", jid)
    assert members(e, jid)[2]["decision"] == "pending"


def test_real_three_legs_dwell_return_independent_origins_and_slowest_speed(group):
    e = group
    jid = accept(e, offer(e))
    wallets = None
    with e.db.read() as c:
        wallets = list(c.execute("SELECT id,money FROM characters ORDER BY id"))
    start(e, jid)
    first = moves(e)
    assert len(first) == 3 and len({m["origin_longitude"] for m in first}) == 3
    assert len({m["estimated_arrival_world"] for m in first}) == 1
    assert all(m["speed_kmh"] <= 3 for m in first)
    arrive(e)
    assert get(e, jid)["phase"] == "stopped" and get(e, jid)["next_index"] == 1
    step(e, 119)
    assert len(moves(e)) == 3
    step(e, 1)
    assert len(moves(e)) == 6 and get(e, jid)["phase"] == "moving"
    arrive(e)
    step(e, 180)
    assert len(moves(e)) == 9
    arrive(e)
    assert get(e, jid)["status"] == "completed"
    assert get(e, jid)["follow_source_event_id"] is None
    with e.db.read() as c:
        assert [tuple(w) for w in c.execute("SELECT id,money FROM characters ORDER BY id")] == [tuple(w) for w in wallets]
        assert c.execute("SELECT COUNT(*) FROM group_journey_legs WHERE status='arrived'").fetchone()[0] == 3
        assert c.execute("SELECT COUNT(*) FROM group_journey_leg_members").fetchone()[0] == 9
        for cid in e.ids:
            person = c.execute("SELECT * FROM characters WHERE id=?", (cid,)).fetchone()
            assert person["longitude"] == pytest.approx(e.loc["longitude"])
            assert person["satiety"] < 95


def test_continue_policy_personal_exit_stops_only_self_remaining_two_finish(group):
    e = group
    jid = accept(e, offer(e, separation_policy="continue"))
    start(e, jid)
    step(e, 20)
    before = moves(e)
    result = choose(e, e.npc, "leave", jid)
    assert result["status"] == "active"
    assert {m["character_id"]: m["status"] for m in moves(e)} == {e.player: "moving", e.npc: "cancelled", e.leader: "moving"}
    assert moves(e)[1]["distance_travelled_km"] == before[1]["distance_travelled_km"] > 0
    step(e, 1800)
    assert get(e, jid)["status"] == "completed"
    assert len(moves(e)) == 7
    with e.db.read() as c:
        source = members(e, jid)[1]["left_event_id"]
        assert c.execute("SELECT COUNT(DISTINCT character_id) FROM character_memories WHERE event_id=?", (source,)).fetchone()[0] == 3


def test_default_exit_pauses_and_only_remaining_members_resume_then_follow_again(group):
    e = group
    jid = accept(e, offer(e))
    start(e, jid)
    step(e, 15)
    assert choose(e, e.npc, "leave", jid)["status"] == "paused"
    assert not [m for m in moves(e) if m["status"] == "moving"]
    assert choose(e, e.player, "resume", jid)["status"] == "resume_pending"
    step(e, 10)
    assert len(moves(e)) == 3
    assert choose(e, e.leader, "resume", jid)["status"] == "active"
    step(e, 10)
    assert len(moves(e)) == 3
    assert get(e, jid)["follow_source_event_id"] is None
    with pytest.raises(ValueError, match="本人已退出"):
        choose(e, e.npc, "resume", jid)
    start(e, jid)
    assert len(moves(e)) == 5


@pytest.mark.parametrize("operation", ["leave", "health"])
def test_leader_exit_or_incapacity_pauses_everyone_without_selecting_new_leader(group, operation):
    e = group
    jid = accept(e, offer(e, separation_policy="continue"))
    start(e, jid)
    step(e, 15)
    if operation == "leave":
        choose(e, e.leader, "leave", jid)
    else:
        with e.db.write() as c:
            c.execute("UPDATE characters SET health=30 WHERE id=?", (e.leader,))
            Groups.maintain(c, e.wid, now(e, c))
    assert get(e, jid)["status"] == "paused" and get(e, jid)["leader_id"] == e.leader
    assert get(e, jid)["follow_source_event_id"] is None
    assert all(m["status"] == "cancelled" for m in moves(e))
    if operation == "leave":
        with pytest.raises(ValueError, match="原领队已退出"):
            choose(e, e.player, "resume", jid)


def test_last_real_start_failure_rolls_back_first_two_movements_events_and_player_follow(group, monkeypatch):
    e = group
    jid = accept(e, offer(e))
    choose(e, e.player, "follow", jid)
    with e.db.read() as c:
        before = [tuple(r) for r in c.execute("SELECT id,longitude,latitude,money FROM characters ORDER BY id")]
        events = c.execute("SELECT COUNT(*) FROM world_events").fetchone()[0]
    original = MovementService._start_solo
    observed = []

    def fail_last(self, c, **kwargs):
        if kwargs["character_id"] == e.leader:
            observed.append(c.execute("SELECT COUNT(*) FROM character_movements WHERE status='moving'").fetchone()[0])
            raise ValueError("最后一名成员的真实路线不能通行")
        return original(self, c, **kwargs)

    monkeypatch.setattr(MovementService, "_start_solo", fail_last)
    with pytest.raises(ValueError, match="不能通行"):
        choose(e, e.leader, "depart", jid)
    assert observed == [2] and not moves(e)
    with e.db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM world_events").fetchone()[0] == events
        assert [tuple(r) for r in c.execute("SELECT id,longitude,latitude,money FROM characters ORDER BY id")] == before
        assert Groups.following(c, Groups._row(c, jid), now(e, c))


@pytest.mark.parametrize("tamper", ["member", "stop", "leader", "deadline", "source_actor", "source_time", "source_payload", "accept_actor", "accept_future", "resume_epoch"])
def test_tampered_terms_acceptance_or_follow_never_start(group, tamper):
    e = group
    jid = accept(e, offer(e))
    choose(e, e.player, "follow", jid)
    with e.db.write() as c:
        row = Groups._row(c, jid)
        source = row["follow_source_event_id"]
        if tamper == "member": c.execute("UPDATE group_journey_members SET ordinal=10 WHERE journey_id=? AND character_id=?", (jid, e.npc))
        if tamper == "stop": c.execute("UPDATE group_journey_stops SET minutes=minutes+1 WHERE journey_id=? AND stop_index=0", (jid,))
        if tamper == "leader": c.execute("UPDATE group_journeys SET leader_id=? WHERE id=?", (e.npc, jid))
        if tamper == "deadline": c.execute("UPDATE group_journeys SET deadline_at=? WHERE id=?", (to_iso(e.at + timedelta(hours=6)), jid))
        if tamper == "source_actor": c.execute("UPDATE world_events SET actor_id=? WHERE id=?", (e.npc, source))
        if tamper == "source_time": c.execute("UPDATE world_events SET occurred_at=? WHERE id=?", (to_iso(e.at + timedelta(minutes=1)), source))
        if tamper == "source_payload": c.execute("UPDATE world_events SET payload_json='{}' WHERE id=?", (source,))
        if tamper in {"accept_actor", "accept_future"}:
            source = c.execute("SELECT decision_event_id FROM group_journey_members WHERE journey_id=? AND character_id=?", (jid, e.npc)).fetchone()[0]
            if tamper == "accept_actor": c.execute("UPDATE world_events SET actor_id=? WHERE id=?", (e.player, source))
            else: c.execute("UPDATE world_events SET occurred_at=? WHERE id=?", (to_iso(e.at + timedelta(days=1)), source))
        if tamper == "resume_epoch": c.execute("UPDATE group_journeys SET resume_epoch=resume_epoch+1 WHERE id=?", (jid,))
        Groups.maintain(c, e.wid, now(e, c))
    assert get(e, jid)["status"] == "paused" and get(e, jid)["follow_source_event_id"] is None
    assert not moves(e)


def test_expiry_clears_follow_and_preserves_unfinished_real_result(group):
    e = group
    jid = accept(e, offer(e, deadline_world_time=to_iso(e.at + timedelta(minutes=30))))
    start(e, jid)
    step(e, 10)
    choose(e, e.player, "pause", jid)
    step(e, 1800)
    assert get(e, jid)["status"] == "expired"
    assert get(e, jid)["follow_source_event_id"] is None
    assert all(m["status"] == "cancelled" and m["distance_travelled_km"] > 0 for m in moves(e))


def test_pause_preserves_unfinished_dwell_and_restart_is_idempotent(group):
    e = group
    jid = accept(e, offer(e))
    start(e, jid)
    arrive(e)
    step(e, 40)
    choose(e, e.player, "pause", jid)
    assert get(e, jid)["remaining_stop_seconds"] == pytest.approx(80)
    step(e, 240)
    e.db = Database(e.settings.database_path)
    e.clock = WorldClockService(e.db, e.settings)
    with e.db.write() as c:
        Groups.install(c)
        before = c.execute("SELECT COUNT(*) FROM world_events").fetchone()[0]
        Groups.maintain(c, e.wid, now(e, c))
        Groups.maintain(c, e.wid, now(e, c))
        assert c.execute("SELECT COUNT(*) FROM world_events").fetchone()[0] == before
    choose(e, e.player, "resume", jid)
    choose(e, e.npc, "resume", jid)
    choose(e, e.leader, "resume", jid)
    choose(e, e.player, "follow", jid)
    step(e, 79)
    assert len(moves(e)) == 3
    step(e, 1)
    assert len(moves(e)) == 6


def test_old_group_end_never_cancels_members_later_independent_route(group):
    e = group
    jid = accept(e, offer(e, separation_policy="continue"))
    start(e, jid)
    step(e, 10)
    choose(e, e.npc, "leave", jid)
    with e.db.write() as c:
        current = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        independent = MovementService().start(c, world_id=e.wid, character_id=e.npc, destination_longitude=current["longitude"] - 0.005, destination_latitude=current["latitude"], world_time=now(e, c))
    choose(e, e.player, "end", jid)
    assert next(m for m in moves(e) if m["id"] == independent.id)["status"] == "moving"


def test_independent_departure_transaction_keeps_old_follow_when_new_route_fails(group):
    e = group
    jid = accept(e, offer(e))
    choose(e, e.player, "follow", jid)
    with e.db.write() as c:
        player = c.execute("SELECT * FROM characters WHERE id=?", (e.player,)).fetchone()
        with pytest.raises(ValueError):
            Groups.start_independent(c, world_id=e.wid, character_id=e.player, destination_longitude=player["longitude"], destination_latitude=player["latitude"], world_time=now(e, c))
        assert Groups.following(c, Groups._row(c, jid), now(e, c))
        independent = Groups.start_independent(c, world_id=e.wid, character_id=e.player, destination_longitude=player["longitude"] - 0.005, destination_latitude=player["latitude"], world_time=now(e, c))
    assert get(e, jid)["status"] == "paused" and get(e, jid)["follow_source_event_id"] is None
    choose(e, e.player, "end", jid)
    assert next(m for m in moves(e) if m["id"] == independent.id)["status"] == "moving"


def test_private_positions_body_schedule_and_visited_lists_never_enter_shared_context(group):
    e = group
    jid = accept(e, offer(e))
    with e.db.read() as c:
        rows = Groups.rows(c, e.wid, e.player, now(e, c))
        context = Groups.context(c, e.wid, e.player, e.npc, now(e, c))
        assert rows and context
        text = json.dumps(rows, ensure_ascii=False)
        for private in ("longitude", "latitude", "health", "satiety", "energy", "visited", "follow_terms_json", "movement_id"):
            assert private not in text
        assert not Groups.rows(c, e.wid, e.outsider, now(e, c))
        assert get(e, jid)["status"] == "accepted"


def test_final_stop_requires_its_full_dwell_and_completion_is_idempotent(group):
    e = group
    jid = accept(e, offer(e, stops=[{"location_id": e.sites[0], "minutes": 2}], return_to_origin=False))
    start(e, jid)
    arrive(e)
    assert get(e, jid)["status"] == "active"
    step(e, 119)
    assert get(e, jid)["status"] == "active"
    step(e, 1)
    assert get(e, jid)["status"] == "completed"
    with e.db.write() as c:
        before = c.execute("SELECT COUNT(*) FROM world_events").fetchone()[0]
        Groups.maintain(c, e.wid, now(e, c))
        Groups.maintain(c, e.wid, now(e, c))
        assert c.execute("SELECT COUNT(*) FROM world_events").fetchone()[0] == before


def test_all_pair_contact_boundary_blocks_third_person(group):
    e = group
    jid = offer(e)
    with e.db.write() as c:
        source = ActionService._record_event(c, world_id=e.wid, tick_id=str(uuid4()), occurred_at=e.at, event_type="social.intimacy_pause", actor_id=e.npc, target_id=e.leader, location_id=None, summary="本人要求暂停联系。", payload={})
        c.execute("INSERT INTO intimate_boundaries(world_id,character_id,other_id,statement,pause_contact,updated_at,source_event_id) VALUES (?,?,?,'请给我空间',1,?,?)", (e.wid, e.npc, e.leader, to_iso(e.at), source))
    with pytest.raises(ValueError, match="留出空间"):
        choose(e, e.npc, "accept", jid)


def test_prior_real_two_person_journey_conflicts_with_group_acceptance(group):
    e = group
    jid = offer(e)
    with e.db.write() as c:
        actor = c.execute("SELECT * FROM characters WHERE id=?", (e.player,)).fetchone()
        npc = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        from world_engine.journeys import JourneyIntent, JourneyService
        old = JourneyService.apply(c, actor, npc, now(e, c), JourneyIntent(operation="propose", meeting_location_id=e.loc["id"], deadline_world_time=to_iso(e.at + timedelta(hours=3)), stops=[{"location_id": e.sites[0], "minutes": 0}], return_to_origin=False, statement="我本人提议旧双人有限路线。"))["journey_id"]
        JourneyService.apply(c, npc, actor, now(e, c), JourneyIntent(operation="accept", journey_id=old, statement="我本人接受这份双人行程。"))
    with pytest.raises(ValueError, match="双人有限旅行"):
        choose(e, e.npc, "accept", jid)


def test_integration_boundary_is_bounded_and_real_clock_can_finish_large_step(group):
    e = group
    jid = accept(e, offer(e))
    choose(e, e.player, "follow", jid)
    with e.db.read() as c:
        assert Groups.boundary(c, e.wid, e.at, e.at + timedelta(hours=1)) == e.at + timedelta(seconds=1)
    step(e, 1800)
    assert get(e, jid)["status"] == "completed"
    assert len(moves(e)) == 9
    with e.db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM world_heartbeats WHERE world_id=?", (e.wid,)).fetchone()[0] > 3


def meeting_offer(e):
    return accept(e, offer(e, meeting_location_id=e.sites[0], stops=[{"location_id": e.sites[1], "minutes": 1}], deadline_world_time=to_iso(e.at + timedelta(minutes=30))))


def test_members_really_walk_to_explicit_meeting_and_player_chooses_own_meeting_route(group):
    e = group
    jid = meeting_offer(e)
    step(e, 1)
    assert len(moves(e)) == 2 and all(m["character_id"] != e.player for m in moves(e))
    step(e, 400)
    assert get(e, jid)["status"] == "accepted"
    assert all(m["status"] == "arrived" for m in moves(e))
    with e.db.write() as c:
        origin = c.execute("SELECT * FROM locations WHERE id=?", (e.sites[0],)).fetchone()
        Groups.start_independent(c, world_id=e.wid, character_id=e.player, destination_longitude=origin["longitude"], destination_latitude=origin["latitude"], world_time=now(e, c))
    assert get(e, jid)["status"] == "accepted"
    arrive(e)
    start(e, jid)
    assert len(moves(e)) == 6 and get(e, jid)["status"] == "active"


@pytest.mark.parametrize("operation", ["pause", "end", "leave", "expire"])
def test_meeting_movement_is_bound_and_cancelled_when_original_authority_stops(group, operation):
    e = group
    jid = meeting_offer(e)
    step(e, 10)
    assert len(moves(e)) == 2 and all(m["status"] == "moving" for m in moves(e))
    if operation == "expire":
        with e.db.write() as c:
            Groups.maintain(c, e.wid, e.at + timedelta(minutes=30))
        assert get(e, jid)["status"] == "expired"
    else:
        choose(e, e.npc if operation == "leave" else e.player, operation, jid)
    assert all(m["status"] == "cancelled" for m in moves(e))


def test_sleep_boundary_stops_real_movement_at_true_npc_bedtime(group):
    e = group
    late = e.at.replace(hour=21, minute=59)
    with e.db.write() as c:
        c.execute("UPDATE worlds SET current_time=? WHERE id=?", (to_iso(late), e.wid))
    jid = accept(e, offer(e, deadline_world_time=to_iso(late + timedelta(hours=4))))
    start(e, jid)
    with e.db.read() as c:
        assert Groups.boundary(c, e.wid, late, late + timedelta(hours=1)) == late.replace(hour=22, minute=0)
    step(e, 60)
    assert get(e, jid)["status"] == "paused"
    assert all(m["status"] == "cancelled" and m["distance_travelled_km"] > 0 for m in moves(e))


def test_exited_proposer_receives_no_later_group_knowledge_or_live_phase(group):
    e = group
    jid = accept(e, offer(e, separation_policy="continue"))
    choose(e, e.player, "leave", jid)
    step(e, 1000)
    with e.db.read() as c:
        assert not c.execute("SELECT 1 FROM character_event_knowledge k JOIN world_events ev ON ev.id=k.event_id WHERE k.character_id=? AND ev.event_type IN ('social.group_journey_departed','social.group_journey_arrived')", (e.player,)).fetchone()
        row = Groups.rows(c, e.wid, e.player, now(e, c))[0]
        assert row["status"] == "left" and row["phase"] is None and row["resume_epoch"] is None
        assert row["next_index"] == 0 and all(s["arrived_at"] is None for s in row["stops"])
        assert all(m["resumed"] is None for m in row["members"])


def test_late_actual_arrival_is_never_clamped_back_to_deadline(group):
    e = group
    jid = accept(e, offer(e, stops=[{"location_id": e.sites[0], "minutes": 0}], return_to_origin=False, deadline_world_time=to_iso(e.at + timedelta(minutes=30))))
    start(e, jid)
    # 哥哥，故意只调用一次真实移动结算，模拟未接边界的延迟回执；不得把晚到时间倒填。
    with e.db.write() as c:
        late = e.at + timedelta(minutes=30, seconds=1)
        MovementService().advance(c, world_id=e.wid, previous_time=e.at, current_time=late, created_at=late)
        Groups.maintain(c, e.wid, late)
        row = Groups._row(c, jid)
        assert row["status"] == "expired"
        assert c.execute("SELECT arrived_at FROM group_journey_stops WHERE journey_id=? AND stop_index=0", (jid,)).fetchone()[0] is None


def test_pending_second_proposal_cannot_commit_proposer_to_two_live_groups(group):
    e = group
    first, second = offer(e), offer(e)
    accept(e, first)
    choose(e, e.npc, "decline", second)
    with pytest.raises(ValueError, match="另一份多人"):
        choose(e, e.leader, "accept", second)
    assert get(e, second)["status"] == "pending"
    with pytest.raises(ValueError, match="另一份多人"):
        offer(e)


def test_inactive_historical_meeting_place_does_not_break_shared_rows(group):
    e = group
    jid = accept(e, offer(e))
    with e.db.write() as c:
        c.execute("UPDATE locations SET is_active=0 WHERE id=?", (e.loc["id"],))
        Groups.maintain(c, e.wid, now(e, c))
        assert Groups._row(c, jid)["status"] == "ended"
        assert Groups.rows(c, e.wid, e.player, now(e, c))[0]["origin_name"] == e.loc["name"]


def test_member_withdraws_resume_and_must_personally_resume_new_epoch(group):
    e = group
    jid = accept(e, offer(e, leader_id=e.player))
    choose(e, e.player, "depart", jid)
    step(e, 10)
    choose(e, e.player, "pause", jid)
    choose(e, e.npc, "resume", jid)
    previous = get(e, jid)["resume_epoch"]
    choose(e, e.npc, "pause", jid)
    assert get(e, jid)["resume_epoch"] == previous + 1
    choose(e, e.player, "resume", jid)
    assert choose(e, e.leader, "resume", jid)["status"] == "resume_pending"
    with pytest.raises(ValueError, match="分别恢复"):
        choose(e, e.player, "depart", jid)
    assert choose(e, e.npc, "resume", jid)["status"] == "active"
    choose(e, e.player, "depart", jid)


def test_historical_context_never_reveals_later_departure_or_arrival(group):
    e = group
    jid = accept(e, offer(e))
    step(e, 1)
    start(e, jid)
    arrive(e)
    with e.db.read() as c:
        row = Groups.rows(c, e.wid, e.player, e.at)[0]
        assert row["status"] == "accepted" and row["phase"] == "ready"
        assert row["next_index"] == 0 and not row["following"]
        assert all(stop["arrived_at"] is None for stop in row["stops"])
