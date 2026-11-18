"""哥哥，新科研只请求人物提案与独立原理评审；每笔费用先预约再调用。"""

import json
import os
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from world_engine.materials import encoded
from world_engine.principle_budget import usage as lineage_usage
from world_engine.principle_service import (
    PrincipleTechnologyService,
    known_technologies,
    technology_metadata,
)
from world_engine.principle_types import IdeaBatch, ResearchBatch, ResearchReviews, ReviewBatch
from world_engine.repository import to_iso, utc_now
from world_engine.science_runner import _owner_alive

PROPOSE = """你是输入指定的世界居民。依据本人身份、技能、目标、记忆、公开知识和本人
掌握的科技，自行决定是否提出有用的新科技。只知道输入资料；不要假装知道他人私有成果。
最多提出2项，简明解释用途、因果原理、前置条件、限制和本人如何应用。可以不研究。
这不是填写实验工单，不要写样品编号、温度流程或宣称完成实测。
技术名字和方法由你创造，不能仅给传统常识改名字。可以组合本人已知科技，parents仅填
known_technologies中的id或完整名称。没有依据时不编造前置知识。结合职业需求发展能力。
世界已有普通日常职业工具、识字与传统工艺，但没有现代网络、无限资源或永动能力。
新研发采用原理评审，效果按世界能力规则量化，不冒充真实实验。解释须说清为什么有效。
应用支持日常劳动耗时、精力消耗以及食物处理/吸收的改善。采用要有明确本人意愿，
adopt_if_approved必须由你决定；application写本人实际打算怎样使用，不替他人同意。
原理建议控制在80至250字，避免空泛口号。返回严格符合给定JSON Schema的对象。"""

PROPOSE += """
科技节点应是可以支持多项应用的底层知识或工艺，level填foundation或technique。
不要把一个铃、锤、柜等具体器具直接作为科技节点，也不要只把器具名字换成“基础技术”。
name概括可复用原理，unlocks说明它真正提供的能力；具体发明放到inventions中，
每个发明说明自身原理、真实前提、限制及本人应用，并分别接受应用评审。
发明不会因为原理通过就自动制造或获得效果，应至少提出可在当前条件下应用的方案。
如有本人已知技术，优先检查能否组合或推进已有原理：connections逐项填写实际ID、
kind(requires必需前置/improves改进/complements互补)及因果解释。
parents只包含requires的技术ID，并与connections中的requires完全一致。
关系必须说明哪项既有能力参与了新原理、缺少它会失去什么，不能为了画连线硬凑联系。
无需前置时允许基础节点；世界常规工具和传统工艺不是本轮新研发的技术，不得虚构为已知节点。
"""

PROPOSE += """
每项发明分别决定adopt_if_approved。根据本人现有技术、已完成应用和不足，继续发展新原理。
如果确实依赖既有原理，必须登记requires并解释上游能力实际如何参与，不用名字相关代替依赖。
支持材料强度material_strength与加工精度craft_precision：这是有真实材料和劳动的制造产物属性，
不是给人物的通用加成。不要预设科技名称、依赖答案或为了数量改变原理标准。
最多2项科技，每项最多2项发明；原理和应用简洁说明，避免超长输出。
"""

PROPOSE += """
必须输出新结构！每项ideas都有level、unlocks、inventions、parents、connections。
inventions是具体发明对象列表；不能把发明直接塞进ideas，也不能把层级省略。
没有前置或关系时parents和connections填[]；没有可行的新研究时ideas填[]。
下面只示意字段结构，占位内容不是科技答案，所有原理与名称由本人判断：
{"reason":"本人研究动机","ideas":[{"name":"本人提出的通用技术名称","level":"foundation","domain":"physical",
"problem":"本人要解决的问题","principle":"本人解释的可复用因果原理",
"requirements":["真实前提"],"limitations":"适用边界","unlocks":["原理支持的能力"],
"parents":[],"connections":[],"application":"本人打算如何应用","adopt_if_approved":true,
"inventions":[{"name":"本人设计的具体发明","principle":"该发明怎样利用上述原理",
"requirements":["真实材料与条件"],"limitations":"实际限制","application":"本人应用计划",
"adopt_if_approved":true}]}]}
"""

