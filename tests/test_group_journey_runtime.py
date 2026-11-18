"""哥哥，这些验收只新建 C:/tmp 下的隔离世界，不调用模型或正式存档。"""

import json
import sqlite3
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
def runtime(tmp_path):
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

    return e

from test_group_journeys import accept, choose, get, meeting_offer, moves, now, offer, start
from world_engine.companion_proposals import CompanionIntent, CompanionProposalService
from world_engine.journeys import JourneyIntent, JourneyService
from world_engine.player_steering import PlayerSteeringService
from world_engine.shared_life import SharedLifeIntent, SharedLifeService
from world_engine.shared_plans import SharedPlanIntent, SharedPlanService


def people(e, c):
    return [c.execute("SELECT * FROM characters WHERE id=?", (cid,)).fetchone() for cid in (e.player, e.npc)]


def old_choice(e, service, choice, *, reverse=False):
    with e.db.write() as c:
        first, second = people(e, c)
        if reverse:
            first, second = second, first
        return service.apply(c, first, second, now(e, c), choice)


def test_real_worldclock_and_unmodified_daily_life_complete_three_people_three_legs(runtime):
    e = runtime
    jid = accept(e, offer(e))
    choose(e, e.player, "follow", jid)
    result = e.clock.heartbeat(e.wid, elapsed_seconds=1800)
    assert result.world_delta_seconds == pytest.approx(1800)
    assert get(e, jid)["status"] == "completed"
    with e.db.read() as c:
        legs = c.execute("SELECT * FROM group_journey_legs WHERE journey_id=? ORDER BY stop_index", (jid,)).fetchall()
        assert len(legs) == 3 and all(leg["status"] == "arrived" for leg in legs)
        assert c.execute("SELECT COUNT(*) FROM group_journey_leg_members WHERE leg_id IN (SELECT id FROM group_journey_legs WHERE journey_id=?)", (jid,)).fetchone()[0] == 9
        for leg in legs:
            actual = c.execute("SELECT m.* FROM group_journey_leg_members lm JOIN character_movements m ON m.id=lm.movement_id WHERE lm.leg_id=?", (leg["id"],)).fetchall()
            assert len({m["id"] for m in actual}) == 3
            assert len({m["estimated_arrival_world"] for m in actual}) == 1
            assert all(m["status"] == "arrived" and m["distance_travelled_km"] > 0 for m in actual)
        assert len({m["origin_longitude"] for m in c.execute("SELECT m.* FROM group_journey_leg_members lm JOIN character_movements m ON m.id=lm.movement_id WHERE lm.leg_id=?", (legs[0]["id"],))}) == 3
        stops = c.execute("SELECT * FROM group_journey_stops WHERE journey_id=? ORDER BY stop_index", (jid,)).fetchall()
        assert all(s["arrived_at"] for s in stops)
        assert from_iso(legs[1]["started_at"]) == from_iso(stops[0]["arrived_at"]) + timedelta(minutes=2)
        assert from_iso(legs[2]["started_at"]) == from_iso(stops[1]["arrived_at"]) + timedelta(minutes=3)
        assert c.execute("SELECT COUNT(*) FROM world_heartbeats WHERE world_id=?", (e.wid,)).fetchone()[0] >= 6
        assert all(c.execute("SELECT satiety FROM characters WHERE id=?", (cid,)).fetchone()[0] < 95 for cid in e.ids)


