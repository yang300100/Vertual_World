"""哥哥，预检有一次本人跟进机会，反复改措辞不能抢走全部预算。"""

from test_science_programs import craft_program
from test_science_runner import Backend, runner

from world_engine.collective_research import CollectiveResearchService as R

pytest_plugins = ["science_fixtures"]


def setup(science_lab, *, followup="execute"):
    db, wid, sid, _, assets, at = science_lab
    with db.write() as c:
        c.execute("UPDATE science_sessions SET status='running' WHERE id=?", (sid,))
    backend = Backend()
    complete = backend.complete

    def reply(**kwargs):
        context = kwargs["user_payload"]
        program = craft_program(assets["actors"][context["actor_id"]])
        has_receipt = any(o["content"].get("kind") == "science_preflight"
                          for o in context["observations"])
        program["phase"] = followup if has_receipt else "preview"
        program["reason"] = "本人检查轮次" + str(len(backend.calls))
        if has_receipt and followup == "empty":
            program = {"reason": "本人暂不行动", "commands": []}
        elif has_receipt and followup == "invalid":
            program = {"reason": "本人无效提交", "commands": [{"op": "unlock"}]}
        backend.program = program
        return complete(**kwargs)

    backend.complete = reply
    return db, wid, sid, at, backend, runner(db, backend, sid)


def test_preflight_author_follows_up_before_any_uncalled_population(science_lab):
    db, wid, _, at, backend, r = setup(science_lab)
    assert r.decide(wid, at)
    assert r.decide(wid, at)
    assert (backend.calls[0]["user_payload"]["actor_id"]
            == backend.calls[1]["user_payload"]["actor_id"])
    with db.read() as c:
        assert c.execute("SELECT 1 FROM science_programs WHERE status='active'").fetchone()


def test_changed_preview_reason_does_not_renew_followup(science_lab):
    _, wid, _, at, backend, r = setup(science_lab, followup="preview")
    for _ in range(3):
        assert r.decide(wid, at)
    actors = [call["user_payload"]["actor_id"] for call in backend.calls]
    assert actors[0] == actors[1] and actors[2] != actors[0]


def test_invalid_followup_is_consumed_and_cannot_monopolize(science_lab):
    _, wid, _, at, backend, r = setup(science_lab, followup="invalid")
    for _ in range(3):
        assert r.decide(wid, at)
    actors = [call["user_payload"]["actor_id"] for call in backend.calls]
    assert actors[0] == actors[1] and actors[2] != actors[0]


def test_real_measurement_renews_followup_after_restart(science_lab):
    db, wid, sid, at, backend, r = setup(science_lab, followup="preview")
    assert r.decide(wid, at)
    assert r.decide(wid, at)
    aid = backend.calls[0]["user_payload"]["actor_id"]
    with db.write() as c:
        R.record_observation(c, aid, at, "本人新的真实测量", {"kind": "observation", "value": 5})
    resumed = runner(db, backend, sid)
    assert resumed.decide(wid, at)
    assert resumed.decide(wid, at)
    assert all(call["user_payload"]["actor_id"] == aid for call in backend.calls)


def test_empty_followup_is_consumed(science_lab):
    _, wid, _, at, backend, r = setup(science_lab, followup="empty")
    for _ in range(3):
        assert r.decide(wid, at)
    actors = [call["user_payload"]["actor_id"] for call in backend.calls]
    assert actors[0] == actors[1] and actors[2] != actors[0]


def test_dropping_old_context_cannot_regrant_consumed_followup(science_lab, monkeypatch):
    from world_engine import science_runner

    _, wid, _, at, backend, r = setup(science_lab, followup="preview")
    assert r.decide(wid, at)
    assert r.decide(wid, at)
    aid = backend.calls[0]["user_payload"]["actor_id"]
    original = science_runner.science_context

    def trimmed(*args, **kwargs):
        context = original(*args, **kwargs)
        if args[1] == aid:
            context["observations"] = context["observations"][1:]
        return context

    monkeypatch.setattr(science_runner, "science_context", trimmed)
    assert r.decide(wid, at)
    assert backend.calls[2]["user_payload"]["actor_id"] != aid
