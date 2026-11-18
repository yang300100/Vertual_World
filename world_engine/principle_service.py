"""哥哥，科技只接受已留档的独立评审，知识与应用仍分别登记。"""

import hashlib
import json
from uuid import NAMESPACE_URL, uuid4, uuid5

from world_engine.event_log import record_event
from world_engine.materials import encoded, material_transaction
from world_engine.principle_types import IdeaBatch, PrincipleDecision, TechnologyIdea
from world_engine.repository import to_iso


def table_exists(c):
    return bool(
        c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='principle_technologies'"
        ).fetchone()
    )


def known_technologies(c, actor_id, at):
    if not table_exists(c):
        return []
    return [
        dict(row)
        for row in c.execute(
            "SELECT t.* FROM principle_knowledge k JOIN principle_technologies t "
            "ON t.id=k.technology_id AND t.world_id=k.world_id "
            "WHERE k.actor_id=? AND k.learned_at<=? ORDER BY t.created_at,t.rowid",
            (actor_id, to_iso(at)),
        )
    ]


class PrincipleTechnologyService:
    @staticmethod
    def propose(c, session_id, actor_id, source_call_id, body, at):
        spec = IdeaBatch.model_validate(body)
        session = c.execute("SELECT * FROM principle_sessions WHERE id=?", (session_id,)).fetchone()
        actor = c.execute("SELECT * FROM characters WHERE id=?", (actor_id,)).fetchone()
        call = c.execute("SELECT * FROM principle_calls WHERE id=?", (source_call_id,)).fetchone()
        if (
            session is None
            or session["status"] != "running"
            or actor is None
            or actor["world_id"] != session["world_id"]
            or actor["is_player"]
            or actor["health"] <= 0
            or call is None
            or call["purpose"] != "propose"
            or call["session_id"] != session_id
            or call["actor_id"] != actor_id
            or call["status"] != "succeeded"
            or IdeaBatch.model_validate_json(call["response_json"]) != spec
        ):
            raise ValueError("提案必须来自本世界NPC本人已留档的模型判断")
        known = known_technologies(c, actor_id, at)
        existing = [
            r[0]
            for r in c.execute(
                "SELECT id FROM principle_proposals WHERE source_call_id=? ORDER BY rowid",
                (source_call_id,),
            )
        ]
        if existing:
            return existing
        parents = {row["id"]: row["id"] for row in known}
        parents.update({row["name"]: row["id"] for row in known})
        ids = []
        with material_transaction(c):
            for idea in spec.ideas:
                references = idea.parents + [r.technology_id for r in idea.connections]
                if any(parent not in parents for parent in references):
                    raise ValueError("研究者不能把尚未知晓的科技当作已有原理")
                idea = idea.model_copy(
                    update={
                        "parents": [parents[p] for p in idea.parents],
                        "connections": [
                            r.model_copy(update={"technology_id": parents[r.technology_id]})
                            for r in idea.connections
                        ],
                    }
                )
                idea = TechnologyIdea.model_validate(idea.model_dump())
                pid = str(uuid4())
                c.execute(
                    "INSERT INTO principle_proposals(id,session_id,world_id,inventor_id,"
                    "source_call_id,spec_json,created_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        pid,
                        session_id,
                        actor["world_id"],
                        actor_id,
                        source_call_id,
                        idea.model_dump_json(),
                        to_iso(at),
                    ),
                )
                ids.append(pid)
        return ids

    @staticmethod
    def review(c, proposal_id, review_call_id, body, at):
        decision = PrincipleDecision.model_validate(body)
        proposal = c.execute(
            "SELECT * FROM principle_proposals WHERE id=?", (proposal_id,)
        ).fetchone()
        call = c.execute("SELECT * FROM principle_calls WHERE id=?", (review_call_id,)).fetchone()
        if proposal is None or decision.proposal_id != proposal_id or call is None:
            raise ValueError("评审必须对应实际提案和已留档请求")
        if (
            call["purpose"] != "review"
            or call["status"] != "succeeded"
            or call["session_id"] != proposal["session_id"]
            or call["actor_id"] is not None
        ):
            raise ValueError("原理评审必须独立于研究者的提案调用")
        saved = json.loads(call["response_json"])["decisions"]
        if not any(PrincipleDecision.model_validate(item) == decision for item in saved):
            raise ValueError("不能改写模型原评审或伪造通过")
        if proposal["status"] != "proposed":
            row = c.execute(
                "SELECT id FROM principle_technologies WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            return row[0] if row else None
        session = c.execute(
            "SELECT * FROM principle_sessions WHERE id=?", (proposal["session_id"],)
        ).fetchone()
        from world_engine.principle_cycles import enabled, project, ready

        cycle = project(c, proposal_id)
        finishing_cycle = cycle and ready(c, cycle, at)
        if session["status"] != "running" and not (
            session["status"] == "stopped" and finishing_cycle
        ):
            raise ValueError("科研会话已经停止")
        idea = TechnologyIdea.model_validate_json(proposal["spec_json"])
        known = {row["id"] for row in known_technologies(c, proposal["inventor_id"], at)}
        if set(idea.parents) - known:
            raise ValueError("前置原理不是本人已经掌握的科技")
        if {r.technology_id for r in idea.connections} - known:
            raise ValueError("技术联系只能引用本人已经掌握的科技")
        requested = {(r.technology_id, r.kind) for r in idea.connections}
        reviewed = {(r.technology_id, r.kind) for r in decision.connections}
        if requested != reviewed or len(reviewed) != len(decision.connections):
            raise ValueError("独立评审须逐项核验全部技术联系，不能增添或遗漏")
        if decision.approved and any(not r.approved for r in decision.connections):
            raise ValueError("技术联系未通过，不能以该关系登记研发达成")
        if idea.level == "invention" and decision.approved and decision.effect is None:
            raise ValueError("旧版单项发明评审通过须保留能力契约")
        if decision.effect and (decision.effect.magic_plan or idea.domain != "physical"):
            raise ValueError("魔法能力须通过逐项发明评审，不能使用旧版单项契约")
        if decision.invention_reviews:
            indices = [r.index for r in decision.invention_reviews]
            if sorted(indices) != list(range(len(idea.inventions))):
                raise ValueError("独立评审须精确覆盖每项衍生发明")
            approved = [r for r in decision.invention_reviews if r.approved]
            if idea.domain == "magic" and any(not r.effect.magic_plan for r in approved):
                raise ValueError("纯魔法应用不能用普通劳动或食物加成冒充")
            if (
                idea.domain == "hybrid"
                and approved
                and not any(r.effect.magic_plan for r in approved)
            ):
                raise ValueError("魔工结合须有真正绑定魔法资源的应用，不能只加领域标签")
            for reviewed in decision.invention_reviews:
                if reviewed.approved and reviewed.effect.magic_plan:
                    design = idea.inventions[reviewed.index]
                    if idea.domain == "physical" or design.magic_plan != reviewed.effect.magic_plan:
                        raise ValueError("魔法应用必须来自本人明确选择的魔纹与器物，不能由评审代选")
                    from world_engine.magic_research import validate

                    actor = c.execute(
                        "SELECT * FROM characters WHERE id=?", (proposal["inventor_id"],)
                    ).fetchone()
                    validate(c, actor, reviewed.effect, at)
        if (
            decision.duplicate_of
            and not c.execute(
                "SELECT 1 FROM principle_technologies WHERE id=? AND world_id=?",
                (decision.duplicate_of, proposal["world_id"]),
            ).fetchone()
        ):
            raise ValueError("评审引用的重复科技不存在于本世界")
        identity = hashlib.sha256(
            encoded(
                {
                    "principle": idea.principle,
                    "effect": decision.effect.model_dump() if decision.effect else None,
                    "parents": sorted(idea.parents),
                }
            ).encode()
        ).hexdigest()
        duplicate = c.execute(
            "SELECT id FROM principle_technologies WHERE world_id=? AND identity_hash=?",
            (proposal["world_id"], identity),
        ).fetchone()
        status = (
            "duplicate"
            if duplicate or decision.duplicate_of
            else ("researched" if decision.approved else "rejected")
        )
        count = c.execute(
            "SELECT count(*) FROM principle_technologies WHERE session_id=?",
            (proposal["session_id"],),
        ).fetchone()[0]
        if status == "researched" and count >= session["target_count"]:
            raise ValueError("本轮研发目标已达到，不继续登记新节点")
        if status == "researched" and (cycle or enabled(session)):
            from world_engine.principle_cycles import defer

            with material_transaction(c):
                pending = c.execute(
                    "SELECT count(*) FROM principle_projects j JOIN principle_proposals p "
                    "ON p.id=j.proposal_id WHERE p.session_id=? AND j.status!='completed'",
                    (session["id"],),
                ).fetchone()[0]
                if not cycle and count + pending >= session["target_count"]:
                    raise ValueError("已达成与已立项研究已占满本轮目标，不继续预约新项目")
                if defer(c, proposal, review_call_id, decision, idea, at):
                    return None
        with material_transaction(c):
            c.execute(
                "UPDATE principle_proposals SET status=?,review_call_id=?,review_json=? WHERE id=?",
                (status, review_call_id, decision.model_dump_json(), proposal_id),
            )
            if status != "researched":
                return None
            tid = str(uuid4())
            actor = c.execute(
                "SELECT * FROM characters WHERE id=?", (proposal["inventor_id"],)
            ).fetchone()
            eid = record_event(
                c,
                world_id=proposal["world_id"],
                tick_id="principle:" + tid,
                occurred_at=at,
                event_type="world.technology_researched",
                actor_id=actor["id"],
                location_id=actor["current_location_id"] or actor["location_id"],
                summary=f"{actor['name']}提出的“{idea.name}”通过原理评审，正式研发达成。",
                payload={
                    "technology_id": tid,
                    "basis": "principle_review",
                    "proposal_id": proposal_id,
                    "review_call_id": review_call_id,
                },
            )
            c.execute(
                "INSERT INTO principle_technologies VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    tid,
                    proposal["world_id"],
                    proposal["session_id"],
                    proposal_id,
                    actor["id"],
                    idea.name,
                    idea.problem,
                    idea.principle,
                    encoded(idea.requirements),
                    idea.limitations,
                    idea.inventions[0].application if idea.inventions else idea.application,
                    decision.effect.model_dump_json() if decision.effect else "null",
                    identity,
                    "principle_review",
                    to_iso(at),
                    eid,
                ),
            )
            for parent in idea.parents:
                c.execute(
                    "INSERT INTO principle_dependencies VALUES (?,?,?)",
                    (proposal["world_id"], parent, tid),
                )
            for relation in decision.connections:
                c.execute(
                    "INSERT INTO principle_relations VALUES (?,?,?,?,?,?)",
                    (
                        proposal["world_id"],
                        relation.technology_id,
                        tid,
                        relation.kind,
                        relation.explanation,
                        review_call_id,
                    ),
                )
            for reviewed in decision.invention_reviews:
                invention_id = str(uuid5(NAMESPACE_URL, tid + ":" + str(reviewed.index)))
                c.execute(
                    "INSERT INTO principle_inventions VALUES (?,?,?,?,?,?,?,?)",
                    (
                        invention_id,
                        tid,
                        proposal["world_id"],
                        reviewed.index,
                        idea.inventions[reviewed.index].model_dump_json(),
                        reviewed.model_dump_json(),
                        reviewed.effect.model_dump_json() if reviewed.effect else None,
                        "approved" if reviewed.approved else "rejected",
                    ),
                )
            c.execute(
                "INSERT INTO principle_knowledge VALUES (?,?,?,?,?)",
                (proposal["world_id"], actor["id"], tid, to_iso(at), None),
            )
            if idea.adopt_if_approved:
                PrincipleTechnologyService.adopt(c, actor["id"], tid, at)
        return tid

    @staticmethod
    def adopt(c, actor_id, technology_id, at):
        known = next(
            (row for row in known_technologies(c, actor_id, at) if row["id"] == technology_id), None
        )
        if known is None:
            raise ValueError("本人尚未掌握这项科技，不能全知采用")
        if not prerequisites_known(c, actor_id, technology_id, at):
            raise ValueError("本人缺少前置技术知识，不能采用衍生能力")
        c.execute(
            "INSERT OR IGNORE INTO principle_adoptions VALUES (?,?,?,?)",
            (known["world_id"], actor_id, technology_id, to_iso(at)),
        )

    @staticmethod
    def graph(c, world_id):
        if not table_exists(c):
            return {"nodes": [], "edges": []}
        nodes = [
            dict(row)
            | technology_metadata(c, row)
            | {"status": "researched", "current_version": 1, "version_status": "researched"}
            for row in c.execute(
                "SELECT * FROM principle_technologies WHERE world_id=? ORDER BY created_at,rowid",
                (world_id,),
            )
        ]
        return {
            "nodes": nodes,
            "edges": [
                dict(row)
                for row in c.execute(
                    "SELECT * FROM principle_dependencies WHERE world_id=?", (world_id,)
                )
            ],
        }

    @staticmethod
    def process(c, world_id, technology_id):
        row = c.execute(
            "SELECT * FROM principle_technologies WHERE id=? AND world_id=?",
            (technology_id, world_id),
        ).fetchone()
        if row is None:
            raise ValueError("科技不存在于本世界")
        proposal = c.execute(
            "SELECT * FROM principle_proposals WHERE id=?", (row["proposal_id"],)
        ).fetchone()
        from world_engine.principle_inventions import has_table

        products = (
            [
                dict(r)
                for r in c.execute(
                    "SELECT p.*,i.condition,q.strength,q.precision,q.wear_accumulated "
                    "FROM principle_invention_prototypes p "
                    "JOIN principle_inventions v ON v.id=p.invention_id "
                    "JOIN item_instances i ON i.id=p.item_id "
                    "LEFT JOIN principle_product_properties q "
                    "ON q.item_id=p.item_id WHERE v.technology_id=?",
                    (technology_id,),
                )
            ]
            if has_table(c, "principle_invention_prototypes")
            else []
        )
        return {
            "technology": dict(row),
            "basis": "principle_review",
            "proposal": json.loads(proposal["spec_json"]),
            "review": json.loads(proposal["review_json"]),
            "inventions": technology_metadata(c, row)["inventions"],
            "prototypes": products,
            "application_attempts": [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM principle_application_attempts "
                    "WHERE technology_id=? ORDER BY rowid",
                    (technology_id,),
                )
            ],
            "impacts": [
                dict(item)
                for item in c.execute(
                    "SELECT i.*,CASE WHEN i.source_kind='life_activity' THEN l.status "
                    "WHEN m.quantity_before>m.quantity_after AND m.satiety_after>m.satiety_before "
                    "THEN 'completed' ELSE 'unproven' END source_status FROM principle_impacts i "
                    "LEFT JOIN character_life_activities l ON l.id=i.source_id "
                    "AND i.source_kind='life_activity' LEFT JOIN principle_meals m "
                    "ON m.id=i.source_id AND i.source_kind='meal' WHERE i.technology_id=? "
                    "ORDER BY i.occurred_at,i.rowid",
                    (technology_id,),
                )
            ],
        }


