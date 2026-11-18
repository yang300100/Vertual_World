"""哥哥，固定案例只验证多发明、物品属性和依赖复用，不作为真实NPC成果。"""

import json

import pytest
from test_principle_research import call, certify, principle_lab  # noqa: F401
from test_principle_tree import foundation

from world_engine.principle_applications import PrincipleApplications
from world_engine.principle_inventions import record_reuse, resolve, wear_prototype
from world_engine.principle_service import PrincipleTechnologyService as T
from world_engine.principle_tree import technology_tree
from world_engine.principle_workshop import seed_inputs


def register_multi(c, sid, actor, at, parent=None, second="material_strength", reject=False):
    connections = (
        []
        if parent is None
        else [
            {
                "technology_id": parent,
                "kind": "requires",
                "explanation": "既有定位基准提供稳定接触面，缺少基准不能实现重复加工。",
            }
        ]
    )
    spec = foundation("定位与结构组合工艺", connections)
    spec["principle"] += "通过纤维交错结构分散局部载荷，并在装配时保持一致定位基准。"
    batch = {"reason": "将已知定位基准与纤维结构结合以改进实际器具", "ideas": [spec]}
    cid = call(c, sid, actor, "propose", batch, at)
    pid = T.propose(c, sid, actor, cid, batch, at)[0]
    effects = [
        {"kind": "labor_time", "activity": "work", "factor": 0.8},
        {"kind": second, "activity": "craft", "factor": 1.25},
    ]
    decision = {
        "proposal_id": pid,
        "approved": True,
        "reasoning": "定位和载荷分散原理自洽，基础技术与具体应用分别核验，不制造无穷收益。",
        "effect": None,
        "connections": [dict(r) | {"approved": True} for r in connections],
        "invention_reviews": [
            {
                "index": index,
                "approved": not (reject and index == 1),
                "reasoning": "真实木料与纤维结构支持此项应用，其能力仅限对应制成物品或劳动。",
                "effect": None
                if reject and index == 1
                else dict(effect)
                | {
                    "explanation": "通过稳定定位或纤维分散载荷，保守改善该项器具的实际用途。",
                    "required_skill": "检验",
                    "carrier": "tool",
                    "materials": [{"kind": "wood", "quantity": 2}],
                },
            }
            for index, effect in enumerate(effects)
        ],
    }
    rid = call(c, sid, None, "review", {"decisions": [decision]}, at)
    tid = T.review(c, pid, rid, decision, at)
    ids = [
        r[0]
        for r in c.execute(
            "SELECT id FROM principle_inventions WHERE technology_id=? ORDER BY design_index",
            (tid,),
        )
    ]
    return tid, ids


def test_two_designs_use_separate_prototypes_materials_and_effects(principle_lab, settings):  # noqa: F811
    db, wid, sid, actors, at = principle_lab
    with db.write() as c:
        seed_inputs(c, wid)
        tid, ids = register_multi(c, sid, actors[0], at)
        with pytest.raises(ValueError, match="明确选择"):
            resolve(c, tid)
        assert all(i["effect_supported"] for i in technology_tree(c, wid)["nodes"][0]["inventions"])
    app = PrincipleApplications(db, settings, wid)
    first = app.apply(tid, ids[0])
    second = app.apply(tid, ids[1])
    assert first["status"] == second["status"] == "applied"
    with db.write() as c:
        prototypes = c.execute("SELECT item_id FROM principle_invention_prototypes").fetchall()
        assert len({r[0] for r in prototypes}) == 2
        wood = c.execute(
            "SELECT i.quantity FROM item_instances i JOIN principle_material_catalogue m "
            "ON m.item_type_id=i.item_type_id WHERE m.kind='wood' "
            "AND i.owner_character_id=?",
            (actors[0],),
        ).fetchone()[0]
        assert wood == 20
        prop = c.execute(
            "SELECT * FROM principle_product_properties WHERE invention_id=?", (ids[1],)
        ).fetchone()
        assert prop["strength"] == 1.25
        # 哥哥，强度指数实际降低磨损速率，不能只在卡片显示一个数字。
        wear_prototype(c, prop["item_id"], 4)
        assert (
            c.execute(
                "SELECT condition FROM item_instances WHERE id=?", (prop["item_id"],)
            ).fetchone()[0]
            == 96
        )
        impacts = c.execute(
            "SELECT invention_id,kind FROM principle_impacts ORDER BY rowid"
        ).fetchall()
        assert [(r[0], r[1]) for r in impacts] == [
            (ids[0], "labor_time"),
            (ids[1], "material_strength"),
        ]


def test_rejected_design_does_not_block_theory_or_unlock_itself(principle_lab):  # noqa: F811
    db, wid, sid, actors, at = principle_lab
    with db.write() as c:
        tid, ids = register_multi(c, sid, actors[0], at, reject=True)
        assert T.graph(c, wid)["nodes"][0]["status"] == "researched"
        assert [
            i["effect_supported"] for i in technology_tree(c, wid)["nodes"][0]["inventions"]
        ] == [True, False]
        with pytest.raises(ValueError, match="尚未通过"):
            resolve(c, tid, ids[1])


def test_completed_child_application_records_real_dependency_reuse(principle_lab, settings):  # noqa: F811
    db, wid, sid, actors, at = principle_lab
    with db.write() as c:
        seed_inputs(c, wid)
        parent, *_ = certify(c, sid, actors[0], at)
        child, ids = register_multi(c, sid, actors[0], at, parent=parent, second="craft_precision")
    outcome = PrincipleApplications(db, settings, wid).apply(child, ids[1])
    assert outcome["status"] == "applied" and outcome["precision"] == 1.25
    with db.write() as c:
        row = c.execute("SELECT * FROM principle_reuses").fetchone()
        assert row["parent_id"] == parent and row["child_id"] == child
        assert row["source_id"] == outcome["activity_id"]
        assert (
            c.execute(
                "SELECT status FROM character_life_activities WHERE id=?", (row["source_id"],)
            ).fetchone()[0]
            == "completed"
        )
        with pytest.raises(ValueError, match="完成的实际应用"):
            record_reuse(c, resolve(c, child, ids[1]), actors[0], "未执行工单", at)


def test_application_feedback_contains_only_own_past_results(principle_lab):  # noqa: F811
    from datetime import timedelta

    from world_engine.principle_runner import PrincipleRunner
    from world_engine.repository import to_iso

    db, _, sid, actors, at = principle_lab
    with db.write() as c:
        tid, _ = register_multi(c, sid, actors[0], at)
        c.execute(
            "INSERT INTO principle_application_attempts(id,technology_id,result_json,occurred_at) "
            "VALUES ('future',?,?,?)",
            (tid, json.dumps({"status": "pending"}), to_iso(at + timedelta(days=1))),
        )
    runner = PrincipleRunner(db, None, sid)
    with db.read() as c:
        actor = c.execute("SELECT * FROM characters WHERE id=?", (actors[0],)).fetchone()
        assert runner.context(c, actor, at)["application_feedback"] == []