REVIEW = """你是独立的世界原理评审者，不是提案人。评审输入的每一项提案，逐项返回决定。
研发标准是原理合理、自洽、符合世界规律且前置条件匹配；不要求底层实物试验。
不允许无限能源、凭空物资、违反因果，或把不确定原理解释成万能能力。
对照already_researched，拒绝仅改名字、同义重写或没有实质区别的重复能力。
不要为了目标数量放宽标准，不替提案人设计新科技。无法合理说明作用因果时拒绝。
通过时把原理绑定到一种保守且有解释的能力：labor_time降低日常劳动分钟，labor_energy
降低日常劳动精力成本，两者activity=work，factor为0.6至0.98；food_nutrition改善食物
处理与吸收，activity=food，factor为1.02至1.4。只有与原理和用途相符才能绑定。
数值为游戏能力的保守量化，不宣称实测。通过的effect.explanation必须说明对应关系。
required_skill填研究者已掌握的适用职业技能，或明确无需专门技能时填null。
carrier必须准确分类：纯工作方法为knowledge，需要实际工具/装置的为tool，营养为food_process。
tool要声明materials（wood/metal/clay/fiber/paper/water/charcoal）的有限用量1至4，
不能把实际设备说成纯知识绕过制造。系统会真实消耗材料、完成原型劳动后才允许工具生效。
应用活动支持work、food，以及实际制造属性的craft。拒绝理由不要透露研究者未知的其他科技细节。
location_kinds只填实际适用的地点种类，不限制时填空列表；不支持的环境条件不能假装已验证。
拒绝时effect=null；重复时approved=false并填duplicate_of的已有id。
必须覆盖输入的全部proposal_id且不重复，不另添方案。返回严格符合给定JSON Schema的对象。"""

REVIEW += """
新提案必须区分基础技术与发明。拒绝把单个器具改名包装成通用基础原理。
unlocks应当从原理真实推出，不允许从现有金属工具倒推出研究者掌握了矿石冶炼。
核对所有衍生发明是否确实来自该原理；各发明的effect分别对应该发明，
不能把医疗、充能、防腐等不支持的效果硬转换成food_nutrition或劳动加成。
connections必须逐项返回，保留technology_id和kind并独立填写approved与因果解释。
若引用未知技术、关系缺少因果依据或必需前置不匹配，整项提案不可通过。
improves是新方法如何改进已有方法，complements是两种技术如何协同；不凭连线赠送数值增益。
"""

REVIEW += """
本版通用技术的effect填null；invention_reviews逐项覆盖所有inventions的index(从0开始)，
分别判定approved、reasoning、effect。原理通过不代表每个发明都通过；可以独立拒绝某个应用。
各发明必须独立对应真实用途，不继承另一个发明的倍率。批准时给出可执行能力契约。
material_strength、craft_precision的activity=craft，carrier=tool，factor为1.02至1.4，
分别提高实际制造物品的强度指数或加工精度指数，初始基准为1，不假装实测。
制造需要有限真实材料和工具。提高强度会降低该原型后续使用的累积磨损速度。
加工精度只能作为已制成物品的属性，不能凭空提高所有劳动或治疗效果。
未支持的杀菌、任意疗伤与无源充能，应拒绝对应发明，不能硬转换成营养能力。
"""


PROPOSE += """
本世界魔法真实存在，domain须为physical（物理）、magic（魔法）或hybrid（魔工结合）。
不能因为没有现代设备就排除魔纹、储能、法杖投影、材料响应与魔法辅助工艺。
magic_context给出本人实际已知完整魔纹、现场自有储能器物及引导资格；公开常识不等于所有纹路知识。
发明需要魔法时，magic_plan填写pattern_id、source_item_id及转移时的receiver_item_id。
无资源不能假装已会施法或有魔法器物；普通木料、职业名和愿望不会生成魔法。
当前不掌握组合语法，不能改变单纹核心功能；可以研究有限摹刻、介质匹配、损耗与投影稳定。
"""

