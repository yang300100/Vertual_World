"""哥哥，每项衍生发明拥有自己的评审、材料、原型与应用，科技知识保持共用。"""

import json


def has_table(c, name):
    return bool(
        c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    )


def invention_records(c, technology_id):
    if not has_table(c, "principle_inventions"):
        return []
    return [
        dict(r)
        for r in c.execute(
            "SELECT * FROM principle_inventions WHERE technology_id=? ORDER BY design_index",
            (technology_id,),
        )
    ]


def candidates(c, technology):
    tech = dict(technology)
    rows = invention_records(c, tech["id"])
    if rows:
        return [
            tech
            | {
                "name": json.loads(r["spec_json"])["name"],
                "application": json.loads(r["spec_json"])["application"],
                "invention_id": r["id"],
                "selection_id": r["id"],
                "effect_json": r["effect_json"],
                "design": json.loads(r["spec_json"]),
            }
            for r in rows
            if r["status"] == "approved"
        ]
    if not json.loads(tech["effect_json"]):
        return []
    proposal = c.execute(
        "SELECT spec_json FROM principle_proposals WHERE id=?", (tech["proposal_id"],)
    ).fetchone()
    designs = json.loads(proposal[0]).get("inventions", [])
    if designs:
        tech.update(name=designs[0]["name"], application=designs[0]["application"])
    return [
        tech
        | {
            "invention_id": None,
            "selection_id": tech["id"],
            "design": designs[0] if designs else {},
        }
    ]


def resolve(c, technology_id, invention_id=None):
    tech = c.execute("SELECT * FROM principle_technologies WHERE id=?", (technology_id,)).fetchone()
    if tech is None:
        raise ValueError("科技不存在")
    options = candidates(c, tech)
    if invention_id:
        chosen = next((o for o in options if o["invention_id"] == invention_id), None)
        if chosen is None:
            raise ValueError("发明不属于该科技或尚未通过独立应用评审")
        return chosen
    if len(options) != 1:
        raise ValueError("请明确选择一项已通过应用评审的衍生发明")
    return options[0]


def selection(c, identifier):
    if has_table(c, "principle_inventions"):
        row = c.execute(
            "SELECT technology_id FROM principle_inventions WHERE id=?", (identifier,)
        ).fetchone()
        if row:
            return resolve(c, row[0], identifier)
    return resolve(c, identifier)


def record_reuse(c, tech, actor_id, source_id, at):
    from world_engine.principle_service import prerequisites_known
    from world_engine.repository import to_iso

    life = c.execute(
        "SELECT * FROM character_life_activities WHERE id=? AND character_id=? "
        "AND world_id=? AND status='completed'",
        (source_id, actor_id, tech["world_id"]),
    ).fetchone()
    meal = c.execute(
        "SELECT * FROM principle_meals WHERE id=? AND actor_id=? AND world_id=? "
        "AND quantity_before>quantity_after AND satiety_after>satiety_before",
        (source_id, actor_id, tech["world_id"]),
    ).fetchone()
    impact = c.execute(
        "SELECT 1 FROM principle_impacts WHERE source_id=? AND actor_id=? "
        "AND technology_id=? AND invention_id IS ?",
        (source_id, actor_id, tech["id"], tech.get("invention_id")),
    ).fetchone()
    detail = c.execute(
        "SELECT spec_json FROM activity_task_details WHERE activity_id=?", (source_id,)
    ).fetchone()
    declared = json.loads(detail[0]).get("principle_application") if detail else None
    expected = {"technology_id": tech["id"], "invention_id": tech.get("invention_id")}
    if (
        not (life or meal)
        or not (impact or declared == expected)
        or not prerequisites_known(c, actor_id, tech["id"], at)
    ):
        raise ValueError("依赖复用须对应完成的实际应用，不能用排队、声明或未知知识充数")
    for row in c.execute(
        "SELECT parent_id FROM principle_dependencies WHERE child_id=?", (tech["id"],)
    ):
        c.execute(
            "INSERT OR IGNORE INTO principle_reuses VALUES (?,?,?,?,?,?)",
            (row[0], tech["id"], tech.get("invention_id"), actor_id, source_id, to_iso(at)),
        )


def wear_prototype(c, item_id, amount=1):
    properties = c.execute(
        "SELECT * FROM principle_product_properties WHERE item_id=?", (item_id,)
    ).fetchone()
    if properties:
        total = properties["wear_accumulated"] + amount / properties["strength"]
        wear = int(total + 1e-9)
        c.execute(
            "UPDATE principle_product_properties SET wear_accumulated=? WHERE item_id=?",
            (max(0, total - wear), item_id),
        )
    else:
        wear = amount
    c.execute("UPDATE item_instances SET condition=MAX(0,condition-?) WHERE id=?", (wear, item_id))
