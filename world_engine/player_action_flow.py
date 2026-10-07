"""明确动作先结算，再让实际在场的 NPC 观察结果并回应。"""

import json
import logging
import math
import re
from threading import Lock, Thread
from uuid import uuid4

from world_engine.actions import ActionService
from world_engine.bounded_calls import submit_call
from world_engine.diagnostic_logging import diagnostic_json
from world_engine.domain import ActionOutcome, ActionProposal, ActionType, PlayerActionResult
from world_engine.geo import great_circle_distance_km
from world_engine.inventory import InventoryError, InventoryService
from world_engine.life import parse_life_activity
from world_engine.model_availability import is_model_billing_error
from world_engine.npc_performance import settle_npc_reply
from world_engine.player_activities import PlayerActivityService, native_action
from world_engine.player_inputs import approach_target_phrase, gift_target_phrase
from world_engine.proximity import VISIBLE_PERSON_RADIUS_KM, same_room
from world_engine.repository import WorldRepository, to_iso, utc_now

_reaction_lock = Lock()
_running_reactions: set[str] = set()
LOGGER = logging.getLogger("virtual-world.engine")


def execute_explicit_action(engine, world_id, text, target_id, *, defer_reaction=False):
    request_id = str(uuid4())
    LOGGER.info(
        "玩家明确行动输入 request_id=%s world_id=%s target_id=%s intent=%s",
        request_id,
        world_id,
        target_id,
        diagnostic_json(
            text,
            (engine.settings.deepseek_api_key, engine.settings.image_api_key),
        ),
    )
    if not text:
        raise ValueError("请在“动作：”后填写具体行动")
    approach_label = approach_target_phrase(text)
    if approach_label:
        result = execute_approach_action(engine, world_id, approach_label, target_id)
        _log_explicit_outcome(engine, request_id, world_id, result)
        return result
    gift = gift_target_phrase(text)
    if gift:
        result = execute_gift_action(
            engine, world_id, gift[0], gift[1], target_id,
            defer_reaction=defer_reaction,
        )
        _log_explicit_outcome(engine, request_id, world_id, result)
        return result
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
                proposal = ActionProposal(
                    actor_id=player.id, action=kind, target_id=target_id, reason=text[:500]
                )
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
            if npc and outcome.accepted and outcome.event_id and not connection.execute(
                "SELECT 1 FROM character_memories WHERE character_id=? AND event_id=? "
                "AND memory_type='experienced'",
                (npc.id, outcome.event_id),
            ).fetchone():
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
        if npc and outcome.accepted and outcome.event_id:
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
        LOGGER.info(
            "玩家明确行动提案 request_id=%s action_id=%s provider=rules action=%s "
            "target_id=%s reason=%s",
            request_id,
            action_id,
            proposal.action.value,
            proposal.target_id,
            diagnostic_json(
                proposal.reason,
                (engine.settings.deepseek_api_key, engine.settings.image_api_key),
            ),
        )
    # 模型慢或失败都不能回滚已经发生的动作，也不能诱导用户重复执行。
    has_reaction = bool(npc and outcome.accepted and outcome.event_id)
    reaction = (
        react_to_action(engine, world_id, outcome.event_id)
        if has_reaction and not defer_reaction else {}
    )
    with engine.database.read() as connection:
        version = engine.repository.get_snapshot(connection, world_id).world.version
    engine._sync_history_safely(world_id, background=True)
    if has_reaction and defer_reaction:
        schedule_action_reaction(engine, world_id, outcome.event_id)
    result = PlayerActionResult(
        world_id=world_id,
        action_id=action_id,
        started_at=started,
        completed_at=utc_now(),
        previous_version=snapshot.world.version,
        current_version=version,
        outcome=outcome,
        provider="rules",
        npc_reply=reaction.get("reply"),
        npc_stage_direction=reaction.get("stage_direction"),
        npc_reply_error=reaction.get("error"),
        npc_reply_pending=has_reaction and defer_reaction,
        activity_progress=progress,
    )
    _log_explicit_outcome(engine, request_id, world_id, result)
    return result