def test_real_npcs_gather_without_moving_player_then_player_chooses_own_route(runtime):
    e = runtime
    jid = meeting_offer(e)
    e.clock.heartbeat(e.wid, elapsed_seconds=400)
    assert get(e, jid)["status"] == "accepted"
    with e.db.write() as c:
        bound = c.execute("SELECT m.* FROM group_journey_members gm JOIN character_movements m ON m.id=gm.meeting_movement_id WHERE gm.journey_id=?", (jid,)).fetchall()
        assert len(bound) == 2 and all(m["status"] == "arrived" for m in bound)
        assert all(m["character_id"] != e.player for m in bound)
        origin = c.execute("SELECT * FROM locations WHERE id=?", (e.sites[0],)).fetchone()
        MovementService().start(c, world_id=e.wid, character_id=e.player, destination_longitude=origin["longitude"], destination_latitude=origin["latitude"], world_time=now(e, c))
    e.clock.heartbeat(e.wid, elapsed_seconds=300)
    assert get(e, jid)["status"] == "accepted"
    choose(e, e.player, "follow", jid)
    e.clock.heartbeat(e.wid, elapsed_seconds=900)
    assert get(e, jid)["status"] == "completed"


def test_real_clock_handles_inactive_meeting_before_first_boundary(runtime):
    e = runtime
    jid = accept(e, offer(e))
    with e.db.write() as c:
        c.execute("UPDATE locations SET is_active=0 WHERE id=?", (e.loc["id"],))
    e.clock.heartbeat(e.wid, elapsed_seconds=1)
    assert get(e, jid)["status"] == "ended"


def test_failed_independent_route_keeps_follow_and_real_start_revokes_it(runtime):
    e = runtime
    jid = accept(e, offer(e))
    choose(e, e.player, "follow", jid)
    with e.db.write() as c:
        player = people(e, c)[0]
        with pytest.raises(ValueError):
            MovementService().start(c, world_id=e.wid, character_id=e.player, destination_longitude=181, destination_latitude=0, world_time=now(e, c))
        assert Groups.following(c, Groups._row(c, jid), now(e, c))
        movement = MovementService().start(c, world_id=e.wid, character_id=e.player, destination_longitude=player["longitude"] - 0.005, destination_latitude=player["latitude"], world_time=now(e, c))
    assert get(e, jid)["status"] == "paused" and not get(e, jid)["follow_source_event_id"]
    choose(e, e.player, "end", jid)
    assert next(m for m in moves(e) if m["id"] == movement.id)["status"] == "moving"


def test_cancel_bound_movement_pauses_all_and_clears_follow(runtime):
    e = runtime
    jid = accept(e, offer(e))
    start(e, jid)
    e.clock.heartbeat(e.wid, elapsed_seconds=10)
    with e.db.write() as c:
        MovementService().cancel(c, world_id=e.wid, character_id=e.npc, world_time=now(e, c))
    assert get(e, jid)["status"] == "paused" and not get(e, jid)["follow_source_event_id"]
    assert all(m["status"] == "cancelled" for m in moves(e) if m["character_id"] in e.ids)


def test_direction_input_revokes_predeparture_follow_and_invalid_input_rolls_back(runtime):
    e = runtime
    jid = accept(e, offer(e))
    choose(e, e.player, "follow", jid)
    with e.db.write() as c:
        actor = people(e, c)[0]
        with pytest.raises(sqlite3.IntegrityError):
            PlayerSteeringService.set_direction(c, actor, (None, 1), 1, now(e, c), e.at)
        assert Groups.following(c, Groups._row(c, jid), now(e, c))
        PlayerSteeringService.set_direction(c, actor, (1, 0), 2, now(e, c), e.at)
        assert PlayerSteeringService.active(c, e.player)
    assert get(e, jid)["status"] == "paused" and not get(e, jid)["follow_source_event_id"]


def test_invalid_vehicle_does_not_revoke_valid_follow(runtime):
    e = runtime
    jid = accept(e, offer(e))
    choose(e, e.player, "follow", jid)
    with e.db.write() as c:
        with pytest.raises(ValueError, match="不可用"):
            MovementService().select_transport(c, world_id=e.wid, character_id=e.player, vehicle_id="missing")
        assert Groups.following(c, Groups._row(c, jid), now(e, c))


