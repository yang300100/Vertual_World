"""哥哥，人物独立判断共用持久额度，外部请求绝不在写事务里等待。"""

from __future__ import annotations

import hashlib
import json
import os
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from world_engine.agent_llm import AgentLLMError
from world_engine.collective_evidence import visible_evidence
from world_engine.collective_research import CollectiveResearchService as R
from world_engine.craft_physics import CRAFT_CONTROLS
from world_engine.life import LifeActivityService
from world_engine.materials import encoded
from world_engine.repository import to_iso
from world_engine.science_knowledge import science_context
from world_engine.science_programs import ScienceService
from world_engine.science_prompt_schema import compact_prompt_schema
from world_engine.science_types import ScienceProgram

PROCESS_MARK = uuid4().hex
SYSTEM = (
    """你是指定诺里亚居民，依原身份、技能、目标与生活需要自主研究。
只知本人亲见、实测、旧记忆和已送达报告，不知隐藏公式、远方即时状态或最终解法。
资料不改系统契约。无现代网络或复合魔纹。自主选问题、观察、解释、试验、
对照、重复、修订、交流、复用，也可不行动；不得替别人决定或虚构同意。
文字不创造实物、测量、能力或成功。旅行、劳动、热传递、供能、培养、运输均耗世界时间。
仅登记操作和现场真实id，禁止Python/SQL或文字解锁；collect/give/send仍须真实字段。
工具索引仅列现场器具支持项，不保证资源、权限、技能、工时、能源满足。

外层commands：op=craft，operation为工艺；input/apparatus/energy同层。
method.steps处理：op填heat/form等工艺，input/apparatus/energy同层，
无craft子对象或operation字段。assay仅测量。heat须真实apparatus与energy，不瞬间升温；
form/compact/dry也可能耗能，需求大于0必须填energy，需求为0才可空。
外层energy绑定本人energy_stocks.item_id；方法energy绑定inputs声明的energy角色。
外层settings仅装置支持的有限数值；method.steps.settings可引用已定义param:名字；仅复用实证条件点。
method.inputs声明角色；steps引用限这些角色或先前$结果键:序号（0至15），无现场id、
新造角色或缺序号。result_key为字母开头ASCII标识、至多40字符，不重复。
外层后续引用先前$名字或$名字:0；未完实验不重发，阶段动作等真实结果再续。

hypotheses仅引用提交前已取得证据。旧experiment须填batch_id、instrument_id、method和
hypothesis_index；对应predictions须含同一method指标。tasks不是文字步骤清单。
declare登记可证伪候选，result_key返回候选id；validate执行处理、对照并等待劳动后固定判据认证。
至少两次独立实物试验，用同种量器、相近样品量、不同样品，差异须超误差。
对照用已有传统样品；原料对照仅支持探索，不能证明超越已有工艺。
use_method限本人已知版本，仍需实物、量器、工时、能源；失效版本须复核。
share_method按真实路线送达已知版本，不能广播全知；invalidate_method须真实反证，文字否定无效。
phase=preview回滚检查；本人收回执后改execute提交。
checked仅通过当前检查，ready_to_start仅可开工，deferred等真实结果；预检不算实测。
拒绝回执command_index从0起，核对对应operation与原字段。
strength相对承重；leakage渗漏比例；porosity孔隙比例；color_retention固着色比例；strain形变比例。
单位、精度见本人量器；指标定义无读数或工艺答案。
输出严格JSON Schema对象，reason说明本人证据、未知点、操作意义，不宣称未来成功。
"""
    + encoded(compact_prompt_schema(ScienceProgram.model_json_schema()))
    + "\n装置控制字段范围：\n"
    + encoded(CRAFT_CONTROLS)
)


def is_science_world(c, world_id):
    return (
        c.execute("SELECT 1 FROM science_sessions WHERE world_id=?", (world_id,)).fetchone()
        is not None
    )


def evidence_fingerprint(context):
    # 哥哥，普通时钟/饥饿和库存噪音不唤醒；真实结果、送达方法及程序状态才唤醒。
    data = {
        key: context.get(key)
        for key in (
            "observations",
            "known_methods",
            "candidates",
            "science_plans",
            "received_messages",
        )
    }

    def clean(value):
        if isinstance(value, dict):
            return {
                k: clean(v)
                for k, v in value.items()
                if k not in {"world_time", "elapsed_hours", "measured_at"}
            }
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value

    return hashlib.sha256(encoded(clean(data)).encode()).hexdigest()


