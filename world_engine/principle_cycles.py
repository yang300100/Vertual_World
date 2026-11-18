"""哥哥，原理研发按真实研究劳动累计，评审通过不代表零时间得到科技。"""

import json
from datetime import timedelta

from world_engine.event_log import record_event
from world_engine.principle_inventions import has_table
from world_engine.repository import from_iso, to_iso

POLICY = {
    "version": 1,
    "foundation_hours": 48,
    "technique_hours": 24,
    "improvement_hours": 12,
    "hybrid_multiplier": 1.5,
    "segment_minutes": 60,
    "daily_minutes": 240,
}


def enabled(session):
    return json.loads(session["manifest_json"]).get("research_cycle_version") == 1


def required_minutes(idea):
    hours = POLICY["foundation_hours"] if idea.level == "foundation" else POLICY["technique_hours"]
    if (
        idea.level == "technique"
        and idea.connections
        and all(r.kind == "improves" for r in idea.connections)
    ):
        hours = POLICY["improvement_hours"]
    if idea.domain == "hybrid":
        hours *= POLICY["hybrid_multiplier"]
    return int(hours * 60)


def project(c, proposal_id):
    if not has_table(c, "principle_projects"):
        return None
    return c.execute(
        "SELECT * FROM principle_projects WHERE proposal_id=?", (proposal_id,)
    ).fetchone()


def ready(c, row, at):
    """哥哥，达成须能从已完成工时复核，不能只信汇总进度或把未来劳动提前登记。"""
    earned = c.execute(
        "SELECT COALESCE(SUM(r.credited_minutes),0) FROM principle_research_work r "
        "JOIN character_life_activities a ON a.id=r.activity_id "
        "JOIN principle_proposals p ON p.id=r.proposal_id WHERE r.proposal_id=? "
        "AND a.kind='research' AND a.status='completed' AND a.world_id=p.world_id "
        "AND a.character_id=p.inventor_id AND r.settled_at<=? AND a.ends_world_time<=? "
        "AND a.last_processed_world_time>=a.ends_world_time",
        (row["proposal_id"], to_iso(at), to_iso(at)),
    ).fetchone()[0]
    return row["earned_minutes"] == row["required_minutes"] == earned


def defer(c, proposal, review_call_id, decision, idea, at):
    row = project(c, proposal["id"])
    if row:
        if row["review_call_id"] != review_call_id:
            raise ValueError("研发项目须保留最初已核验的独立评审")
        if row["earned_minutes"] >= row["required_minutes"] and not ready(c, row, at):
            raise ValueError("研发进度缺少当前时间之前真实完成的研究工时")
        return not ready(c, row, at)
    c.execute(
        "INSERT INTO principle_projects(proposal_id,world_id,review_call_id,required_minutes,"
        "status,policy_json,started_at) VALUES (?,?,?,?,'researching',?,?)",
        (
            proposal["id"],
            proposal["world_id"],
            review_call_id,
            required_minutes(idea),
            json.dumps(POLICY, ensure_ascii=False),
            to_iso(at),
        ),
    )
    c.execute(
        "UPDATE principle_proposals SET review_call_id=?,review_json=? WHERE id=?",
        (review_call_id, decision.model_dump_json(), proposal["id"]),
    )
    record_event(
        c,
        world_id=proposal["world_id"],
        tick_id="principle-project:" + proposal["id"],
        occurred_at=at,
        event_type="world.technology_research_started",
        actor_id=proposal["inventor_id"],
        summary=f"“{idea.name}”进入原理研究，须完成实际研究周期后才能研发达成。",
        payload={"proposal_id": proposal["id"], "required_minutes": required_minutes(idea)},
    )
    return True


def settle_activity(c, activity, at, status):
    if not has_table(c, "principle_research_work"):
        return
    work = c.execute(
        "SELECT * FROM principle_research_work WHERE activity_id=?", (activity["id"],)
    ).fetchone()
    if not work or work["settled_at"]:
        return
    actual = c.execute(
        "SELECT * FROM character_life_activities WHERE id=?", (activity["id"],)
    ).fetchone()
    proposal = c.execute(
        "SELECT * FROM principle_proposals WHERE id=?", (work["proposal_id"],)
    ).fetchone()
    credit = 0
    if (
        status == "completed"
        and actual["status"] == "completed"
        and actual["kind"] == "research"
        and actual["world_id"] == proposal["world_id"]
        and actual["character_id"] == proposal["inventor_id"]
        and from_iso(actual["ends_world_time"]) <= at
        and from_iso(actual["last_processed_world_time"]) >= from_iso(actual["ends_world_time"])
    ):
        minutes = int(
            (
                from_iso(actual["ends_world_time"]) - from_iso(actual["started_world_time"])
            ).total_seconds()
            / 60
        )
        credit = min(minutes, work["planned_minutes"])
    c.execute(
        "UPDATE principle_research_work SET credited_minutes=?,settled_at=? WHERE activity_id=?",
        (credit, to_iso(at), activity["id"]),
    )
    c.execute(
        "UPDATE principle_projects SET earned_minutes=MIN(required_minutes,earned_minutes+?),"
        "status=?,reason=? WHERE proposal_id=? AND status IN ('researching','paused')",
        (
            credit,
            "researching" if credit else "paused",
            "" if credit else "研究活动中断，本段未增加进度",
            work["proposal_id"],
        ),
    )