def test_existing_pending_dual_invitation_cannot_be_accepted_after_group(runtime):
    e = runtime
    old = old_choice(e, JourneyService, JourneyIntent(operation="propose", meeting_location_id=e.loc["id"], deadline_world_time=to_iso(e.at + timedelta(hours=3)), stops=[{"location_id": e.sites[0], "minutes": 0}], return_to_origin=False, statement="我提议原双人旅行。"))["journey_id"]
    accept(e, offer(e))
    with pytest.raises(ValueError, match="多人有限"):
        old_choice(e, JourneyService, JourneyIntent(operation="accept", journey_id=old, statement="我接受原双人旅行。"), reverse=True)
    assert old_choice(e, JourneyService, JourneyIntent(operation="decline", journey_id=old, statement="我婉拒原双人旅行。"), reverse=True)["status"] == "declined"


def test_pending_short_invitation_cannot_start_after_group(runtime):
    e = runtime
    old = old_choice(e, CompanionProposalService, CompanionIntent(operation="propose", minutes=10, statement="我提议十分钟短时同行。"))["proposal_id"]
    accept(e, offer(e))
    with pytest.raises(ValueError, match="多人有限"):
        old_choice(e, CompanionProposalService, CompanionIntent(operation="accept", proposal_id=old, statement="我接受十分钟。"), reverse=True)
    assert old_choice(e, CompanionProposalService, CompanionIntent(operation="decline", proposal_id=old, statement="我婉拒。"), reverse=True)["status"] == "declined"


@pytest.mark.parametrize("group_first", [False, True])
@pytest.mark.parametrize("kind", ["moment", "preparation"])
def test_old_shared_activity_and_group_block_each_other_at_acceptance(runtime, group_first, kind):
    e = runtime
    if kind == "moment":
        service = SharedLifeService
        pending = old_choice(e, service, SharedLifeIntent(operation="invite", kind="talk", minutes=10, statement="我提议留十分钟聊天。"))["moment_id"]
        choice = SharedLifeIntent(operation="accept", moment_id=pending, statement="我接受原聊天安排。")
    else:
        service = SharedPlanService
        pending = old_choice(e, service, SharedPlanIntent(operation="propose", location_id=e.loc["id"], deadline_world_time=to_iso(e.at + timedelta(days=2)), own_task={"kind":"earn_money","target":12}, other_task={"kind":"prepare_food","target":1}, statement="我提议各自筹备后会合。"))["plan_id"]
        choice = SharedPlanIntent(operation="accept", plan_id=pending, statement="我接受本人原筹备分工。")
    jid = offer(e)
    if group_first:
        accept(e, jid)
        with pytest.raises(ValueError, match="多人有限"):
            old_choice(e, service, choice, reverse=True)
    else:
        old_choice(e, service, choice, reverse=True)
        with pytest.raises(ValueError, match="共同活动或筹备"):
            choose(e, e.npc, "accept", jid)


