"""哥哥，睡眠中的计划不能抢走实际可研究居民的付费额度。"""

from datetime import timedelta

from test_science_runner import Backend, runner

from world_engine.collective_research import CollectiveResearchService as R
from world_engine.life import LifeActivityService

pytest_plugins = ["science_fixtures"]


def resting_session(science_lab):
    db, wid, sid, actors, _, at = science_lab
    with db.write() as c:
        c.execute("UPDATE science_sessions SET status='running' WHERE id=?", (sid,))
        for aid in actors:
            LifeActivityService.start(c, R.actor(c, aid), at, "rest", 60)
    backend = Backend()
    return db, wid, sid, actors, at, backend, runner(db, backend, sid)


def test_sleeping_population_has_no_requests_or_reservations(science_lab):
    db, wid, sid, _, at, backend, service = resting_session(science_lab)
    assert not service.decide(wid, at)
    assert not backend.calls
    with db.read() as c:
        count = c.execute(
            "SELECT count(*) FROM science_model_calls WHERE session_id=?", (sid,)
        ).fetchone()[0]
        status = c.execute("SELECT status FROM science_sessions WHERE id=?", (sid,)).fetchone()[0]
        assert count == 0 and status == "running"


def test_rest_completion_makes_actor_eligible_without_restarting(science_lab):
    db, wid, _, _, at, backend, service = resting_session(science_lab)
    assert not service.decide(wid, at)
    end = at + timedelta(hours=1)
    with db.write() as c:
        LifeActivityService.advance(c, wid, end, "离线调度验证")
    assert service.decide(wid, end)
    assert len(backend.calls) == 1


def test_available_actor_precedes_busy_uncalled_actor(science_lab):
    db, wid, sid, actors, _, at = science_lab
    busy, ready = sorted(actors)
    with db.write() as c:
        c.execute("UPDATE science_sessions SET status='running' WHERE id=?", (sid,))
        LifeActivityService.start(c, R.actor(c, busy), at, "rest", 60)
    backend = Backend()
    assert runner(db, backend, sid).decide(wid, at)
    assert backend.calls[0]["user_payload"]["actor_id"] == ready
