"""明确动作先结算，再让实际在场的 NPC 观察结果并回应。"""

import json
import re
from uuid import uuid4

from world_engine.actions import ActionService
from world_engine.bounded_calls import submit_call
from world_engine.domain import ActionProposal, ActionType, PlayerActionResult
from world_engine.geo import great_circle_distance_km
from world_engine.life import parse_life_activity
from world_engine.player_activities import PlayerActivityService, native_action
from world_engine.proximity import VISIBLE_PERSON_RADIUS_KM, same_room
from world_engine.repository import to_iso, utc_now


def execute_explicit_action(engine, world_id, text, target_id):
    if not text:
        raise ValueError("请在“动作：”后填写具体行动")
    started = utc_now()
    action_id = str(uuid4())
    service = PlayerActivityService()
    proposal = None
    with engine.database.write() as connection:
        snapshot = engine.repository.get_snapshot(connection, world_id)
        player = next((item for item in snapshot.characters if item.is_player), None)
        if player is None:
            raise ValueError("当前世界还没有玩家角色")
        npc = service.observer(connection, snapshot, player, target_id)
        kind = native_action(text)
        map_record_id = None
        if re.fullmatch(r"(?:我)?核对(?:现场)?地图[。！!]?", text.strip()):
            record = connection.execute(
                "SELECT id FROM player_activity_records WHERE world_id=? AND player_id=? "
                "AND location_id=? AND step_key IN ('road_notes','field_notes') "
                "AND status='completed' ORDER BY created_at DESC LIMIT 1",
                (world_id, player.id, player.current_location_id or player.location_id),
            ).fetchone()
            if record is None:
                raise ValueError("请先在当前地点留下真实的道路或现场观测记录")
            map_record_id = record["id"]
            kind = ActionType.ACTIVITY
        life_request = parse_life_activity(text)
        if life_request:
            kind = ActionType.REST if life_request[0] == "rest" else ActionType.IDLE
        progress = []
        if kind is None:
            outcome, progress = service.execute(
                connection,
                snapshot=snapshot,
                player=player,
                npc=npc,
                text=text,
                action_id=action_id,
            )
        else:
            if map_record_id:
                proposal = ActionProposal(
                    actor_id=player.id, action=ActionType.ACTIVITY, reason=text[:500],
                    metadata={"map_record_id": map_record_id},
                )
            elif life_request:
                proposal = ActionProposal(
                    actor_id=player.id, action=kind, reason=text[:500],
                    metadata={
                        "life_activity": life_request[0], "duration_minutes": life_request[1],
                    },
                )
            elif kind in {ActionType.REST, ActionType.WORK, ActionType.EAT}:
                proposal = ActionProposal(actor_id=player.id, action=kind, reason=text[:500])
            elif kind is ActionType.ATTACK and target_id:
                proposal = ActionProposal(actor_id=player.id, action=kind, target_id=target_id, reason=text[:500])
            else:
                proposal = engine.fallback_provider.plan_player_action(snapshot, player, text)
            if proposal.action != kind:
                raise ValueError("无法确定该动作的对象或目的地，请补充明确名称")
            proposal.dialogue = proposal.reply = None
            proposal.metadata.update({"input_kind": "action", "player_action_text": text})
            outcome = engine.actions.execute(
                connection,
                world_id=world_id,
                tick_id=action_id,
                occurred_at=snapshot.world.current_time,
                proposal=proposal,
            )
            if npc and outcome.event_id:
                ActionService._record_memory(
                    connection,
                    world_id=world_id,
                    character_id=npc.id,
                    event_id=outcome.event_id,
                    memory_type="experienced",
                    summary=f"{player.name}的行动结果：{outcome.summary}",
                    importance=5,
                )
            health = connection.execute(
                "SELECT health FROM characters WHERE id=?", (player.id,)
            ).fetchone()[0]
            if health <= 0:
                connection.execute("UPDATE characters SET health=60 WHERE id=?", (player.id,))
                ActionService._record_event(
                    connection,
                    world_id=world_id,
                    tick_id=action_id,
                    occurred_at=snapshot.world.current_time,
                    event_type="world.player_defeat",
                    actor_id=player.id,
                    target_id=None,
                    location_id=player.location_id,
                    summary="你被击倒，昏倒在地，许久后才在原地苏醒。",
                    payload={"source_action_event_id": outcome.event_id},
                )
        if npc and outcome.event_id:
            connection.execute(
                """INSERT INTO player_action_reactions(source_event_id,world_id,npc_id,status)
                   VALUES (?,?,?,'pending')""",
                (outcome.event_id, world_id, npc.id),
            )
        connection.execute(
            "UPDATE worlds SET version=version+1,updated_at=? WHERE id=?",
            (to_iso(utc_now()), world_id),
        )
    if proposal is not None:
        engine._tombstone_destroyed_targets(world_id, [(outcome, proposal)])
    # 模型慢或失败都不能回滚已经发生的动作，也不能诱导用户重复执行。
    reaction = react_to_action(engine, world_id, outcome.event_id) if npc else {}
    with engine.database.read() as connection:
        version = engine.repository.get_snapshot(connection, world_id).world.version
    engine._sync_history_safely(world_id)
    return PlayerActionResult(
        world_id=world_id,
        action_id=action_id,
        started_at=started,
        completed_at=utc_now(),
        previous_version=snapshot.world.version,
        current_version=version,
        outcome=outcome,
        provider="rules",
        npc_reply=reaction.get("reply"),
        npc_reply_error=reaction.get("error"),
        activity_progress=progress,
    )


