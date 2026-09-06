"""玩家的观察与文书工作：保存实际产物，区分实测、草稿与未完成项。"""

from __future__ import annotations

import json
import re
from uuid import uuid4

from world_engine.actions import ActionService
from world_engine.domain import ActionOutcome, ActionType
from world_engine.geo import great_circle_distance_km
from world_engine.proximity import VISIBLE_PERSON_RADIUS_KM
from world_engine.repository import to_iso, utc_now


def native_action(text: str) -> ActionType | None:
    """只检查动作开头，不让“记录打斗”中的单字触发攻击。"""
    for words, kind in (
        ("攻击|袭击|砍向|砍杀|揍|教训", ActionType.ATTACK),
        ("捡起|拾起|拾取", ActionType.GATHER),
        ("使用|装备|卸下|服用|喝下|放下|丢下|用", ActionType.USE),
        ("前往|赶去|动身|去往", ActionType.TRAVEL),
        ("开始工作|去工作|干活|工作", ActionType.WORK),
        ("休息|睡觉|睡一觉|躺下休息", ActionType.REST),
        ("吃饭|进食|用饭|吃", ActionType.EAT),
    ):
        if re.match(rf"^(?:我)?(?:{words})", text):
            return kind
    return None


def activity_steps(text: str) -> list[tuple[str, str]]:
    steps = []
    if re.search(r"记录|记下|记载|测绘|勘察", text):
        if re.search(r"道路|路线|路况|路段", text):
            steps.append(("road_notes", "道路观察记录"))
        if re.search(r"星位|星象|星图", text):
            steps.append(("star_notes", "星位观察记录"))
        if not steps:
            steps.append(("field_notes", "现场观察笔记"))
    if re.search(r"誊清|誊写|抄写|抄录", text):
        steps.append(("map_copy" if "图" in text else "document_copy", "誊清稿"))
    if re.search(r"核对|校对|校核|比对", text):
        steps.append(("comparison", "核对清单"))
    if not steps and re.search(r"^(?:我)?(?:观察|查看|检查|调查|整理)", text):
        steps.append(("inspection", "现场检查记录"))
    return steps