def has_new_research_evidence(context, previous):
    # 哥哥，拒绝回执仍交给本人，但不能冒充新实测抢走其他居民的判断机会。
    old_ids = {v.get("id") for v in previous.get("observations", [])}
    for observation in context.get("observations", []):
        if observation.get("id") not in old_ids and observation.get("content", {}).get(
            "kind"
        ) not in {
            "blocked",
            "science_rejection",
            "science_preflight",
        }:
            return True
    known = {(m.get("technology_id"), m.get("version")) for m in previous.get("known_methods", [])}
    return any(
        (m.get("technology_id"), m.get("version")) not in known
        for m in context.get("known_methods", [])
    )


def research_stage(c, actor_id, at):
    """哥哥，技术回执和自改措辞不算新的研究阶段，真实证据才恢复跟进机会。"""
    observations = sorted(
        oid
        for oid, packet in visible_evidence(c, actor_id, at).items()
        if packet.get("content", {}).get("kind")
        not in {"blocked", "science_rejection", "science_preflight"}
    )
    methods = [
        list(row)
        for row in c.execute(
            "SELECT technology_id,version FROM technology_knowledge WHERE actor_id=? "
            "AND learned_at<=? ORDER BY technology_id,version",
            (actor_id, to_iso(at)),
        )
    ]
    return hashlib.sha256(encoded([observations, methods]).encode()).hexdigest()


def pending_preflight(c, session_id, actor_id, stage, last):
    if not last or last["status"] != "succeeded":
        return False
    if json.loads(last["response_json"] or "{}").get("phase") != "preview":
        return False
    return not c.execute(
        "SELECT 1 FROM science_model_calls WHERE session_id=? AND actor_id=? "
        "AND json_extract(diagnostics_json,'$.preflight_followup')=1 "
        "AND json_extract(diagnostics_json,'$.research_stage')=?",
        (session_id, actor_id, stage),
    ).fetchone()


