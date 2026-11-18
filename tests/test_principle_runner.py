"""哥哥，真实提案和评审共享额度，离线后端只检验计账与知识边界。"""

from types import SimpleNamespace

from test_principle_research import idea, principle_lab  # noqa: F401

from world_engine.principle_runner import PrincipleRunner
from world_engine.principle_types import IdeaBatch


class Backend:
    max_retries = 0

    def __init__(self, usage=(100, 50), invalid=False):
        self.calls = []
        self.usage = usage
        self.invalid = invalid

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        if "proposals" in kwargs["user_payload"]:
            data = {
                "decisions": [
                    {
                        "proposal_id": p["proposal_id"],
                        "approved": True,
                        "reasoning": "离线判定：减少重复定位能节省劳动时间，作用与限制匹配。",
                        "effect": {
                            "kind": "labor_time",
                            "activity": "work",
                            "factor": 0.8,
                            "explanation": "利用固定接触面减少本人的日常工作定位时间。",
                            "required_skill": "检验",
                            "carrier": "knowledge",
                            "materials": [],
                        },
                    }
                    for p in kwargs["user_payload"]["proposals"]
                ]
            }
        else:
            proposed = idea("机械定位原理")
            proposed.update(
                level="foundation",
                domain="physical",
                unlocks=["可重复机械定位"],
                inventions=[
                    {
                        k: idea()[k]
                        for k in ("name", "principle", "requirements", "limitations", "application")
                    }
                ],
            )
            data = {"reason": "本人希望减少重复定位劳动", "ideas": [proposed]}
        if self.invalid:
            data = {"wrong": "无效正文"}
        return SimpleNamespace(data=data, input_tokens=self.usage[0], output_tokens=self.usage[1])


def test_real_workflow_uses_two_separate_recorded_calls(principle_lab):  # noqa: F811
    db, wid, sid, _, at = principle_lab
    backend = Backend()
    runner = PrincipleRunner(db, backend, sid)
    assert runner.step(at)
    assert len(backend.calls) == 2
    with db.read() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM principle_technologies WHERE world_id=?", (wid,)
            ).fetchone()[0]
            == 1
        )
        assert runner.usage(c)["accounted_tokens"] == 300
        assert (
            c.execute("SELECT actor_id FROM principle_calls WHERE purpose='review'").fetchone()[0]
            is None
        )


def test_new_runner_does_not_count_a_device_as_foundational_research(principle_lab):  # noqa: F811
    class DeviceBackend(Backend):
        def complete(self, **kwargs):
            response = super().complete(**kwargs)
            response.data = {"reason": "本人提出一件具体定位器具", "ideas": [idea()]}
            return response

    db, _, sid, _, at = principle_lab
    backend = DeviceBackend()
    assert PrincipleRunner(db, backend, sid).step(at)
    assert len(backend.calls) == 1
    with db.read() as c:
        assert not c.execute("SELECT 1 FROM principle_technologies").fetchone()
        assert "衍生发明" in c.execute("SELECT error FROM principle_calls").fetchone()[0]


def test_live_research_contract_requires_layer_and_inventions():
    import pytest

    from world_engine.principle_types import ResearchBatch

    schema = ResearchBatch.model_json_schema()
    required = schema["$defs"]["ResearchIdea"]["required"]
    assert {"level", "unlocks", "inventions", "parents", "connections"} <= set(required)
    with pytest.raises(ValueError):
        ResearchBatch.model_validate({"reason": "旧格式不能默认为新科技", "ideas": [idea()]})


def test_two_protocol_failures_stop_without_spending_remaining_quota(principle_lab):  # noqa: F811
    import json

    db, _, sid, _, at = principle_lab
    with db.write() as c:
        c.execute(
            "UPDATE principle_sessions SET manifest_json=? WHERE id=?",
            (json.dumps({"multi_invention_reviews": True}), sid),
        )
    backend = Backend(invalid=True)
    runner = PrincipleRunner(db, backend, sid)
    assert runner.step(at) and runner.step(at)
    assert not runner.step(at)
    assert len(backend.calls) == 2
    with db.read() as c:
        assert c.execute("SELECT status FROM principle_sessions").fetchone()[0] == "stopped"


def test_unknown_usage_is_reserved_and_stops_without_acceptance(principle_lab):  # noqa: F811
    db, _, sid, actors, at = principle_lab
    runner = PrincipleRunner(db, Backend(usage=(None, None)), sid)
    runner.call("propose", actors[0], {}, IdeaBatch, "离线契约")
    with db.read() as c:
        row = c.execute("SELECT * FROM principle_calls").fetchone()
        assert row["status"] == "unknown" and row["accounted_tokens"] == row["reserved_tokens"]
        assert c.execute("SELECT status FROM principle_sessions").fetchone()[0] == "stopped"
        assert not c.execute("SELECT 1 FROM principle_technologies").fetchone()


def test_partial_unknown_never_discards_reported_tokens(principle_lab):  # noqa: F811
    db, _, sid, actors, _ = principle_lab
    runner = PrincipleRunner(db, Backend(usage=(999999, None)), sid)
    runner.call("propose", actors[0], {}, IdeaBatch, "离线契约")
    with db.read() as c:
        row = c.execute("SELECT * FROM principle_calls").fetchone()
        assert row["accounted_tokens"] >= 999999
        assert row["status"] == "unknown"


def test_local_validation_failure_keeps_known_paid_usage(principle_lab):  # noqa: F811
    db, _, sid, actors, at = principle_lab
    runner = PrincipleRunner(db, Backend(invalid=True), sid)
    _, data = runner.call("propose", actors[0], {}, IdeaBatch, "离线契约")
    assert data is None
    with db.read() as c:
        row = c.execute("SELECT * FROM principle_calls").fetchone()
        assert row["status"] == "failed" and row["accounted_tokens"] == 150
        assert c.execute("SELECT status FROM principle_sessions").fetchone()[0] == "running"


def test_proposer_does_not_receive_other_actor_private_principles(principle_lab):  # noqa: F811
    from test_principle_research import certify

    db, _, sid, actors, at = principle_lab
    with db.write() as c:
        certify(c, sid, actors[0], at)
    runner = PrincipleRunner(db, None, sid)
    with db.read() as c:
        actor = c.execute("SELECT * FROM characters WHERE id=?", (actors[1],)).fetchone()
        assert not runner.context(c, actor, at)["known_technologies"]


def test_future_memory_is_not_available_to_current_researcher(principle_lab):  # noqa: F811
    from datetime import timedelta
    from uuid import uuid4

    from world_engine.event_log import record_event
    from world_engine.repository import to_iso

    db, wid, sid, actors, at = principle_lab
    with db.write() as c:
        future = at + timedelta(hours=1)
        eid = record_event(
            c,
            world_id=wid,
            tick_id="test-future",
            occurred_at=future,
            event_type="research.future",
            actor_id=actors[0],
            summary="未来发生的事情",
        )
        c.execute(
            "INSERT INTO character_memories(id,world_id,character_id,event_id,memory_type,summary,"
            "importance,confidence,created_at) "
            "VALUES (?,?,?,?,'experienced','未来才知道的答案',9,1,?)",
            (str(uuid4()), wid, actors[0], eid, to_iso(future)),
        )
    runner = PrincipleRunner(db, None, sid)
    with db.read() as c:
        actor = c.execute("SELECT * FROM characters WHERE id=?", (actors[0],)).fetchone()
        assert "未来才知道的答案" not in runner.context(c, actor, at)["memories"]
