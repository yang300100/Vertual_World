"""哥哥，科技树呈现可复用原理；旧发明只做有出处的归纳，不改写研发历史。"""

import json

from world_engine.principle_service import PrincipleTechnologyService
from world_engine.technologies import TechnologyService

# 哥哥，这些规则只整理旧版器具记录；新科技的名字与关系仍由人物提案、独立评审决定。
LEGACY_GROUPS = (
    (
        "mechanics",
        "基础机械传动",
        ("脚踏", "绞木"),
        "通过杠杆、转轴与连杆传递人的动力，交换力与速度。",
    ),
    (
        "heat",
        "燃烧与热量控制",
        ("慢烧", "暖身"),
        "控制通风、燃料与散热，使热量按所需速度产生和释放。",
    ),
    (
        "materials",
        "纤维与陶土成形",
        ("骨料", "陶土配方"),
        "纤维与陶土形成可塑材料，配比与热处理影响孔隙和强度。",
    ),
    (
        "separation",
        "多孔材料吸附与过滤",
        ("滤水", "蒸滤", "储药", "预滤", "覆伤"),
        "利用孔隙、纤维拦截与表面吸附处理液体或水汽；效果须分别判断。",
    ),
    (
        "food",
        "食物保存与热加工",
        ("口粮", "焖粮", "缓饥"),
        "通过分装、防潮或湿热加工改变食物保存与进食条件。",
    ),
    (
        "signals",
        "机械与燃烧信号传递",
        ("绊索", "信标"),
        "把张力变化或受控燃烧转换成可被感知的声响与气味。",
    ),
    (
        "aquatic",
        "水生习性与选择性捕获",
        ("诱鱼", "集鱼", "鱼笼"),
        "利用水流、振动或体型差异引导鱼类并进行选择。",
    ),
    ("buoyancy", "基础浮力应用", ("浮绳",), "封闭空气腔排开水产生浮力，宽带分散支撑载荷。"),
)

LEGACY_LINKS = (
    ("mechanics", "signals", "张力与机械传动可把绊索触发传递到响板。", ("张力", "响板")),
    ("heat", "signals", "信标使用受控燃烧产生持续的气味信号。", ("燃烧", "气味")),
    ("heat", "materials", "陶土烧结使用缓慢升温、保温与降温控制。", ("烧结", "降温")),
    ("materials", "separation", "陶土与纤维形成过滤及吸附结构的材料载体。", ("黏土", "微孔")),
    ("heat", "food", "湿热加工利用温和热源使淀粉吸水、软化。", ("淀粉", "热")),
    ("separation", "food", "多孔材料吸湿原理用于分格包装的防潮处理。", ("吸湿", "分格")),
    ("materials", "buoyancy", "封闭的空心陶珠作为保存空气的浮力载体。", ("空心", "浮珠")),
)


def _table(c, name):
    return bool(
        c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    )


def technology_tree(c, world_id):
    original = PrincipleTechnologyService.graph(c, world_id)
    legacy = TechnologyService.graph(c, world_id)
    records = original["nodes"] + legacy["nodes"]
    nodes, edges, groups, membership = [], [], {}, {}
    for record in records:
        if record.get("level") in {"foundation", "technique"}:
            node = dict(record) | {"basis": "principle_review", "source_ids": [record["id"]]}
            # 哥哥，新版逐项评审；旧记录仍只有首个发明拥有能力契约。
            node["inventions"] = [
                dict(i)
                | {
                    "technology_id": record["id"],
                    "effect_supported": i.get("effect_supported", n == 0),
                }
                for n, i in enumerate(record["inventions"])
            ]
            nodes.append(node)
            membership[record["id"]] = record["id"]
            continue
        name = record["name"]
        match = next(
            (
                g
                for g in LEGACY_GROUPS
                if any(k in name for k in g[2]) and not (g[0] == "mechanics" and "暖身" in name)
            ),
            None,
        )
        key, title, _, description = match or (
            "unclassified:" + record["id"],
            "待归纳原理 · " + name,
            (),
            "现有记录不足以归纳通用技术，保留原始发明及其研发依据。",
        )
        if key not in groups:
            groups[key] = {
                "id": "summary:" + key,
                "name": title,
                "level": "foundation",
                "domain": "physical",
                "basis": "derived_summary",
                "status": "organized",
                "current_version": 1,
                "principle": description,
                "unlocks": [],
                "inventions": [],
                "source_ids": [],
                "limitations": "这是既有发明的展示归纳，不代表NPC新研发了该基础技术。",
            }
            nodes.append(groups[key])
        group = groups[key]
        group["source_ids"].append(record["id"])
        membership[record["id"]] = group["id"]
        group["inventions"].append(
            {
                "name": name,
                "technology_id": record["id"],
                "principle": record.get("principle", "具体原理见原始研发记录"),
                "requirements": json.loads(record.get("requirements_json", "[]")),
                "limitations": record.get("limitations", "限制见原始研发记录"),
                "application": record.get("application", "用途见原始研发记录"),
                "effect_supported": True,
                "basis": record.get("basis", "physical_review"),
            }
        )
    for parent, child, explanation, evidence in LEGACY_LINKS:
        if parent not in groups or child not in groups:
            continue
        sources = [
            r["id"]
            for r in records
            if membership[r["id"]] in {groups[parent]["id"], groups[child]["id"]}
            and all(term in r.get("principle", "") for term in evidence)
        ]
        if sources:
            edges.append(
                {
                    "parent_id": groups[parent]["id"],
                    "child_id": groups[child]["id"],
                    "kind": "related",
                    "basis": "derived_summary",
                    "explanation": explanation,
                    "source_ids": sources,
                }
            )
    reviewed = []
    if _table(c, "principle_relations"):
        reviewed = [
            dict(r) | {"basis": "principle_review"}
            for r in c.execute("SELECT * FROM principle_relations WHERE world_id=?", (world_id,))
        ]
    pairs = {(r["parent_id"], r["child_id"]) for r in reviewed if r["kind"] == "requires"}
    for relation in original["edges"] + legacy["edges"]:
        if (relation["parent_id"], relation["child_id"]) not in pairs:
            reviewed.append(
                dict(relation)
                | {
                    "kind": "requires",
                    "basis": "recorded_dependency",
                    "explanation": "原始研发记录登记的前置技术。",
                }
            )
    internal = []
    for relation in reviewed:
        parent, child = membership.get(relation["parent_id"]), membership.get(relation["child_id"])
        if parent is None or child is None:
            continue
        projected = dict(relation) | {
            "parent_id": parent,
            "child_id": child,
            "source_parent_id": relation["parent_id"],
            "source_child_id": relation["child_id"],
        }
        (internal if parent == child else edges).append(projected)
    return {
        "nodes": nodes,
        "edges": edges,
        "internal_relations": internal,
        "research_record_count": len(records),
        "researched_technology_count": sum(n.get("basis") == "principle_review" for n in nodes),
        "invention_count": sum(len(n["inventions"]) for n in nodes),
        "reviewed_relation_count": len(reviewed),
        "domain_counts": {
            d: sum(n.get("domain", "physical") == d for n in nodes)
            for d in ("physical", "magic", "hybrid")
        },
        "notice": "旧成果按原理归纳；归纳联系不增加研发数量或自动解锁能力。"
        if groups
        else "科技与关系来自人物提案和独立评审；发明分别核验与应用。",
    }
