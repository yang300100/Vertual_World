"""哥哥，恢复查实际账；必要性使用有利于单人的下界，不能把签字算研究。"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import timedelta

from world_engine.collective_regions import RegionService
from world_engine.geo import great_circle_distance_km
from world_engine.material_physics import MaterialState
from world_engine.materials import MaterialService as M
from world_engine.materials import encoded
from world_engine.repository import from_iso, to_iso


def record_processing(c, inputs, outputs, source_kind, source_id, operation, settings, hours):
    row = c.execute(
        "SELECT id,item_ids_json FROM material_ledger WHERE operation='transform' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    if row is None or json.loads(row["item_ids_json"]) != [*inputs, *outputs]:
        raise ValueError("实际处理与材料账不一致，不能登记因果来源")
    c.execute(
        "INSERT INTO collective_process_traces VALUES (?,?,?,?,?,?)",
        (row["id"], source_kind, source_id, operation, encoded(settings), hours),
    )


def record_exchange_trace(c, operation, source_id, settings, hours):
    row = c.execute(
        "SELECT id,operation FROM material_ledger ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    if not row or row["operation"] != operation:
        raise ValueError("交换来源与实际材料账不一致")
    c.execute(
        "INSERT INTO collective_process_traces VALUES (?,?,?,?,?,?)",
        (row["id"], "exchange", source_id, operation, encoded(settings), hours),
    )


def replay_waste_leak(inputs, settings, changed):
    from dataclasses import replace

    load = 0
    for iid, original in settings["loads"].items():
        state = changed.get(iid)
        load += state.mass + state.water + state.particles if state else sum(original.values())
    ratio = min(
        1,
        -math.expm1(-settings["integrated_rate"])
        + max(0, 1 - settings["capacity"] / max(1e-9, load)),
    )
    dose = inputs[0].mobile * ratio
    return [
        replace(inputs[0], mobile=inputs[0].mobile - dose),
        replace(inputs[1], mobile=inputs[1].mobile + dose),
    ]


def replay_component_chain(original, steps, *, withdrawn_index):
    from world_engine.material_physics import advance, process

    if not 0 <= withdrawn_index < len(steps):
        raise ValueError("撤回组件须存在于实际处理链")
    actual = withdrawn = original
    retained = []
    for i, step in enumerate(steps):
        auxiliaries = tuple(
            MaterialState(**x) if isinstance(x, dict) else x for x in step["auxiliaries"]
        )
        actual = process(actual, step["operation"], step["settings"], step["hours"], auxiliaries)[0]
        if i == withdrawn_index:
            withdrawn = advance(withdrawn, step["hours"])
            retained.extend(auxiliaries)
        else:
            outputs = process(
                withdrawn, step["operation"], step["settings"], step["hours"], auxiliaries
            )
            withdrawn = outputs[0]
            retained.extend(outputs[1:])
    return {
        "actual": actual,
        "withdrawn": withdrawn,
        "retained_side_materials": retained,
        "subsequent_steps_recomputed": len(steps) - withdrawn_index - 1,
    }


def _states(row, column):
    return [MaterialState(**x) for x in json.loads(row[column])]


def _safe(state, region, kind):
    if kind == "seed":
        return (
            state.carrier == "seed"
            and state.mass > 0
            and state.viability >= region["seed_baseline"] * 0.8
        )
    if state.mass <= 0:
        return False
    return (
        state.mobile / state.mass <= region["salt_limit"]
        and (kind == "land" or state.particles / state.mass <= region["particle_limit"])
        and (kind != "food" or state.nutrition > 0)
    )


def materially_changed(actual, withdrawn, kind):
    if kind == "seed":
        return actual.viability - withdrawn.viability >= 0.01
    for field in ("mobile", "particles"):
        a = getattr(actual, field) / max(1e-9, actual.mass)
        b = getattr(withdrawn, field) / max(1e-9, withdrawn.mass)
        if b - a > max(1e-8, 0.01 * max(a, b)):
            return True
    return False


def component_counterfactual(primary, auxiliaries, operation, settings, hours, source=None):
    from world_engine.material_physics import advance, process

    if source and source["previous_task_json"]:
        previous = json.loads(source["previous_task_json"])
        current = json.loads(source["task_json"])
        if any(
            previous.get(k) != current.get(k)
            for k in ("batch_id", "quantity", "auxiliary_ids", "auxiliary_quantities")
        ):
            raise ValueError("原料变更需要重建前一版本的实际供料，不能假用当前原料")
        # 只退回本次物理修改，保留原来已有的处理步骤。
        return process(
            primary,
            previous["operation"],
            previous["settings"],
            previous["hours"],
            tuple(auxiliaries),
        )
    return (advance(primary, hours),)


def task_source_for_trace(c, trace):
    if trace["source_kind"] != "work":
        return None
    row = c.execute(
        "SELECT s.* FROM collective_work_orders w JOIN collective_task_sources s ON s.proposal_id=w.proposal_id AND s.version=w.version AND s.task_key=w.task_key WHERE w.id=?",
        (trace["source_id"],),
    ).fetchone()
    return dict(row) if row else None


def component_withdrawals(c, world_id):
    """哥哥，沿真实物品的账目重算；遇到尚无重算器的交换就保留未验证，绝不复用后状态。"""
    from dataclasses import asdict

    from world_engine.material_physics import advance, combine, fraction, process

    rows = c.execute(
        "SELECT rowid,* FROM material_ledger WHERE world_id=? ORDER BY rowid", (world_id,)
    ).fetchall()
    traces = {
        r["ledger_id"]: dict(r)
        for r in c.execute(
            "SELECT t.* FROM collective_process_traces t JOIN material_ledger l ON l.id=t.ledger_id WHERE l.world_id=?",
            (world_id,),
        )
    }
    terminals = {}
    for row in c.execute(
        "SELECT content_json FROM collective_observations WHERE world_id=? AND kind='ration_delivery'",
        (world_id,),
    ):
        data = json.loads(row[0])
        terminals[data["batch_id"]] = (data["region_id"], "food")
    for row in c.execute(
        "SELECT spec_json FROM collective_domain_jobs WHERE world_id=? AND status='completed'",
        (world_id,),
    ):
        data = json.loads(row["spec_json"])
        if data.get("op") in {"land", "plant"}:
            for iid in [data.get("batch_id"), *data.get("inputs", [])]:
                if iid:
                    terminals[iid] = (data["region_id"], "land" if data["op"] == "land" else "seed")
    regions = {
        r["id"]: dict(r)
        for r in c.execute("SELECT * FROM collective_regions WHERE world_id=?", (world_id,))
    }
    results = []
    for index, ledger in enumerate(rows):
        trace = traces.get(ledger["id"])
        if not trace or trace["source_kind"] in {"growth", "exchange"}:
            continue
        before, after = _states(ledger, "before_json"), _states(ledger, "after_json")
        ids = json.loads(ledger["item_ids_json"])
        output_ids = ids[len(before) :]
        # 不执行被撤回的处理；原料等待同样时长，辅料保留于独立隔离账户。
        task_source = task_source_for_trace(c, trace)
        try:
            alternatives = component_counterfactual(
                before[0],
                before[1:],
                trace["operation"],
                json.loads(trace["settings_json"]),
                trace["hours"],
                task_source,
            )
        except ValueError as exc:
            results.append(
                {
                    **trace,
                    "proved_material_effect": False,
                    "reason": str(exc),
                    "unsupported_ledger_ids": [ledger["id"]],
                    "endpoints": [],
                }
            )
            continue
        if len(alternatives) > len(after):
            results.append(
                {
                    **trace,
                    "proved_material_effect": False,
                    "reason": "旧步骤产生不同物品流，须显式重建",
                    "unsupported_ledger_ids": [ledger["id"]],
                    "endpoints": [],
                }
            )
            continue
        changed = {
            iid: alternatives[i] if i < len(alternatives) else fraction(state, 0)
            for i, (iid, state) in enumerate(zip(output_ids, after, strict=True))
        }
        side_materials = before[1:]
        endpoints = []
        unsupported = []
        recomputed = 0

        def inspect(iid, actual, changed=changed, endpoints=endpoints):
            if iid in terminals and iid in changed:
                rid, kind = terminals[iid]
                region = regions.get(rid)
                if region:
                    endpoints.append(
                        {
                            "item_id": iid,
                            "region_id": rid,
                            "kind": kind,
                            "actual_safe": _safe(actual, region, kind),
                            "withdrawn_safe": _safe(changed[iid], region, kind),
                            "actual": asdict(actual),
                            "withdrawn": asdict(changed[iid]),
                        }
                    )

        for iid, state in zip(output_ids, after, strict=True):
            inspect(iid, state)
        for later in rows[index + 1 :]:
            original_before, original_after = (
                _states(later, "before_json"),
                _states(later, "after_json"),
            )
            later_ids = json.loads(later["item_ids_json"])
            op = later["operation"]
            input_ids = later_ids[: len(original_before)]
            if not set(input_ids) & changed.keys():
                continue
            inputs = []
            for iid, original in zip(input_ids, original_before, strict=True):
                state = changed.get(iid, original)
                delta = original.age_hours - state.age_hours
                inputs.append(advance(state, delta) if delta > 1e-8 else state)
                if iid in changed:
                    changed[iid] = inputs[-1]
                inspect(iid, original)
            mapped = {}
            if op == "split":
                denominator = original_before[0].mass + original_before[0].water
                if denominator <= 0:
                    unsupported.append(later["id"])
                    break
                for iid, state in zip(later_ids, original_after, strict=True):
                    mapped[iid] = fraction(inputs[0], (state.mass + state.water) / denominator)
            elif op == "merge":
                source_amount = original_before[0].mass + original_before[0].water
                merged_amount = original_after[0].mass + original_after[0].water
                target_amount = original_before[1].mass + original_before[1].water
                if source_amount <= 0:
                    unsupported.append(later["id"])
                    break
                ratio = min(1, max(0, (merged_amount - target_amount) / source_amount))
                mapped[later_ids[1]] = combine((inputs[1], fraction(inputs[0], ratio)))
                if len(original_after) == 2:
                    mapped[later_ids[0]] = fraction(inputs[0], 1 - ratio)
            elif op == "transform" and later["id"] in traces:
                t = traces[later["id"]]
                outputs = process(
                    inputs[0],
                    t["operation"],
                    json.loads(t["settings_json"]),
                    t["hours"],
                    tuple(inputs[1:]),
                )
                if len(outputs) != len(original_after):
                    unsupported.append(later["id"])
                    break
                mapped = dict(zip(later_ids[len(inputs) :], outputs, strict=True))
                recomputed += 1
            elif op == "crop_growth" and later["id"] in traces:
                from world_engine.collective_production import ProductionService

                t = traces[later["id"]]
                parameters = json.loads(t["settings_json"])
                outputs, _ = ProductionService.growth_states(
                    *inputs, parameters["target"], t["hours"], accessible=parameters["accessible"]
                )
                mapped = dict(zip(later_ids, outputs, strict=True))
                recomputed += 1
            elif (
                op == "transform"
                and len(inputs) == 1
                and len(original_after) == 2
                and {s.carrier for s in original_after} == {"grain", "seed"}
            ):
                from dataclasses import replace

                denominator = original_before[0].mass
                if denominator <= 0:
                    unsupported.append(later["id"])
                    break
                mapped = {
                    iid: replace(fraction(inputs[0], s.mass / denominator), carrier=s.carrier)
                    for iid, s in zip(later_ids[len(inputs) :], original_after, strict=True)
                }
                recomputed += 1
            elif op in {"loose_waste_runoff", "waste_leak"}:
                from dataclasses import replace

                if later["id"] not in traces:
                    unsupported.append(later["id"])
                    break
                settings = json.loads(traces[later["id"]]["settings_json"])
                if op == "waste_leak":
                    mapped = dict(
                        zip(later_ids, replay_waste_leak(inputs, settings, changed), strict=True)
                    )
                    recomputed += 1
                    for iid in input_ids:
                        changed.pop(iid, None)
                    changed.update(mapped)
                    continue
                mobile_ratio = dust_ratio = settings["ratio"]
                if mobile_ratio < 0 or dust_ratio < 0 or mobile_ratio > 1 or dust_ratio > 1:
                    unsupported.append(later["id"])
                    break
                dose = inputs[0].mobile * mobile_ratio
                dust = inputs[0].particles * dust_ratio
                outputs = [
                    replace(
                        inputs[0],
                        mobile=inputs[0].mobile - dose,
                        particles=inputs[0].particles - dust,
                    ),
                    replace(inputs[1], mobile=inputs[1].mobile + dose),
                ]
                if len(inputs) == 3:
                    outputs.append(replace(inputs[2], particles=inputs[2].particles + dust))
                elif dust:
                    unsupported.append(later["id"])
                    break
                mapped = dict(zip(later_ids, outputs, strict=True))
                recomputed += 1
            elif op == "natural_time":
                mapped = {
                    iid: advance(state, max(0, final.age_hours - original.age_hours))
                    for iid, state, original, final in zip(
                        later_ids, inputs, original_before, original_after, strict=True
                    )
                }
            elif op in {"move", "transfer"}:
                mapped = dict(zip(later_ids[-len(original_after) :], inputs, strict=True))
            elif op == "consume":
                # 摄入/封存是终点，不把消费后的实际污染汇倒填回反事实。
                for iid in input_ids:
                    changed.pop(iid, None)
                if len(original_after) > 1:
                    denominator = original_before[0].mass + original_before[0].water
                    if denominator > 0:
                        remainder = original_after[1]
                        changed[input_ids[0]] = fraction(
                            inputs[0], (remainder.mass + remainder.water) / denominator
                        )
                continue
            else:
                unsupported.append(later["id"])
                break
            for iid in input_ids:
                changed.pop(iid, None)
            changed.update(mapped)
            for iid, state in zip(later_ids[-len(original_after) :], original_after, strict=True):
                inspect(iid, state)
        results.append(
            {
                **trace,
                "task_source": task_source,
                "counterfactual_kind": "revert_revision"
                if task_source and task_source["previous_task_json"]
                else "remove_new_operation",
                "subsequent_steps_recomputed": recomputed,
                "endpoints": endpoints,
                "unsupported_ledger_ids": unsupported,
                "unused_auxiliaries_retained": [asdict(x) for x in side_materials],
                "proved_material_effect": not unsupported
                and any(
                    x["actual_safe"]
                    and not x["withdrawn_safe"]
                    and materially_changed(
                        MaterialState(**x["actual"]), MaterialState(**x["withdrawn"]), x["kind"]
                    )
                    for x in endpoints
                ),
                "scope": "相同处理前状态、原物品流和后续实际处理；未重放人物再次决策",
            }
        )
    return results


def future_maintenance_check(db_path, world_id, *, hours=336):
    import sqlite3
    import tempfile
    from contextlib import closing
    from dataclasses import replace
    from pathlib import Path

    from world_engine.collective_clock import next_boundary
    from world_engine.config import Settings
    from world_engine.database import Database
    from world_engine.engine import WorldEngine

    with tempfile.TemporaryDirectory(prefix="collective-maintenance-") as directory:
        target = Path(directory) / "world.db"
        with closing(
            sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
        ) as source:
            with closing(sqlite3.connect(target)) as copied:
                source.backup(copied)
        db = Database(target, legacy_world_defaults=False)
        settings = replace(
            Settings(database_path=target),
            collective_api_enabled=False,
            research_api_enabled=False,
            decision_provider="rules",
            world_agent_enabled=False,
            debug_model_io=False,
            history_logging_enabled=False,
            knowledge_enabled=False,
            active_character_limit=100,
            adjudication_interval_minutes=180000,
        )
        engine = WorldEngine(db, settings)
        try:
            with db.read() as c:
                start = from_iso(
                    c.execute(
                        "SELECT w.current_time FROM worlds w WHERE id=?", (world_id,)
                    ).fetchone()[0]
                )
                before_calls = c.execute(
                    "SELECT COUNT(*) FROM collective_model_calls WHERE world_id=?", (world_id,)
                ).fetchone()[0]
                previous_health = {
                    r["id"]: r["health"]
                    for r in c.execute(
                        "SELECT id,health FROM characters WHERE world_id=?", (world_id,)
                    )
                }
                windows = [
                    r[0]
                    for r in c.execute(
                        "SELECT observation_window_hours FROM collective_waste_sites WHERE world_id=?",
                        (world_id,),
                    )
                ]
            hours = max(hours, *windows) if windows else hours
            at, endpoint = start, start + timedelta(hours=hours)
            while at < endpoint:
                with db.read() as c:
                    boundary = min(next_boundary(c, world_id, at), endpoint)
                    scale = c.execute(
                        "SELECT time_scale FROM world_clock WHERE world_id=?", (world_id,)
                    ).fetchone()[0]
                if scale <= 0:
                    raise ValueError("维护观察副本的物理时钟没有运行")
                at = engine.heartbeat(
                    world_id, elapsed_seconds=(boundary - at).total_seconds() / scale
                ).current_time
            with db.read() as c:
                views = RegionService.view(c, world_id, at)
                deaths = c.execute(
                    "SELECT COUNT(*) FROM collective_deaths WHERE world_id=?", (world_id,)
                ).fetchone()[0]
                after_calls = c.execute(
                    "SELECT COUNT(*) FROM collective_model_calls WHERE world_id=?", (world_id,)
                ).fetchone()[0]
                unsafe = c.execute(
                    "SELECT COUNT(*) FROM collective_food_days d JOIN collective_regions r ON r.id=d.region_id WHERE r.world_id=? AND d.consumed_at>? AND (d.shortage>0 OR d.safe_nutrition<d.required_nutrition)",
                    (world_id, to_iso(start)),
                ).fetchone()[0]
                dangerous = c.execute(
                    "SELECT COUNT(*) FROM collective_exposures WHERE world_id=? AND settled_at>? AND damage>0",
                    (world_id, to_iso(start)),
                ).fetchone()[0]
                health_losses = [
                    r["id"]
                    for r in c.execute(
                        "SELECT id,health FROM characters WHERE world_id=?", (world_id,)
                    )
                    if r["health"] < previous_health[r["id"]]
                ]
                result = {
                    "passed": deaths == 0
                    and unsafe == 0
                    and dangerous == 0
                    and not health_losses
                    and all(v["land_ready"] and v["waste_ready"] for v in views)
                    and M.audit(c, world_id)["balanced"]
                    and before_calls == after_calls,
                    "hours": hours,
                    "api_calls": after_calls - before_calls,
                    "recorded_deaths": deaths,
                    "unsafe_food_days": unsafe,
                    "dangerous_exposures": dangerous,
                    "people_with_new_body_damage": health_losses,
                    "new_plans": 0,
                    "retains_existing_scheduled_labor": True,
                    "source_database_read_only": True,
                }
        finally:
            engine.close()
    return result


def audit_run(db_path, baseline_path, manifest):
    import sqlite3
    from contextlib import closing
    from pathlib import Path

    db_path, baseline_path = Path(db_path), Path(baseline_path)
    initial_hash = database_fingerprint(db_path)
    baseline_hash = hashlib.sha256(baseline_path.read_bytes()).hexdigest()
    with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as c:
        c.row_factory = sqlite3.Row
        world_id = manifest.get("world_id") or c.execute("SELECT id FROM worlds").fetchone()[0]
        at = from_iso(
            c.execute("SELECT w.current_time FROM worlds w WHERE id=?", (world_id,)).fetchone()[0]
        )
        recovery = recovery_check(c, world_id, at)
        candidates = contribution_candidates(c, world_id)
        withdrawals = component_withdrawals(c, world_id)
        bound = single_person_bound(c, world_id)
        qualified = []
        novel_components = set()
        for candidate in candidates["candidates"]:
            if candidate["exact_duplicate_hypotheses"]:
                continue
            effects = []
            for withdrawal in withdrawals:
                if not withdrawal["proved_material_effect"]:
                    continue
                if withdrawal["source_kind"] == "work":
                    source = c.execute(
                        "SELECT p.* FROM collective_work_orders w JOIN collective_task_sources s ON s.proposal_id=w.proposal_id AND s.version=w.version AND s.task_key=w.task_key JOIN collective_programs p ON p.id=s.program_id WHERE w.id=?",
                        (withdrawal["source_id"],),
                    ).fetchone()
                else:
                    source = c.execute(
                        "SELECT p.* FROM collective_jobs j JOIN collective_programs p ON p.id=j.program_id WHERE j.id=?",
                        (withdrawal["source_id"],),
                    ).fetchone()
                if not source or source["character_id"] != candidate["actor_id"]:
                    continue
                call = c.execute(
                    "SELECT * FROM collective_model_calls WHERE id=? AND status='succeeded' AND actor_id=?",
                    (source["call_id"], source["character_id"]),
                ).fetchone()
                if not call or json.loads(call["response_json"]) != json.loads(source["spec_json"]):
                    continue
                specification = json.loads(source["spec_json"])
                cited = {
                    eid
                    for part in [
                        *specification.get("hypotheses", []),
                        *specification.get("commands", []),
                    ]
                    for eid in part.get("evidence_ids", [])
                }
                if withdrawal.get("task_source"):
                    cited = set(json.loads(withdrawal["task_source"]["evidence_ids_json"]))
                if not supports_component(c, candidate, withdrawal, source, cited):
                    continue
                signature = encoded(
                    [
                        withdrawal["operation"],
                        {
                            k: float(f"{v:.6g}")
                            for k, v in json.loads(withdrawal["settings_json"]).items()
                        },
                    ]
                )
                if signature in novel_components:
                    continue
                effects.append(
                    {
                        **withdrawal,
                        "component_program_id": source["id"],
                        "links_actual_research_evidence": True,
                        "component_signature": signature,
                    }
                )
            if effects:
                novel_components.update(e["component_signature"] for e in effects)
                qualified.append({**candidate, "causal_effects": effects})
        ids = {x["actor_id"] for x in qualified}
        contribution = {
            **candidates,
            "passed": len(ids) >= 3,
            "qualified_count": len(ids),
            "qualified": qualified,
        }
    maintenance = (
        future_maintenance_check(db_path, world_id)
        if recovery["passed"]
        else {"passed": False, "reason": "真实恢复尚未通过，未额外执行长期副本", "api_calls": 0}
    )
    guard = (
        manifest.get("database_guard") == "逐事务完整存档核对"
        and not manifest.get("offline")
        and not manifest.get("init_only")
    )
    baseline_valid = manifest.get("baseline_sha256", baseline_hash) == baseline_hash
    source_valid = False
    runtime_valid = False
    if manifest.get("manifest"):
        from scripts.demo_collective_disaster import runtime_manifest, source_manifest

        source_valid = source_manifest() == manifest["manifest"]
        runtime_valid = manifest.get("runtime") == runtime_manifest()
    unchanged = initial_hash == database_fingerprint(db_path)
    result = {
        "goal_achieved": all(
            (
                recovery["passed"],
                contribution["passed"],
                bound["proved_impossible"],
                maintenance["passed"],
                guard,
                baseline_valid,
                source_valid,
                runtime_valid,
                unchanged,
            )
        ),
        "recovery": recovery,
        "contribution": contribution,
        "component_withdrawals": withdrawals,
        "single_person": bound,
        "future_maintenance": maintenance,
        "autonomy": {
            "formal_frozen_guard": guard,
            "baseline_hash_matches": baseline_valid,
            "source_manifest_matches": source_valid,
            "runtime_versions_match": runtime_valid,
        },
        "api_calls_during_audit": 0,
        "formal_database_unchanged": unchanged,
    }
    return result


def database_fingerprint(path):
    import sqlite3
    from contextlib import closing
    from pathlib import Path

    from scripts.demo_autonomous_disaster import FrozenDatabase

    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as c:
        c.execute("BEGIN")
        return FrozenDatabase.fingerprint(c)


def _recorded_output(c, world_id, item_id):
    # 哥哥，已经吃掉的收成仍要从不可变转化账核对当时的真实负荷。
    for row in c.execute(
        "SELECT item_ids_json,after_json FROM material_ledger WHERE world_id=? AND operation='transform' ORDER BY rowid DESC",
        (world_id,),
    ):
        states = json.loads(row["after_json"])
        ids = json.loads(row["item_ids_json"])[-len(states) :]
        if item_id in ids:
            return MaterialState(**states[ids.index(item_id)])
    return None


def next_cycle_yield_lower_bound(seed, soil, source):
    from world_engine.material_physics import salt_total

    if source.mass <= 0 or soil.mass <= 0:
        return 0
    # 最坏情况下所有既有土盐均可迁移；忽略吸收造成的压力下降，得到有利性可证明的下界。
    stress = salt_total(soil) / soil.mass * 200
    mean_viability = seed.viability * (-math.expm1(-stress) / stress if stress > 1e-12 else 1)
    growth = min(seed.mass * 50, soil.mass * 0.8, source.mass) * mean_viability / (1 + stress)
    return (seed.nutrition + growth * source.nutrition / source.mass) * 0.95


def recovery_check(c, world_id, at, *, expected_regions=21):
    regions = []
    for view in RegionService.view(c, world_id, at):
        days = c.execute(
            "SELECT * FROM collective_food_days WHERE region_id=? AND consumed_at>? AND consumed_at<=? ORDER BY consumed_at",
            (view["id"], to_iso(at - timedelta(days=14)), to_iso(at)),
        ).fetchall()
        continuous = len(days) == 14 and all(
            abs(r["required_nutrition"] - view["daily_nutrition"]) < 1e-8
            and r["safe_nutrition"] + 1e-8 >= view["daily_nutrition"]
            and r["shortage"] < 1e-8
            for r in days
        )
        continuous = continuous and all(
            from_iso(b["consumed_at"]) - from_iso(a["consumed_at"]) == timedelta(days=1)
            for a, b in zip(days, days[1:], strict=False)
        )
        crops = c.execute(
            "SELECT * FROM collective_crops WHERE region_id=? AND status='harvested' ORDER BY harvested_at DESC",
            (view["id"],),
        ).fetchall()
        crop = crops[0] if crops else None
        food = _recorded_output(c, world_id, crop["food_id"]) if crop else None
        seed = M.state(c, crop["seed_id"]) if crop else None
        cycle = bool(
            crop
            and from_iso(crop["due_at"]) - from_iso(crop["planted_at"]) == timedelta(days=90)
            and from_iso(crop["harvested_at"]) >= from_iso(crop["due_at"])
            and food
            and food.carrier == "grain"
            and food.nutrition + 1e-8 >= view["daily_nutrition"] * 90
            and food.mobile / max(1e-9, food.mass) <= view["salt_limit"]
            and food.particles / max(1e-9, food.mass) <= view["particle_limit"]
        )
        seed_ready = bool(
            seed
            and seed.carrier == "seed"
            and seed.mass > 0
            and seed.viability + 1e-8 >= view["seed_baseline"] * 0.8
        )
        source = M.state(c, view["growth_source_id"])
        soil = M.state(c, view["soil_id"])
        next_yield = 0
        if seed_ready and source and source.mass > 0 and soil:
            next_yield = next_cycle_yield_lower_bound(seed, soil, source)
        seed_ready = seed_ready and next_yield + 1e-8 >= view["daily_nutrition"] * 90
        row = {
            "region_id": view["id"],
            "continuous_food": continuous,
            "safe_reserve": view["safe_reserve_days"] + 1e-8 >= 14,
            "land": view["land_ready"],
            "actual_ninety_day_cycle": cycle,
            "actual_seed_for_next_cycle": seed_ready,
            "waste_observed": view["waste_ready"],
            "no_deaths": view["deaths"] == 0,
        }
        row["passed"] = all(v for k, v in row.items() if k != "region_id")
        regions.append(row)
    deaths = c.execute(
        "SELECT COUNT(*) FROM collective_deaths WHERE world_id=?", (world_id,)
    ).fetchone()[0]
    recent = c.execute(
        "SELECT COUNT(*) FROM collective_exposures WHERE world_id=? AND ingested_at>? AND (damage>0 OR (settled_at IS NULL AND dose>.005))",
        (world_id, to_iso(at - timedelta(days=14))),
    ).fetchone()[0]
    balanced = M.audit(c, world_id)["balanced"]
    return {
        "passed": len(regions) == expected_regions
        and all(r["passed"] for r in regions)
        and deaths == 0
        and recent == 0
        and balanced,
        "regions": regions,
        "expected_regions": expected_regions,
        "recorded_deaths": deaths,
        "recent_dangerous_exposures": recent,
        "material_balanced": balanced,
        "future_maintenance_audit_required": True,
    }


def contribution_candidates(c, world_id):
    candidates = []
    seen = set()
    for program in c.execute(
        "SELECT p.*,m.actor_id,m.status call_status,m.response_json FROM collective_programs p JOIN collective_model_calls m ON m.id=p.call_id WHERE p.world_id=? AND m.status='succeeded' ORDER BY p.rowid",
        (world_id,),
    ):
        spec = json.loads(program["spec_json"])
        if program["actor_id"] != program["character_id"] or not spec.get("hypotheses"):
            continue
        if json.loads(program["response_json"]) != spec:
            continue
        measurements = []
        for job in c.execute(
            "SELECT * FROM collective_jobs WHERE program_id=? AND status='completed' AND observation_id IS NOT NULL",
            (program["id"],),
        ):
            command = json.loads(job["spec_json"])
            result = json.loads(job["result_json"])
            if (
                result.get("kind") != "measurement"
                or not result.get("prediction")
                or from_iso(result["prediction_recorded_at"]) >= from_iso(result["measured_at"])
            ):
                continue
            measurements.append(
                {
                    "job_id": job["id"],
                    "observation_id": job["observation_id"],
                    "method": command["method"],
                    "hypothesis": command["hypothesis"],
                    "result": result,
                    "design_actor_id": program["character_id"],
                    "execution_actor_id": program["character_id"],
                    "tested_components": [
                        {"operation": command["operation"], "settings": command.get("settings", {})}
                    ]
                    if command.get("operation")
                    else batch_processing_history(c, world_id, result.get("batch_id")),
                }
            )
        for order in c.execute(
            "SELECT w.*,r.observation_id FROM collective_work_orders w JOIN collective_task_sources s ON s.proposal_id=w.proposal_id AND s.version=w.version AND s.task_key=w.task_key JOIN collective_assay_results r ON r.order_id=w.id WHERE s.program_id=? AND w.status='completed'",
            (program["id"],),
        ):
            command = json.loads(order["spec_json"])
            result = json.loads(order["result_json"]).get("measurement")
            if (
                not result
                or not result.get("prediction")
                or from_iso(result["prediction_recorded_at"]) >= from_iso(result["measured_at"])
            ):
                continue
            hypotheses = [h for h in spec["hypotheses"] if result["prediction"] in h["predictions"]]
            if not hypotheses:
                continue
            measurements.append(
                {
                    "job_id": order["id"],
                    "observation_id": order["observation_id"],
                    "method": command["method"],
                    "hypothesis": hypotheses[0],
                    "result": result,
                    "design_actor_id": program["character_id"],
                    "execution_actor_id": order["worker_id"],
                    "tested_components": batch_processing_history(c, world_id, result["batch_id"]),
                }
            )
        if not measurements:
            continue
        signature = hashlib.sha256(encoded(spec["hypotheses"]).encode("utf-8")).hexdigest()
        copied = signature in seen
        seen.add(signature)
        candidates.append(
            {
                "actor_id": program["character_id"],
                "call_id": program["call_id"],
                "program_id": program["id"],
                "independent_prediction_and_actual_measurement": True,
                "exact_duplicate_hypotheses": copied,
                "measurements": measurements,
                "causal_use_review_required": True,
            }
        )
    # 哥哥，独立做过实验只是候选，必须继续证实其修改真的进入方案并改变后果。
    return {
        "passed": False,
        "qualified_count": 0,
        "candidates": candidates,
        "reason": "候选研究仍需实际组件来源、修改和撤回因果证据；签字与转寄不计贡献",
    }


def batch_processing_history(c, world_id, batch_id):
    if not batch_id:
        return []
    ids = {batch_id}
    components = []
    # 哥哥，从实测样本沿不可变账向前追溯，委托检验也能指向真正被检验的处理。
    for row in c.execute(
        "SELECT l.*,t.operation treatment,t.settings_json FROM material_ledger l LEFT JOIN collective_process_traces t ON t.ledger_id=l.id WHERE l.world_id=? ORDER BY l.rowid DESC",
        (world_id,),
    ):
        before = json.loads(row["before_json"])
        after = json.loads(row["after_json"])
        item_ids = json.loads(row["item_ids_json"])
        outputs = (
            item_ids[len(before) :]
            if row["operation"] == "transform"
            else item_ids[-len(after) :]
            if after
            else []
        )
        if not ids.intersection(outputs):
            continue
        if row["treatment"]:
            components.append(
                {"operation": row["treatment"], "settings": json.loads(row["settings_json"])}
            )
        ids.update(item_ids[: len(before)])
    return components


def evidence_aliases(c, actor_id, observation_id):
    ids = {observation_id}
    rows = c.execute(
        "SELECT id,source_observation_id FROM collective_observations WHERE character_id=?",
        (actor_id,),
    ).fetchall()
    while True:
        expanded = ids | {r["id"] for r in rows if r["source_observation_id"] in ids}
        if expanded == ids:
            return ids
        ids = expanded


def supports_component(c, candidate, withdrawal, source, cited):
    from world_engine.collective_research import CollectiveResearchService as R

    for measurement in candidate["measurements"]:
        if not cited.intersection(
            evidence_aliases(c, candidate["actor_id"], measurement["observation_id"])
        ):
            continue
        if not any(
            t["operation"] == withdrawal["operation"] for t in measurement["tested_components"]
        ):
            continue
        # 同一程序内随便做过一个实验不能代替相关读数：实际改变还须影响被测物理量。
        for endpoint in withdrawal["endpoints"]:
            actual = R._measurement(MaterialState(**endpoint["actual"]), measurement["method"])
            other = R._measurement(MaterialState(**endpoint["withdrawn"]), measurement["method"])
            if abs(other - actual) > max(
                measurement["result"].get("precision", 1e-8),
                0.01 * max(abs(actual), abs(other)),
                1e-8,
            ):
                return True
    return False


def single_person_bound(c, world_id, *, horizon_hours=5760):
    locations = [
        dict(r)
        for r in c.execute(
            "SELECT DISTINCT l.id,l.name,l.longitude,l.latitude FROM collective_regions r JOIN locations l ON l.id=r.location_id WHERE r.world_id=?",
            (world_id,),
        )
    ]
    actors = [
        dict(r)
        for r in c.execute(
            "SELECT a.id,a.name,a.movement_speed_kmh FROM characters a JOIN collective_people p ON p.character_id=a.id WHERE a.world_id=?",
            (world_id,),
        )
    ]
    speeds = [r["movement_speed_kmh"] for r in actors]
    # 放开技能、学习及所有已有车辆的许可，仍不能使同一身体处于多个地方。
    speeds.extend(
        r[0] for r in c.execute("SELECT speed_kmh FROM vehicles WHERE world_id=?", (world_id,))
    )
    speed = max(speeds, default=5)
    if not math.isfinite(speed) or speed <= 0:
        raise ValueError("单人审计需要实际有限的最快移动能力")
    seen = {0} if locations else set()
    edges = []
    total = 0
    while len(seen) < len(locations):
        choices = []
        for a in seen:
            for b in range(len(locations)):
                if b in seen:
                    continue
                first, second = locations[a], locations[b]
                d = great_circle_distance_km(
                    first["longitude"], first["latitude"], second["longitude"], second["latitude"]
                )
                choices.append((d, a, b))
        distance, a, b = min(choices)
        seen.add(b)
        total += distance
        edges.append(
            {"from": locations[a]["id"], "to": locations[b]["id"], "distance_km": distance}
        )
    hours = total / speed
    return {
        "proved_impossible": bool(locations) and hours > horizon_hours,
        "horizon_hours": horizon_hours,
        "required_regions": len(locations),
        "mst_distance_km": total,
        "fastest_speed_kmh": speed,
        "travel_hours_lower_bound": hours,
        "edges": edges,
        "actors": actors,
        "permissions_relaxed": True,
        "skills_relaxed": True,
        "allows_sequential_and_small_batches": True,
        "allows_cost_free_learning": True,
        "knowledge": "全部最终资料与答案，不收取研究时间",
        "labor_hours_included": 0,
        "explanation": "每区实播与实收均要求同一身体实际在场。访问全部地区的任意路线长度不小于直线最小生成树；忽略地形、返程、睡眠、研究、种植和收获劳动，使用最快已有移动能力。这是有利于单人的全局下界，并不声称单人无法想到理论。",
    }
