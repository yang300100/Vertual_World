"""哥哥，每次结算只跨一个已知事件边界，物理时间与现实等待分开。"""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import timedelta

from world_engine.material_physics import advance
from world_engine.materials import MaterialService as M
from world_engine.repository import from_iso


def next_boundary(c, world_id, at, limit_hours=1):
    if not math.isfinite(limit_hours) or limit_hours <= 0 or limit_hours > 1:
        raise ValueError("心跳边界上限须在0到1世界小时内")
    result = at + timedelta(hours=limit_hours)
    queries = (
        (
            "SELECT estimated_arrival_world FROM character_movements WHERE world_id=? AND status='moving'",
        ),
        (
            "SELECT ends_world_time FROM character_life_activities WHERE world_id=? AND status='running'",
        ),
        (
            "SELECT j.due_at FROM collective_jobs j JOIN collective_programs p ON p.id=j.program_id WHERE p.world_id=? AND j.status IN ('working','incubating')",
        ),
        (
            "SELECT due_at FROM collective_work_orders w JOIN collective_proposals p ON p.id=w.proposal_id WHERE p.world_id=? AND w.status='working'",
        ),
        ("SELECT due_at FROM collective_messages WHERE world_id=? AND delivered_at IS NULL",),
        (
            "SELECT m.due_at FROM research_messages m JOIN research_networks n ON n.id=m.network_id "
            "WHERE n.world_id=? AND m.delivered_event_id IS NULL AND m.failed_event_id IS NULL",
        ),
        ("SELECT due_at FROM collective_crops WHERE world_id=? AND status='growing'",),
        ("SELECT due_at FROM collective_exposures WHERE world_id=? AND settled_at IS NULL",),
        ("SELECT next_due_at FROM collective_recurring_jobs WHERE world_id=? AND status='active'",),
        (
            "SELECT spoils_world_time FROM item_instances WHERE world_id=? AND spoils_world_time IS NOT NULL",
        ),
    )
    for (query,) in queries:
        for row in c.execute(query, (world_id,)):
            if row[0]:
                due = from_iso(row[0])
                if at < due < result:
                    result = due
    for row in c.execute(
        "SELECT last_consumed_at FROM collective_regions WHERE world_id=?", (world_id,)
    ):
        due = from_iso(row[0]) + timedelta(days=1)
        if at < due < result:
            result = due
    for row in c.execute(
        "SELECT j.started_at,j.spec_json FROM collective_jobs j JOIN collective_programs p ON p.id=j.program_id WHERE p.world_id=? AND j.status='working'",
        (world_id,),
    ):
        spec = json.loads(row["spec_json"])
        if spec.get("operation") and not spec.get("processing_applied_hours"):
            due = from_iso(row["started_at"]) + timedelta(hours=spec["hours"])
            if at < due < result:
                result = due
    policy = c.execute(
        "SELECT policy_json FROM material_modes WHERE world_id=?", (world_id,)
    ).fetchone()
    rate = json.loads(policy[0]).get("satiety_loss_per_world_hour", 3) if policy else 3
    if rate > 0:
        for actor in c.execute(
            "SELECT a.satiety,COALESCE(s.satiety_residual,0) residual FROM characters a LEFT JOIN character_state_accumulators s ON s.character_id=a.id WHERE a.world_id=? AND a.health>0 AND NOT EXISTS (SELECT 1 FROM character_life_activities l WHERE l.character_id=a.id AND l.status='running')",
            (world_id,),
        ):
            if actor["satiety"] > 60:
                delay = (actor["satiety"] - 60 - actor["residual"]) / rate
                if delay > 1e-8:
                    result = min(result, at + timedelta(hours=delay))
    return result


