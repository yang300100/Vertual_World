"""有限的动作与发言序列：每一步结果先落库，失败不伪造后续完成。"""

import json
import re
from datetime import timedelta
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from world_engine.actions import ActionService
from world_engine.bounded_calls import submit_call
from world_engine.domain import ActionProposal, ActionType
from world_engine.geo import great_circle_distance_km
from world_engine.interiors import InteriorService
from world_engine.life import LifeActivityError, LifeActivityService, parse_life_activity
from world_engine.player_activities import PlayerActivityService, native_action
from world_engine.proximity import VOICE_RADIUS_KM, same_room
from world_engine.repository import WorldRepository, from_iso, to_iso, utc_now


class SequenceStep(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: Literal["action", "speech"]
    text: str = Field(min_length=1, max_length=1000)
    target_character_id: str | None = None


class SequenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=8, max_length=100)
    steps: list[SequenceStep] = Field(min_length=2, max_length=6)
    target_character_id: str | None = None
    delivery: Literal["normal", "whisper", "shout"] = "normal"


def explicit_steps(text):
    matches = list(re.finditer(r"(?m)^\s*(动作|行动|说话|对话|发言)\s*[:：]\s*", text))
    if len(matches) < 2:
        return []
    if text[: matches[0].start()].strip():
        return []
    result = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end() : end].strip()
        if body:
            result.append(
                SequenceStep(kind="action" if match[1] in {"动作", "行动"} else "speech", text=body)
            )
    return result


def local_action(c, engine, wid, player, snapshot, text, tick_id):
    LifeActivityService.assert_available(c, player.id)
    actor = c.execute("SELECT * FROM characters WHERE id=?", (player.id,)).fetchone()
    interior = InteriorService.scene(c, actor)
    if text.strip() in {"起身", "站起来"}:
        if not actor["current_fixture_id"]:
            raise ValueError("当前没有坐在座位上")
        result = InteriorService.operate_fixture(
            c, actor, actor["current_fixture_id"], "stand", snapshot.world.current_time
        )
        return {"accepted": True, **result}
    if text.strip() in {"离开房间", "出门"}:
        if not actor["current_room_id"]:
            raise ValueError("当前不在房间里")
        result = InteriorService.door(
            c, actor, actor["current_room_id"], "exit", snapshot.world.current_time
        )
        return {"accepted": True, **result}
    choices = []
    for door in interior["doors"]:
        if door["name"] in text:
            choices.append(("door", door))
    for fixture in interior["fixtures"]:
        if fixture["name"] in text:
            choices.append(("fixture", fixture))
    if choices and any(
        word in text for word in ("进入", "离开", "打开", "关上", "关闭", "解锁", "锁上", "坐")
    ):
        if len(choices) != 1:
            raise ValueError("有多个可能的对象，请明确名称")
        kind, target = choices[0]
        operation = next(
            (
                op
                for word, op in (
                    ("进入", "enter"),
                    ("离开", "exit"),
                    ("解锁", "unlock"),
                    ("锁上", "lock"),
                    ("打开", "open"),
                    ("关上", "close"),
                    ("关闭", "close"),
                    ("坐", "sit"),
                )
                if word in text
            ),
            None,
        )
        fn = InteriorService.door if kind == "door" else InteriorService.operate_fixture
        return {
            "accepted": True,
            **fn(c, actor, target["id"], operation, snapshot.world.current_time),
        }
    kind = native_action(text)
    timed = parse_life_activity(text)
    if timed:
        kind = ActionType.REST if timed[0] == "rest" else ActionType.IDLE
        proposal = ActionProposal(
            actor_id=player.id,
            action=kind,
            reason=text,
            metadata={"life_activity": timed[0], "duration_minutes": timed[1]},
        )
    elif kind:
        proposal = engine.fallback_provider.plan_player_action(snapshot, player, text)
        if kind in {ActionType.REST, ActionType.WORK, ActionType.EAT}:
            proposal = ActionProposal(actor_id=player.id, action=kind, reason=text)
        if proposal.action != kind:
            raise ValueError("动作对象或目的地尚不明确")
    else:
        result, _ = PlayerActivityService().execute(
            c, snapshot=snapshot, player=player, npc=None, text=text, action_id=tick_id
        )
        return result.model_dump(mode="json")
    proposal.metadata.update({"input_kind": "action", "player_action_text": text})
    return (
        ActionService()
        .execute(
            c,
            world_id=wid,
            tick_id=tick_id,
            occurred_at=snapshot.world.current_time,
            proposal=proposal,
        )
        .model_dump(mode="json")
    )