def technology_metadata(c, row):
    spec = json.loads(
        c.execute(
            "SELECT spec_json FROM principle_proposals WHERE id=?", (row["proposal_id"],)
        ).fetchone()[0]
    )
    metadata = {
        "level": spec.get("level", "invention"),
        "unlocks": spec.get("unlocks", []),
        "inventions": spec.get("inventions", []),
        "domain": spec.get("domain", "physical"),
    }
    from world_engine.principle_cycles import cycle_view, project

    cycle = project(c, row["proposal_id"])
    metadata["research_cycle"] = cycle_view(cycle) if cycle else None
    from world_engine.principle_inventions import invention_records

    records = invention_records(c, row["id"])
    if records:
        metadata["inventions"] = [
            json.loads(r["spec_json"])
            | {
                "invention_id": r["id"],
                "status": r["status"],
                "review": json.loads(r["review_json"]),
                "effect_supported": r["status"] == "approved",
            }
            for r in records
        ]
    return metadata


def application_technology(c, row, invention_id=None):
    from world_engine.principle_inventions import resolve

    return resolve(c, row["id"], invention_id)


def prerequisites_known(c, actor_id, technology_id, at):
    """哥哥，检查完整前置链；改进与互补关系不伪装成硬性门槛。"""
    pending, seen = [technology_id], set()
    known = {t["id"] for t in known_technologies(c, actor_id, at)}
    while pending:
        child = pending.pop()
        if child in seen:
            continue
        seen.add(child)
        for row in c.execute(
            "SELECT parent_id FROM principle_dependencies WHERE child_id=?", (child,)
        ):
            if row[0] not in known:
                return False
            pending.append(row[0])
    return True
