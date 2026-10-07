"""哥哥，方法知识只来自本人研究或实际送达，观察者的全局图不进人物输入。"""

from __future__ import annotations

import json

from world_engine.collective_evidence import visible_evidence
from world_engine.collective_research import CollectiveResearchService as R
from world_engine.materials import encoded
from world_engine.repository import from_iso, to_iso


def known_methods(c, actor_id, at):
    actor = R.actor(c, actor_id)
    rows = c.execute(
        "SELECT k.*,v.method_json,v.claim_json,v.conditions_json,"
        "COALESCE(json_extract(r.spec_json,'$.name'),t.name) name,t.status "
        "FROM technology_knowledge k JOIN technology_versions v ON v.techn"
        "ology_id=k.technology_id AND v.version=k.version AND v.world_id=k"
        ".world_id "
        "JOIN technologies t ON t.id=k.technology_id AND t.world_id=k.world_id "
        "LEFT JOIN science_candidate_revisions r ON r.candidate_id=v.source_candidate_id "
        "AND r.revision=v.source_revision AND r.world_id=v.world_id "
        "WHERE k.actor_id=? AND k.world_id=? AND k.learned_at<=? ORDER BY k.learned_at,k.rowid",
        (actor_id, actor["world_id"], to_iso(at)),
    ).fetchall()
    result = []
    for row in rows:
        data = dict(row)
        for name in ("method", "claim", "conditions", "evidence_ids"):
            data[name] = json.loads(data.pop(name + "_json"))
        result.append(data)
    return result


def method_packet(c, sender_id, technology_id, version, at):
    method = next(
        (
            m
            for m in known_methods(c, sender_id, at)
            if m["technology_id"] == technology_id and m["version"] == version
        ),
        None,
    )
    if method is None:
        raise ValueError("本人尚不知道这个科技方法版本")
    return {
        "world_id": method["world_id"],
        "technology_id": technology_id,
        "version": version,
        "method": method["method"],
        "claim": method["claim"],
        "conditions": method["conditions"],
        "name": method["name"],
        "sender_id": sender_id,
        "propagation_depth": method["propagation_depth"] + 1,
    }


def share_method(
    c, sender_id, recipient_id, technology_id, version, at, *, evidence_ids=(), text=""
):
    from world_engine.collective_messages import MessageService

    return MessageService.send(
        c,
        sender_id,
        recipient_id,
        at,
        text=text,
        evidence_ids=evidence_ids,
        technology_id=technology_id,
        technology_version=version,
    )


def receive_method(c, message_id, at):
    message = c.execute("SELECT * FROM collective_messages WHERE id=?", (message_id,)).fetchone()
    if message is None or not message["delivered_at"] or message["delivered_at"] > to_iso(at):
        raise ValueError("方法必须真实送达后才能取得")
    packet = json.loads(message["content_json"]).get("method_packet")
    if packet is None:
        return
    if (
        packet.get("world_id") != message["world_id"]
        or packet.get("sender_id") != message["sender_id"]
    ):
        raise ValueError("方法附件跨世界或发送人不一致")
    # 哥哥，核对发送时确实已知的不可变版本，不按世界最新版本替换附件。
    actual = method_packet(
        c,
        message["sender_id"],
        packet["technology_id"],
        packet["version"],
        from_iso(message["sent_at"]),
    )
    if actual != packet:
        raise ValueError("方法附件不等于发送人当时已知的原始版本")
    evidence = json.loads(message["content_json"]).get("evidence", [])
    c.execute(
        (
            "INSERT OR IGNORE INTO technology_knowledge(world_id,actor_id,tech"
            "nology_id,version,source_actor_id,source_message_id,evidence_ids_"
            "json,learned_at,propagation_depth) VALUES (?,?,?,?,?,?,?,?,?)"
        ),
        (
            message["world_id"],
            message["recipient_id"],
            packet["technology_id"],
            packet["version"],
            message["sender_id"],
            message_id,
            encoded([e["id"] for e in evidence]),
            message["delivered_at"],
            packet["propagation_depth"],
        ),
    )


