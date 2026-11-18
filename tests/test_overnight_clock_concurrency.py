"""哥哥，并发刷新共用真实时间基线，住宿边界也须保证能向前走一步。"""

# ruff: noqa: F811
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Lock, get_ident

import pytest
from test_lived_world import town  # noqa: F401
from test_overnight_stays import begin, current_time, room_at_stop, stopped_long_journey

from world_engine.clock import WorldClockService
from world_engine.overnight import OvernightService
from world_engine.repository import from_iso, to_iso
from world_engine.worker import WorldWorker


@pytest.mark.parametrize("with_stay", [False, True])
@pytest.mark.parametrize("peer", ["refresh", "api", "worker", "clock_change"])
def test_interleaved_real_time_entries_do_not_count_the_same_interval_twice(
    town,
    monkeypatch,
    with_stay,
    peer,
):
    e = town
    if with_stay:
        journey = stopped_long_journey(e)
        room = room_at_stop(e, journey)
        begin(e, journey, room)
    before = current_time(e)
    elapsed = 3600 if with_stay else 120
    target = datetime(2035, 1, 1, 8, tzinfo=UTC)
    with e.db.write() as c:
        c.execute(
            "UPDATE world_clock SET last_heartbeat_real_time=? WHERE world_id=?",
            (to_iso(target - timedelta(seconds=elapsed)), e.wid),
        )
    monkeypatch.setattr("world_engine.clock.utc_now", lambda: target)
    monkeypatch.setattr("world_engine.worker.utc_now", lambda: target)
    original = WorldClockService._heartbeat_once
    original_change = WorldClockService._set_time_scale_once
    barrier, lock, seen = Barrier(2), Lock(), set()

    def meet_before_write():
        # 哥哥，让两次入口都走完外层读取，再竞争真实写事务，稳定重现基线竞态。
        with lock:
            first = get_ident() not in seen
            seen.add(get_ident())
        if first:
            barrier.wait(timeout=15)

    def before_write(self, *args, **kwargs):
        meet_before_write()
        return original(self, *args, **kwargs)

    def before_change(self, *args, **kwargs):
        meet_before_write()
        return original_change(self, *args, **kwargs)

    monkeypatch.setattr(WorldClockService, "_heartbeat_once", before_write)
    monkeypatch.setattr(WorldClockService, "_set_time_scale_once", before_change)
    other = WorldClockService(e.db, e.settings)
    worker = WorldWorker(e.settings) if peer == "worker" else None

    def run_peer():
        if peer == "worker":
            return list(worker._world_heartbeats(e.wid, None))
        if peer == "clock_change":
            return other.set_time_scale(e.wid, 2, real_now=target)
        if peer == "api":
            response = e.client.post(f"/api/worlds/{e.wid}/heartbeat")
            assert response.status_code == 200, response.text
            return response.json()
        return other.heartbeat(e.wid, real_now=target)

    with ThreadPoolExecutor(max_workers=2) as pool:
        own = pool.submit(e.clock.heartbeat, e.wid, real_now=target)
        concurrent = pool.submit(run_peer)
        own.result(timeout=30)
        concurrent.result(timeout=30)
    assert current_time(e) == before + timedelta(seconds=elapsed)
    with e.db.read() as c:
        baseline = c.execute(
            "SELECT last_heartbeat_real_time FROM world_clock WHERE world_id=?",
            (e.wid,),
        ).fetchone()[0]
    assert from_iso(baseline) == target


def test_fractional_hunger_boundary_cannot_round_back_to_current_time(town):
    e = town
    journey = stopped_long_journey(e)
    room = room_at_stop(e, journey)
    begin(e, journey, room)
    at = current_time(e)
    with e.db.write() as c:
        c.execute("UPDATE characters SET satiety=6 WHERE id=?", (e.npc,))
        c.execute(
            "UPDATE character_state_accumulators SET satiety_residual=? WHERE character_id=?",
            (0.999999999999, e.npc),
        )
        boundary = OvernightService.boundary(c, e.wid, at, at + timedelta(hours=1), 3)
    assert at < boundary <= at + timedelta(seconds=1)


def test_catchup_rebuilds_world_target_after_an_interleaved_scale_change(town):
    e = town
    journey = stopped_long_journey(e)
    room = room_at_stop(e, journey)
    begin(e, journey, room)
    before = current_time(e)
    baseline = datetime(2035, 1, 1, 8, tzinfo=UTC)
    target = baseline + timedelta(seconds=4000 / 2.7)
    with e.db.write() as c:
        c.execute(
            "UPDATE world_clock SET time_scale=2.7,last_heartbeat_real_time=? WHERE world_id=?",
            (to_iso(baseline), e.wid),
        )
    progress = {}
    first = e.clock.heartbeat(
        e.wid,
        real_now=target,
        _single_boundary=True,
        _catchup_progress=progress,
    )
    assert first.current_time == before + timedelta(minutes=15)
    other = WorldClockService(e.db, e.settings)
    other.set_time_scale(e.wid, 5, real_now=first.real_time)
    # 哥哥，另一事务已调速，旧进度中的终点必须作废，余下现实时间按最新流速重新计算。
    expected = first.current_time + timedelta(
        seconds=(target - first.real_time).total_seconds() * 5,
    )
    for _ in range(20):
        if progress.get("complete"):
            break
        e.clock.heartbeat(
            e.wid,
            real_now=target,
            _single_boundary=True,
            _catchup_progress=progress,
        )
    assert progress["complete"]
    assert current_time(e) == expected
    assert progress["world_target"] == expected
