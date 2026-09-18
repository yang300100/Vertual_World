"""联络、信笺与长期委托的接口。

原先这些路由与辅助函数都内嵌在 `api.create_app` 里，最长的 `create_app` 超过
1600 行。这里按对话域整体搬出，行为与之完全一致（路径、响应模型、错误码与
错误文案一字未改）。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Annotated, Any, Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from world_engine.agent_llm import AgentLLMError
from world_engine.api_deps import require_world_time_text as _world_time
from world_engine.bounded_calls import submit_call
from world_engine.character_growth import CharacterGrowthService
from world_engine.contracts import KINDS, ContractError, ContractService
from world_engine.geo import great_circle_distance_km
from world_engine.proximity import VISIBLE_PERSON_RADIUS_KM, same_room
from world_engine.repository import from_iso, to_iso, utc_now
from world_engine.roleplay import npc_reply_system_prompt


class ContactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipient_id: str = Field(min_length=1, max_length=100)


class MessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipient_id: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=1000)


class NpcTextResult(BaseModel):
    """模型生成的 NPC 可见文本。"""

    model_config = ConfigDict(extra="forbid")

    reply: str = Field(min_length=1, max_length=900)
    social_move: Literal["answer", "question", "evade", "boundary", "refuse", "offer"] = "answer"
    topic: str | None = Field(default=None, max_length=160)


class LongTermRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipient_id: str = Field(min_length=1, max_length=100)
    operation_type: str = Field(min_length=1, max_length=60)
    terms: dict[str, Any] = Field(default_factory=dict)


class LongTermConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accept_counter_terms: bool = True


class DialogueContextServices:
    """对话域路由需要读取的少量设置。

    对话路由只用到「模型调用超时秒数」，它来自 `Settings`，而 `DecisionProvider`
    并不暴露同名属性。这里显式取出该值，避免让路由工厂依赖整个 `Settings` 类型。
    """

    __slots__ = ("world_agent_timeout_seconds",)

    def __init__(self, settings: object) -> None:
        self.world_agent_timeout_seconds = settings.world_agent_timeout_seconds


def build_dialogue_router(database, engine, repository, resolved_settings) -> APIRouter:
    """联络、信笺与长期委托的接口。"""

    router = APIRouter(prefix="/api/worlds/{world_id}", tags=["对话"])

    def _record_social_fact(
        connection: sqlite3.Connection,
        *,
        world_id: str,
        actor_id: str,
        target_id: str,
        event_type: str,
        summary: str,
        payload: dict[str, object],
        importance: int = 5,
    ) -> str:
        """为双方共同经历写同一条可审计事件与各自记忆。"""
        now = to_iso(utc_now())
        event_id = str(uuid4())
        connection.execute(
            """
            INSERT INTO world_events(
                id, world_id, tick_id, occurred_at, event_type, actor_id, target_id,
                summary, importance, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'routine', ?, ?)
            """,
            (
                event_id, world_id, f"social:{event_id}", _world_time(connection, world_id),
                event_type, actor_id, target_id, summary,
                json.dumps(payload, ensure_ascii=False), now,
            ),
        )
        from world_engine.society import SocietyService

        SocietyService.observe_event(connection, event_id)
        for character_id in (actor_id, target_id):
            connection.execute(
                """
                INSERT INTO character_memories(
                    id, world_id, character_id, event_id, memory_type,
                    summary, importance, confidence, created_at
                ) VALUES (?, ?, ?, ?, 'experienced', ?, ?, 1.0, ?)
                """,
                (str(uuid4()), world_id, character_id, event_id, summary, importance, now),
            )
        return event_id

    def _relationship_score(
        connection: sqlite3.Connection, world_id: str, npc_id: str, player_id: str
    ) -> int:
        rows = connection.execute(
            """
            SELECT affinity, trust FROM relationships
            WHERE world_id = ? AND (
                (source_character_id = ? AND target_character_id = ?)
                OR (source_character_id = ? AND target_character_id = ?)
            )
            """,
            (world_id, npc_id, player_id, player_id, npc_id),
        ).fetchall()
        if not rows:
            return 0
        return round(sum(int(row["affinity"]) + int(row["trust"]) for row in rows) / len(rows))

    def _npc_response(
        *,
        connection: sqlite3.Connection,
        world_id: str,
        label: str,
        npc: sqlite3.Row,
        player: sqlite3.Row,
        channel: str,
        player_text: str,
        interaction: str,
        details: dict[str, object],
    ) -> str:
        """通过统一上下文管线生成 NPC 可见文本；模型故障时不以模板替代。"""

        backend = engine.agent_model_backend
        if backend is None:
            raise HTTPException(status_code=503, detail="NPC 对话模型未配置，无法生成回应")
        snapshot = repository.get_snapshot(connection, world_id)
        npc_state = snapshot.character_by_id(str(npc["id"]))
        player_state = snapshot.character_by_id(str(player["id"]))
        if npc_state is None or player_state is None:
            raise HTTPException(status_code=404, detail="人物不存在")
        context = engine.dialogue_context.build(
            connection,
            snapshot=snapshot,
            npc=npc_state,
            player=player_state,
            player_text=player_text,
            conversation=None,
            channel=channel,
            interaction=interaction,
            decision_details=details,
        )
        try:
            result = submit_call(backend.complete,
                label=label,
                system_prompt=npc_reply_system_prompt(include_topic=True),
                roleplay=True,
                user_payload=context,
                schema=TypeAdapter(NpcTextResult),
            ).result(timeout=resolved_settings.world_agent_timeout_seconds)
            reply = result.data.reply.strip()
        except (AgentLLMError, ValueError, TypeError, KeyError, TimeoutError, RuntimeError) as exc:
            raise HTTPException(
                status_code=503, detail="NPC 对话模型暂时不可用，请稍后重试"
            ) from exc
        forbidden = ("纳米机器人", "人工智能", "系统权限", "RAG", "prompt", "数据库")
        if not reply or any(term in reply for term in forbidden):
            raise HTTPException(
                status_code=503,
                detail="NPC 对话模型返回了不安全的内容，请稍后重试",
            )
        return reply

    def _contact_decision(
        connection: sqlite3.Connection, world_id: str, player: sqlite3.Row, npc: sqlite3.Row
    ) -> tuple[str, str]:
        score = _relationship_score(connection, world_id, npc["id"], player["id"])
        try:
            traits = set(json.loads(npc["traits_json"] or "[]"))
        except (json.JSONDecodeError, TypeError):
            traits = set()
        if score <= -35 or (traits & {"警惕", "孤僻", "戒备"} and score < 10):
            status = "rejected"
        else:
            status = "accepted"
        return status, _npc_response(
            connection=connection,
            world_id=world_id,
            label="contact_reply",
            npc=npc,
            player=player,
            channel="contact_request",
            player_text="我想与你交换联络信笺。",
            interaction="当面请求交换联络信笺",
            details={"status": status, "relationship_score": score},
        )

    def _letter_reply(
        connection: sqlite3.Connection,
        *,
        world_id: str,
        contact_id: str,
        npc: sqlite3.Row,
        content: str,
        world_time: str,
    ) -> str:
        """以 NPC 身份回复远程信笺，不使用任何文本模板。"""

        player = connection.execute(
            "SELECT * FROM characters WHERE world_id = ? AND is_player = 1", (world_id,)
        ).fetchone()
        if player is None:
            raise HTTPException(status_code=404, detail="玩家角色不存在")
        history = [
            {
                "sender_id": row["sender_id"],
                "content": row["content"],
                "world_time": row["world_time"],
            }
            for row in connection.execute(
                """SELECT sender_id, content, world_time FROM character_messages
                WHERE contact_id = ? ORDER BY world_time DESC, created_at DESC, rowid DESC LIMIT 6""",
                (contact_id,),
            ).fetchall()
        ]
        history.reverse()
        return _npc_response(
            connection=connection,
            world_id=world_id,
            label="letter_reply",
            npc=npc,
            player=player,
            channel="letter",
            player_text=content,
            interaction="远程信笺回复；不得声称看见对方、立刻到场或已执行行动",
            details={
                "world_time": world_time,
                "incoming_letter": content,
                "recent_letters": history,
            },
        )

    def _long_term_decision(
        connection: sqlite3.Connection,
        world_id: str,
        player: sqlite3.Row,
        npc: sqlite3.Row,
        operation_type: str,
        terms: dict[str, Any],
    ) -> tuple[str, dict[str, Any] | None, str]:
        """规则只裁定事务状态；NPC 解释文本统一由模型生成。"""
        allowed = {"委托", "雇佣", "借贷", "租赁", "住房", "约定", "约定改期", "reschedule_appointment", "学习", "commission", "employment", "loan", "lease", "lodging", "appointment", "learning"}
        if operation_type not in allowed:
            return "npc_rejected", None, "该事务类型必须先走世界元素注册审议。"
        terms_text = json.dumps(terms, ensure_ascii=False).lower()
        identity_change_terms = (
            "结婚", "成婚", "订婚", "婚姻", "婚配", "求婚", "配偶", "夫妻", "嫁给", "娶我", "娶你",
            "收养", "继承", "遗产", "监护", "家族成员", "宗族", "产权", "所有权", "土地转让", "土地所有", "房产",
            "marriage", "marry", "wedding", "spouse", "adoption", "inheritance", "guardianship", "ownership", "land title",
        )
        if any(term in terms_text for term in identity_change_terms):
            return "npc_rejected", None, "提议涉及身份或权属变更，必须先走世界元素注册审议。"
        if operation_type in {"约定改期", "reschedule_appointment"} or (operation_type in {"约定", "appointment"} and terms.get("meeting_world_time")):
            from world_engine.schedules import ScheduleError, ScheduleService
            try:
                ScheduleService.validate(connection,world_id,player["id"],npc["id"],terms,from_iso(_world_time(connection,world_id)),amendment=operation_type in {"约定改期","reschedule_appointment"})
            except ScheduleError as exc:
                return "npc_rejected",None,str(exc)
        if operation_type in {"住房","lodging"}:
            room=connection.execute("SELECT r.*,p.nightly_rate FROM life_rooms r JOIN room_rental_rates p ON p.room_id=r.id WHERE r.id=? AND r.world_id=? AND r.owner_character_id=?",(terms.get("room_id"),world_id,npc["id"])).fetchone()
            if room is None:return "npc_rejected",None,"该房间不属于对方或尚未开放出租。"
            if connection.execute("SELECT 1 FROM contract_fulfillments WHERE kind='lodging' AND asset_id=? AND status='active'",(room["id"],)).fetchone():return "npc_rejected",None,"房间已有有效租约。"
            days=terms.get("duration_days",1);payment=terms.get("payment",0)
            if not isinstance(days,int) or isinstance(days,bool) or days<1:return "npc_rejected",None,"住房天数需要是正整数。"
            price=room["nightly_rate"]*days
            if not isinstance(payment,int) or payment<price:return "npc_countered",{**terms,"payment":price},"需要先确认完整租金，才会授予限期使用权。"
        if operation_type in {"学习", "learning"}:
            skill = str(terms.get("skill") or "").strip()
            known_skills = set(json.loads(npc["skills_json"] or "[]"))
            if not skill or skill not in known_skills:
                return "npc_rejected", None, "NPC 不具备可教授的请求技能。"
        score = _relationship_score(connection, world_id, npc["id"], player["id"])
        if score <= -35:
            return "npc_rejected", None, "当前关系不允许接受这项长期事务。"
        if score < 15 and operation_type not in {"约定", "appointment", "约定改期", "reschedule_appointment"}:
            counter = dict(terms)
            amount_key = "payment" if "payment" in counter else "amount" if "amount" in counter else None
            if amount_key is not None:
                try:
                    counter[amount_key] = max(0, int(counter[amount_key]) * 2)
                except (TypeError, ValueError):
                    return "npc_rejected", None, "金额不是有效的非负整数，无法形成约定。"
            else:
                counter["payment"] = 9
            return "npc_countered", counter, "需要先接受 NPC 提出的反提案，才能确认执行。"
        return "npc_accepted", None, "事务尚未执行，等待玩家最终确认。"

    def _safe_terms(raw: object) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="事务条款必须是对象")
        try:
            encoded = json.dumps(raw, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="事务条款无法保存") from exc
        if len(encoded) > 4000:
            raise HTTPException(status_code=400, detail="事务条款过长")
        return raw

    @router.post("/contacts")
    def request_contact(world_id: str, payload: ContactRequest) -> dict[str, object]:
        with database.read() as connection:
            version = repository.get_snapshot(connection, world_id).world.version
            player = connection.execute(
                "SELECT * FROM characters WHERE world_id = ? AND is_player = 1", (world_id,)
            ).fetchone()
            npc = connection.execute(
                "SELECT * FROM characters WHERE id = ? AND world_id = ? AND is_player = 0",
                (payload.recipient_id, world_id),
            ).fetchone()
            if player is None or npc is None:
                raise HTTPException(status_code=404, detail="人物不存在")
            if not same_room(player, npc) or great_circle_distance_km(
                player["longitude"], player["latitude"], npc["longitude"], npc["latitude"]
            ) > VISIBLE_PERSON_RADIUS_KM:
                raise HTTPException(status_code=403, detail="只能与100米内、实际相遇的NPC交换联络信笺")
            status, response = _contact_decision(connection, world_id, player, npc)
        with database.write() as connection:
            if repository.get_snapshot(connection, world_id).world.version != version:
                raise HTTPException(status_code=409, detail="世界状态已变化，请重新提交")
            now = to_iso(utc_now())
            existing = connection.execute(
                "SELECT id, status FROM character_contacts WHERE world_id = ? AND requester_id = ? AND recipient_id = ?",
                (world_id, player["id"], npc["id"]),
            ).fetchone()
            contact_id = existing["id"] if existing is not None else str(uuid4())
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO character_contacts(
                        id, world_id, requester_id, recipient_id, status, response_reason, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (contact_id, world_id, player["id"], npc["id"], status, response, now, now),
                )
            else:
                connection.execute(
                    "UPDATE character_contacts SET status = ?, response_reason = ?, updated_at = ? WHERE id = ?",
                    (status, response, now, contact_id),
                )
            event_id = _record_social_fact(
                connection, world_id=world_id, actor_id=player["id"], target_id=npc["id"],
                event_type="social.contact_exchange", summary=response,
                payload={"contact_id": contact_id, "status": status}, importance=5,
            )
            connection.execute("UPDATE character_contacts SET source_event_id = ? WHERE id = ?", (event_id, contact_id))
            if status == "accepted" and (existing is None or existing["status"] != "accepted"):
                # 同意即完成双方信笺交换；NPC 留下的首条短笺复用本次模型回应。
                world_time = _world_time(connection, world_id)
                player_note = "我将自己的联络信笺交给了你，愿日后互通消息。"
                npc_note = response
                connection.executemany(
                    """INSERT INTO character_messages(
                        id, world_id, contact_id, sender_id, recipient_id, content, world_time, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        (str(uuid4()), world_id, contact_id, player["id"], npc["id"], player_note, world_time, now),
                        (str(uuid4()), world_id, contact_id, npc["id"], player["id"], npc_note, world_time, now),
                    ],
                )
            connection.execute(
                "UPDATE worlds SET version = version + 1, updated_at = ? WHERE id = ?", (now, world_id)
            )
            return {"status": status, "contact_id": contact_id, "response": response}

    @router.get("/contacts")
    def list_contacts(
        world_id: str,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict[str, object]]:
        with database.read() as connection:
            _world_time(connection, world_id)
            return [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT c.*, n.name AS name, n.identity AS identity
                    FROM character_contacts c JOIN characters n ON n.id = c.recipient_id
                    WHERE c.world_id = ? AND c.status = 'accepted'
                    ORDER BY c.updated_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (world_id, limit, offset),
                ).fetchall()
            ]

    @router.post("/messages")
    def send_message(world_id: str, payload: MessageRequest) -> dict[str, object]:
        with database.read() as connection:
            version = repository.get_snapshot(connection, world_id).world.version
            player = connection.execute(
                "SELECT * FROM characters WHERE world_id = ? AND is_player = 1", (world_id,)
            ).fetchone()
            npc = connection.execute(
                "SELECT * FROM characters WHERE id = ? AND world_id = ? AND is_player = 0",
                (payload.recipient_id, world_id),
            ).fetchone()
            if player is None or npc is None:
                raise HTTPException(status_code=404, detail="人物不存在")
            contact = connection.execute(
                """
                SELECT id FROM character_contacts
                WHERE world_id = ? AND requester_id = ? AND recipient_id = ? AND status = 'accepted'
                """,
                (world_id, player["id"], npc["id"]),
            ).fetchone()
            if contact is None:
                raise HTTPException(status_code=403, detail="尚未交换联络信笺，不能远程交谈")
            now, world_time = to_iso(utc_now()), _world_time(connection, world_id)
            message_id = str(uuid4())
            content = payload.content.strip()
            reply = _letter_reply(
                connection,
                world_id=world_id,
                contact_id=contact["id"],
                npc=npc,
                content=content,
                world_time=world_time,
            )
        with database.write() as connection:
            if repository.get_snapshot(connection, world_id).world.version != version:
                raise HTTPException(status_code=409, detail="世界状态已变化，请重新寄送")
            connection.execute(
                """INSERT INTO character_messages(
                    id, world_id, contact_id, sender_id, recipient_id, content, world_time, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (message_id, world_id, contact["id"], player["id"], npc["id"], content, world_time, now),
            )
            reply_id = str(uuid4())
            connection.execute(
                """INSERT INTO character_messages(
                    id, world_id, contact_id, sender_id, recipient_id, content, world_time, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (reply_id, world_id, contact["id"], npc["id"], player["id"], reply, world_time, now),
            )
            _record_social_fact(
                connection, world_id=world_id, actor_id=player["id"], target_id=npc["id"],
                event_type="social.letter", summary=f"{player['name']}与{npc['name']}通过联络信笺交换了消息。",
                payload={"contact_id": contact["id"], "message_id": message_id, "reply_id": reply_id}, importance=4,
            )
            connection.execute("UPDATE worlds SET version = version + 1, updated_at = ? WHERE id = ?", (now, world_id))
            return {"id": message_id, "reply_id": reply_id, "reply": reply, "status": "sent"}

    @router.get("/messages")
    def list_messages(
        world_id: str,
        recipient_id: str = Query(min_length=1, max_length=100),
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict[str, object]]:
        with database.read() as connection:
            player = connection.execute(
                "SELECT id FROM characters WHERE world_id = ? AND is_player = 1", (world_id,)
            ).fetchone()
            if player is None:
                raise HTTPException(status_code=404, detail="当前世界没有玩家角色")
            contact = connection.execute(
                """SELECT id FROM character_contacts WHERE world_id = ? AND requester_id = ?
                   AND recipient_id = ? AND status = 'accepted'""",
                (world_id, player["id"], recipient_id),
            ).fetchone()
            if contact is None:
                raise HTTPException(status_code=403, detail="尚未交换联络信笺")
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM character_messages WHERE contact_id = ?
                       ORDER BY world_time, created_at, rowid LIMIT ? OFFSET ?""",
                    (contact["id"], limit, offset),
                ).fetchall()
            ]

    @router.get("/long-term-requests")
    def list_long_term(
        world_id: str,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict[str, object]]:
        with database.read() as connection:
            _world_time(connection, world_id)
            rows = connection.execute(
                """SELECT r.*, p.name AS requester_name, n.name AS recipient_name
                   FROM long_term_operation_requests r
                   JOIN characters p ON p.id = r.requester_id
                   JOIN characters n ON n.id = r.recipient_id
                   WHERE r.world_id = ?
                   ORDER BY r.created_at DESC LIMIT ? OFFSET ?""",
                (world_id, limit, offset),
            ).fetchall()
            # 一次性取回本页涉及的履约记录，避免逐条查询（原实现是每请求一次 SELECT）。
            request_ids = [str(row["id"]) for row in rows]
            fulfillments: dict[str, dict[str, object]] = {}
            if request_ids:
                placeholders = ",".join("?" for _ in request_ids)
                for row in connection.execute(
                    f"""SELECT c.*, (SELECT COUNT(*) FROM contract_receipts e
                        WHERE e.request_id=c.request_id) AS completed_units
                        FROM contract_fulfillments c
                        WHERE c.request_id IN ({placeholders})""",  # noqa: S608 - 占位符按请求数生成
                    request_ids,
                ).fetchall():
                    fulfillments[str(row["request_id"])] = dict(row)
            result: list[dict[str, object]] = []
            for row in rows:
                item = dict(row)
                item["terms"] = json.loads(item.pop("terms_json"))
                raw_counter = item.pop("counter_terms_json", None)
                item["counter_terms"] = json.loads(raw_counter) if raw_counter else None
                item["fulfillment"] = fulfillments.get(str(row["id"]))
                result.append(item)
            return result

    @router.post("/long-term-requests")
    def submit_long_term(world_id: str, payload: LongTermRequest) -> dict[str, object]:
        with database.read() as connection:
            version = repository.get_snapshot(connection, world_id).world.version
            player = connection.execute(
                "SELECT * FROM characters WHERE world_id = ? AND is_player = 1", (world_id,)
            ).fetchone()
            npc = connection.execute(
                "SELECT * FROM characters WHERE id = ? AND world_id = ? AND is_player = 0",
                (payload.recipient_id, world_id),
            ).fetchone()
            if player is None or npc is None:
                raise HTTPException(status_code=404, detail="人物不存在")
            terms = _safe_terms(payload.terms)
            status, counter_terms, decision_basis = _long_term_decision(
                connection, world_id, player, npc, payload.operation_type.strip(), terms
            )
            system_notice = (
                decision_basis
                if status == "npc_rejected" and ("世界元素注册审议" in decision_basis or payload.operation_type.strip() in {"约定","appointment","约定改期","reschedule_appointment"})
                else None
            )
            response = _npc_response(
                connection=connection,
                world_id=world_id,
                label="long_term_reply",
                npc=npc,
                player=player,
                channel="long_term_review",
                player_text=(
                    f"我提出一项{payload.operation_type.strip()}："
                    f"{json.dumps(terms, ensure_ascii=False)}"
                ),
                interaction=(
                    "审阅长期事务；不得声称已执行，状态和反提案由规则确定。"
                    "不得把系统规则、注册审议或审核流程说成自己的话。"
                ),
                details={
                    "operation_type": payload.operation_type.strip(),
                    "terms": terms,
                    "status": status,
                    "counter_terms": counter_terms,
                    "decision_basis": decision_basis,
                },
            )
        with database.write() as connection:
            if repository.get_snapshot(connection, world_id).world.version != version:
                raise HTTPException(status_code=409, detail="世界状态已变化，请重新提交")
            now, request_id = to_iso(utc_now()), str(uuid4())
            event_id = _record_social_fact(
                connection, world_id=world_id, actor_id=player["id"], target_id=npc["id"],
                event_type="social.long_term_review", summary=response,
                payload={"request_id": request_id, "operation_type": payload.operation_type.strip(), "status": status}, importance=7,
            )
            connection.execute(
                """INSERT INTO long_term_operation_requests(
                    id, world_id, requester_id, recipient_id, operation_type, terms_json, status,
                    npc_response, system_notice, counter_terms_json, source_event_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    request_id, world_id, player["id"], npc["id"], payload.operation_type.strip(),
                    json.dumps(terms, ensure_ascii=False), status, response, system_notice,
                    json.dumps(counter_terms, ensure_ascii=False) if counter_terms else None,
                    event_id, now, now,
                ),
            )
            connection.execute("UPDATE worlds SET version = version + 1, updated_at = ? WHERE id = ?", (now, world_id))
            return {
                "id": request_id,
                "status": status,
                "npc_response": response,
                "system_notice": system_notice,
                "counter_terms": counter_terms,
            }

    @router.delete("/long-term-requests/{request_id}")
    def remove_long_term(world_id: str, request_id: str) -> dict[str, object]:
        """由提出事务的玩家主动结束，并删除尚未结束的事务痕迹。"""
        with database.write() as connection:
            request = connection.execute(
                "SELECT * FROM long_term_operation_requests WHERE id = ? AND world_id = ?",
                (request_id, world_id),
            ).fetchone()
            player = connection.execute(
                "SELECT id FROM characters WHERE world_id = ? AND is_player = 1", (world_id,)
            ).fetchone()
            if request is None:
                raise HTTPException(status_code=404, detail="长期事务不存在")
            contract = connection.execute("SELECT * FROM contract_fulfillments WHERE request_id=?", (request_id,)).fetchone()
            try:
                ContractService.cancel(connection, contract, _world_time(connection, world_id))
            except ContractError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if player is None or request["requester_id"] != player["id"]:
                raise HTTPException(status_code=403, detail="只有提出该事务的玩家可以结束它")

            # 事务在确认时可能生成待办与双方记忆；取消后它们不能继续作为世界中的有效约定。
            event_ids: list[str] = []
            for event in connection.execute(
                "SELECT id, payload_json FROM world_events WHERE world_id = ?", (world_id,)
            ).fetchall():
                try:
                    payload = json.loads(event["payload_json"])
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(payload, dict) and payload.get("request_id") == request_id:
                    event_ids.append(str(event["id"]))
            if event_ids:
                placeholders = ", ".join("?" for _ in event_ids)
                connection.execute(
                    f"DELETE FROM npc_todos WHERE world_id = ? AND source_event_id IN ({placeholders})",
                    (world_id, *event_ids),
                )
                # character_memories 会随 world_events 的外键级联删除。
                connection.execute(
                    f"DELETE FROM world_events WHERE id IN ({placeholders})", event_ids
                )
            connection.execute(
                "DELETE FROM long_term_operation_requests WHERE id = ? AND world_id = ?",
                (request_id, world_id),
            )
            now = to_iso(utc_now())
            connection.execute(
                "UPDATE worlds SET version = version + 1, updated_at = ? WHERE id = ?",
                (now, world_id),
            )
            return {"id": request_id, "status": "removed"}

    @router.post("/long-term-requests/{request_id}/confirm")
    def confirm_long_term(world_id: str, request_id: str, payload: LongTermConfirmRequest) -> dict[str, object]:
        with database.write() as connection:
            request = connection.execute(
                "SELECT * FROM long_term_operation_requests WHERE id = ? AND world_id = ?",
                (request_id, world_id),
            ).fetchone()
            if request is None:
                raise HTTPException(status_code=404, detail="长期事务不存在")
            if request["status"] not in {"npc_accepted", "npc_countered"}:
                raise HTTPException(status_code=409, detail="该事务当前不能确认")
            if request["status"] == "npc_countered" and not payload.accept_counter_terms:
                raise HTTPException(status_code=409, detail="请使用结束事务操作取消反提案")
            try:
                terms = json.loads(request["counter_terms_json"] if request["status"] == "npc_countered" else request["terms_json"])
            except (json.JSONDecodeError, TypeError) as exc:
                raise HTTPException(status_code=409, detail="事务条款已损坏，不能执行") from exc
            terms = _safe_terms(terms)
            try:
                payment = int(terms.get("payment", terms.get("amount", 0)))
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="报酬必须是非负整数") from exc
            if payment < 0 or payment > 1_000_000:
                raise HTTPException(status_code=400, detail="报酬超出可执行范围")
            player = connection.execute("SELECT * FROM characters WHERE id = ?", (request["requester_id"],)).fetchone()
            npc = connection.execute("SELECT * FROM characters WHERE id = ?", (request["recipient_id"],)).fetchone()
            if player is None or npc is None:
                raise HTTPException(status_code=409, detail="事务参与者已不存在")
            if KINDS.get(request["operation_type"], request["operation_type"]) == "learning" and int(player["money"]) < payment:
                raise HTTPException(status_code=409, detail="你的货币不足，事务没有执行")
            now = to_iso(utc_now())
            if payment and KINDS.get(request["operation_type"], request["operation_type"]) == "learning":
                connection.execute("UPDATE characters SET money = money - ?, updated_at = ? WHERE id = ?", (payment, now, player["id"]))
                connection.execute("UPDATE characters SET money = money + ?, updated_at = ? WHERE id = ?", (payment, now, npc["id"]))
            operation_type = request["operation_type"]
            try:
                due = ContractService.start(connection, request, terms, _world_time(connection, world_id))
            except ContractError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            todo_id = None
            if operation_type in {"委托", "雇佣", "约定", "commission", "employment", "appointment"}:
                todo_id = str(uuid4())
                title = str(terms.get("title") or f"履行与{player['name']}的{operation_type}")[:160]
                details = str(terms.get("details") or "由已确认的长期事务生成。")[:1000]
                connection.execute(
                    """INSERT INTO npc_todos(id, world_id, character_id, title, details, created_at, updated_at, contract_id, due_world_time)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (todo_id, world_id, npc["id"], title, details, now, now, request_id, due),
                )
            summary = f"{player['name']}与{npc['name']}确认了{operation_type}事务，按已约定的规则开始履约。"
            event_id = _record_social_fact(
                connection, world_id=world_id, actor_id=player["id"], target_id=npc["id"],
                event_type="social.long_term_applied", summary=summary,
                payload={"request_id": request_id, "payment": payment, "todo_id": todo_id, "terms": terms}, importance=8,
            )
            if operation_type in {"学习", "learning"}:
                skill = str(terms.get("skill") or "").strip()
                teacher_skills = set(json.loads(npc["skills_json"] or "[]"))
                if not skill or skill not in teacher_skills:
                    raise HTTPException(status_code=409, detail="NPC 不具备该技能，无法完成教学")
                skills = list(json.loads(player["skills_json"] or "[]"))
                if skill not in skills:
                    skills.append(skill)
                    connection.execute(
                        "UPDATE characters SET skills_json = ? WHERE id = ?",
                        (json.dumps(skills, ensure_ascii=False), player["id"]),
                    )
                CharacterGrowthService.gain_skill_proficiency(
                    connection,
                    character_id=player["id"],
                    world_id=world_id,
                    skill_name=skill,
                    amount=10,
                    event_id=event_id,
                )
            if todo_id:
                connection.execute("UPDATE npc_todos SET source_event_id = ? WHERE id = ?", (event_id, todo_id))
            connection.execute(
                "UPDATE long_term_operation_requests SET status = 'applied', source_event_id = ?, updated_at = ? WHERE id = ?",
                (event_id, now, request_id),
            )
            connection.execute("UPDATE worlds SET version = version + 1, updated_at = ? WHERE id = ?", (now, world_id))
            return {"id": request_id, "status": "applied", "event_id": event_id, "todo_id": todo_id, "payment": payment}

    @router.post("/long-term-requests/{request_id}/repay")
    def repay_long_term(world_id: str, request_id: str) -> dict[str, object]:
        with database.write() as connection:
            contract = connection.execute(
                "SELECT * FROM contract_fulfillments WHERE request_id=? AND world_id=?",
                (request_id, world_id),
            ).fetchone()
            if contract is None:
                raise HTTPException(status_code=404, detail="借贷事务不存在")
            try:
                ContractService.repay(connection, contract, _world_time(connection, world_id))
            except ContractError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            repository.bump_version(connection, world_id)
            return {"status": "completed"}

    return router