def _log_explicit_outcome(engine, request_id, world_id, result):
    LOGGER.info(
        "玩家明确行动结算 request_id=%s action_id=%s event_id=%s accepted=%s action=%s summary=%s",
        request_id,
        result.action_id,
        result.outcome.event_id,
        result.outcome.accepted,
        result.outcome.action.value,
        diagnostic_json(
            result.outcome.summary,
            (engine.settings.deepseek_api_key, engine.settings.image_api_key),
        ),
    )


def schedule_action_reaction(engine, world_id, event_id):
    """Yuki 让已保存的行动立刻返回，再在后台补齐 NPC 回应。"""
    with _reaction_lock:
        if event_id in _running_reactions:
            return
        _running_reactions.add(event_id)

    def run():
        try:
            result = react_to_action(engine, world_id, event_id)
            if result.get("error"):
                with engine.database.write() as connection:
                    connection.execute(
                        "UPDATE player_action_reactions SET status='failed',error_text=? "
                        "WHERE source_event_id=? AND world_id=? AND status='pending'",
                        (result["error"], event_id, world_id),
                    )
            engine._sync_history_safely(world_id, background=True)
        finally:
            with _reaction_lock:
                _running_reactions.discard(event_id)

    Thread(target=run, daemon=True, name="player-action-reaction").start()


def action_reaction_status(engine, world_id, event_id):
    with engine.database.read() as connection:
        job = connection.execute(
            "SELECT * FROM player_action_reactions WHERE source_event_id=? AND world_id=?",
            (event_id, world_id),
        ).fetchone()
        if job is None:
            return {"status": "missing", "error": "该行动没有在场的回应对象"}
        status = job["status"]
        if status == "ready":
            return {"status": "ready", **_saved_reply(connection, job)}
        if status == "failed":
            return {"status": "failed", "error": job["error_text"]}
    # 服务重启后尚未完成的回应，在下一次查询时继续生成。
    schedule_action_reaction(engine, world_id, event_id)
    return {"status": "pending"}


def execute_gift_action(
    engine, world_id, item_name, target_label, selected_target_id, *, defer_reaction=False,
):
    """Yuki 只把背包里真正拥有的一件物品交给近处的明确对象。"""
    from world_engine.society import SocietyService

    started = utc_now()
    action_id = str(uuid4())
    with engine.database.write() as connection:
        snapshot = engine.repository.get_snapshot(connection, world_id)
        player = next((person for person in snapshot.characters if person.is_player), None)
        if player is None:
            raise ValueError("当前世界还没有玩家角色")
        visible = [
            person for person in snapshot.characters
            if not person.is_player and person.health > 0 and same_room(player, person)
            and great_circle_distance_km(
                player.longitude, player.latitude, person.longitude, person.latitude,
            ) <= VISIBLE_PERSON_RADIUS_KM
        ]
        row_order = {
            row[0]: index for index, row in enumerate(connection.execute(
                "SELECT id FROM characters WHERE world_id=? AND is_player=0 AND health>0",
                (world_id,),
            ))
        }
        visible.sort(key=lambda person: row_order[person.id])
        matching = []
        for index, person in enumerate(visible, 1):
            known = SocietyService.label(connection, player.id, person.id)
            labels = {person.name, known}
            if known == "尚未认识的人":
                labels.add(f"陌生人{index}")
            if target_label in labels or (
                selected_target_id == person.id and target_label in {"他", "她", "对方", "你"}
            ):
                matching.append(person)
        if len(matching) != 1:
            raise ValueError("找不到唯一的赠物对象；请核对姓名或选择人物")
        target = matching[0]
        if selected_target_id and selected_target_id != target.id:
            raise ValueError("赠物对象与所选人物不一致")
        if great_circle_distance_km(
            player.longitude, player.latitude, target.longitude, target.latitude,
        ) > 0.003:
            raise ValueError("请先走到对方身边，3米内才能转交物品")
        item = InventoryService.find(connection, world_id, player.id, item_name)
        if not InventoryService.owned(item, player.id):
            raise ValueError(f"背包中没有你拥有的{item_name}")
        summary = f"你将{item_name}赠与{target.name}。"
        event_id = ActionService._record_event(
            connection,
            world_id=world_id,
            tick_id=action_id,
            occurred_at=snapshot.world.current_time,
            event_type="action.transfer",
            actor_id=player.id,
            target_id=target.id,
            location_id=player.current_location_id or player.location_id,
            summary=summary,
            payload={"item_name": item_name, "quantity": 1, "player_action_text": summary},
        )
        before = InventoryService.snapshot(connection, world_id)
        try:
            InventoryService.transfer(connection, item, target.id, 1)
        except InventoryError as exc:
            raise ValueError(str(exc)) from exc
        InventoryService.audit(connection, world_id, event_id, before)
        ActionService._record_memory(
            connection, world_id=world_id, character_id=target.id, event_id=event_id,
            memory_type="experienced", summary=summary, importance=5,
        )
        connection.execute(
            "INSERT INTO player_action_reactions(source_event_id,world_id,npc_id,status) "
            "VALUES (?,?,?,'pending')",
            (event_id, world_id, target.id),
        )
        connection.execute(
            "UPDATE worlds SET version=version+1,updated_at=? WHERE id=?",
            (to_iso(utc_now()), world_id),
        )
    reaction = {} if defer_reaction else react_to_action(engine, world_id, event_id)
    with engine.database.read() as connection:
        version = engine.repository.get_snapshot(connection, world_id).world.version
    engine._sync_history_safely(world_id, background=True)
    if defer_reaction:
        schedule_action_reaction(engine, world_id, event_id)
    return PlayerActionResult(
        world_id=world_id, action_id=action_id, started_at=started, completed_at=utc_now(),
        previous_version=snapshot.world.version, current_version=version,
        outcome=ActionOutcome(
            accepted=True, actor_id=player.id, action=ActionType.TRANSFER,
            summary=summary, event_id=event_id,
        ),
        provider="rules", npc_reply=reaction.get("reply"),
        npc_stage_direction=reaction.get("stage_direction"),
        npc_reply_error=reaction.get("error"), npc_reply_pending=defer_reaction,
    )