class PlayerActivityService:
    @staticmethod
    def observer(connection, snapshot, player, target_id):
        candidates = []
        if target_id:
            candidates.append(target_id)
        candidates.extend(
            row[0]
            for row in connection.execute(
                """SELECT npc_character_id FROM npc_conversation_sessions
               WHERE world_id=? AND counterpart_character_id=? AND status='active'
               ORDER BY last_world_time DESC,updated_at DESC LIMIT 6""",
                (snapshot.world.id, player.id),
            )
        )
        for candidate in dict.fromkeys(candidates):
            npc = snapshot.character_by_id(candidate)
            if (
                npc
                and not npc.is_player
                and npc.health > 0
                and great_circle_distance_km(
                    player.longitude, player.latitude, npc.longitude, npc.latitude
                )
                <= VISIBLE_PERSON_RADIUS_KM
            ):
                return npc
            # 已明确选择的人物不在场时，其他 NPC 不接管私人会话。
            if target_id:
                break
        return None

    @staticmethod
    def request_context(connection, world_id, player_id, npc_id):
        if not npc_id:
            return None
        # 只使用该 NPC 对该玩家说过的原话，不能从他人的任务中抄取上下文。
        rows = connection.execute(
            """SELECT t.event_id,t.content FROM npc_conversation_turns t
               JOIN world_events e ON e.id=t.event_id WHERE t.world_id=?
               AND t.speaker_character_id=? AND t.listener_character_id=?
               AND e.event_type='action.socialize'
               ORDER BY t.world_time DESC,t.created_at DESC,t.turn_index DESC LIMIT 12""",
            (world_id, npc_id, player_id),
        ).fetchall()
        return next(
            (dict(row) for row in rows if activity_steps(row["content"])),
            dict(rows[0]) if rows else None,
        )

    def execute(self, connection, *, snapshot, player, npc, text, action_id):
        world_id = snapshot.world.id
        steps = activity_steps(text)
        request = self.request_context(connection, world_id, player.id, npc.id if npc else None)
        # 一句话不是全能指令：未覆盖的复合动作必须明确留作未执行。
        unsupported = [
            part.strip()
            for part in re.split(r"[，,；;。\n]", text)
            if part.strip() and not activity_steps(part.strip())
        ]
        moving = connection.execute(
            "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",
            (player.id,),
        ).fetchone()
        rejection = None
        if not steps:
            rejection = "这项行动尚无可执行规则，未改变人物、物品或任务状态。"
        elif player.energy < len(steps) * 2:
            rejection = "精力不足以完成这项工作，请先休息。"
        elif moving:
            rejection = "当前正在移动，请停下或抵达后再进行现场记录与文书工作。"
        event_id = ActionService._record_event(
            connection,
            world_id=world_id,
            tick_id=action_id,
            occurred_at=snapshot.world.current_time,
            event_type="action.rejected" if rejection else "action.activity",
            actor_id=player.id,
            target_id=npc.id if npc else None,
            location_id=player.current_location_id or player.location_id,
            summary=rejection or "玩家进行现场与文书工作。",
            payload={"input_kind": "action", "player_action_text": text},
        )
        progress = []
        if not rejection:
            location = snapshot.location_by_id(player.current_location_id or player.location_id)
            local_observation = {
                "location": location.name if location else "野外",
                "longitude": player.longitude,
                "latitude": player.latitude,
                "world_time": to_iso(snapshot.world.current_time),
                "visible_landmarks": [
                    feature.name
                    for feature in snapshot.map_features
                    if feature.is_known
                    and great_circle_distance_km(
                        player.longitude, player.latitude, feature.longitude, feature.latitude
                    )
                    <= VISIBLE_PERSON_RADIUS_KM
                ][:8],
            }
            maps = connection.execute(
                """SELECT i.id,t.name FROM item_instances i JOIN item_types t ON t.id=i.item_type_id
                   WHERE i.world_id=? AND i.container_id=?
                   AND i.container_type='character_inventory'
                   AND i.owner_character_id=? AND i.quantity>0
                   AND (t.name LIKE '%地图%' OR t.name LIKE '%旧图%'
                        OR t.category='map')""",
                (world_id, player.id, player.id),
            ).fetchall()
            source = [dict(item) for item in maps]
            for key, title in steps:
                status = "completed"
                detail = "已保存当前位置、时间和可见地标的现场笔记。"
                if key == "road_notes":
                    detail = "已建立当前路段的现场记录；尚未走过的路段不计入实测。"
                elif key == "star_notes":
                    status = "partial"
                    detail = "已建立观星记录页；当前没有可验证的星位测量数据，星位仍待实测。"
                elif key in {"map_copy", "document_copy"}:
                    status = "completed" if source and key == "map_copy" else "partial"
                    detail = (
                        "已誊清随身原图，并保留原图引用。"
                        if status == "completed"
                        else (
                            "已按现场笔记建立誊清草稿；原始图件或文书尚未提供，不能认定原文已完整誊写。"
                        )
                    )
                elif key == "comparison":
                    status = "partial"
                    detail = "已保存现场记录与誊清稿的核对清单；图面细节与未实测路段仍待逐项验证。"
                record_id = str(uuid4())
                content = {
                    "result": detail,
                    "observation": local_observation,
                    "source_items": source if key in {"map_copy", "comparison"} else [],
                    "request_text": request["content"][:800] if request else None,
                }
                connection.execute(
                    """INSERT INTO player_activity_records(id,world_id,player_id,npc_id,
                       source_event_id,request_event_id,step_key,title,status,content_json,
                       location_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        record_id,
                        world_id,
                        player.id,
                        npc.id if npc else None,
                        event_id,
                        request["event_id"] if request else None,
                        key,
                        title,
                        status,
                        json.dumps(content, ensure_ascii=False),
                        player.current_location_id or player.location_id,
                        to_iso(utc_now()),
                    ),
                )
                progress.append(
                    {
                        "id": record_id,
                        "step": key,
                        "title": title,
                        "status": status,
                        "result": detail,
                    }
                )
            connection.execute(
                "UPDATE characters SET energy=energy-?,updated_at=? WHERE id=?",
                (len(steps) * 2, to_iso(utc_now()), player.id),
            )
        expected = activity_steps(request["content"]) if request else []
        completed_keys = {step["step"] for step in progress if step["status"] == "completed"}
        if request:
            completed_keys.update(
                row[0]
                for row in connection.execute(
                    """SELECT step_key FROM player_activity_records WHERE player_id=?
                   AND request_event_id=? AND status='completed'""",
                    (player.id, request["event_id"]),
                )
            )
        remaining = [title for key, title in expected if key not in completed_keys]
        summary = rejection or "；".join(item["result"] for item in progress)
        if unsupported and not rejection:
            summary += "；其余未支持的动作尚未执行：" + "、".join(unsupported)
        payload = {
            "input_kind": "action",
            "player_action_text": text,
            "result": summary,
            "activity_progress": progress,
            "remaining_tasks": remaining,
            "request_event_id": request["event_id"] if request else None,
            "request_text": request["content"][:800] if request else None,
        }
        connection.execute(
            "UPDATE world_events SET summary=?,payload_json=? WHERE id=?",
            (summary, json.dumps(payload, ensure_ascii=False), event_id),
        )
        for cid in [player.id, *([npc.id] if npc else [])]:
            ActionService._record_memory(
                connection,
                world_id=world_id,
                character_id=cid,
                event_id=event_id,
                memory_type="experienced",
                summary=f"{player.name}的行动结果：{summary}",
                importance=6,
            )
        return ActionOutcome(
            accepted=not rejection,
            actor_id=player.id,
            action=ActionType.ACTIVITY,
            summary=summary,
            event_id=event_id,
            rejection_reason=rejection,
        ), progress

    @staticmethod
    def recent_records(connection, world_id, player_id, npc_id=None):
        rows = connection.execute(
            """SELECT id,source_event_id,request_event_id,step_key,title,status,content_json
               FROM player_activity_records WHERE world_id=? AND player_id=?
               AND (? IS NULL OR npc_id=?) ORDER BY created_at DESC,rowid DESC LIMIT 12""",
            (world_id, player_id, npc_id, npc_id),
        ).fetchall()
        return [{**dict(row), "content": json.loads(row["content_json"])} for row in rows]
