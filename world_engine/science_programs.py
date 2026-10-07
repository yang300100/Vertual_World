"""哥哥，科研程序只安排 NPC 提交的动作，真实结果仍由身体、工坊与物质账结算。"""

from __future__ import annotations

import json
from uuid import uuid4

from world_engine.collective_cooperation import CooperativeService as C
from world_engine.collective_evidence import known_evidence
from world_engine.collective_research import CollectiveResearchService as R
from world_engine.collective_types import CollectiveCommand
from world_engine.collective_work import WorkService as W
from world_engine.materials import encoded, material_transaction
from world_engine.repository import to_iso
from world_engine.science_types import AssayStep, CraftCommand, MethodSpec, ScienceProgram


def resolve(value, bindings):
    if isinstance(value, str) and value.startswith("$"):
        parts = value[1:].split(":")
        if parts[0] not in bindings:
            raise ValueError("尚未取得前置动作的实际结果：" + value)
        result = bindings[parts[0]]
        for index in parts[1:]:
            if not index.isdigit() or not isinstance(result, list) or int(index) >= len(result):
                raise ValueError("前置实际产物编号不存在：" + value)
            result = result[int(index)]
        return result
    if isinstance(value, dict):
        return {k: resolve(v, bindings) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, bindings) for v in value]
    return value


def start_execution(
    c,
    session_id,
    actor_id,
    method,
    bindings,
    parameters,
    at,
    *,
    source_program_id=None,
    candidate_id=None,
    candidate_revision=None,
    technology_id=None,
    version=None,
):
    """哥哥，方法编译为真实依赖工单，不提前填任何读数或成果。"""
    method = method if isinstance(method, MethodSpec) else MethodSpec.model_validate(method)
    actor = R.actor(c, actor_id)
    session = c.execute(
        "SELECT * FROM science_sessions WHERE id=? AND world_id=?", (session_id, actor["world_id"])
    ).fetchone()
    if session is None or (
        session["status"] not in {"prepared", "running"}
        and not (session["status"] == "stopping" and source_program_id)
    ):
        raise ValueError("科研会话不可执行")
    if set(bindings) != set(method.inputs):
        raise ValueError("方法必须绑定所有且仅有已声明的输入角色")
    if set(parameters) - set(method.parameters):
        raise ValueError("方法参数尚未定义")
    params = {}
    for key, spec in method.parameters.items():
        value = parameters.get(key, spec.default)
        if (
            isinstance(value, bool)
            or not isinstance(value, (float, int))
            or not spec.minimum <= value <= spec.maximum
        ):
            raise ValueError("方法参数超出已声明条件")
        params[key] = value
    original_method, original_bindings, original_params = method, dict(bindings), dict(params)
    from world_engine.technology_methods import compile_method

    compilation = compile_method(
        c,
        actor_id,
        method,
        bindings,
        params,
        at,
        root_identity=(technology_id, version) if technology_id else None,
    )
    method, bindings, params = compilation.method, compilation.bindings, compilation.parameters
    tasks, results = [], {}
    for index, step in enumerate(method.steps):
        if step.op == "use_method":
            raise ValueError("嵌套方法须经过已知科技实例化器展开")

        def ref(value):
            if value in bindings:
                return bindings[value]
            parts = value[1:].rsplit(":", 1)
            if len(parts) != 2 or parts[0] not in results:
                raise ValueError("方法前置实物引用不存在")
            return "task:" + results[parts[0]] + ":" + parts[1]

        key = "step" + str(index)
        task = {
            "key": key,
            "worker_id": actor_id,
            "batch_id": ref(step.input),
            "operation": step.op,
            "quantity": getattr(step, "quantity", 1),
            "hours": getattr(step, "hours", 0.5),
            "depends_on": [tasks[-1]["key"]] if tasks else [],
            "purpose": "执行本人提交的方法",
        }
        # 哥哥，方法有串行操作；历史产物引用要显式依赖对应工单。
        if step.op == "assay":
            tool_id = ref(step.instrument)
            tool = c.execute(
                "SELECT * FROM collective_instruments WHERE item_id=? AND world_id=?",
                (tool_id, actor["world_id"]),
            ).fetchone()
            if tool is None or tool["method"] != step.metric:
                raise ValueError("方法未绑定同指标实际量器")
            task.update(
                hours=tool["duration_hours"],
                method=step.metric,
                instrument_id=tool_id,
                prediction={"metric": step.metric, "low": -1e9, "high": 1e9},
            )
        else:
            settings = {
                k: params[v[6:]] if isinstance(v, str) else v for k, v in step.settings.items()
            }
            task.update(
                settings=settings,
                apparatus_id=ref(step.apparatus) if step.apparatus else None,
                energy_id=ref(step.energy) if step.energy else None,
                auxiliary_ids=[ref(x) for x in step.auxiliary_inputs],
            )
        refs = [task["batch_id"], *task.get("auxiliary_ids", [])]
        for value in refs:
            if value.startswith("task:") and value.split(":")[1] not in task["depends_on"]:
                task["depends_on"].append(value.split(":")[1])
        tasks.append(task)
        if step.result_key:
            results[step.result_key] = key
    eid = str(uuid4())
    with material_transaction(c):
        c.execute(
            "INSERT INTO technology_executions(id,session_id,world_id,actor_id"
            ",technology_id,version,"
            "candidate_id,candidate_revision,method_json,bindings_json,paramet"
            "ers_json,source_program_id,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                eid,
                session_id,
                actor["world_id"],
                actor_id,
                technology_id,
                version,
                candidate_id,
                candidate_revision,
                original_method.model_dump_json(),
                encoded(original_bindings),
                encoded(original_params),
                source_program_id,
                to_iso(at),
            ),
        )
        pid = C.propose(c, actor_id, {"reason": "本人安排可执行研究方法", "tasks": tasks}, at)
        keys = [t["key"] for t in tasks]
        C.accept(
            c,
            actor_id,
            {
                "proposal_id": pid,
                "version": 1,
                "task_keys": keys,
                "reason": "按本人提交的方法开展实际劳动",
            },
            at,
        )
        C.start(c, actor_id, {"proposal_id": pid, "version": 1}, at)
        orders = c.execute(
            "SELECT * FROM collective_work_orders WHERE proposal_id=? ORDER BY rowid", (pid,)
        ).fetchall()
        for index, order in enumerate(orders):
            c.execute(
                "INSERT INTO science_execution_work VALUES (?,?,?)", (eid, order["id"], index)
            )
        for span in compilation.spans:
            c.execute(
                "INSERT INTO science_method_spans VALUES (?,?,?,?,?,?,?)",
                (
                    eid,
                    actor["world_id"],
                    span["technology_id"],
                    span["version"],
                    span["start_step"],
                    span["end_step"],
                    to_iso(at),
                ),
            )
        c.execute(
            "UPDATE technology_executions SET work_ids_json=? WHERE id=?",
            (encoded([o["id"] for o in orders]), eid),
        )
    return eid


