"""哥哥，离线案例验证原理与发明分层，不算新的真实科研证据。"""

import json

import pytest
from test_principle_research import call, certify, idea, principle_lab  # noqa: F401

from world_engine.principle_applications import PrincipleApplications
from world_engine.principle_effects import adjust_task
from world_engine.principle_service import (
    PrincipleTechnologyService as T,
)
from world_engine.principle_service import (
    application_technology,
)
from world_engine.principle_tree import technology_tree
from world_engine.repository import to_iso


def foundation(name="机械定位原理", connections=()):
    body = idea(name)
    body.update(
        level="foundation",
        unlocks=["可重复的机械定位"],
        parents=[r["technology_id"] for r in connections if r["kind"] == "requires"],
        connections=list(connections),
        inventions=[
            {
                "name": "定位夹具",
                "principle": body["principle"],
                "requirements": body["requirements"],
                "limitations": body["limitations"],
                "application": body["application"],
            },
            {
                "name": "校准挡块",
                "principle": body["principle"],
                "requirements": body["requirements"],
                "limitations": body["limitations"],
                "application": "本人打算以后按同一定位原理制作校准挡块",
            },
        ],
    )
    return body


def register(c, sid, actor, at, body, reviews=None):
    batch = {"reason": "从重复定位中提炼通用的机械约束原理", "ideas": [body]}
    cid = call(c, sid, actor, "propose", batch, at)
    pid = T.propose(c, sid, actor, cid, batch, at)[0]
    normalized = json.loads(
        c.execute("SELECT spec_json FROM principle_proposals WHERE id=?", (pid,)).fetchone()[0]
    )
    decision = {
        "proposal_id": pid,
        "approved": True,
        "reasoning": "通用定位原理支持不同工件的定位设计，前置原理及因果关系与应用匹配。",
        "effect": {
            "kind": "labor_time",
            "activity": "work",
            "factor": 0.8,
            "explanation": "首个定位夹具减少重复对齐所需的劳动时间。",
            "required_skill": "检验",
            "carrier": "knowledge",
            "materials": [],
        },
        "connections": reviews
        if reviews is not None
        else [dict(r) | {"approved": True} for r in normalized["connections"]],
    }
    rid = call(c, sid, None, "review", {"decisions": [decision]}, at)
    return T.review(c, pid, rid, decision, at)


def test_one_technology_contains_multiple_designs_without_count_inflation(principle_lab):  # noqa: F811
    db, wid, sid, actors, at = principle_lab
    with db.write() as c:
        tid = register(c, sid, actors[0], at, foundation())
        tree = technology_tree(c, wid)
        assert len(tree["nodes"]) == tree["researched_technology_count"] == 1
        assert tree["invention_count"] == 2
        assert [i["effect_supported"] for i in tree["nodes"][0]["inventions"]] == [True, False]
        tech = c.execute("SELECT * FROM principle_technologies WHERE id=?", (tid,)).fetchone()
        assert application_technology(c, tech)["name"] == "定位夹具"
        assert not c.execute("SELECT 1 FROM principle_prototypes").fetchone()


def test_named_prerequisite_normalized_and_independently_reviewed(principle_lab):  # noqa: F811
    db, wid, sid, actors, at = principle_lab
    with db.write() as c:
        parent = register(c, sid, actors[0], at, foundation())
        relation = {
            "technology_id": "机械定位原理",
            "kind": "requires",
            "explanation": "前置定位约束提供重复定位面，缺少它无法维持新方法的统一校准。",
        }
        child = register(c, sid, actors[0], at, foundation("组合定位工艺", [relation]))
        tree = technology_tree(c, wid)
        edge = tree["edges"][0]
        assert (edge["parent_id"], edge["child_id"], edge["kind"]) == (parent, child, "requires")
        assert edge["basis"] == "principle_review" and edge["review_call_id"]