def react_to_action(engine, world_id, event_id):
    try:
        with engine.database.read() as connection:
            job = connection.execute(
                "SELECT * FROM player_action_reactions WHERE source_event_id=? AND world_id=?",
                (event_id, world_id),
            ).fetchone()
            if job is None:
                return {"error": "该行动没有在场的回应对象"}
            if job["status"] == "ready":
                return _saved_reply(connection, job)
            event = connection.execute(
                "SELECT * FROM world_events WHERE id=?", (event_id,)
            ).fetchone()
            if event is None:
                return {"error": "行动记录已不存在"}
            snapshot = engine.repository.get_snapshot(connection, world_id)
            player = snapshot.character_by_id(event["actor_id"])
            npc = snapshot.character_by_id(job["npc_id"])
            if (
                player is None
                or npc is None
                or not player.is_player
                or not same_room(player, npc)
                or great_circle_distance_km(
                    player.longitude, player.latitude, npc.longitude, npc.latitude
                )
                > VISIBLE_PERSON_RADIUS_KM
            ):
                return {"error": "行动已经保存，原对话对象目前不在场"}
            payload = json.loads(event["payload_json"])
            conversation = engine.conversations.load_context(
                connection,
                snapshot=snapshot,
                player_id=player.id,
                npc=npc,
            )
            context = engine.dialogue_context.build(
                connection,
                snapshot=snapshot,
                npc=npc,
                player=player,
                player_text="",
                conversation=conversation,
                channel="action_observation",
                interaction="玩家正在行动而不是发言；只根据已结算结果回应，指出具体进展和下一步",
                decision_details={
                    "input_kind": "action",
                    "source_event_id": event_id,
                    "settled_action": event["summary"],
                    "requested_action": payload.get("player_action_text")
                    or payload.get("metadata", {}).get("player_action_text"),
                    "activity_progress": payload.get("activity_progress", []),
                    "remaining_tasks": payload.get("remaining_tasks", []),
                    "original_request": payload.get("request_text"),
                    "accepted": event["event_type"] != "action.rejected",
                },
            )
        reply = (
            submit_call(
                engine.decision_provider.respond_to_player, npc=npc, player=player, context=context
            )
            .result(timeout=engine.settings.deepseek_timeout_seconds)
            .reply
        )
        with engine.database.write() as connection:
            current = connection.execute(
                "SELECT * FROM player_action_reactions WHERE source_event_id=? AND world_id=?",
                (event_id, world_id),
            ).fetchone()
            if current is None:
                return {"error": "行动记录已不存在"}
            if current["status"] == "ready":
                return _saved_reply(connection, current)
            if (
                engine.repository.get_snapshot(connection, world_id).world.version
                != snapshot.world.version
            ):
                return {"error": "行动已经保存，场景已变化，可重试 NPC 回应"}
            reaction_id = ActionService._record_event(
                connection,
                world_id=world_id,
                tick_id=f"reaction:{event_id}",
                occurred_at=snapshot.world.current_time,
                event_type="action.reaction",
                actor_id=npc.id,
                target_id=player.id,
                location_id=npc.current_location_id or npc.location_id,
                summary=f"{npc.name}对你的行动作出回应：{reply}",
                payload={
                    "source_action_event_id": event_id,
                    "reply": reply,
                    "input_kind": "action_reaction",
                },
            )
            engine.conversations.record_exchange(
                connection,
                world_id=world_id,
                npc_id=npc.id,
                counterpart_id=player.id,
                event_id=reaction_id,
                world_time=snapshot.world.current_time,
                player_text=f"【行动结果】{event['summary']}",
                npc_text=reply,
                player_message_kind="action",
            )
            ActionService._record_memory(
                connection,
                world_id=world_id,
                character_id=player.id,
                event_id=reaction_id,
                memory_type="experienced",
                summary=f"{npc.name}对我的行动回应：{reply}",
                importance=6,
            )
            connection.execute(
                """UPDATE player_action_reactions
                   SET status='ready',reaction_event_id=?,error_text=NULL
                   WHERE source_event_id=?""",
                (reaction_id, event_id),
            )
            connection.execute("UPDATE worlds SET version=version+1 WHERE id=?", (world_id,))
        return {"reply": reply, "event_id": reaction_id}
    except Exception:
        error = "行动结果已经保存；NPC 回应暂时不可用，可单独重试回应，无需重做动作"
        with engine.database.write() as connection:
            connection.execute(
                """UPDATE player_action_reactions SET status='failed',error_text=?
                   WHERE world_id=? AND source_event_id=? AND status!='ready'""",
                (error, world_id, event_id),
            )
        return {"error": error}


def _saved_reply(connection, job):
    row = connection.execute(
        "SELECT payload_json FROM world_events WHERE id=?", (job["reaction_event_id"],)
    ).fetchone()
    return (
        {"reply": json.loads(row[0])["reply"], "event_id": job["reaction_event_id"]} if row else {}
    )