REVIEW += """
魔法不是禁区。可以通过符合世界规律、本人知识和实物条件的魔法或魔工原理。
魔法能力的magic_plan必须与提案人的选择一致，activity=magic。
magic_energy_cost降低已知单纹的储能消耗，factor=0.6至0.98；
magic_transfer_efficiency提高相容自有器物间的转移效率，factor=1.02至1.4。
仍受登记魔纹的输出、损耗与器物容量约束，不用身体精力替代魔力，不凭空充能。
不能改写或组合未知魔纹，不能凭法师身份获得引导资格，不能越权使用家族或公共设施。
人物只有相对档位；不要向研究者透露作者层真名、谱系数值与公式。
任意伤害、治疗等尚未支持的核心效果仍拒绝对应应用，不换皮为营养或劳动加成。
"""


class PrincipleRunner:
    def __init__(self, database, backend, session_id, *, knowledge=None):
        if backend is not None and getattr(backend, "max_retries", 0):
            raise ValueError("原理科研禁止技术自动重试")
        self.database, self.backend, self.session_id = database, backend, session_id
        self.knowledge = knowledge
        with database.write() as c:
            for row in c.execute(
                "SELECT * FROM principle_calls WHERE session_id=? AND status='reserved'",
                (session_id,),
            ).fetchall():
                if not _owner_alive(row["owner_pid"]):
                    c.execute(
                        "UPDATE principle_calls SET status='unknown',"
                        "accounted_tokens=reserved_tokens,error='进程中断，用量未知，不重发' "
                        "WHERE id=?",
                        (row["id"],),
                    )
                    self.stop(c, "中断请求的完整预约费用已保留")

    def usage(self, c):
        session = c.execute(
            "SELECT * FROM principle_sessions WHERE id=?", (self.session_id,)
        ).fetchone()
        rows = c.execute(
            "SELECT * FROM principle_calls WHERE session_id=?", (self.session_id,)
        ).fetchall()
        return {
            "calls": len(rows),
            "accounted_tokens": sum(r["accounted_tokens"] or 0 for r in rows),
            "reserved_tokens": sum(r["reserved_tokens"] for r in rows if r["status"] == "reserved"),
            "max_calls": session["max_calls"],
            "token_limit": session["token_limit"],
            **(
                {
                    "cumulative": lineage_usage(
                        json.loads(session["manifest_json"])["budget_lineage"],
                        expected_run=(str(self.database.path.resolve()), self.session_id),
                    )
                }
                if json.loads(session["manifest_json"]).get("budget_lineage")
                else {}
            ),
        }

    def stop(self, c, reason):
        c.execute(
            "UPDATE principle_sessions SET status='stopped',stop_reason=? WHERE id=?",
            (reason, self.session_id),
        )

    def call(self, purpose, actor_id, payload, model, prompt):
        if self.backend is None:
            raise ValueError("未配置科研后端，不请求模型")
        system = prompt + "\n" + encoded(model.model_json_schema())
        reserve = len((system + encoded(payload)).encode("utf-8")) + 1024 + 4096
        with self.database.write() as c:
            session = c.execute(
                "SELECT * FROM principle_sessions WHERE id=?", (self.session_id,)
            ).fetchone()
            usage = self.usage(c)
            if session["status"] != "running" or usage["reserved_tokens"]:
                return None, None
            if usage["calls"] >= session["max_calls"]:
                self.stop(c, "达到本轮调用上限")
                return None, None
            if usage["accounted_tokens"] + reserve > session["token_limit"]:
                self.stop(c, "剩余额度不足以完整预约下一笔请求")
                return None, None
            overall = usage.get("cumulative", usage)
            if (
                overall["reserved_tokens"]
                or overall["calls"] >= overall["max_calls"]
                or overall["accounted_tokens"] + reserve > overall["token_limit"]
            ):
                self.stop(c, "达到继承账目的总额度或仍有未结算请求")
                return None, None
            cid = str(uuid4())
            c.execute(
                "INSERT INTO principle_calls(id,session_id,actor_id,purpose,status,input_json,"
                "system_prompt,reserved_tokens,created_at,owner_pid) "
                "VALUES (?,?,?,?,'reserved',?,?,?,?,?)",
                (
                    cid,
                    self.session_id,
                    actor_id,
                    purpose,
                    encoded(payload),
                    system,
                    reserve,
                    to_iso(utc_now()),
                    os.getpid(),
                ),
            )
        data, error, input_tokens, output_tokens, diagnostics = None, None, None, None, {}
        model_name = getattr(self.backend, "model", None)
        try:
            response = self.backend.complete(
                system_prompt=system,
                user_payload=payload,
                schema=TypeAdapter(model),
                label="principle:" + purpose,
                max_output_tokens=4096,
                thinking=False,
                roleplay=False,
            )
            input_tokens, output_tokens = response.input_tokens, response.output_tokens
            model_name = getattr(response, "model_name", None) or model_name
            diagnostics = getattr(response, "diagnostics", {}) or {}
            data = model.model_validate(response.data)
        except Exception as exc:
            error = str(exc)
            input_tokens = (
                getattr(exc, "input_tokens", None) if input_tokens is None else input_tokens
            )
            output_tokens = (
                getattr(exc, "output_tokens", None) if output_tokens is None else output_tokens
            )
            diagnostics = getattr(exc, "diagnostics", {}) or {}
            if isinstance(exc, ValidationError):
                diagnostics["stage"] = "parse"
        known = (
            isinstance(input_tokens, int)
            and not isinstance(input_tokens, bool)
            and input_tokens >= 0
            and isinstance(output_tokens, int)
            and not isinstance(output_tokens, bool)
            and output_tokens >= 0
        )
        valid_input = (
            input_tokens
            if (
                isinstance(input_tokens, int)
                and not isinstance(input_tokens, bool)
                and input_tokens >= 0
            )
            else None
        )
        valid_output = (
            output_tokens
            if (
                isinstance(output_tokens, int)
                and not isinstance(output_tokens, bool)
                and output_tokens >= 0
            )
            else None
        )
        cost = (
            input_tokens + output_tokens
            if known
            else max(reserve, (valid_input or 0) + (valid_output or 0))
        )
        with self.database.write() as c:
            c.execute(
                "UPDATE principle_calls SET status=?,response_json=?,accounted_tokens=?,"
                "input_tokens=?,output_tokens=?,error=?,finished_at=? WHERE id=?",
                (
                    "unknown" if not known else "succeeded" if data else "failed",
                    data.model_dump_json() if data else None,
                    cost,
                    valid_input,
                    valid_output,
                    error,
                    to_iso(utc_now()),
                    cid,
                ),
            )
            c.execute(
                "UPDATE principle_calls SET model_name=?,diagnostics_json=? WHERE id=?",
                (model_name, encoded(diagnostics), cid),
            )
            if not known or cost > reserve:
                self.stop(c, "用量未知或超过预约，停止新增调用")
            elif error and diagnostics.get("stage") not in {"parse", "invalid_response"}:
                self.stop(c, "模型请求失败：" + error)
        return cid, data

    def context(self, c, actor, at):
        methods = [
            {k: t[k] for k in ("id", "name", "principle", "limitations", "application")}
            | technology_metadata(c, t)
            for t in known_technologies(c, actor["id"], at)
        ]
        context = {
            "actor": {k: actor[k] for k in ("id", "name", "identity")},
            "skills": json.loads(actor["skills_json"]),
            "research_focus": json.loads(
                c.execute(
                    "SELECT manifest_json FROM principle_sessions WHERE id=?", (self.session_id,)
                ).fetchone()[0]
            ).get("research_focus"),
            "requested_property": json.loads(
                c.execute(
                    "SELECT manifest_json FROM principle_sessions WHERE id=?", (self.session_id,)
                ).fetchone()[0]
            ).get("requested_property"),
            "goals": json.loads(actor["goals_json"]),
            "available_items": [
                dict(r)
                for r in c.execute(
                    "SELECT t.name,t.category,i.quantity,i.condition FROM item_instances i "
                    "JOIN item_types t ON t.id=i.item_type_id WHERE i.world_id=? "
                    "AND i.owner_character_id=? AND i.quantity>0 ORDER BY i.id LIMIT 10",
                    (actor["world_id"], actor["id"]),
                )
            ],
            "location": dict(
                c.execute(
                    "SELECT id,name,kind FROM locations WHERE id=?",
                    (actor["current_location_id"] or actor["location_id"],),
                ).fetchone()
            ),
            "known_technologies": methods,
            "memories": [
                r[0]
                for r in c.execute(
                    "SELECT m.summary FROM character_memories m LEFT JOIN world_events e "
                    "ON e.id=m.event_id AND e.world_id=m.world_id "
                    "WHERE m.character_id=? AND m.world_id=? AND m.created_at<=? "
                    "AND (m.event_id IS NULL OR e.occurred_at<=?) "
                    "ORDER BY m.importance DESC,m.created_at DESC LIMIT 5",
                    (actor["id"], actor["world_id"], to_iso(at), to_iso(at)),
                )
            ],
            "feedback": [
                dict(r)
                for r in c.execute(
                    "SELECT status,error FROM principle_calls WHERE actor_id=? AND session_id=? "
                    "ORDER BY rowid DESC LIMIT 2",
                    (actor["id"], self.session_id),
                )
            ],
            "review_feedback": [
                json.loads(r[0])
                for r in c.execute(
                    "SELECT review_json FROM principle_proposals WHERE inventor_id=? "
                    "AND session_id=? AND status='rejected' ORDER BY rowid DESC LIMIT 2",
                    (actor["id"], self.session_id),
                )
            ],
            "application_feedback": [
                dict(r)
                for r in c.execute(
                    "SELECT t.name,a.result_json FROM principle_application_attempts a "
                    "JOIN principle_technologies t ON t.id=a.technology_id "
                    "WHERE t.inventor_id=? AND t.world_id=? AND a.occurred_at<=? "
                    "ORDER BY a.rowid DESC LIMIT 4",
                    (actor["id"], actor["world_id"], to_iso(at)),
                )
            ],
        }
        from world_engine.magic_research import public_context

        context["magic_context"] = public_context(c, actor, at)
        from world_engine.phenomena import observations
        from world_engine.phenomena_rules import REGISTRY

        context["phenomenon_observations"] = observations(c, actor["id"], at)
        context["phenomenon_mechanisms"] = REGISTRY.public_capabilities()
        if self.knowledge:
            hits = self.knowledge.search(
                actor["identity"] + " " + actor["goals_json"],
                audiences={"character_common"},
                limit=3,
                max_total_chars=4000,
            )
            context["public_knowledge"] = [h.chunk.content for h in hits]
        return context

    def step(self, at):
        with self.database.read() as c:
            session = c.execute(
                "SELECT * FROM principle_sessions WHERE id=?", (self.session_id,)
            ).fetchone()
            if session["status"] != "running":
                return False
            count = c.execute(
                "SELECT count(*) FROM principle_technologies WHERE session_id=?", (self.session_id,)
            ).fetchone()[0]
            if count >= session["target_count"]:
                return False
            from world_engine.principle_inventions import has_table

            if (
                has_table(c, "principle_projects")
                and c.execute(
                    "SELECT 1 FROM principle_projects j JOIN principle_proposals p "
                    "ON p.id=j.proposal_id "
                    "WHERE p.session_id=? AND j.status!='completed'",
                    (self.session_id,),
                ).fetchone()
            ):
                # 哥哥，已有项目先付出实际研究时间，等待期间不反复请求模型。
                return True
            actors = c.execute(
                "SELECT * FROM characters WHERE world_id=? AND is_player=0 "
                "AND health>=40 AND json_array_length(skills_json)>0 ORDER BY id",
                (session["world_id"],),
            ).fetchall()
            if not actors:
                raise ValueError("没有具备原有职业技能的存活NPC")
            # 哥哥，每人先思考，再有一次基于本人新知识的继续机会，然后轮到其他居民。
            rounds = json.loads(session["manifest_json"]).get("research_rounds_per_actor", 2)
            actor = actors[(session["cursor"] // rounds) % len(actors)]
            payload = self.context(c, actor, at)
        modern = json.loads(session["manifest_json"]).get("multi_invention_reviews")
        focused_prompt = PROPOSE
        if payload["research_focus"] == "manufactured_item_properties":
            focused_prompt += (
                "\n本次专题：在本人现有知识与真实场所条件下，研究制成物品的强度或加工精度。"
            )
            focused_prompt += "不预设技术答案，不改说成食物或通用劳动加成。"
            focused_prompt += "提出能在本人当前地点制作与使用的发明；若确实无法做到可以不研究。"
            if payload["requested_property"]:
                focused_prompt += (
                    "本次指定关注的物品属性为：" + payload["requested_property"] + "。"
                )
        cid, batch = self.call(
            "propose", actor["id"], payload, ResearchBatch if modern else IdeaBatch, focused_prompt
        )
        with self.database.write() as c:
            c.execute(
                "UPDATE principle_sessions SET cursor=cursor+1 WHERE id=?", (self.session_id,)
            )
            if batch is None:
                recent = c.execute(
                    "SELECT status FROM principle_calls WHERE session_id=? "
                    "AND purpose='propose' ORDER BY rowid DESC LIMIT 2",
                    (self.session_id,),
                ).fetchall()
                if len(recent) == 2 and all(r[0] == "failed" for r in recent):
                    self.stop(c, "连续两次提案结构校验失败，停止新增调用保留预算")
                return cid is not None
            if any(idea.level == "invention" for idea in batch.ideas):
                c.execute(
                    "UPDATE principle_calls SET error=? WHERE id=?",
                    ("新科研须提出可复用技术，把具体器具放入衍生发明", cid),
                )
                return True
            try:
                ids = PrincipleTechnologyService.propose(
                    c, self.session_id, actor["id"], cid, batch.model_dump(), at
                )
            except ValueError as exc:
                c.execute("UPDATE principle_calls SET error=? WHERE id=?", (str(exc), cid))
                return True
            if not ids:
                return True
            proposals = [
                {
                    "proposal_id": pid,
                    "idea": json.loads(
                        c.execute(
                            "SELECT spec_json FROM principle_proposals WHERE id=?", (pid,)
                        ).fetchone()[0]
                    ),
                }
                for pid in ids
            ]
            already = [
                {
                    k: t[k]
                    for k in (
                        "id",
                        "name",
                        "principle",
                        "limitations",
                        "level",
                        "unlocks",
                        "domain",
                    )
                }
                for t in PrincipleTechnologyService.graph(c, actor["world_id"])["nodes"]
            ]
        rid, review = self.call(
            "review",
            None,
            {"proposals": proposals, "researcher": payload, "already_researched": already},
            ResearchReviews if modern else ReviewBatch,
            REVIEW,
        )
        if review is None:
            return True
        if {d.proposal_id for d in review.decisions} != set(ids) or len(review.decisions) != len(
            ids
        ):
            with self.database.write() as c:
                c.execute(
                    "UPDATE principle_calls SET error='评审没有精确覆盖本次全部提案' WHERE id=?",
                    (rid,),
                )
            return True
        with self.database.write() as c:
            decisions = review.decisions
            if payload["research_focus"] == "manufactured_item_properties":
                decisions = sorted(
                    decisions,
                    key=lambda d: (
                        not any(
                            r.approved
                            and r.effect
                            and r.effect.kind
                            in (
                                {payload["requested_property"]}
                                if payload["requested_property"]
                                else {"material_strength", "craft_precision"}
                            )
                            for r in d.invention_reviews
                        )
                    ),
                )
            for decision in decisions:
                idea = next(
                    p["idea"] for p in proposals if p["proposal_id"] == decision.proposal_id
                )
                if json.loads(session["manifest_json"]).get("multi_invention_reviews") and (
                    decision.effect is not None
                    or len(decision.invention_reviews) != len(idea["inventions"])
                ):
                    c.execute(
                        "UPDATE principle_calls SET error=? WHERE id=?",
                        ("新版评审必须独立覆盖所有发明，不能沿用首个发明的能力", rid),
                    )
                    continue
                count = c.execute(
                    "SELECT count(*) FROM principle_technologies WHERE session_id=?",
                    (self.session_id,),
                ).fetchone()[0]
                if count >= session["target_count"]:
                    break
                try:
                    PrincipleTechnologyService.review(
                        c, decision.proposal_id, rid, decision.model_dump(), at
                    )
                except ValueError as exc:
                    c.execute("UPDATE principle_calls SET error=? WHERE id=?", (str(exc), rid))
            from world_engine.principle_cycles import tick

            tick(c, actor["world_id"], at)
        return True
