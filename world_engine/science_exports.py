"""哥哥，报告直接整理原始记录，不用额外模型补故事。"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

from world_engine.science_chronicle import technology_process
from world_engine.science_freeze import verify_freeze
from world_engine.science_runner import ScienceRunner
from world_engine.technologies import TechnologyService


def sanitize(value):
    if isinstance(value, dict):
        return {
            k: sanitize(v)
            for k, v in value.items()
            if str(k).lower().replace("-", "_")
            not in {"api_key", "apikey", "deepseek_api_key", "authorization", "cookie", "password"}
        }
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, str):
        return re.sub(r"(?i)Bearer\s+[A-Za-z0-9_.-]+", "Bearer [已隐藏]", value)
    return value


def graph_svg(graph):
    nodes, edges = graph["nodes"], graph["edges"]
    if not nodes:
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" width="680" height="120">'
            '<rect width="100%" height="100%" fill="#171d29"/>'
            '<text x="24" y="65" fill="white">尚无已验证科技</text></svg>'
        )
    ranks = {n["id"]: 0 for n in nodes}
    for _ in nodes:
        change = False
        for e in edges:
            if (
                e["parent_id"] in ranks
                and e["child_id"] in ranks
                and ranks[e["child_id"]] <= ranks[e["parent_id"]]
            ):
                ranks[e["child_id"]] = ranks[e["parent_id"]] + 1
                change = True
        if not change:
            break
    rows, pos = {}, {}
    for n in nodes:
        level = ranks[n["id"]]
        row = rows.get(level, 0)
        rows[level] = row + 1
        pos[n["id"]] = (20 + level * 330, 20 + row * 100)
    result = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{(max(ranks.values()) + 1) * 330}" '
        f'height="{max(rows.values()) * 100 + 30}">'
        '<rect width="100%" height="100%" fill="#171d29"/>'
    ]
    for e in edges:
        if e["parent_id"] in pos and e["child_id"] in pos:
            a, b = pos[e["parent_id"]], pos[e["child_id"]]
            result.append(
                f'<path d="M {a[0] + 290} {a[1] + 35} L {b[0]} {b[1] + 35}" '
                'stroke="#93b6b0" fill="none"/>'
            )
    for n in nodes:
        x, y = pos[n["id"]]
        color = "#4a2830" if n["version_status"] == "invalidated" else "#213b38"
        result.append(
            f'<rect x="{x}" y="{y}" width="290" height="74" rx="10" fill="{color}" '
            'stroke="#73bbaa"/>'
            f'<text x="{x + 12}" y="{y + 27}" fill="white">{html.escape(n["name"])}</text>'
            f'<text x="{x + 12}" y="{y + 53}" fill="#cbd6d3">版本{n["current_version"]} · '
            f"{html.escape(n['version_status'])}</text>"
        )
    return "".join(result) + "</svg>"


def export_science(database, session_id, output_dir):
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    with database.read() as c:
        if not c.in_transaction:
            c.execute("BEGIN")
        session = c.execute("SELECT * FROM science_sessions WHERE id=?", (session_id,)).fetchone()
        if session is None:
            raise ValueError("科研会话不存在")
        wid = session["world_id"]
        view = object.__new__(ScienceRunner)
        view.session_id = session_id
        usage = view._usage(c)
        graph = TechnologyService.graph(c, wid)
        calls = [
            dict(r)
            for r in c.execute(
                "SELECT * FROM science_model_calls WHERE session_id=? ORDER BY rowid", (session_id,)
            )
        ]
        programs = [
            dict(r)
            for r in c.execute(
                "SELECT * FROM science_programs WHERE session_id=? ORDER BY rowid", (session_id,)
            )
        ]
        commands = [
            dict(r)
            for r in c.execute(
                (
                    "SELECT d.* FROM science_commands d JOIN science_programs p ON "
                    "p.id=d.program_id WHERE p.session_id=? ORDER BY p.rowid,d.command_index"
                ),
                (session_id,),
            )
        ]
        executions = [
            dict(r)
            for r in c.execute(
                "SELECT * FROM technology_executions WHERE session_id=? ORDER BY rowid",
                (session_id,),
            )
        ]
        actors = [
            dict(r)
            for r in c.execute(
                (
                    "SELECT id,name,identity,skills_json,goals_json,health,energy,satiety "
                    "FROM characters WHERE world_id=? AND is_player=0 ORDER BY id"
                ),
                (wid,),
            )
        ]
        messages = [
            dict(r)
            for r in c.execute(
                "SELECT * FROM collective_messages WHERE world_id=? ORDER BY rowid", (wid,)
            )
        ]
        observations = [
            dict(r)
            for r in c.execute(
                "SELECT * FROM collective_observations WHERE world_id=? ORDER BY world_time,rowid",
                (wid,),
            )
        ]
        candidates = [
            dict(r)
            for r in c.execute(
                "SELECT * FROM technology_candidates WHERE session_id=? ORDER BY rowid",
                (session_id,),
            )
        ]
        work = [
            dict(r)
            for r in c.execute(
                (
                    "SELECT o.* FROM collective_work_orders o JOIN collective_proposals p ON "
                    "p.id=o.proposal_id WHERE p.world_id=? ORDER BY o.rowid"
                ),
                (wid,),
            )
        ]
        ledger = [
            dict(r)
            for r in c.execute(
                "SELECT * FROM material_ledger WHERE world_id=? ORDER BY rowid", (wid,)
            )
        ]
        events = [
            dict(r)
            for r in c.execute(
                (
                    "SELECT * FROM world_events WHERE world_id=? AND event_type LIKE "
                    "'world.technology_%' ORDER BY occurred_at,rowid"
                ),
                (wid,),
            )
        ]
        processes = {n["id"]: technology_process(c, wid, n["id"]) for n in graph["nodes"]}
        frozen = json.loads(session["freeze_json"])
        freeze_ok = verify_freeze(c, session_id)
        verified = [
            n
            for n in graph["nodes"]
            if n["status"] == "verified" and n["version_status"] == "verified"
        ]
        verified_ids = {n["id"] for n in verified}
        dependencies = [
            e
            for e in graph["edges"]
            if e["parent_id"] in verified_ids and e["child_id"] in verified_ids
        ]
        uses = [
            e
            for e in executions
            if e["technology_id"] in verified_ids and e["status"] == "completed"
        ]
        calls_by_id = {r["id"]: r for r in calls}
        programs_by_id = {r["id"]: r for r in programs}
        traceable = bool(verified)
        for n in verified:
            candidate = next(r for r in candidates if r["id"] == n["candidate_id"])
            source = programs_by_id.get(candidate["source_program_id"])
            traceable &= bool(
                source
                and source["call_id"] in calls_by_id
                and calls_by_id[source["call_id"]]["response_json"]
            )
        budget_ok = (
            usage["calls"] <= usage["max_calls"]
            and usage["accounted_tokens"] + usage["reserved_tokens"] <= usage["token_limit"]
        )
        report = {
            "session_id": session_id,
            "world_id": wid,
            "verified_technologies": len(verified),
            "actual_dependencies": len(dependencies),
            "completed_reuses": len(uses),
            "api_traceable": bool(traceable),
            "freeze_intact": freeze_ok,
            "budget_within_limit": budget_ok,
            "goal_achieved": bool(
                len(verified) >= 3
                and dependencies
                and uses
                and traceable
                and budget_ok
                and freeze_ok
                and frozen.get("verification", {}).get("run_kind") == "real"
            ),
            "stop_reason": session["stop_reason"],
            "limitations": "仅有限自然模型中的能力；离线固定程序不作为真实NPC成功证据",
        }

    def write_json(name, data):
        (output / name).write_text(
            json.dumps(sanitize(data), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def block(data):
        return "\n```json\n" + json.dumps(sanitize(data), ensure_ascii=False, indent=2) + "\n```\n"

    write_json("api-usage.json", usage)
    write_json("科技图.json", graph)
    write_json("freeze.json", frozen)
    write_json("audit.json", report)
    write_json(
        "raw-records.json",
        {
            "session": dict(session),
            "calls": calls,
            "programs": programs,
            "commands": commands,
            "executions": executions,
            "candidates": candidates,
            "work": work,
            "material_ledger": ledger,
            "actors": actors,
            "messages": messages,
            "observations": observations,
            "events": events,
        },
    )
    (output / "科技图.svg").write_text(graph_svg(graph), encoding="utf-8")
    process_dir = output / "突破过程"
    process_dir.mkdir(exist_ok=True)
    for tid, process in processes.items():
        write_json("突破过程/" + tid + ".json", process)
        (process_dir / (tid + ".md")).write_text(
            "# " + process["technology"]["name"] + "\n" + block(process), encoding="utf-8"
        )
    names = {a["id"]: a["name"] for a in actors}
    text = [
        "# 科研处理过程",
        "全部请求与实际执行分别留存；读数来自真实工单，文字不算成果。",
        "## API用量",
        block(usage),
    ]
    for index, call in enumerate(calls, 1):
        text += [
            f"## 第{index}次判断 · {names.get(call['actor_id'], call['actor_id'])}",
            f"世界时间：{call['created_at']}；状态：{call['status']}；请求编号：{call['id']}",
            "### 原始输入与回应",
            block(call),
            "### 人物程序与实际执行",
            block([p for p in programs if p["call_id"] == call["id"]]),
        ]
    text += [
        "## 候选及拒绝原因",
        block(candidates),
        "## 实际工单与结果",
        block(work),
        "## 实测与传递事实",
        block(observations),
        "## 最终验收",
        block(report),
        "完整动作、实物账与方法执行见 raw-records.json；输出编号可能已被后续消耗，并非当前库存。",
    ]
    (output / "处理过程.md").write_text("\n\n".join(text), encoding="utf-8")
    (output / "全部人物与通信.md").write_text(
        "# 全部人物与通信\n\n## 人物\n"
        + block(actors)
        + "\n## 原始消息与实际送达\n"
        + block(messages),
        encoding="utf-8",
    )
    (output / "编年.md").write_text(
        "# 科研世界编年\n" + (block(events) if events else "\n尚无已验证突破事件。\n"),
        encoding="utf-8",
    )
    (output / "最终验收.md").write_text(
        "# 最终验收\n\n"
        + (
            "全部真实验收条件满足。"
            if report["goal_achieved"]
            else "尚未达成目标；不能用候选、文字或离线测试成果代替真实突破。"
        )
        + block(report)
        + "\n## 费用\n"
        + block(usage),
        encoding="utf-8",
    )
    return report