def settle_executions(c, world_id, at):
    for execution in c.execute(
        "SELECT * FROM technology_executions WHERE world_id=? AND status IN ('pending','running')",
        (world_id,),
    ).fetchall():
        orders = c.execute(
            "SELECT o.* FROM science_execution_work l JOIN collective_work_ord"
            "ers o ON o.id=l.order_id "
            "WHERE l.execution_id=? ORDER BY l.step_index",
            (execution["id"],),
        ).fetchall()
        if not orders:
            continue
        statuses = {o["status"] for o in orders}
        terminal = statuses <= {"completed"} or (
            bool(statuses & {"blocked", "interrupted"}) and "working" not in statuses
        )
        status = (
            "completed" if statuses == {"completed"} else ("rejected" if terminal else "running")
        )
        # 哥哥，历史中间产物留在工单；调用者只取得末个处理步骤仍存在的成品。
        produced = [o for o in orders if json.loads(o["spec_json"])["operation"] != "assay"]
        outputs = [
            iid
            for iid in json.loads(produced[-1]["outputs_json"] if produced else "[]")
            if c.execute(
                "SELECT 1 FROM item_instances WHERE id=? AND quantity>0", (iid,)
            ).fetchone()
        ]
        observations = [
            r[0]
            for o in orders
            for r in c.execute(
                "SELECT observation_id FROM collective_assay_results WHERE order_id=?", (o["id"],)
            )
        ]
        inputs = {iid for o in orders for iid in json.loads(o["inputs_json"])}
        historical_outputs = {iid for o in orders for iid in json.loads(o["outputs_json"])}
        ids = inputs | historical_outputs | set(json.loads(execution["bindings_json"]).values())
        ledgers = [
            r["id"]
            for r in c.execute(
                "SELECT * FROM material_ledger WHERE world_id=? AND world_time>=?",
                (world_id, execution["created_at"]),
            )
            if ids & set(json.loads(r["item_ids_json"]))
        ]
        error = (
            "; ".join(
                json.loads(o["result_json"] or "{}").get("error", "中断")
                for o in orders
                if o["status"] in {"blocked", "interrupted"}
            )
            or None
        )
        started = next((o["started_at"] for o in orders if o["started_at"]), None)
        c.execute(
            "UPDATE technology_executions SET status=?,started_at=COALESCE(sta"
            "rted_at,?),completed_at=?,"
            "output_ids_json=?,observation_ids_json=?,ledger_ids_json=?,error=? WHERE id=?",
            (
                status,
                started,
                to_iso(at) if terminal else None,
                encoded(outputs),
                encoded(observations),
                encoded(ledgers),
                error,
                execution["id"],
            ),
        )

        if status == "completed":
            from world_engine.technology_methods import track_material_sources

            track_material_sources(c, execution["id"], at)