def _complete(c, row, at):
    from world_engine.materials import material_transaction
    from world_engine.principle_service import PrincipleTechnologyService

    proposal = c.execute(
        "SELECT * FROM principle_proposals WHERE id=?", (row["proposal_id"],)
    ).fetchone()
    try:
        with material_transaction(c):
            tid = PrincipleTechnologyService.review(
                c,
                row["proposal_id"],
                row["review_call_id"],
                json.loads(proposal["review_json"]),
                at,
            )
            c.execute(
                "UPDATE principle_projects SET status=?,technology_id=?,completed_at=?,reason=? "
                "WHERE proposal_id=?",
                (
                    "completed" if tid else "blocked",
                    tid,
                    to_iso(at) if tid else None,
                    "" if tid else "成果已重复或无法登记",
                    row["proposal_id"],
                ),
            )
    except ValueError as exc:
        c.execute(
            "UPDATE principle_projects SET status='blocked',reason=? WHERE proposal_id=?",
            (str(exc), row["proposal_id"]),
        )


def tick(c, world_id, at):
    """哥哥，休息、饥饿、行程、作息和约定优先；不补算没有发生的研究时间。"""
    if not has_table(c, "principle_projects"):
        return
    from world_engine.daily_life import DailyLifeService
    from world_engine.life import LifeActivityService
    from world_engine.routines import RoutineService
    from world_engine.schedules import ScheduleService

    rows = c.execute(
        "SELECT j.* FROM principle_projects j JOIN principle_proposals p ON p.id=j.proposal_id "
        "WHERE j.world_id=? AND j.status IN ('researching','paused') ORDER BY j.started_at,j.rowid",
        (world_id,),
    ).fetchall()
    for row in rows:
        if row["earned_minutes"] >= row["required_minutes"]:
            _complete(c, row, at)
            continue
        actor = c.execute(
            "SELECT a.* FROM characters a JOIN principle_proposals p ON p.inventor_id=a.id "
            "WHERE p.id=?",
            (row["proposal_id"],),
        ).fetchone()
        policy = json.loads(row["policy_json"])
        reason = ""
        if actor["health"] < 40:
            reason = "身体状况暂不适合研究"
        elif actor["satiety"] <= 30 or actor["energy"] <= 25:
            reason = "需要先用饭或休息"
        elif (
            LifeActivityService.running(c, actor["id"])
            or c.execute(
                "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",
                (actor["id"],),
            ).fetchone()
        ):
            # 哥哥，同一项目已开始的研究段继续正常推进，不把它当成外部阻碍。
            active = c.execute(
                "SELECT 1 FROM principle_research_work r JOIN character_life_activities a "
                "ON a.id=r.activity_id WHERE r.proposal_id=? AND a.status='running'",
                (row["proposal_id"],),
            ).fetchone()
            if active:
                continue
            reason = "正在进行其他活动或行程"
        elif DailyLifeService.is_sleep_time(actor, at) or DailyLifeService.is_sleep_time(
            actor, at + timedelta(minutes=policy["segment_minutes"], microseconds=-1)
        ):
            reason = "当前为日常休息时段"
        else:
            appointment = ScheduleService.assess(c, actor, at)
            routine = RoutineService.context(c, actor, at)
            if (
                appointment
                and ScheduleService.planned_minutes(appointment, at) < policy["segment_minutes"]
            ):
                reason = "需优先处理已有约定"
            elif routine and (
                routine["slot"]
                or routine["next_boundary"] < at + timedelta(minutes=policy["segment_minutes"])
            ):
                reason = "需保留已有作息时段"
        day = at.replace(hour=0, minute=0, second=0, microsecond=0)
        used = c.execute(
            "SELECT COALESCE(SUM(r.planned_minutes),0) FROM principle_research_work r "
            "JOIN character_life_activities a ON a.id=r.activity_id "
            "WHERE a.character_id=? AND a.started_world_time>=? AND a.started_world_time<?",
            (actor["id"], to_iso(day), to_iso(day + timedelta(days=1))),
        ).fetchone()[0]
        if used >= policy["daily_minutes"]:
            reason = "今日研究时间已用完，等待下一日"
        if reason:
            c.execute(
                "UPDATE principle_projects SET status='paused',reason=? WHERE proposal_id=?",
                (reason, row["proposal_id"]),
            )
            continue
        minutes = min(
            policy["segment_minutes"],
            policy["daily_minutes"] - used,
            row["required_minutes"] - row["earned_minutes"],
        )
        aid = LifeActivityService.start(c, actor, at, "research", minutes)
        c.execute(
            "INSERT INTO principle_research_work(activity_id,proposal_id,planned_minutes) "
            "VALUES (?,?,?)",
            (aid, row["proposal_id"], minutes),
        )
        c.execute(
            "UPDATE principle_projects SET status='researching',reason='' WHERE proposal_id=?",
            (row["proposal_id"],),
        )


def cycle_view(row):
    return {
        "status": row["status"],
        "required_minutes": row["required_minutes"],
        "earned_minutes": row["earned_minutes"],
        "remaining_minutes": row["required_minutes"] - row["earned_minutes"],
        "progress": round(100 * row["earned_minutes"] / row["required_minutes"], 1),
        "reason": row["reason"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
    }


def projects(c, world_id):
    if not has_table(c, "principle_projects"):
        return []
    return [
        {
            "id": row["proposal_id"],
            "name": json.loads(row["spec_json"])["name"],
            "domain": json.loads(row["spec_json"]).get("domain", "physical"),
            "principle": json.loads(row["spec_json"])["principle"],
            "requirements": json.loads(row["spec_json"])["requirements"],
            "limitations": json.loads(row["spec_json"])["limitations"],
            "cycle": cycle_view(row),
        }
        for row in c.execute(
            "SELECT j.*,p.spec_json FROM principle_projects j JOIN principle_proposals p "
            "ON p.id=j.proposal_id WHERE j.world_id=? AND j.status!='completed' "
            "ORDER BY j.started_at,j.rowid",
            (world_id,),
        )
    ]