def execute_approach_action(engine, world_id, label, target_id):
    """Yuki 依玩家可见称呼找到人物，结算同一场景内的短距离走近。"""
    from world_engine.life import LifeActivityService
    from world_engine.society import SocietyService

    started = utc_now()
    action_id = str(uuid4())
    with engine.database.write() as connection:
        snapshot = engine.repository.get_snapshot(connection, world_id)
        player = next((item for item in snapshot.characters if item.is_player), None)
        if player is None:
            raise ValueError("当前世界还没有玩家角色")
        visible = [
            item for item in snapshot.characters
            if not item.is_player and item.health > 0 and same_room(player, item)
            and great_circle_distance_km(
                player.longitude, player.latitude, item.longitude, item.latitude,
            )
            <= VISIBLE_PERSON_RADIUS_KM
        ]
        # Yuki 沿用快照的展示顺序编号，让称呼与界面人物列表对应。
        labels = {}
        for index, item in enumerate(visible, 1):
            known = SocietyService.label(connection, player.id, item.id)
            labels[item.id] = f"陌生人{index}" if known == "尚未认识的人" else known
        target = next((item for item in visible if labels[item.id] == label), None)
        if target is None and target_id:
            target = next(
                (item for item in visible
                 if item.id == target_id and label in {"他", "她", "对方"}),
                None,
            )
        if target is None:
            raise ValueError("找不到这位可见人物；请核对称呼或先走近")
        if target_id and target_id != target.id:
            raise ValueError("行动中称呼的人物与所选对象不一致")
        distance = great_circle_distance_km(
            player.longitude, player.latitude, target.longitude, target.latitude
        )
        moved = distance > 0.003
        energy_cost = math.ceil(distance * 1000 / 25) if moved else 0
        if moved:
            LifeActivityService.assert_available(connection, player.id)
            if connection.execute(
                "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",
                (player.id,),
            ).fetchone():
                raise ValueError("请先取消当前行程，再走向附近人物")
            if player.health <= 0 or player.energy < energy_cost:
                raise ValueError("身体状态或精力不足以走近对方")
            # Yuki 只在同一空间的百米视野内结算这几步，并立刻写入真实坐标。
            connection.execute(
                "UPDATE characters SET longitude=?,latitude=?,location_id=?,current_location_id=?,"
                "energy=energy-?,updated_at=? WHERE id=?",
                (
                    target.longitude, target.latitude, target.location_id,
                    target.current_location_id, energy_cost, to_iso(started), player.id,
                ),
            )
        summary = (
            f"你走到{labels[target.id]}身边。" if moved
            else f"你已经在{labels[target.id]}身边。"
        )
        event_id = ActionService._record_event(
            connection,
            world_id=world_id,
            tick_id=action_id,
            occurred_at=snapshot.world.current_time,
            event_type="action.approach",
            actor_id=player.id,
            target_id=target.id,
            location_id=player.current_location_id or player.location_id,
            summary=summary,
            payload={
                "target_character_id": target.id,
                "distance_m": round(distance * 1000),
                "energy_cost": energy_cost,
                "origin_longitude": player.longitude,
                "origin_latitude": player.latitude,
                "destination_longitude": target.longitude,
                "destination_latitude": target.latitude,
            },
        )
        connection.execute(
            "UPDATE worlds SET version=version+1,updated_at=? WHERE id=?",
            (to_iso(utc_now()), world_id),
        )
        version = snapshot.world.version + 1
    engine._sync_history_safely(world_id, background=True)
    return PlayerActionResult(
        world_id=world_id,
        action_id=action_id,
        started_at=started,
        completed_at=utc_now(),
        previous_version=snapshot.world.version,
        current_version=version,
        outcome=ActionOutcome(
            accepted=True,
            actor_id=player.id,
            action=ActionType.TRAVEL,
            summary=summary,
            event_id=event_id,
        ),
        provider="rules",
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
        draft = (
            submit_call(
                engine.decision_provider.respond_to_player, npc=npc, player=player, context=context
            )
            .result(timeout=engine.settings.deepseek_timeout_seconds)
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
            current_snapshot = engine.repository.get_snapshot(connection, world_id)
            current_player = current_snapshot.character_by_id(player.id)
            current_npc = current_snapshot.character_by_id(npc.id)
            if (
                current_player is None or current_npc is None
                or not same_room(current_player, current_npc)
                or great_circle_distance_km(
                    current_player.longitude, current_player.latitude,
                    current_npc.longitude, current_npc.latitude,
                ) > VISIBLE_PERSON_RADIUS_KM
            ):
                return {"error": "行动已经保存，原对话对象目前不在场"}
            npc_actor = connection.execute(
                "SELECT * FROM characters WHERE id=? AND world_id=?", (npc.id, world_id)
            ).fetchone()
            settled = settle_npc_reply(
                connection,
                npc=npc_actor,
                player_id=player.id,
                at=current_snapshot.world.current_time,
                channel="action_observation",
                reply=draft,
            )
            LOGGER.info(
                "玩家行动回应结算 source_event_id=%s npc_id=%s action_status=%s "
                "action_event_id=%s",
                event_id,
                npc.id,
                settled.action_status or "no_action",
                settled.action_event_id,
            )
            reply = settled.reply
            reaction_id = ActionService._record_event(
                connection,
                world_id=world_id,
                tick_id=f"reaction:{event_id}",
                occurred_at=current_snapshot.world.current_time,
                event_type="action.reaction",
                actor_id=npc.id,
                target_id=player.id,
                location_id=npc.current_location_id or npc.location_id,
                summary=f"{npc.name}对你的行动作出回应：{reply}",
                payload={
                    "source_action_event_id": event_id,
                    "reply": reply,
                    "stage_direction": settled.stage_direction,
                    "npc_action_status": settled.action_status,
                    "npc_action_event_id": settled.action_event_id,
                    "input_kind": "action_reaction",
                },
            )
            engine.conversations.record_exchange(
                connection,
                world_id=world_id,
                npc_id=npc.id,
                counterpart_id=player.id,
                event_id=reaction_id,
                world_time=current_snapshot.world.current_time,
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
            WorldRepository().bump_version(connection, world_id)
        return {
            "reply": reply,
            "stage_direction": settled.stage_direction,
            "event_id": reaction_id,
        }
    except Exception as exc:
        LOGGER.warning(
            "玩家行动回应失败 source_event_id=%s error_type=%s",
            event_id,
            type(exc).__name__,
        )
        if is_model_billing_error(exc):
            error = (
                "行动结果已经保存；DeepSeek 账户余额或额度不足，"
                "请检查账户后单独重试 NPC 回应，无需重做动作"
            )
        else:
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
        {
            "reply": json.loads(row[0])["reply"],
            "stage_direction": json.loads(row[0]).get("stage_direction"),
            "event_id": job["reaction_event_id"],
        } if row else {}
    )