def test_missing_or_invented_relationship_review_cannot_register(principle_lab):  # noqa: F811
    db, _, sid, actors, at = principle_lab
    with db.write() as c:
        parent = register(c, sid, actors[0], at, foundation())
        relation = {
            "technology_id": parent,
            "kind": "improves",
            "explanation": "新校准步骤减少既有定位方法的重复测量，并保留原有约束。",
        }
        with pytest.raises(ValueError, match="逐项核验"):
            register(c, sid, actors[0], at, foundation("校准改进工艺", [relation]), reviews=[])
        assert c.execute("SELECT count(*) FROM principle_technologies").fetchone()[0] == 1
        assert not c.execute("SELECT 1 FROM principle_relations").fetchone()


def test_prerequisite_chain_blocks_real_application_without_knowledge(principle_lab, settings):  # noqa: F811
    db, wid, sid, actors, at = principle_lab
    with db.write() as c:
        root = register(c, sid, actors[0], at, foundation())
        relation = {
            "technology_id": root,
            "kind": "requires",
            "explanation": "定位约束提供稳定基准，组合方法依赖该基准实现重复对齐。",
        }
        child = register(c, sid, actors[0], at, foundation("组合定位工艺", [relation]))
        c.execute("DELETE FROM principle_knowledge WHERE technology_id=?", (root,))
        actor = c.execute("SELECT * FROM characters WHERE id=?", (actors[0],)).fetchone()
        task = {
            "kind": "work",
            "duration_minutes": 60,
            "energy_cost": 16,
            "application_skill": "检验",
        }
        assert adjust_task(c, actor, task, at, (child,))["duration_minutes"] == 60
        with pytest.raises(ValueError, match="缺少前置"):
            T.adopt(c, actors[0], child, at)
    assert PrincipleApplications(db, settings, wid).apply(child)["status"] == "needs_prerequisite"
    with db.write() as c:
        c.execute(
            "INSERT INTO principle_knowledge VALUES (?,?,?,?,?)",
            (wid, actors[0], root, to_iso(at), None),
        )
        assert adjust_task(c, actor, task, at, (child,))["duration_minutes"] == 48


def test_complementary_relation_does_not_grant_extra_buff(principle_lab):  # noqa: F811
    db, wid, sid, actors, at = principle_lab
    with db.write() as c:
        parent = register(c, sid, actors[0], at, foundation())
        relation = {
            "technology_id": parent,
            "kind": "complements",
            "explanation": "另一种测量约束能与定位基准交叉检查，但不会凭关系增加动力。",
        }
        body = foundation("交叉测量方法", [relation])
        body["principle"] += "采用两个独立测量方向交叉检查误差，减少单方向基准偏差的累积。"
        child = register(c, sid, actors[0], at, body)
        actor = c.execute("SELECT * FROM characters WHERE id=?", (actors[0],)).fetchone()
        task = {
            "kind": "work",
            "duration_minutes": 60,
            "energy_cost": 16,
            "application_skill": "检验",
        }
        assert adjust_task(c, actor, task, at, (child,))["duration_minutes"] == 48
        assert not c.execute("SELECT 1 FROM principle_dependencies").fetchone()
        assert technology_tree(c, wid)["edges"][0]["kind"] == "complements"


def test_legacy_view_groups_inventions_without_forging_research_or_dependencies(principle_lab):  # noqa: F811
    db, wid, sid, actors, at = principle_lab
    with db.write() as c:
        certify(c, sid, actors[0], at, name="脚踏锻锤")
        certify(c, sid, actors[0], at, name="绞木缓降器", factor=0.9)
        changes = c.total_changes
        tree = technology_tree(c, wid)
        assert c.total_changes == changes
        assert len(tree["nodes"]) == 1 and tree["invention_count"] == 2
        assert tree["nodes"][0]["name"] == "基础机械传动"
        assert tree["nodes"][0]["basis"] == "derived_summary"
        assert tree["researched_technology_count"] == tree["reviewed_relation_count"] == 0
        assert c.execute("SELECT count(*) FROM principle_technologies").fetchone()[0] == 2
        assert not c.execute("SELECT 1 FROM principle_dependencies").fetchone()
        assert not c.execute("SELECT 1 FROM principle_impacts").fetchone()