def execute_sequence(engine, wid, request):
    canonical = request.model_dump_json(exclude={"request_id"})
    token = str(uuid4())
    now = utc_now()
    with engine.database.write() as c:
        snapshot = engine.repository.get_snapshot(c, wid)
        player = next((p for p in snapshot.characters if p.is_player), None)
        if player is None:
            raise ValueError("当前世界没有玩家")
        row = c.execute(
            "SELECT * FROM player_action_sequences WHERE world_id=? AND request_key=?",
            (wid, request.request_id),
        ).fetchone()
        if row:
            if row["payload_json"] != canonical:
                raise ValueError("序列标识已用于其他内容")
            if row["status"] == "completed":
                return {
                    "status": "completed",
                    "results": json.loads(row["results_json"]),
                    "id": row["id"],
                }
            if (
                row["status"] == "running"
                and row["claim_time"]
                and from_iso(row["claim_time"]) > now - timedelta(minutes=5)
            ):
                raise ValueError("该序列正在执行，请稍后读取结果")
            sid = row["id"]
        else:
            sid = str(uuid4())
            c.execute(
                "INSERT INTO player_action_sequences(id,world_id,player_id,request_key,payload_json,status) VALUES (?,?,?,?,?,'pending')",
                (sid, wid, player.id, request.request_id, canonical),
            )
        c.execute(
            "UPDATE player_action_sequences SET status='running',claim_token=?,claim_time=?,error=NULL WHERE id=?",
            (token, to_iso(now), sid),
        )
    while True:
        with engine.database.read() as c:
            row = c.execute("SELECT * FROM player_action_sequences WHERE id=?", (sid,)).fetchone()
            index = row["next_index"]
            if row["claim_token"] != token:
                raise ValueError("序列已由另一次请求接管")
            if index >= len(request.steps):
                return {
                    "id": sid,
                    "status": "completed",
                    "results": json.loads(row["results_json"]),
                }
            snapshot = engine.repository.get_snapshot(c, wid)
            player = snapshot.character_by_id(row["player_id"])
            step = request.steps[index]
        try:
            proposal = None
            if step.kind == "speech":
                with engine.database.read() as c:
                    LifeActivityService.assert_available(c, player.id, talking=True)
                    target = engine.conversations.resolve_target(
                        c,
                        snapshot=snapshot,
                        player=player,
                        intent="说话：" + step.text,
                        target_character_id=step.target_character_id or request.target_character_id,
                    )
                    if target is None:
                        nearby = [
                            p
                            for p in snapshot.characters
                            if not p.is_player
                            and same_room(player, p)
                            and great_circle_distance_km(
                                player.longitude, player.latitude, p.longitude, p.latitude
                            )
                            <= VOICE_RADIUS_KM[request.delivery]
                        ]
                        if len(nearby) != 1:
                            raise ValueError("这一步发言需要明确交谈对象")
                        target = nearby[0]
                    if (
                        great_circle_distance_km(
                            player.longitude, player.latitude, target.longitude, target.latitude
                        )
                        > VOICE_RADIUS_KM[request.delivery]
                    ):
                        raise ValueError("对方不在当前音量范围内")
                    context = engine.dialogue_context.build(
                        c,
                        snapshot=snapshot,
                        npc=target,
                        player=player,
                        player_text=step.text,
                        conversation=None,
                        channel="in_person",
                        interaction="承接已经执行的前序动作",
                        decision_details={"delivery": request.delivery},
                    )
                reply = submit_call(
                    engine.decision_provider.respond_to_player,
                    npc=target,
                    player=player,
                    context=context,
                ).result(timeout=engine.settings.deepseek_timeout_seconds)
                proposal = ActionProposal(
                    actor_id=player.id,
                    action=ActionType.SOCIALIZE,
                    target_id=target.id,
                    reason="按自己的意图继续交谈",
                    dialogue=step.text,
                    reply=reply.reply,
                    metadata={"delivery": request.delivery, "sequence_id": sid},
                )
            with engine.database.write() as c:
                current = c.execute(
                    "SELECT * FROM player_action_sequences WHERE id=?", (sid,)
                ).fetchone()
                if current["claim_token"] != token or current["next_index"] != index:
                    raise ValueError("序列状态已经变化")
                fresh = engine.repository.get_snapshot(c, wid)
                if proposal and fresh.world.version != snapshot.world.version:
                    raise ValueError("发言期间场景已变化，请继续时重新判断")
                player = fresh.character_by_id(player.id)
                if proposal:
                    outcome = ActionService().execute(
                        c,
                        world_id=wid,
                        tick_id=f"sequence:{sid}:{index}",
                        occurred_at=fresh.world.current_time,
                        proposal=proposal,
                    )
                    result = outcome.model_dump(mode="json")
                    if outcome.accepted:
                        engine.conversations.record_exchange(
                            c,
                            world_id=wid,
                            npc_id=proposal.target_id,
                            counterpart_id=player.id,
                            event_id=outcome.event_id,
                            world_time=fresh.world.current_time,
                            player_text=step.text,
                            npc_text=proposal.reply,
                        )
                else:
                    result = local_action(
                        c, engine, wid, player, fresh, step.text, f"sequence:{sid}:{index}"
                    )
                results = json.loads(current["results_json"])
                results.append({"step": index + 1, "kind": step.kind, "text": step.text, **result})
                status = "completed" if index + 1 == len(request.steps) else "running"
                if not result["accepted"]:
                    status = "failed"
                c.execute(
                    "UPDATE player_action_sequences SET next_index=?,results_json=?,status=?,error=? WHERE id=?",
                    (
                        index + 1 if result["accepted"] else index,
                        json.dumps(results, ensure_ascii=False),
                        status,
                        None if result["accepted"] else result["summary"],
                        sid,
                    ),
                )
                WorldRepository().bump_version(c, wid)
                if status != "running":
                    return {"id": sid, "status": status, "results": results}
        except Exception as exc:
            status = "waiting" if isinstance(exc, LifeActivityError) else "failed"
            with engine.database.write() as c:
                c.execute(
                    "UPDATE player_action_sequences SET status=?,error=? WHERE id=? AND claim_token=?",
                    (status, str(exc)[:500], sid, token),
                )
                results = json.loads(
                    c.execute(
                        "SELECT results_json FROM player_action_sequences WHERE id=?", (sid,)
                    ).fetchone()[0]
                )
            return {"id": sid, "status": status, "results": results, "error": str(exc)[:500]}