def advance_materials(c, world_id, hours):
    skip = set()
    from world_engine.collective_production import ProductionService

    at = from_iso(
        c.execute("SELECT w.current_time FROM worlds w WHERE id=?", (world_id,)).fetchone()[0]
    )
    for crop in c.execute(
        "SELECT c.*,r.soil_id FROM collective_crops c JOIN collective_regions r ON r.id=c.region_id WHERE c.world_id=? AND c.status='growing'",
        (world_id,),
    ).fetchall():
        # 土壤返溶已包含在根系/活力的联合积分中，不能再推进一次。
        skip.add(crop["soil_id"])
        ProductionService._grow(c, crop, at + timedelta(hours=hours))
    for row in c.execute(
        "SELECT inputs_json,spec_json FROM collective_work_orders w JOIN collective_proposals p ON p.id=w.proposal_id WHERE p.world_id=? AND w.status='working'",
        (world_id,),
    ):
        if json.loads(row["spec_json"])["operation"] != "assay":
            skip.update(json.loads(row["inputs_json"]))
    for row in c.execute(
        "SELECT j.sample_id,j.spec_json FROM collective_jobs j JOIN collective_programs p ON p.id=j.program_id WHERE p.world_id=? AND j.status IN ('working','incubating')",
        (world_id,),
    ):
        spec = json.loads(row["spec_json"])
        if spec.get("operation") and not spec.get("processing_applied_hours"):
            if row["sample_id"]:
                skip.add(row["sample_id"])
            skip.update(spec.get("auxiliary_samples", []))
    skip.update(
        row[0]
        for row in c.execute(
            "SELECT crop_item_id FROM collective_crops WHERE world_id=? AND status IN ('growing','mature','harvesting')",
            (world_id,),
        )
    )
    inert = {}
    for row in c.execute(
        "SELECT item_id FROM material_batches WHERE world_id=?", (world_id,)
    ).fetchall():
        if row[0] not in skip:
            old = M.state(c, row[0])
            new = advance(old, hours)
            if replace(new, age_hours=old.age_hours) == old:
                inert[row[0]] = new
            else:
                M.rebalance(c, {row[0]: new}, operation="natural_time")
    if inert:
        M.rebalance(c, inert, operation="natural_age")


def settle(c, world_id, at):
    from world_engine.collective_messages import MessageService
    from world_engine.collective_production import ProductionService
    from world_engine.collective_regions import RegionService
    from world_engine.collective_research import CollectiveResearchService as R
    from world_engine.collective_work import WorkService

    R.tick(c, world_id, at)
    WorkService.tick(c, world_id, at)
    ProductionService.tick(c, world_id, at)
    MessageService.deliver(c, world_id, at)
    RegionService.tick(c, world_id, at)
    for program in c.execute(
        "SELECT id,character_id FROM collective_programs WHERE world_id=? AND status='active'",
        (world_id,),
    ).fetchall():
        try:
            R._drive(c, program["id"], at)
        except ValueError as exc:
            c.execute(
                "UPDATE collective_programs SET status='blocked',error=? WHERE id=?",
                (str(exc), program["id"]),
            )
            R.record_observation(
                c,
                program["character_id"],
                at,
                "原阶段程序受到实际条件限制",
                {"kind": "blocked", "reason": str(exc)},
            )


def basic_life(c, world_id, at):
    from world_engine.collective_research import CollectiveResearchService as R
    from world_engine.economy import EconomyService
    from world_engine.life import LifeActivityService

    for actor in c.execute(
        "SELECT a.* FROM characters a JOIN collective_people p ON p.character_id=a.id WHERE a.world_id=? AND p.enabled=1 AND a.health>0 ORDER BY a.rowid",
        (world_id,),
    ).fetchall():
        if LifeActivityService.running(c, actor["id"]):
            continue
        moving = c.execute(
            "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",
            (actor["id"],),
        ).fetchone()
        if actor["satiety"] <= 60:
            meal = EconomyService.food(c, actor, at=at)
            if meal:
                continue
            # 哥哥，只记录真实缺粮障碍，规则不会替人物研究或选择救灾方法。
            existing = c.execute(
                "SELECT 1 FROM collective_observations WHERE character_id=? AND kind='food_shortage'",
                (actor["id"],),
            ).fetchone()
            if not existing:
                R.record_observation(
                    c,
                    actor["id"],
                    at,
                    "本人没有可按已有饮食安排食用的随身口粮",
                    {"kind": "food_shortage"},
                )
        if actor["energy"] <= 50:
            if not moving:
                LifeActivityService.start(c, actor, at, "rest", 360)