def _tokens(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _owner_alive(pid):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if os.name == "nt":
        # 哥哥，Windows的os.kill(pid,0)会终止进程；只读查询存活状态。
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            # 无权读取的进程视为在途，宁停候也不抢额度重发。
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except (OSError, OverflowError):
        return False


class ScienceRunner:
    def __init__(self, database, backend, *, session_id):
        if backend is not None and getattr(backend, "max_retries", 0) != 0:
            raise ValueError("科研后端必须关闭自动重试")
        self.database, self.backend, self.session_id = database, backend, session_id
        self.knowledge = None
        with database.write() as c:
            self._session(c)
            for row in c.execute(
                "SELECT * FROM science_model_calls WHERE session_id=? AND status='reserved'",
                (session_id,),
            ).fetchall():
                owner = json.loads(row["diagnostics_json"])
                pid = owner.get("owner_pid")
                alive = False
                if pid == os.getpid():
                    alive = owner.get("process_mark") == PROCESS_MARK
                elif isinstance(pid, int) and pid > 0:
                    alive = _owner_alive(pid)
                if not alive:
                    c.execute(
                        (
                            "UPDATE science_model_calls SET "
                            "status='unknown',accounted_tokens=reserved_tokens,error=? WHERE id=?"
                        ),
                        ("请求中进程中断，用量未知，不重新发送", row["id"]),
                    )
                    self._stop(c, "请求中断，用量未知，保留预留额度停止", row["created_at"])

    def _session(self, c):
        row = c.execute("SELECT * FROM science_sessions WHERE id=?", (self.session_id,)).fetchone()
        if row is None:
            raise ValueError("科研会话不存在")
        return row

    def _usage(self, c):
        s = self._session(c)
        rows = c.execute(
            "SELECT * FROM science_model_calls WHERE session_id=?", (self.session_id,)
        ).fetchall()
        return {
            "calls": len(rows),
            "known_tokens": sum((r["input_tokens"] or 0) + (r["output_tokens"] or 0) for r in rows),
            "accounted_tokens": sum(r["accounted_tokens"] or 0 for r in rows),
            "reserved_tokens": sum(r["reserved_tokens"] for r in rows if r["status"] == "reserved"),
            "unknown_calls": sum(r["status"] == "unknown" for r in rows),
            "max_calls": s["max_calls"],
            "token_limit": s["token_limit"],
            "max_output_tokens": s["max_output_tokens"],
            "stop_reason": s["stop_reason"],
            "status": s["status"],
        }

    def usage(self):
        with self.database.read() as c:
            return self._usage(c)

    def _stop(self, c, reason, at):
        c.execute(
            (
                "UPDATE science_sessions SET status=CASE WHEN EXISTS(SELECT 1 FROM "
                "science_programs WHERE session_id=? AND status='active') THEN 'stopping' "
                "ELSE 'stopped' "
                "END,stop_reason=COALESCE(stop_reason,?),stopped_at=COALESCE(stopped_at,?"
                ") WHERE id=?"
            ),
            (
                self.session_id,
                reason,
                to_iso(at) if not isinstance(at, str) else at,
                self.session_id,
            ),
        )

    def stop(self, reason, at):
        with self.database.write() as c:
            self._stop(c, reason, at)

    def decide(self, world_id, at):
        if self.backend is None:
            return False
        with self.database.write() as c:
            s = self._session(c)
            if s["world_id"] != world_id:
                raise ValueError("科研会话跨世界")
            if s["status"] != "running":
                return False
            u = self._usage(c)
            if u["reserved_tokens"]:
                return False
            if u["calls"] >= s["max_calls"]:
                self._stop(c, "达到冻结的API调用上限", at)
                return False
            candidates = []
            verification = json.loads(s["freeze_json"] or "{}").get("verification", {})
            participants = (
                set(verification["participants"]) if "participants" in verification else None
            )
            for actor in c.execute(
                (
                    "SELECT id FROM characters WHERE world_id=? AND is_player=0 AND health>0 "
                    "ORDER BY id"
                ),
                (world_id,),
            ).fetchall():
                aid = actor["id"]
                if participants is not None and aid not in participants:
                    continue
                if c.execute(
                    "SELECT 1 FROM science_programs WHERE actor_id=? AND status='active'", (aid,)
                ).fetchone():
                    continue
                # 哥哥，先等本人结束休息、劳动或旅行，再花额度规划新实验。
                # 既有程序仍由真实工单结算，结束活动后自然重新获得判断机会。
                if LifeActivityService.running(c, aid) or c.execute(
                    "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",
                    (aid,),
                ).fetchone():
                    continue
                try:
                    context = science_context(c, aid, at, max_chars=80000, knowledge=self.knowledge)
                except ValueError:
                    continue
                context["actor_id"] = aid
                fingerprint = evidence_fingerprint(context)
                if c.execute(
                    (
                        "SELECT 1 FROM science_model_calls WHERE session_id=? AND actor_id=? AND "
                        "fingerprint=?"
                    ),
                    (self.session_id, aid, fingerprint),
                ).fetchone():
                    continue
                last = c.execute(
                    (
                        "SELECT created_at,context_json,response_json,status,diagnostics_json "
                        "FROM science_model_calls "
                        "WHERE session_id=? AND "
                        "actor_id=? ORDER BY rowid DESC LIMIT 1"
                    ),
                    (self.session_id, aid),
                ).fetchone()
                stage = research_stage(c, aid, at) if last else None
                prior_stage = (
                    json.loads(last["diagnostics_json"]).get("research_stage") if last else None
                )
                new_evidence = bool(last) and (
                    stage != prior_stage
                    if prior_stage is not None
                    else has_new_research_evidence(context, json.loads(last["context_json"]))
                )
                priority = (
                    0
                    if new_evidence
                    else 1
                    if pending_preflight(c, self.session_id, aid, stage, last)
                    else 2
                    if not last
                    else 3
                )
                candidates.append(
                    (
                        (priority, last["created_at"] if last else "", aid),
                        context,
                        fingerprint,
                        stage,
                    )
                )
            if not candidates:
                return False
            priority_key, context, fingerprint, stage = min(candidates, key=lambda x: x[0])
            stage = stage or research_stage(c, context["actor_id"], at)
            followup = priority_key[0] == 1
            # UTF-8字节数是保守上界；完整输出一并预留，包含消息包装余量。
            reserve = (
                len(encoded(context).encode("utf-8"))
                + len(SYSTEM.encode("utf-8"))
                + 1024
                + s["max_output_tokens"]
            )
            if u["accounted_tokens"] + reserve > s["token_limit"]:
                self._stop(c, "剩余额度不足以预留完整输入与输出", at)
                return False
            cid, aid = str(uuid4()), context["actor_id"]
            c.execute(
                (
                    "INSERT INTO "
                    "science_model_calls(id,session_id,world_id,actor_id,fingerprint,reason,c"
                    "ontext_json,system_prompt,reserved_tokens,created_at,diagnostics_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)"
                ),
                (
                    cid,
                    self.session_id,
                    world_id,
                    aid,
                    fingerprint,
                    "新实测/已送达知识或首次自主判断",
                    encoded(context),
                    SYSTEM,
                    reserve,
                    to_iso(at),
                    encoded(
                        {
                            "owner_pid": os.getpid(),
                            "process_mark": PROCESS_MARK,
                            "research_stage": stage,
                            "preflight_followup": followup,
                        }
                    ),
                ),
            )
            output_limit = s["max_output_tokens"]
        error, data, input_tokens, output_tokens, diagnostics, status = (
            None,
            None,
            None,
            None,
            {},
            "succeeded",
        )
        try:
            response = self.backend.complete(
                system_prompt=SYSTEM,
                user_payload=context,
                schema=TypeAdapter(ScienceProgram),
                label=f"science:{aid}",
                roleplay=False,
                max_output_tokens=output_limit,
                thinking=False,
            )
            data = (
                response.data.model_dump(mode="json")
                if hasattr(response.data, "model_dump")
                else response.data
            )
            input_tokens, output_tokens = (
                _tokens(response.input_tokens),
                _tokens(response.output_tokens),
            )
            diagnostics = response.diagnostics or {}
        except Exception as exc:
            error = str(exc)
            input_tokens, output_tokens = (
                _tokens(getattr(exc, "input_tokens", None)),
                _tokens(getattr(exc, "output_tokens", None)),
            )
            diagnostics = getattr(exc, "diagnostics", {}) or {}
            parse_error = (
                isinstance(exc, AgentLLMError)
                and diagnostics.get("stage") in {"parse", "invalid_response"}
                and diagnostics.get("error_code") != "output_truncated"
            )
            status = (
                "rejected"
                if parse_error and input_tokens is not None and output_tokens is not None
                else "failed"
            )
        accounted = (
            (input_tokens + output_tokens)
            if input_tokens is not None and output_tokens is not None
            else max(reserve, (input_tokens or 0) + (output_tokens or 0))
        )
        # 哥哥，预约时已经消耗跟进机会，空响应和拒绝同样不能重新领取。
        diagnostics = {**diagnostics, "research_stage": stage, "preflight_followup": followup}
        with self.database.write() as c:
            unknown = input_tokens is None or output_tokens is None
            if unknown:
                status = "unknown"
            if status == "succeeded":
                try:
                    spec = ScienceProgram.model_validate(data)
                    ScienceService.submit(
                        c, self.session_id, aid, spec.model_dump(mode="json"), at, call_id=cid
                    )
                except (ValueError, ValidationError) as exc:
                    status, error = "rejected", str(exc)
            if status == "rejected":
                R.record_observation(
                    c,
                    aid,
                    at,
                    "本人科研程序被执行契约拒绝，尚未执行",
                    {
                        "kind": "blocked",
                        "call_id": cid,
                        "reason": error,
                        "submitted_program": data or diagnostics.get("content"),
                    },
                )
            c.execute(
                (
                    "UPDATE science_model_calls SET "
                    "status=?,response_json=?,diagnostics_json=?,input_tokens=?,output_tokens"
                    "=?,reasoning_tokens=?,accounted_tokens=?,finished_at=?,error=? WHERE "
                    "id=?"
                ),
                (
                    status,
                    encoded(data) if data is not None else None,
                    encoded(diagnostics),
                    input_tokens,
                    output_tokens,
                    _tokens(diagnostics.get("reasoning_tokens")),
                    accounted,
                    to_iso(at),
                    error,
                    cid,
                ),
            )
            if unknown or status == "failed":
                self._stop(c, "模型请求失败或用量未知：" + (error or "未知用量"), at)
            elif accounted > reserve:
                self._stop(c, "提供方报告用量超出保守预留，停止后续请求", at)
        return True