def test_group_migration_adds_exactly_five_tables_and_preserves_every_old_row(runtime):
    e = runtime
    names = {"group_journeys", "group_journey_members", "group_journey_stops", "group_journey_legs", "group_journey_leg_members"}
    with e.db.write() as c:
        for name in ("group_journey_leg_members", "group_journey_legs", "group_journey_stops", "group_journey_members", "group_journeys"):
            c.execute('DROP TABLE "' + name + '"')
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        before = {name: [tuple(row) for row in c.execute('SELECT * FROM "' + name + '" ORDER BY rowid')] for name in tables}
        Groups.install(c)
        after_names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert after_names - set(tables) == names
        for name in tables:
            assert [tuple(row) for row in c.execute('SELECT * FROM "' + name + '" ORDER BY rowid')] == before[name]
        assert all(c.execute('SELECT COUNT(*) FROM "' + name + '"').fetchone()[0] == 0 for name in names)
        Groups.install(c)
        assert {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")} == after_names


def test_delayed_refresh_uses_each_real_stop_boundary(runtime):
    e = runtime
    jid = accept(e, offer(e))
    choose(e, e.player, "follow", jid)
    with e.db.write() as c:
        c.execute("UPDATE world_clock SET last_heartbeat_real_time=? WHERE world_id=?", (to_iso(e.at), e.wid))
    result = e.clock.heartbeat(e.wid, real_now=e.at + timedelta(minutes=30))
    assert result.world_delta_seconds == pytest.approx(1800)
    assert get(e, jid)["status"] == "completed"
    with e.db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM group_journey_stops WHERE journey_id=? AND arrived_at IS NOT NULL", (jid,)).fetchone()[0] == 3


def test_worker_single_boundary_loop_finishes_all_stops(runtime):
    e = runtime
    jid = accept(e, offer(e))
    choose(e, e.player, "follow", jid)
    with e.db.write() as c:
        c.execute("UPDATE world_clock SET last_heartbeat_real_time=? WHERE world_id=?", (to_iso(e.at), e.wid))
    progress = {}
    for count in range(30):
        e.clock.heartbeat(e.wid, real_now=e.at + timedelta(minutes=30), _single_boundary=True, _catchup_progress=progress)
        if progress.get("complete"):
            break
    assert progress.get("complete") and count >= 5
    assert get(e, jid)["status"] == "completed"


def test_valid_vehicle_revokes_follow_after_permission_validation(runtime):
    e = runtime
    jid = accept(e, offer(e))
    choose(e, e.player, "follow", jid)
    with e.db.write() as c:
        vehicle_id = str(uuid4())
        c.execute("INSERT INTO vehicles(id,world_id,name,movement_type,speed_kmh,owner_character_id,metadata_json,created_at,updated_at) VALUES (?,?,'独立自行车','land',12,?,'{}',?,?)", (vehicle_id, e.wid, e.player, to_iso(e.at), to_iso(e.at)))
        MovementService().select_transport(c, world_id=e.wid, character_id=e.player, vehicle_id=vehicle_id)
        assert c.execute("SELECT active_vehicle_id FROM characters WHERE id=?", (e.player,)).fetchone()[0] == vehicle_id
    assert get(e, jid)["status"] == "paused" and not get(e, jid)["follow_source_event_id"]


def test_leader_becomes_incapable_and_every_remaining_member_must_resume(runtime):
    e = runtime
    jid = accept(e, offer(e))
    start(e, jid)
    e.clock.heartbeat(e.wid, elapsed_seconds=10)
    with e.db.write() as c:
        c.execute("UPDATE characters SET health=0 WHERE id=?", (e.leader,))
    e.clock.heartbeat(e.wid, elapsed_seconds=1)
    assert get(e, jid)["status"] == "paused" and not get(e, jid)["follow_source_event_id"]
    with e.db.write() as c:
        # 哥哥，只恢复本条故障注入的身体条件，实际途中位置和资源保持原样。
        c.execute("UPDATE characters SET health=100 WHERE id=?", (e.leader,))
        # 哥哥，暂停后 NPC 仍有自己的生活；恢复前本人先明确停下新开始的独立路线。
        for movement in c.execute("SELECT * FROM character_movements WHERE world_id=? AND status='moving'", (e.wid,)).fetchall():
            if movement["character_id"] in e.ids:
                MovementService().cancel(c, world_id=e.wid, character_id=movement["character_id"], world_time=now(e, c))
    choose(e, e.npc, "resume", jid)
    choose(e, e.leader, "resume", jid)
    assert get(e, jid)["status"] == "resume_pending"
    choose(e, e.player, "resume", jid)
    assert get(e, jid)["status"] == "active" and not get(e, jid)["follow_source_event_id"]
    with pytest.raises(ValueError, match="跟随授权"):
        choose(e, e.leader, "depart", jid)
    choose(e, e.player, "follow", jid)
    e.clock.heartbeat(e.wid, elapsed_seconds=1800)
    assert get(e, jid)["status"] == "completed"


def test_failed_gathering_route_pauses_group_without_rolling_back_real_clock(runtime, monkeypatch):
    e = runtime
    jid = meeting_offer(e)
    original = MovementService._plan_route
    def blocked(self, c, **kwargs):
        if kwargs["actor"]["id"] == e.npc:
            raise ValueError("隔离故障注入：本人集合路线被阻断")
        return original(self, c, **kwargs)
    monkeypatch.setattr(MovementService, "_plan_route", blocked)
    e.clock.heartbeat(e.wid, elapsed_seconds=1)
    assert get(e, jid)["status"] == "paused"
    with e.db.read() as c:
        assert now(e, c) == e.at + timedelta(seconds=1)
        assert not c.execute("SELECT 1 FROM group_journey_leg_members lm JOIN character_movements m ON m.id=lm.movement_id WHERE m.status='moving'").fetchone()


def test_short_companionship_already_accepted_blocks_group(runtime):
    e = runtime
    jid = offer(e)
    pending = old_choice(e, CompanionProposalService, CompanionIntent(operation="propose", minutes=10, statement="我提议十分钟同行。"))["proposal_id"]
    old_choice(e, CompanionProposalService, CompanionIntent(operation="accept", proposal_id=pending, statement="我本人接受十分钟同行。"), reverse=True)
    with pytest.raises(ValueError, match="短时同行"):
        choose(e, e.npc, "accept", jid)


def test_existing_group_blocks_future_appointment_entry_and_preserves_rejection(runtime):
    from world_engine.appointments import AppointmentIntent, AppointmentService
    e = runtime
    jid = accept(e, offer(e))
    with pytest.raises(ValueError, match="多人有限"):
        old_choice(e, AppointmentService, AppointmentIntent(operation="propose", kind="talk", location_id=e.loc["id"], meeting_world_time=to_iso(e.at + timedelta(hours=1)), window_minutes=10, minutes=10, statement="我提议一小时后聊天。"))
    assert get(e, jid)["status"] == "accepted"


@pytest.mark.parametrize("mode", ["collective", "science"])
def test_explicit_group_contract_runs_through_special_world_clock_branch(runtime, mode):
    e = runtime
    with e.db.write() as c:
        c.execute("INSERT INTO material_modes(world_id,policy_json) VALUES (?,?)", (e.wid, json.dumps({"mode": mode})))
    jid = accept(e, offer(e))
    choose(e, e.player, "follow", jid)
    e.clock.heartbeat(e.wid, elapsed_seconds=1800)
    assert get(e, jid)["status"] == "completed"
    with e.db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM group_journey_stops WHERE journey_id=? AND arrived_at IS NOT NULL", (jid,)).fetchone()[0] == 3
        assert not c.execute("SELECT 1 FROM science_model_calls WHERE world_id=?", (e.wid,)).fetchone()


def test_remaining_people_resume_with_original_leader_after_proposer_has_really_left(runtime):
    e = runtime
    jid = accept(e, offer(e, separation_policy="continue"))
    start(e, jid)
    choose(e, e.player, "leave", jid)
    with e.db.write() as c:
        player = people(e, c)[0]
        independent = MovementService().start(c, world_id=e.wid, character_id=e.player, destination_longitude=player["longitude"] - 0.005, destination_latitude=player["latitude"], world_time=now(e, c))
    e.clock.heartbeat(e.wid, elapsed_seconds=180)
    choose(e, e.npc, "pause", jid)
    assert choose(e, e.npc, "resume", jid)["status"] == "resume_pending"
    assert choose(e, e.leader, "resume", jid)["status"] == "active"
    e.clock.heartbeat(e.wid, elapsed_seconds=1800)
    assert get(e, jid)["status"] == "completed"
    assert next(m for m in moves(e) if m["id"] == independent.id)["status"] == "arrived"
