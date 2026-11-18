"""哥哥，实物科研页面只整理真实工单与复用记录，不预测突破或编造收益。"""

import json
import math

from world_engine.materials import encoded
from world_engine.principle_inventions import has_table
from world_engine.repository import from_iso


def _orders(c, world_id, *, program_id=None, execution_id=None):
    clause = "e.source_program_id=?" if program_id is not None else "e.id=?"
    identity = program_id if program_id is not None else execution_id
    return c.execute(
        "SELECT o.*,a.started_world_time labor_started,"
        "a.last_processed_world_time labor_processed FROM collective_work_orders o "
        "JOIN science_execution_work l ON l.order_id=o.id "
        "JOIN technology_executions e ON e.id=l.execution_id "
        "LEFT JOIN character_life_activities a ON a.id=o.activity_id "
        "WHERE e.world_id=? AND " + clause + " ORDER BY o.rowid",
        (world_id, identity),
    ).fetchall()


def _minutes(hours):
    if isinstance(hours, bool) or not isinstance(hours, (int, float)):
        return 0
    return hours * 60 if math.isfinite(hours) and hours >= 0 else 0


def _result(order):
    value = json.loads(order["result_json"] or "null")
    return value if isinstance(value, dict) else {}


def _native_order(activity, operation, at, *, status, due_at, sample_id=None):
    planned = max(
        0,
        (
            from_iso(activity["ends_world_time"]) - from_iso(activity["started_world_time"])
        ).total_seconds()
        / 3600,
    )
    is_research = activity["kind"] == "research"
    effective = min(at, from_iso(activity["last_processed_world_time"]))
    worked = min(
        planned,
        max(0, (effective - from_iso(activity["started_world_time"])).total_seconds() / 3600),
    )
    return {
        "status": status,
        "spec_json": encoded({"operation": operation, "hours": planned, "batch_id": sample_id}),
        "inputs_json": "[]",
        "due_at": due_at,
        "labor_started": activity["started_world_time"],
        "labor_processed": activity["last_processed_world_time"],
        "result_json": encoded({"actual_hours": worked if is_research else 0}),
        "is_research": is_research,
    }


def _native_orders(c, program, at):
    result = []
    seen_sources = set()
    commands = c.execute(
        "SELECT source_kind,source_id FROM science_commands WHERE program_id=? "
        "AND source_kind IN ('collective','resonance') ORDER BY command_index",
        (program["id"],),
    ).fetchall()
    for command in commands:
        identity = (command["source_kind"], command["source_id"])
        if identity in seen_sources:
            continue
        # 哥哥，幂等采纳同一程序不产生第二份真实劳动。
        seen_sources.add(identity)
        if command["source_kind"] == "collective":
            for job in c.execute(
                "SELECT j.* FROM collective_jobs j JOIN collective_programs p ON p.id=j.program_id "
                "WHERE p.id=? AND p.world_id=? AND p.character_id=? ORDER BY j.command_index",
                (command["source_id"], program["world_id"], program["actor_id"]),
            ).fetchall():
                activity = c.execute(
                    "SELECT * FROM character_life_activities WHERE id=? "
                    "AND world_id=? AND character_id=?",
                    (job["activity_id"], program["world_id"], program["actor_id"]),
                ).fetchone()
                if activity is None:
                    continue
                status = "working" if job["status"] in {"working", "incubating"} else job["status"]
                result.append(
                    _native_order(
                        activity,
                        json.loads(job["spec_json"])["op"],
                        at,
                        status=status,
                        due_at=job["due_at"],
                        sample_id=job["sample_id"],
                    )
                )
        else:
            # 哥哥，分段装置劳动的来源由真实开始事件保存，不能只读最后一个activity。
            for activity in c.execute(
                "SELECT a.*,e.payload_json FROM character_life_activities a "
                "JOIN world_events e ON e.id=a.source_event_id "
                "JOIN research_programs r ON r.id=json_extract(e.payload_json,'$.program_id') "
                "WHERE r.id=? AND r.character_id=? AND a.character_id=? AND a.world_id=? "
                "AND e.world_id=? AND e.event_type='research.work_started' ORDER BY a.rowid",
                (
                    command["source_id"],
                    program["actor_id"],
                    program["actor_id"],
                    program["world_id"],
                    program["world_id"],
                ),
            ).fetchall():
                result.append(
                    _native_order(
                        activity,
                        json.loads(activity["payload_json"])["operation"],
                        at,
                        status="working" if activity["status"] == "running" else activity["status"],
                        due_at=activity["ends_world_time"],
                    )
                )
    return result


