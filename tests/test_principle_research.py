"""哥哥，固定测试原理只核验边界，不作为真实NPC研发20项的证据。"""

from datetime import timedelta
from uuid import uuid4

import pytest

from world_engine.materials import encoded
from world_engine.principle_effects import adjust_task, record_task
from world_engine.principle_service import PrincipleTechnologyService as T
from world_engine.repository import to_iso

pytest_plugins = ["collective_fixtures"]


@pytest.fixture
def principle_lab(lab):
    db, wid, actors, _, _, at = lab
    sid = str(uuid4())
    with db.write() as c:
        c.execute(
            "INSERT INTO principle_sessions(id,world_id,status,target_count,max_calls,"
            "token_limit,created_at) VALUES (?,?,'running',20,60,600000,?)",
            (sid, wid, to_iso(at)),
        )
    return db, wid, sid, actors, at


def idea(name="离线标准夹具", parents=()):
    return {
        "name": name,
        "problem": "重复手工定位消耗大量时间",
        "principle": "利用固定定位面和可重复的机械接触约束，减少每次重新对齐的动作；"
        "定位约束保持一致时，工人能够在不提高动力和不减少材料用量的条件下节省操作时间。",
        "requirements": ["现有工坊", "熟练的工人"],
        "limitations": "只能用于形状相近的重复工件",
        "parents": list(parents),
        "application": "本人在下一次日常工作时采用定位方法",
        "adopt_if_approved": True,
    }


def call(c, sid, actor, purpose, body, at):
    cid = str(uuid4())
    c.execute(
        "INSERT INTO principle_calls(id,session_id,actor_id,purpose,status,input_json,"
        "system_prompt,response_json,reserved_tokens,accounted_tokens,input_tokens,"
        "output_tokens,created_at,owner_pid) VALUES (?,?,?,?,'succeeded','{}','离线测试',"
        "?,100,100,100,0,?,1)",
        (cid, sid, actor, purpose, encoded(body), to_iso(at)),
    )
    return cid


def certify(
    c,
    sid,
    actor,
    at,
    *,
    name="离线标准夹具",
    parents=(),
    factor=0.8,
    kind="labor_time",
    carrier="knowledge",
    materials=(),
    required_skill="检验",
):
    batch = {"reason": "本人希望减少日常工作中的重复定位", "ideas": [idea(name, parents)]}
    cid = call(c, sid, actor, "propose", batch, at)
    pid = T.propose(c, sid, actor, cid, batch, at)[0]
    review = {
        "proposal_id": pid,
        "approved": True,
        "reasoning": "机械定位约束减少重复动作，成本与能力边界明确，不引入额外能源或无穷收益。",
        "effect": {
            "kind": kind,
            "activity": "food" if kind == "food_nutrition" else "work",
            "factor": factor,
            "explanation": "固定定位约束让同样的日常工作减少重新校准的时间。",
            "required_skill": required_skill,
            "carrier": carrier,
            "materials": list(materials),
        },
    }
    rid = call(c, sid, None, "review", {"decisions": [review]}, at)
    return T.review(c, pid, rid, review, at), pid, rid, review


def test_principle_approval_is_researched_without_fabricating_trials(principle_lab):
    db, wid, sid, actors, at = principle_lab
    with db.write() as c:
        tid, *_ = certify(c, sid, actors[0], at)
        node = T.graph(c, wid)["nodes"][0]
        assert node["id"] == tid and node["status"] == "researched"
        assert node["basis"] == "principle_review"
        assert not c.execute("SELECT 1 FROM science_claim_trials").fetchone()
        assert not c.execute("SELECT 1 FROM principle_impacts").fetchone()
        event = c.execute(
            "SELECT importance FROM world_events WHERE id=?", (node["event_id"],)
        ).fetchone()
        assert event[0] == "major"


def test_actual_task_duration_changes_and_impact_is_idempotent(principle_lab):
    from world_engine.life import LifeActivityService

    db, _, sid, actors, at = principle_lab
    with db.write() as c:
        tid, *_ = certify(c, sid, actors[0], at)
        actor = c.execute("SELECT * FROM characters WHERE id=?", (actors[0],)).fetchone()
        spec = adjust_task(
            c,
            actor,
            {
                "kind": "work",
                "duration_minutes": 60,
                "energy_cost": 16,
                "application_skill": "检验",
            },
            at,
            (tid,),
        )
        aid = LifeActivityService.start(c, actor, at, "work", spec["duration_minutes"])
        record_task(c, actor["id"], spec, aid, at)
        record_task(c, actor["id"], spec, aid, at)
        assert spec["duration_minutes"] == 48
        assert c.execute(
            "SELECT ends_world_time FROM character_life_activities WHERE id=?", (aid,)
        ).fetchone()[0] == to_iso(at + timedelta(minutes=48))
        assert (
            c.execute(
                "SELECT count(*) FROM principle_impacts WHERE technology_id=?", (tid,)
            ).fetchone()[0]
            == 1
        )


def test_unknown_technology_cannot_affect_other_actor(principle_lab):
    db, _, sid, actors, at = principle_lab
    with db.write() as c:
        tid, *_ = certify(c, sid, actors[0], at)
        with pytest.raises(ValueError, match="尚未掌握"):
            T.adopt(c, actors[1], tid, at)
        actor = c.execute("SELECT * FROM characters WHERE id=?", (actors[1],)).fetchone()
        assert (
            adjust_task(c, actor, {"kind": "work", "duration_minutes": 60, "energy_cost": 16}, at)[
                "duration_minutes"
            ]
            == 60
        )


def test_parent_edges_only_use_actor_known_researched_principles(principle_lab):
    db, wid, sid, actors, at = principle_lab
    with db.write() as c:
        parent, *_ = certify(c, sid, actors[0], at)
        child, *_ = certify(c, sid, actors[0], at, name="离线组合定位", parents=[parent])
        assert T.graph(c, wid)["edges"] == [
            {"world_id": wid, "parent_id": parent, "child_id": child}
        ]
        batch = {"reason": "本人希望研究新的方法", "ideas": [idea(parents=[child])]}
        cid = call(c, sid, actors[1], "propose", batch, at)
        with pytest.raises(ValueError, match="尚未知晓"):
            T.propose(c, sid, actors[1], cid, batch, at)


def test_changed_review_cannot_forge_a_more_powerful_effect(principle_lab):
    db, _, sid, actors, at = principle_lab
    with db.write() as c:
        _, pid, rid, review = certify(c, sid, actors[0], at)
        review["effect"]["factor"] = 0.6
        with pytest.raises(ValueError, match="改写"):
            T.review(c, pid, rid, review, at)


def test_budget_fields_are_immutable(principle_lab):
    import sqlite3

    db, _, sid, _, _ = principle_lab
    with db.write() as c:
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE principle_sessions SET max_calls=100 WHERE id=?", (sid,))


def test_repeated_proposal_call_returns_original_ids(principle_lab):
    db, _, sid, actors, at = principle_lab
    with db.write() as c:
        batch = {"reason": "本人希望减少重复定位劳动", "ideas": [idea()]}
        cid = call(c, sid, actors[0], "propose", batch, at)
        first = T.propose(c, sid, actors[0], cid, batch, at)
        assert T.propose(c, sid, actors[0], cid, batch, at) == first
        assert c.execute("SELECT count(*) FROM principle_proposals").fetchone()[0] == 1