class ScienceService:
    @staticmethod
    def submit(c, session_id, actor_id, program, at, *, call_id=None):
        spec = (
            program
            if isinstance(program, ScienceProgram)
            else ScienceProgram.model_validate(program)
        )
        actor = R.actor(c, actor_id)
        session = c.execute(
            "SELECT * FROM science_sessions WHERE id=? AND world_id=?",
            (session_id, actor["world_id"]),
        ).fetchone()
        if session is None or session["status"] not in {"prepared", "running"}:
            raise ValueError("科研会话不属于本人世界或已停止")
        if c.execute(
            "SELECT 1 FROM science_programs WHERE actor_id=? AND status='active'", (actor_id,)
        ).fetchone():
            raise ValueError("本人已有执行中的科研程序")
        referenced = {eid for h in spec.hypotheses for eid in h.evidence_ids}
        if referenced - known_evidence(c, actor_id, at):
            raise ValueError("假说不能引用本人未知、私有或未来证据")
        pid = str(uuid4())
        with material_transaction(c):
            c.execute(
                (
                    "INSERT INTO science_programs(id,session_id,world_id,actor_id,spec"
                    "_json,created_at,call_id) VALUES (?,?,?,?,?,?,?)"
                ),
                (
                    pid,
                    session_id,
                    actor["world_id"],
                    actor_id,
                    spec.model_dump_json(),
                    to_iso(at),
                    call_id,
                ),
            )
            for index, command in enumerate(spec.commands):
                c.execute(
                    (
                        "INSERT INTO science_commands(program_id,command_index,spec_json) "
                        "VALUES (?,?,?)"
                    ),
                    (pid, index, command.model_dump_json()),
                )
            ScienceService._drive(c, pid, at)
        return pid

    @staticmethod
    def _drive(c, pid, at):
        row = c.execute("SELECT * FROM science_programs WHERE id=?", (pid,)).fetchone()
        if row["status"] != "active":
            return
        spec = ScienceProgram.model_validate_json(row["spec_json"])
        bindings = json.loads(row["bindings_json"])
        pc = row["pc"]
        while pc < len(spec.commands):
            cmdrow = c.execute(
                "SELECT * FROM science_commands WHERE program_id=? AND command_index=?", (pid, pc)
            ).fetchone()
            command = spec.commands[pc]
            try:
                if cmdrow["status"] == "running":
                    if cmdrow["source_kind"] == "collective":
                        source = c.execute(
                            "SELECT * FROM collective_programs WHERE id=?", (cmdrow["source_id"],)
                        ).fetchone()
                        if source["status"] == "active":
                            return
                        if source["status"] != "completed":
                            raise ValueError("原有阶段程序实际未完成：" + str(source["error"]))
                        result = json.loads(source["bindings_json"]).get("science_result")
                    elif cmdrow["source_kind"] == "resonance":
                        source = c.execute(
                            "SELECT * FROM research_programs WHERE id=?", (cmdrow["source_id"],)
                        ).fetchone()
                        if source["status"] == "active":
                            return
                        if source["status"] != "complete":
                            raise ValueError("共振器程序实际未完成：" + source["status"])
                        result = json.loads(source["results_json"])
                    else:
                        source = c.execute(
                            "SELECT * FROM technology_executions WHERE id=?", (cmdrow["source_id"],)
                        ).fetchone()
                        if source["status"] in {"pending", "running"}:
                            return
                        if source["status"] != "completed":
                            raise ValueError("方法实际未完成：" + str(source["error"]))
                        result = json.loads(
                            source["observation_ids_json"]
                            if command.op == "assay"
                            else source["output_ids_json"]
                        )
                else:
                    raw = command.model_dump(mode="json")
                    local_scope = (
                        "method"
                        if command.op in {"declare", "revise_claim"}
                        else "program"
                        if command.op == "resonance"
                        else None
                    )
                    local_body = raw.pop(local_scope) if local_scope else None
                    resolved = resolve(raw, bindings)
                    if local_scope:
                        # 哥哥，方法蓝图的$步骤引用在每次实际实例化时解析，不读外层程序绑定。
                        resolved[local_scope] = local_body
                    command = type(command).model_validate(resolved)
                    evidence = set(getattr(command, "evidence_ids", []))
                    if evidence - known_evidence(c, row["actor_id"], at):
                        raise ValueError("动作不能引用本人未知、私有或未来证据")
                    with material_transaction(c):
                        kind, source_id, result = ScienceService._dispatch(
                            c, row, spec, command, at
                        )
                        if source_id:
                            c.execute(
                                (
                                    "UPDATE science_commands SET status='running',"
                                    "source_kind=?,source_id=?,started_at=? "
                                    "WHERE program_id=? AND command_index=?"
                                ),
                                (kind, source_id, to_iso(at), pid, pc),
                            )
                            W.tick(c, row["world_id"], at)
                            settle_executions(c, row["world_id"], at)
                            new = (
                                c.execute(
                                    "SELECT * FROM technology_executions WHERE id=?", (source_id,)
                                ).fetchone()
                                if kind == "execution"
                                else None
                            )
                            if new and new["status"] == "rejected":
                                raise ValueError(new["error"] or "实际工单不能执行")
                            return
                if getattr(command, "result_key", None):
                    bindings[command.result_key] = result
                c.execute(
                    (
                        "UPDATE science_commands SET status='completed',result_json=?,comp"
                        "leted_at=? WHERE program_id=? AND command_index=?"
                    ),
                    (encoded(result), to_iso(at), pid, pc),
                )
                pc += 1
                c.execute(
                    "UPDATE science_programs SET pc=?,bindings_json=? WHERE id=?",
                    (pc, encoded(bindings), pid),
                )
            except ValueError as exc:
                c.execute(
                    (
                        "UPDATE science_commands SET status='rejected',error=?,completed_a"
                        "t=? WHERE program_id=? AND command_index=?"
                    ),
                    (str(exc), to_iso(at), pid, pc),
                )
                c.execute(
                    (
                        "UPDATE science_programs SET status='rejected',error=?,completed_a"
                        "t=? WHERE id=?"
                    ),
                    (str(exc), to_iso(at), pid),
                )
                R.record_observation(
                    c,
                    row["actor_id"],
                    at,
                    "本人科研动作未能执行",
                    {
                        "kind": "science_rejection",
                        "program_id": pid,
                        "command_index": pc,
                        "operation": getattr(command, "operation", command.op),
                        "submitted_command": command.model_dump(mode="json"),
                        "reason": str(exc),
                    },
                )
                return
        c.execute(
            "UPDATE science_programs SET status='completed',completed_at=? WHERE id=?",
            (to_iso(at), pid),
        )

    @staticmethod
    def _dispatch(c, row, spec, command, at):
        if command.op == "resonance":
            from world_engine.autonomous_research import ResearchService

            pid = ResearchService.accept_program(
                c, row["actor_id"], command.program.model_dump(mode="json"), at
            )
            source = c.execute("SELECT * FROM research_programs WHERE id=?", (pid,)).fetchone()
            if source["status"] != "active" and source["status"] != "complete":
                raise ValueError("共振器程序无法开始：" + source["status"])
            return "resonance", pid, None
        if isinstance(command, CollectiveCommand):
            if command.op in {"observe", "experiment"} and command.operation:
                raise ValueError("科研处理须用craft并绑定实际能源；旧实验入口仅允许纯检验")
            raw = command.model_dump(mode="json")
            raw["result_key"] = "science_result"
            pid = R.submit(
                c,
                row["actor_id"],
                {
                    "reason": spec.reason,
                    "hypotheses": [h.model_dump(mode="json") for h in spec.hypotheses],
                    "commands": [raw],
                },
                at,
            )
            return "collective", pid, None
        if isinstance(command, (CraftCommand, AssayStep)):
            raw = command.model_dump(mode="json")
            if isinstance(command, CraftCommand):
                raw["op"] = raw.pop("operation")
            roles, bound = {"sample": "material"}, {"sample": raw["input"]}
            raw["input"] = "sample"
            for field, typ in (
                ("apparatus", "apparatus"),
                ("energy", "energy"),
                ("instrument", "instrument"),
            ):
                if raw.get(field):
                    roles[field], bound[field], raw[field] = typ, raw[field], field
            for index, iid in enumerate(raw.get("auxiliary_inputs", [])):
                key = "aux" + str(index)
                roles[key], bound[key] = "material", iid
                raw["auxiliary_inputs"][index] = key
            method = {"inputs": roles, "steps": [raw]}
            eid = start_execution(
                c,
                row["session_id"],
                row["actor_id"],
                method,
                bound,
                {},
                at,
                source_program_id=row["id"],
            )
            return "execution", eid, None
        if command.op in {"declare", "revise_claim", "validate"}:
            from world_engine.technologies import TechnologyService

            result = getattr(TechnologyService, command.op)(
                c,
                row["session_id"],
                row["actor_id"],
                command.model_dump(mode="json"),
                at,
                source_program_id=row["id"],
            )
            return ("execution", result, None) if command.op == "validate" else (None, None, result)
        if command.op == "use_method":
            from world_engine.technology_methods import execute_method

            eid = execute_method(
                c,
                row["session_id"],
                row["actor_id"],
                command.technology_id,
                command.version,
                command.bindings,
                command.parameters,
                at,
                source_program_id=row["id"],
            )
            return "execution", eid, None
        if command.op == "share_method":
            from world_engine.science_knowledge import share_method

            mids = [
                share_method(
                    c,
                    row["actor_id"],
                    recipient,
                    command.technology_id,
                    command.version,
                    at,
                    evidence_ids=command.evidence_ids,
                    text=command.text,
                )
                for recipient in command.recipients
            ]
            return None, None, mids
        if command.op == "invalidate_method":
            from world_engine.technologies import TechnologyService

            version = c.execute(
                "SELECT current_version FROM technologies WHERE id=? AND world_id=?",
                (command.technology_id, row["world_id"]),
            ).fetchone()
            if version is None or version[0] != command.version:
                raise ValueError("反证对应的科技版本不是当前版本")
            TechnologyService.invalidate(
                c,
                command.technology_id,
                row["actor_id"],
                tuple(command.evidence_ids),
                command.reason,
                at,
            )
            return None, None, command.technology_id
        raise ValueError("尚无可执行的通用研究动作")

    @staticmethod
    def tick(c, world_id, at, *, heartbeat_id=None):
        from world_engine.autonomous_research import ResearchService
        from world_engine.collective_clock import settle
        from world_engine.technologies import TechnologyService

        ResearchService.tick(c, world_id, at)
        settle(c, world_id, at)
        settle_executions(c, world_id, at)
        for row in c.execute(
            "SELECT id FROM science_programs WHERE world_id=? AND status='active' ORDER BY rowid",
            (world_id,),
        ).fetchall():
            ScienceService._drive(c, row["id"], at)
        for row in c.execute(
            "SELECT t.* FROM technology_candidates t JOIN science_sessions s ON s.id=t.session_id "
            "WHERE t.world_id=? AND t.status IN ('proposed','needs_evidence') "
            "AND s.status IN ('running','stopping')",
            (world_id,),
        ).fetchall():
            try:
                result = TechnologyService.certify(c, row["id"], row["actor_id"], at)
            except ValueError as exc:
                result = {"status": "needs_evidence", "gaps": [str(exc)]}
                c.execute(
                    "UPDATE technology_candidates SET gaps_json=? WHERE id=?",
                    (encoded(result["gaps"]), row["id"]),
                )
            if result["status"] in {"verified", "duplicate"}:
                R.record_observation(
                    c,
                    row["actor_id"],
                    at,
                    "本人提交的方法已完成固定判据核验",
                    {"kind": "certification", **result},
                )
        c.execute(
            "UPDATE science_sessions SET status='stopped' WHERE world_id=? AND status='stopping' "
            "AND NOT EXISTS(SELECT 1 FROM science_programs WHERE world_id=? AND status='active')",
            (world_id, world_id),
        )

    @staticmethod
    def context(c, actor_id, at):
        from world_engine.science_knowledge import science_context

        return science_context(c, actor_id, at, max_chars=40000)