def _stage(c, world_id, order, at):
    task = json.loads(order["spec_json"])
    planned = _minutes(task["hours"])
    worked = 0
    is_research = order.get("is_research", True) if isinstance(order, dict) else True
    if (
        is_research
        and order["status"] == "working"
        and order["labor_started"]
        and order["labor_processed"]
    ):
        start = from_iso(order["labor_started"])
        end = min(at, from_iso(order["labor_processed"]))
        worked = min(planned, max(0, (end - start).total_seconds() / 60))
    inputs = json.loads(order["inputs_json"])
    sample_id = inputs[0] if inputs else task.get("batch_id")
    sample = c.execute(
        "SELECT t.name FROM item_instances i JOIN item_types t ON t.id=i.item_type_id "
        "WHERE i.id=? AND i.world_id=?",
        (sample_id, world_id),
    ).fetchone()
    return {
        "operation": task["operation"],
        "status": order["status"],
        "planned_minutes": planned,
        "worked_minutes": round(worked, 6),
        "due_at": order["due_at"],
        "sample_name": sample["name"] if sample else "研究现场",
        "is_research": is_research,
    }


def physical_projects(c, world_id):
    if not has_table(c, "science_programs"):
        return []
    at = from_iso(
        c.execute("SELECT w.current_time FROM worlds w WHERE w.id=?", (world_id,)).fetchone()[0]
    )
    result = []
    for program in c.execute(
        "SELECT * FROM science_programs WHERE world_id=? AND status='active' ORDER BY rowid",
        (world_id,),
    ).fetchall():
        spec = json.loads(program["spec_json"])
        declaration = next((cmd for cmd in spec["commands"] if cmd["op"] == "declare"), None)
        orders = [dict(o) for o in _orders(c, world_id, program_id=program["id"])]
        orders += _native_orders(c, program, at)
        stage = next((o for o in orders if o["status"] == "working"), None)
        stage = stage or next((o for o in orders if o["status"] == "queued"), None)
        domain = "physical"
        # 哥哥，普通共振研究装置不等于魔纹，不能仅凭入口名称标成魔法。
        completed = [o for o in orders if o["status"] == "completed" and o.get("is_research", True)]
        settled = sum(
            _minutes(_result(o).get("actual_hours"))
            for o in orders
            if o["status"] in {"completed", "interrupted"}
        )
        result.append(
            {
                "id": program["id"],
                "mode": "physical_experiment",
                "domain": domain,
                "name": declaration["name"] if declaration else "实物科研试验",
                "principle": declaration["problem"]
                if declaration
                else "当前提交的是实物试验计划，观察和后续结论以实际执行结果为准。",
                "source_reason": spec["reason"],
                "requirements": ["现场材料与器具", "实际研究劳动", "重复试验与对照证据"],
                "limitations": "程序完成不等于科技达成，结论仍需真实检验与认证。",
                "cycle": {
                    "kind": "empirical",
                    "status": "researching"
                    if stage and stage["status"] == "working" and stage.get("is_research", True)
                    else "paused"
                    if stage
                    else "unmapped",
                    # 哥哥，未知的后续试验不能被塞进一个承诺必定达成的倒计时。
                    "required_minutes": None,
                    "progress": None,
                    "remaining_minutes": None,
                    "earned_minutes": round(settled, 6),
                    "settled_steps": len(completed),
                    "current_stage": _stage(c, world_id, stage, at) if stage else None,
                    "reason": "还需按实测结果判断、重复和认证，不能承诺科技达成日期。"
                    if stage
                    else (
                        "当前阶段的时间来源尚未完整汇总，请查看原始记录；"
                        "不能把缺少数据当成暂停或完成。"
                    ),
                    "started_at": program["created_at"],
                    "completed_at": None,
                },
            }
        )
    return result


def physical_impacts(c, world_id):
    if not has_table(c, "technology_executions"):
        return []
    result = []
    executions = c.execute(
        "SELECT e.*,t.name technology_name,a.name actor_name FROM technology_executions e "
        "JOIN technologies t ON t.id=e.technology_id AND t.world_id=e.world_id "
        "JOIN characters a ON a.id=e.actor_id AND a.world_id=e.world_id "
        "WHERE e.world_id=? AND e.status='completed' AND e.completed_at IS NOT NULL "
        "ORDER BY e.completed_at DESC,e.rowid DESC",
        (world_id,),
    ).fetchall()
    for execution in executions:
        orders = _orders(c, world_id, execution_id=execution["id"])
        # 哥哥，只登记候选、运行中或中断的工单，不能贡献一次已完成应用。
        if not orders or any(
            o["status"] != "completed" or _result(o).get("completed") is not True for o in orders
        ):
            continue
        hours = sum(_minutes(_result(o).get("actual_hours")) for o in orders) / 60
        outputs = json.loads(execution["output_ids_json"])
        result.append(
            {
                "id": "physical:" + execution["id"],
                "world_id": world_id,
                "technology_id": execution["technology_id"],
                "invention_id": None,
                "technology_name": execution["technology_name"],
                "actor_name": execution["actor_name"],
                "source_kind": "technology_execution",
                "source_id": execution["id"],
                "source_status": "completed",
                "occurred_at": execution["completed_at"],
                "before_json": encoded({"本次记录的产物批次": 0, "已结算劳动小时": 0}),
                "after_json": encoded(
                    {"本次记录的产物批次": len(outputs), "已结算劳动小时": round(hours, 6)}
                ),
                "notice": "这里只记录本次已结算的劳动和产物，不据此推算产能或收益。",
            }
        )
    return result