def science_context(c, actor_id, at, *, max_chars, knowledge=None):
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1000:
        raise ValueError("人物科研上下文预算不足")
    base = R.context(c, actor_id, at)
    actor = R.actor(c, actor_id)
    base["original_skills"] = json.loads(actor["skills_json"])
    base["traits"] = json.loads(actor["traits_json"])
    base["personal_memories"] = [
        dict(r)
        for r in c.execute(
            (
                "SELECT id,event_id,memory_type,summary,confidence,created_at FROM "
                "character_memories WHERE character_id=? AND world_id=? AND created_at<=? "
                "ORDER BY importance DESC,created_at DESC LIMIT 12"
            ),
            (actor_id, actor["world_id"], to_iso(at)),
        )
    ]
    base["personal_relationships"] = [
        dict(r)
        for r in c.execute(
            "SELECT target_character_id,affinity,trust,updated_at FROM relationships "
            "WHERE source_character_id=? AND world_id=? AND updated_at<=?",
            (actor_id, actor["world_id"], to_iso(at)),
        )
    ]
    base["observations"] = [p for p in visible_evidence(c, actor_id, at).values()]
    base["known_methods"] = known_methods(c, actor_id, at)
    # 哥哥，曾经学到的方法不会让人物遥知远方之后的失效事件；执行器独立校验当前效力。
    for method in base["known_methods"]:
        method.pop("status", None)
        method["learned_as"] = "verified"
    base["science_plans"] = []
    required = set()
    for row in c.execute(
        (
            "SELECT * FROM science_programs WHERE actor_id=? AND created_at<=?"
            " ORDER BY created_at,rowid"
        ),
        (actor_id, to_iso(at)),
    ):
        data = {k: row[k] for k in ("id", "pc", "status", "error", "created_at")}
        data["program"] = json.loads(row["spec_json"])
        for h in data["program"].get("hypotheses", []):
            required.update(h["evidence_ids"])
        if row["status"] == "active":
            for cmd in data["program"].get("commands", []):
                required.update(cmd.get("evidence_ids", []))
            base["science_plans"].append(data)
        else:
            data["actual_result_bindings"] = json.loads(row["bindings_json"])
            base["own_submissions"].append(data)
    base["candidates"] = []
    for row in c.execute(
        (
            "SELECT * FROM technology_candidates WHERE actor_id=? AND created_"
            "at<=? ORDER BY created_at,rowid"
        ),
        (actor_id, to_iso(at)),
    ):
        base["candidates"].append(
            {k: row[k] for k in ("id", "revision", "name", "problem", "status", "created_at")}
            | {"spec": json.loads(row["spec_json"]), "gaps": json.loads(row["gaps_json"])}
        )
        required.update(json.loads(row["spec_json"]).get("evidence_ids", []))
    base["energy_stocks"] = [
        dict(r)
        for r in c.execute(
            "SELECT s.item_id,s.energy_per_unit,s.unit,i.quantity FROM science"
            "_energy_stocks s JOIN item_instances i ON i.id=s.item_id "
            "WHERE s.world_id=? AND i.owner_character_id=? AND ((i.container_t"
            "ype='ground' AND i.container_id=?) OR (i.container_type='characte"
            "r_inventory' AND i.container_id=?))",
            (R.actor(c, actor_id)["world_id"], actor_id, base["location_id"], actor_id),
        )
    ]
    base["public_knowledge"] = []
    if knowledge is not None:
        query = " ".join((str(base["identity"]), str(base["goals"])))
        hits = knowledge.search(
            query, audiences={"character_common"}, limit=4, max_total_chars=5000
        )
        base["public_knowledge"] = [
            {"source": h.chunk.source, "text": h.chunk.content} for h in hits
        ]
    base["context_diagnostics"] = {"dropped_old_records": 0, "max_chars": max_chars}
    # 哥哥，丢弃完整旧记录，绝不把 JSON 或当前方法从中间截断。
    optional = [p for p in base["observations"] if p["id"] not in required]
    while len(encoded(base)) > max_chars and optional:
        old = optional.pop(0)
        base["observations"].remove(old)
        base["context_diagnostics"]["dropped_old_records"] += 1
    while len(encoded(base)) > max_chars and base["own_submissions"]:
        base["own_submissions"].pop(0)
        base["context_diagnostics"]["dropped_old_records"] += 1
    if len(encoded(base)) > max_chars:
        raise ValueError("当前方法与计划所需的完整上下文超出预算，不能截断证据后调用")
    return base
