"""哥哥，研发验收读取原始提案、评审和完成的应用，测试数据不会伪装为真实达标。"""

import json
from pathlib import Path

from world_engine.principle_service import PrincipleTechnologyService


def export_principle(database, session_id, output):
    folder = Path(output)
    folder.mkdir(parents=True, exist_ok=True)
    with database.read() as c:
        c.execute("BEGIN")
        session = c.execute("SELECT * FROM principle_sessions WHERE id=?", (session_id,)).fetchone()
        if session is None:
            raise ValueError("科研会话不存在")
        manifest = json.loads(session["manifest_json"])
        from world_engine.phenomena import audit_world

        phenomenon_audit = audit_world(c, session["world_id"])
        from world_engine.principle_cycles import cycle_view
        from world_engine.principle_inventions import has_table

        research_cycles = (
            [
                {
                    "proposal_id": row["proposal_id"],
                    "technology_id": row["technology_id"],
                    "cycle": cycle_view(row),
                }
                for row in c.execute(
                    "SELECT j.* FROM principle_projects j JOIN principle_proposals p "
                    "ON p.id=j.proposal_id "
                    "WHERE p.session_id=?",
                    (session_id,),
                )
            ]
            if has_table(c, "principle_projects")
            else []
        )
        graph = PrincipleTechnologyService.graph(c, session["world_id"])
        calls = [
            dict(r)
            for r in c.execute(
                "SELECT * FROM principle_calls WHERE session_id=? ORDER BY rowid", (session_id,)
            )
        ]
        proposals = [
            dict(r)
            for r in c.execute(
                "SELECT * FROM principle_proposals WHERE session_id=?", (session_id,)
            )
        ]
        impacts = [
            dict(r)
            for r in c.execute(
                "SELECT i.*,t.name technology_name,a.name actor_name,"
                "CASE WHEN i.source_kind='life_activity' THEN l.status "
                "WHEN m.quantity_before>m.quantity_after AND m.satiety_after>m.satiety_before "
                "THEN 'completed' ELSE 'unproven' END source_status "
                "FROM principle_impacts i JOIN principle_technologies t ON t.id=i.technology_id "
                "JOIN characters a ON a.id=i.actor_id LEFT JOIN character_life_activities l "
                "ON l.id=i.source_id AND i.source_kind='life_activity' "
                "LEFT JOIN principle_meals m ON m.id=i.source_id AND i.source_kind='meal' "
                "WHERE i.world_id=? "
                "ORDER BY i.rowid",
                (session["world_id"],),
            )
        ]
        completed_ids = {r["technology_id"] for r in impacts if r["source_status"] == "completed"}
        call_by_id = {r["id"]: r for r in calls}
        current_nodes = [n for n in graph["nodes"] if n["session_id"] == session_id]
        traceable = bool(current_nodes) and all(
            (
                call_by_id.get(p["source_call_id"], {}).get("status") == "succeeded"
                and call_by_id.get(p["review_call_id"], {}).get("status") == "succeeded"
            )
            for p in proposals
            if p["status"] == "researched"
        )
        accounted = sum(r["accounted_tokens"] or 0 for r in calls)
        reserved = sum(r["reserved_tokens"] for r in calls if r["status"] == "reserved")
        budget_ok = (
            len(calls) <= session["max_calls"] and accounted + reserved <= session["token_limit"]
        )
        count = len(graph["nodes"])
        from world_engine.principle_inventions import has_table

        modern = has_table(c, "principle_inventions")
        inventions = (
            [
                dict(r)
                for r in c.execute(
                    "SELECT v.* FROM principle_inventions v JOIN principle_technologies t "
                    "ON t.id=v.technology_id WHERE t.session_id=?",
                    (session_id,),
                )
            ]
            if modern
            else []
        )
        reuses = (
            [
                dict(r)
                for r in c.execute(
                    "SELECT r.* FROM principle_reuses r JOIN principle_technologies t "
                    "ON t.id=r.child_id WHERE t.session_id=?",
                    (session_id,),
                )
            ]
            if modern
            else []
        )
        modern_prototypes = (
            c.execute(
                "SELECT count(*) FROM principle_invention_prototypes p JOIN principle_inventions v "
                "ON v.id=p.invention_id JOIN principle_technologies t ON t.id=v.technology_id "
                "WHERE t.session_id=?",
                (session_id,),
            ).fetchone()[0]
            if modern
            else 0
        )
        applied_designs = (
            {
                r[0]
                for r in c.execute(
                    "SELECT a.invention_id FROM principle_application_attempts a "
                    "LEFT JOIN character_life_activities l "
                    "ON l.id=json_extract(a.result_json,'$.activity_id') "
                    "LEFT JOIN principle_meals m ON m.id=json_extract(a.result_json,'$.meal_id') "
                    "WHERE json_extract(a.result_json,'$.status')='applied' AND "
                    "(l.status='completed' OR (m.quantity_before>m.quantity_after "
                    "AND m.satiety_after>m.satiety_before))"
                )
            }
            if modern
            else set()
        )
        multi = any(
            sum(v["technology_id"] == t["id"] and v["id"] in applied_designs for v in inventions)
            >= 2
            for t in graph["nodes"]
        )
        property_impacts = sum(
            i["kind"] in {"material_strength", "craft_precision"}
            and i["source_status"] == "completed"
            for i in impacts
        )
        overall = None
        if manifest.get("budget_lineage"):
            from world_engine.principle_budget import usage

            overall = usage(manifest["budget_lineage"])
            budget_ok = (
                budget_ok
                and overall["calls"] <= overall["max_calls"]
                and overall["accounted_tokens"] + overall["reserved_tokens"]
                <= overall["token_limit"]
            )
        report = {
            "session_id": session_id,
            "world_id": session["world_id"],
            "basis": "principle_review",
            "research_cycles": research_cycles,
            "initial_technologies": manifest.get("initial_technologies"),
            "researched_technologies": count,
            "dependencies": len(graph["edges"]),
            "manufactured_prototypes": c.execute(
                "SELECT count(*) FROM principle_prototypes p JOIN principle_technologies t "
                "ON t.id=p.technology_id WHERE t.session_id=?",
                (session_id,),
            ).fetchone()[0]
            + modern_prototypes,
            "recorded_impacts": len(impacts),
            "technologies_with_completed_impacts": len(completed_ids),
            "calls": len(calls),
            "accounted_tokens": accounted,
            "reserved_tokens": reserved,
            "budget_within_limit": budget_ok,
            "api_traceable": traceable,
            "status": session["status"],
            "stop_reason": session["stop_reason"],
            "invention_count": len(inventions),
            "approved_inventions": sum(v["status"] == "approved" for v in inventions),
            "actual_dependency_reuses": len(reuses),
            "multiple_inventions_applied": multi,
            "completed_property_impacts": property_impacts,
            "research_achieved": count >= session["target_count"] and traceable,
            "new_technologies_this_session": len(current_nodes),
            "cumulative_usage": overall,
            "goal_achieved": bool(
                manifest.get("run_kind") == "real"
                and manifest.get("initial_technologies") == 0
                and count >= session["target_count"]
                and len(completed_ids) >= session["target_count"]
                and traceable
                and budget_ok
                and reserved == 0
            ),
        }
        if manifest.get("acceptance_version") == "chain_inventions_v1":
            report["goal_achieved"] = bool(
                manifest.get("run_kind") == "real"
                and manifest.get("initial_technologies") == 0
                and report["research_achieved"]
                and report["dependencies"] > 0
                and reuses
                and multi
                and property_impacts
                and budget_ok
                and reserved == 0
            )
        if manifest.get("acceptance_version") == "continuation_properties_v1":
            current_ids = {n["id"] for n in current_nodes}
            kinds = (
                {manifest["requested_property"]}
                if manifest.get("requested_property")
                else {"material_strength", "craft_precision"}
            )
            new_property_impacts = sum(
                i["technology_id"] in current_ids
                and i["kind"] in kinds
                and i["source_status"] == "completed"
                for i in impacts
            )
            report["completed_new_property_impacts"] = new_property_impacts
            report["research_achieved"] = (
                len(current_nodes) >= session["target_count"] and traceable
            )
            report["goal_achieved"] = bool(
                report["research_achieved"]
                and new_property_impacts
                and budget_ok
                and reserved == 0
                and manifest.get("run_kind") == "real"
            )
    for name, value in (
        ("audit.json", report),
        ("科技树.json", graph),
        ("世界影响.json", impacts),
        ("费用.json", calls),
        ("原始提案.json", proposals),
        ("衍生发明.json", inventions),
        ("实际依赖复用.json", reuses),
        ("现象与规则审计.json", phenomenon_audit),
    ):
        (folder / name).write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    lines = [
        "# 原理科研与世界影响验收",
        "",
        "研发依据：原理评审；实物实验不是本轮达成条件。",
        f"初始科技：{report['initial_technologies']}；当前研发达成：{count}；依赖：{report['dependencies']}。",
        f"有已结算影响的科技：{len(completed_ids)}；调用：{len(calls)}；"
        f"累计计账：{accounted} token。",
        f"达标：{'是' if report['goal_achieved'] else '否'}；"
        f"停止原因：{report['stop_reason'] or '尚在进行'}。",
        f"独立发明：{len(inventions)}；实际依赖复用：{len(reuses)}；"
        f"多发明应用：{multi}；已结算物品属性变化：{property_impacts}。",
        "累计继承费用：" + json.dumps(overall, ensure_ascii=False),
        "",
        "## 研发科技",
    ]
    for node in graph["nodes"]:
        lines.extend(["", "### " + node["name"], node["principle"], "限制：" + node["limitations"]])
    lines.extend(["", "## 实际应用变化"])
    for impact in impacts:
        lines.append(
            f"- {impact['actor_name']}应用{impact['technology_name']}："
            f"{impact['before_json']} → {impact['after_json']}；{impact['source_status']}。"
        )
    (folder / "验收报告.md").write_text("\n".join(lines), encoding="utf-8")
    return report
