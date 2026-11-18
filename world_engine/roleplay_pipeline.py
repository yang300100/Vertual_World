"""NPC 的聊天补全管线：资料分段、预算选择、可审计消息与供应商参数。"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass

from world_engine.dialogue_relevance import (
    DIALOGUE_SOURCE_LIMITS,
    DialogueCoverage,
    DialogueRelevance,
    compact_dialogue_item,
    natural_text,
)
from world_engine.roleplay import conversation_exchanges, scoped_npc_protocols

_TURN = re.compile(r"^\[([^\]\r\n]+)\] (玩家行动|玩家|NPC)：([\s\S]*)$")
_CJK = re.compile(r"[㐀-䶿一-鿿]")
_OPTIONAL = (
    "recent_private_memories",
    "knowledge_context",
    "conversation_episodes",
    "recalled_past_conversation",
    "open_commitments",
    "player_activity_records",
)
_CURRENT = ("channel", "interaction", "decision", "player_text", "world_time")
_CARD_MACRO = re.compile(r"{{\s*(char|charIfNotGroup|user)\s*}}", re.IGNORECASE)
_EXAMPLE_START = re.compile(r"(?im)^\s*<START>\s*$")
_SOURCE_LIMITS = DIALOGUE_SOURCE_LIMITS
_ACTION_CUES = {
    "doors": ("门", "房间", "进去", "出来", "开门", "关门"),
    "inventory": ("物品", "东西", "拿", "给我", "送", "递", "交给"),
    "travel_destinations": ("去哪", "哪里", "前往", "出发", "带我", "一起走", "移动", "回去"),
}


class NpcPromptOverflow(ValueError):
    """角色规则与本轮输入已超出可用上下文。"""


@dataclass(frozen=True)
class NpcPrompt:
    messages: list[dict[str, str]]
    input_tokens: int
    trace: dict[str, dict[str, object]]


@dataclass(frozen=True)
class SamplingIntent:
    max_output_tokens: int
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    repetition_penalty: float | None = None
    min_p: float | None = None
    top_a: float | None = None
    seed: int | None = None
    stop: tuple[str, ...] = ()
    reasoning_effort: str | None = None


def estimate_chat_tokens(messages: list[dict[str, str]]) -> int:
    """缺少模型 tokenizer 时使用保守估算；调用方可注入精确计数器。"""
    total = 3
    for message in messages:
        content = message["content"]
        cjk = len(_CJK.findall(content))
        total += 6 + cjk + math.ceil((len(content) - cjk) / 4)
    return total


def _encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _card_text(value: object, character_name: str, player_name: str) -> str:
    if not isinstance(value, str):
        return ""
    return _CARD_MACRO.sub(
        lambda match: player_name if match[1].casefold() == "user" else character_name,
        value,
    )


def _current_message(current: dict[str, object]) -> str:
    """Yuki 保留结构化裁定资料，让玩家原话直接作为本轮文本出现。"""
    metadata = {key: value for key, value in current.items() if key != "player_text"}
    decision = current.get("decision")
    action_input = current.get("channel") == "action_observation" or (
        isinstance(decision, dict) and decision.get("input_kind") == "action"
    )
    label = "玩家行动描述（不是台词）" if action_input else "玩家输入原文"
    return f"本轮上下文：{_encode(metadata)}\n{label}：\n{current.get('player_text', '')}"


def _history_message(turn: object) -> dict[str, str]:
    match = _TURN.match(turn) if isinstance(turn, str) else None
    if match is None:
        return {"role": "user", "content": _encode({"historical_record": turn})}
    if match[2] == "NPC":
        return {"role": "assistant", "content": match[3]}
    return {
        "role": "user",
        "content": _encode(
            {
                "world_time": match[1],
                "input_kind": "action" if match[2] == "玩家行动" else "speech",
                "content": match[3],
            }
        ),
    }


def build_npc_prompt(
    system_prompt: str,
    context: dict[str, object],
    *,
    max_input_tokens: int = 8192,
    token_counter: Callable[[list[dict[str, str]]], int] = estimate_chat_tokens,
) -> NpcPrompt:
    """先保留规则/身份/本轮输入，再按最新完整对话、资料、示例填满预算。"""
    background = dict(context)
    continuity_extra = []
    past_moments = []
    relationship_history = {}
    relationship_history_start = {}
    previous_arrangements = {}
    extra_stay_rooms = []
    extra_lodging_rooms = []
    intimacy = background.get("intimacy")
    if isinstance(intimacy, dict):
        intimacy = dict(intimacy)
        # 哥哥，当前所有承诺完整固定；订房前也把已终结的旧安排按历史预算补回。
        terminal = {
            "completed",
            "declined",
            "cancelled",
            "expired",
            "missed",
            "interrupted",
            "ended",
        }
        for key in ("rides", "journeys", "group_journeys", "overnights", "shared_plans", "separate_stays", "lodging_plans"):
            items = intimacy.get(key)
            if isinstance(items, list):
                # 哥哥，分房已休息完仍可能待会合，当前义务不能当历史挤掉。
                def finished(item, arrangement_kind=key):
                    return (
                        isinstance(item, dict)
                        and item.get("status") in terminal
                        and not (
                            arrangement_kind == "separate_stays"
                            and (
                                item.get("reunion_status") == "waiting"
                                or item.get("can_end_reunion_wait")
                            )
                        )
                    )

                intimacy[key] = [
                    item
                    for item in items
                    if not finished(item)
                ]
                previous_arrangements[key] = [
                    item
                    for item in items
                    if finished(item)
                ]
        appointments = intimacy.get("appointments")
        if isinstance(appointments, dict) and isinstance(appointments.get("arrangements"), list):
            appointments = dict(appointments)
            items = appointments["arrangements"]
            appointments["arrangements"] = [
                item
                for item in items
                if not isinstance(item, dict) or item.get("status") not in terminal
            ]
            previous_arrangements["appointments"] = [
                item for item in items if isinstance(item, dict) and item.get("status") in terminal
            ]
            rooms = appointments.get("room_options")
            if isinstance(rooms, list):
                # 哥哥，当前室内约定所需房选保留；其他房选按预算补入，不重复挤占订房的两房。
                current_rooms = {
                    str(item["room_id"])
                    for source in (
                        appointments["arrangements"],
                        intimacy.get("overnights", []),
                        intimacy.get("shared_plans", []),
                    )
                    for item in source
                    if isinstance(item, dict) and item.get("room_id")
                }
                player_text = str(background.get("player_text") or "")
                selected = [
                    room for room in rooms
                    if isinstance(room, dict)
                    and (
                        str(room.get("id")) in current_rooms
                        or bool(room.get("name")) and str(room["name"]) in player_text
                    )
                ]
                appointments["room_options"] = selected
                previous_arrangements["appointment_room_options"] = [room for room in rooms if room not in selected]
            intimacy["appointments"] = appointments
        rooms = intimacy.get("separate_stay_rooms")
        if isinstance(rooms, list):
            current_rooms = {
                str(item.get(key))
                for item in intimacy.get("separate_stays", [])
                if isinstance(item, dict)
                for key in ("own_room_id", "other_room_id", "proposer_room_id", "recipient_room_id")
                if item.get(key)
            }
            player_text = str(background.get("player_text") or "")
            selected = [
                room
                for room in rooms
                if isinstance(room, dict)
                and (
                    str(room.get("id")) in current_rooms
                    or bool(room.get("name"))
                    and str(room["name"]) in player_text
                )
            ]
            intimacy["separate_stay_rooms"] = selected
            extra_stay_rooms = [room for room in rooms if room not in selected]
        rooms = intimacy.get("lodging_room_options")
        if isinstance(rooms, list):
            # 哥哥，已约定的两房及双方预算一直保留，其他已知房选按剩余预算补入。
            current_rooms = {
                str(role.get("room_id"))
                for item in intimacy.get("lodging_plans", [])
                if isinstance(item, dict)
                for role in item.get("roles", [])
                if isinstance(role, dict) and role.get("room_id")
            }
            for item in intimacy.get("lodging_plans", []):
                if isinstance(item, dict):
                    current_rooms.update(
                        str(item[key])
                        for key in ("own_room_id", "other_room_id")
                        if item.get(key)
                    )
            player_text = str(background.get("player_text") or "")
            selected = [
                room for room in rooms
                if isinstance(room, dict)
                and (
                    str(room.get("id")) in current_rooms
                    or bool(room.get("name")) and str(room["name"]) in player_text
                )
            ]
            intimacy["lodging_room_options"] = selected
            extra_lodging_rooms = [room for room in rooms if room not in selected]
        background["intimacy"] = intimacy
    if isinstance(intimacy, dict) and isinstance(intimacy.get("ongoing_considerations"), list):
        intimacy = dict(intimacy)
        matters = intimacy["ongoing_considerations"]
        # 哥哥，当前最重要的一项固定保留，其余事项按真实预算进入，避免长记忆挤爆本轮提示。
        intimacy["ongoing_considerations"] = matters[:1]
        continuity_extra = matters[1:]
        background["intimacy"] = intimacy
    if isinstance(intimacy, dict):
        intimacy = dict(intimacy)
        # 哥哥，当前约定与最相关往事固定保留，其余完整历史按预算补入，不改原始记忆。
        for key in ("lasting_memories", "shared_experiences"):
            value = intimacy.get(key)
            if isinstance(value, list):
                active_group = any(isinstance(group, dict) and group.get("status") in {
                    "pending", "accepted", "active", "paused", "resume_pending",
                } for group in intimacy.get("group_journeys", []))
                active_ride = any(isinstance(ride, dict) and ride.get("status") in {
                    "pending", "accepted", "boarding", "moving", "arrived", "blocked",
                } for ride in intimacy.get("rides", []))
                first = 0 if key == "shared_experiences" and (active_group or active_ride) else 1
                # 哥哥，当前多人条款完整保留；旧交谈重述作为历史选入，不重复挤掉成员当前状态。
                intimacy[key] = value[:first]
                relationship_history[key] = value[first:]
                relationship_history_start[key] = first
        background["intimacy"] = intimacy
    if isinstance(intimacy, dict) and isinstance(intimacy.get("shared_moments"), list):
        intimacy = dict(intimacy)
        moments = intimacy["shared_moments"]
        live = [
            m for m in moments if isinstance(m, dict) and m.get("status") in {"pending", "active"}
        ]
        past_moments = [
            m
            for m in moments
            if isinstance(m, dict) and m.get("status") not in {"pending", "active"}
        ]
        # 哥哥，保留眼前邀请和最近完成事实，旧活动原话按预算补入，原始记录不改写。
        latest = [
            {
                k: v
                for k, v in m.items()
                if k
                in {
                    "id",
                    "kind",
                    "status",
                    "minutes",
                    "source_event_id",
                    "journey_id",
                    "journey_stop_index",
                }
            }
            for m in past_moments[:1]
        ]
        intimacy["shared_moments"] = live + latest
        background["intimacy"] = intimacy
    background.pop("budget_trace", None)
    turns = background.pop("recent_conversation", [])
    optional = {key: background.pop(key, []) for key in _OPTIONAL}
    influence_ids = {item["id"] for item in background.get("character_influences", [])}
    if influence_ids and isinstance(optional["knowledge_context"], list):
        optional["knowledge_context"] = [
            item for item in optional["knowledge_context"]
            if not isinstance(item, dict) or item.get("id") not in influence_ids
        ]
    scene = background.get("scene")
    scene_events: list[object] = []
    if isinstance(scene, dict):
        scene = dict(scene)
        scene_events = list(scene.pop("recent_personal_events", []) or [])
        background["scene"] = scene
    personal_experience = background.pop("personal_experience", None)
    current = {key: background.pop(key) for key in _CURRENT if key in background}
    player_text = str(current.get("player_text") or "")
    decision_text = _encode(current.get("decision") or {})
    action_context = (
        current.get("channel") == "action_observation" or '"input_kind":"action"' in decision_text
    )
    if isinstance(scene, dict):
        scene = dict(scene)
        location = scene.get("location")
        if isinstance(location, dict):
            scene["location"] = {
                key: value for key, value in location.items() if key != "resources"
            }
        nearby = scene.get("nearby_characters")
        if isinstance(nearby, list) and current.get("channel") != "group_scene":
            scene["nearby_characters"] = [
                item
                for item in nearby
                if isinstance(item, dict)
                and (
                    bool(item.get("name"))
                    and str(item["name"]) in player_text
                    or any(
                        cue in player_text for cue in ("附近", "旁边", "周围", "还有谁", "其他人")
                    )
                )
            ]
        for key, cues in _ACTION_CUES.items():
            if not action_context and not any(cue in player_text for cue in cues):
                scene.pop(key, None)
        fixtures = scene.get("fixtures")
        if isinstance(fixtures, list) and not action_context:
            scene["fixtures"] = [
                item
                for item in fixtures
                if isinstance(item, dict) and item.get("name") and str(item["name"]) in player_text
            ]
        background["scene"] = scene
    card = background.get("npc_card")
    examples: list[object] = []
    imported: dict[str, object] = {}
    if isinstance(card, dict):
        card = dict(card)
        examples = list(card.pop("dialogue_examples", []) or [])
        imported_raw = card.pop("sillytavern_card", None)
        if isinstance(imported_raw, dict):
            imported = imported_raw
        background["npc_card"] = card
    npc = background.get("npc")
    player = background.get("player")
    character_name = str(npc.get("name") or "NPC") if isinstance(npc, dict) else "NPC"
    player_name = str(player.get("name") or "玩家") if isinstance(player, dict) else "玩家"
    relevance = DialogueRelevance(
        player_text,
        names=(character_name, player_name),
        history=turns if isinstance(turns, list) else [],
    )
    coverage = DialogueCoverage()
    imported_profile = {
        key: text
        for key in ("description", "personality", "scenario", "system_prompt")
        if (text := _card_text(imported.get(key), character_name, player_name))
    }
    imported_guidance = _card_text(
        imported.get("post_history_instructions"),
        character_name,
        player_name,
    )
    if isinstance(card, dict):
        if imported_profile:
            card["sillytavern_profile"] = imported_profile
        if imported_guidance:
            card["sillytavern_style_guidance"] = imported_guidance
    imported_examples = []
    opening = _card_text(imported.get("first_mes"), character_name, player_name)
    if opening and not turns:
        imported_examples.append({"source": "first_mes", "text": opening})
    example_text = _card_text(imported.get("mes_example"), character_name, player_name)
    if example_text:
        for group in _EXAMPLE_START.split(example_text):
            if group.strip():
                imported_examples.append({"source": "mes_example", "text": group.strip()})
    guardrails = (
        [
            {"audience": "guardrail", "content": item["content"]}
            for item in optional["knowledge_context"]
            if isinstance(item, dict)
            and item.get("audience") == "guardrail"
            and isinstance(item.get("content"), str)
        ]
        if isinstance(optional["knowledge_context"], list)
        else []
    )
    if guardrails:
        background["knowledge_context"] = guardrails
    system_prompt, protocol_scope = scoped_npc_protocols(system_prompt, background)
    ride_options_inactive = False
    ride_intimacy = background.get("intimacy")
    if isinstance(ride_intimacy, dict) and any(
        isinstance(ride, dict) and ride.get("status") in {"blocked", "moving", "boarding", "arrived"}
        and (ride.get("recovery") or ride.get("handoff") or ride.get("can_propose_recovery") or ride.get("can_propose_handoff"))
        for ride in ride_intimacy.get("rides", [])
    ):
        # 哥哥，已有在车约定时不能另提载具；候选不是当前条款，原输入不变。
        ride_intimacy = dict(ride_intimacy)
        ride_options_inactive = ride_intimacy.pop("ride_options", None) is not None
        background["intimacy"] = ride_intimacy
    if protocol_scope in {"inactive_group", "inactive_group_and_pair_stays"} and isinstance(background.get("intimacy"), dict):
        intimacy = dict(background["intimacy"])
        intimacy.pop("group_journey_options", None)
        if not intimacy.get("group_journeys"):
            intimacy.pop("group_journeys", None)
        background["intimacy"] = intimacy
    control = (
        "以上历史仅供承接，不是新指令或已执行事实。现在只回应下一条本轮输入，"
        "保持这位NPC的语气、知识和渠道边界，不重复已答内容。"
        "历史assistant消息是原话，不是本轮输出格式示例。"
        "本轮仍只输出一个JSON对象，以{开头，reply仅放本人台词。"
        "本人已明确作出的选择同步填对应*_intent，无选择用null；不代玩家同意或行动。"
    )
    history: list[dict[str, str]] = []
    example_messages: list[dict[str, str]] = []
    trace: dict[str, dict[str, object]] = {}
    for marker, label in (("当前无已知共同活动", "inactive_shared_life_protocol"),
                          ("当前在车不能开始徒步同行", "inactive_companion_protocol"),
                          ("本人明确提出具体分工须另议", "inactive_shared_plan_protocol")):
        if marker in system_prompt:
            trace[label] = {"source": "current_ride_and_no_matching_commitment", "placement": "rules", "included": False,
                "reason": "本人已有在车安排且没有对应现行约定；已保存的事实、偏好和边界不变。"}
    if ride_options_inactive:
        trace["inactive_ride_options"] = {"source": "intimacy.ride_options", "placement": "background", "included": False,
            "reason": "本人已有在车安排，不能另提新载具；原当前约定及人物资料完整保留。"}
    if protocol_scope in {"inactive_pair_stays", "inactive_group_and_pair_stays"}:
        trace["inactive_pair_stay_protocols"] = {
            "source": "no_applicable_pair_journey_or_current_stay", "placement": "rules",
            "included": False, "reason": "只省略当前无法执行的双人旅程住宿协议，全部当前事实和成员约定仍保留。",
        }
    elif protocol_scope == "inactive_group":
        trace["inactive_group_protocols"] = {
            "source": "existing_pair_journey_or_current_stay", "placement": "rules", "included": False,
            "reason": "保留现行双人约定及其协议，只省略目前不能另提的多人意图和新组团候选。",
        }
    source_usage: dict[str, tuple[int, int]] = {}

    def render() -> list[dict[str, str]]:
        return [
            {"role": "system", "content": system_prompt + "\n" + control},
            {"role": "user", "content": _encode(background)},
            *example_messages,
            *history,
            {"role": "user", "content": _current_message(current)},
        ]

    mandatory = token_counter(render())
    trace["mandatory"] = {
        "source": "rules+character+current",
        "placement": "fixed",
        "tokens": mandatory,
        "included": True,
    }
    if imported_profile:
        trace["sillytavern_profile"] = {
            "source": "npc_card.sillytavern_card",
            "placement": "character_data",
            "tokens": token_counter([{"role": "user", "content": _encode(imported_profile)}]),
            "included": True,
        }
    if imported_guidance:
        trace["sillytavern_post_history"] = {
            "source": "npc_card.sillytavern_card",
            "placement": "character_data",
            "tokens": token_counter([{"role": "user", "content": imported_guidance}]),
            "included": True,
        }
    if mandatory > max_input_tokens:
        raise NpcPromptOverflow(
            f"NPC 必需提示需要 {mandatory} token，可用输入预算为 {max_input_tokens}"
        )
    booking_section = background.get("intimacy")
    booking_records = booking_section.get("lodging_plans", []) if isinstance(booking_section, dict) else []
    booking_active = any(
        isinstance(item, dict) and item.get("status") in {"pending", "accepted"}
        for item in booking_records
    )
    if isinstance(booking_section, dict):
        booking_active = booking_active or any(
            isinstance(item,dict) and item.get("status") == "pending"
            for item in booking_section.get("companion_proposals",[])
        )
        # 哥哥，订房后的新邀请同样要完整输出选择，只减少可选旧历史。
        booking_active = booking_active or bool(booking_section.get("lodging_handoff_sources")) or any(
            isinstance(item, dict) and item.get("source_lodging_plan_id")
            and (item.get("status") in {"pending", "accepted", "active"}
                 or item.get("reunion_status") == "waiting" or item.get("can_end_reunion_wait"))
            for item in booking_section.get("separate_stays", [])
        )
        booking_active = booking_active or any(
            isinstance(stay, dict) and any(
                isinstance(change, dict) and change.get("status") == "pending"
                for change in stay.get("reschedule_requests", [])
            )
            for stay in booking_section.get("separate_stays", [])
        )
    journey_active = bool(background.get("shared_travel")) or (
        isinstance(booking_section, dict) and any(
            isinstance(item, dict)
            and item.get("status") in {"pending", "accepted", "active", "paused", "resume_pending"}
            for key in ("journeys", "group_journeys") for item in booking_section.get(key, [])
        )
    )
    # 哥哥，有限旅行也需完整表达选择；保留全部当前承诺，只少装可选历史。
    optional_input_limit = (
        min(max_input_tokens, max(mandatory, 7400))
        if booking_active or journey_active else max_input_tokens
    )
    trace["optional_budget"] = {
        "source": "lodging_output_headroom" if booking_active else "journey_output_headroom"
        if journey_active else "configured_input_budget",
        "placement": "budget",
        "tokens": 0,
        "included": True,
        "limit": optional_input_limit,
        "required_limit": max_input_tokens,
    }

    def rejected(source: str, index: int | None, reason: str) -> None:
        trace_key = source + (f":{index}" if index is not None else "")
        trace[trace_key] = {
            "source": source,
            "placement": "background",
            "tokens": 0,
            "included": False,
            "reason": reason,
        }

    def select_candidate(source: str, item: object, index: int) -> object | None:
        if source == "knowledge_context" and isinstance(item, dict):
            if item.get("audience") == "guardrail":
                return None
        if source in _SOURCE_LIMITS and coverage.repeated(item, source=source):
            if isinstance(item, dict) and (event_id := item.get("event_id") or item.get("id")):
                coverage.event_ids.add(str(event_id))
            rejected(source, index, "duplicate_content")
            return None
        important_memory = (
            source == "recent_private_memories"
            and isinstance(item, dict)
            and isinstance(item.get("importance"), int | float)
            and item["importance"] >= 8
        )
        generic_past = relevance.asks_about_past and not relevance.terms
        if source in _SOURCE_LIMITS and not (
            relevance.matches(item)
            or important_memory
            or (
                generic_past
                and source
                in {
                    "conversation_episodes",
                    "scene.recent_personal_events",
                    "personal_experience.known_events",
                }
            )
        ):
            rejected(source, index, "unrelated_topic")
            return None
        return compact_dialogue_item(source.rsplit(".", 1)[-1], item, relevance)

    def fits(source: str, cost: int, total: int) -> tuple[bool, str]:
        if total > optional_input_limit:
            return False, "total_budget"
        if source in _SOURCE_LIMITS:
            token_limit, item_limit = _SOURCE_LIMITS[source]
            used, count = source_usage.get(source, (0, 0))
            if used + cost > token_limit or count >= item_limit:
                return False, "source_budget"
        return True, "selected"

    def admitted(source: str, cost: int, original: object, projected: object) -> None:
        used, count = source_usage.get(source, (0, 0))
        source_usage[source] = (used + cost, count + 1)
        # Yuki 节选只覆盖真正发送的原话，不把未发送的整段事件都标成已知。
        excerpt = (
            isinstance(projected, dict) and projected.get("summary_kind") == "original_excerpt"
        )
        coverage.remember(projected, event_ids=False)
        if not excerpt:
            coverage.remember(original)

    def add_optional(key: str, items: object) -> None:
        if not isinstance(items, list):
            return
        for index, item in enumerate(items):
            projected = select_candidate(key, item, index)
            if projected is None:
                continue
            before = token_counter(render())
            background.setdefault(key, []).append(projected)
            after = token_counter(render())
            included, reason = fits(key, after - before, after)
            if not included:
                background[key].pop()
                if not background[key]:
                    background.pop(key)
            else:
                admitted(key, after - before, item, projected)
            trace[f"{key}:{index}"] = {
                "source": key,
                "placement": "background",
                "tokens": after - before,
                "included": included,
                "reason": reason,
            }

    def add_nested_optional(parent: str, key: str, value: object, index: int | None) -> None:
        source = f"{parent}.{key}"
        projected = select_candidate(source, value, index) if index is not None else value
        if projected is None:
            return
        before = token_counter(render())
        section = background.setdefault(parent, {})
        if index is None:
            section[key] = projected
        else:
            section.setdefault(key, []).append(projected)
        after = token_counter(render())
        included, reason = fits(source, after - before, after)
        if not included:
            if index is None:
                section.pop(key)
            else:
                section[key].pop()
                if not section[key]:
                    section.pop(key)
            if not section:
                background.pop(parent)
        else:
            admitted(source, after - before, value, projected)
        trace_key = f"{parent}.{key}" + (f":{index}" if index is not None else "")
        trace[trace_key] = {
            "source": trace_key,
            "placement": "background",
            "tokens": after - before,
            "included": included,
            "reason": reason,
        }

    # Yuki 先为眼前身体状态和已成立的约定留位，再用完整问答填剩余预算。
    add_optional("open_commitments", optional["open_commitments"])
    current_experience = ("emotion", "food_discomfort", "shared_appointments", "wonder_context")
    if isinstance(personal_experience, dict):
        for key in current_experience:
            value = personal_experience.get(key)
            if isinstance(value, list):
                for index, item in enumerate(value):
                    add_nested_optional("personal_experience", key, item, index)
            elif value is not None:
                add_nested_optional("personal_experience", key, value, None)

    for index, item in enumerate(continuity_extra, start=1):
        add_nested_optional("intimacy", "ongoing_considerations", item, index)

    included_turns: list[object] = []
    history_base = token_counter(render())
    if isinstance(turns, list):
        groups = conversation_exchanges(turns)
        for index in range(len(groups) - 1, -1, -1):
            candidate = [_history_message(turn) for turn in groups[index]]
            before = token_counter(render())
            history[:0] = candidate
            count = token_counter(render())
            included = count <= optional_input_limit
            if not included:
                del history[: len(candidate)]
            else:
                included_turns[:0] = groups[index]
            trace[f"recent_conversation:{index}"] = {
                "source": "recent_conversation",
                "placement": "in_chat",
                "tokens": count - before,
                "included": included,
            }
            if not included:
                break
    trace["recent_conversation"] = {
        "source": "history",
        "placement": "in_chat",
        "included": not any(
            not value["included"]
            for key, value in trace.items()
            if key.startswith("recent_conversation:")
        ),
        "tokens": token_counter(render()) - history_base,
    }

    for index, item in enumerate(extra_stay_rooms):
        add_nested_optional("intimacy", "separate_stay_rooms", item, index)
    for index, item in enumerate(extra_lodging_rooms):
        add_nested_optional("intimacy", "lodging_room_options", item, index)

    for key, items in previous_arrangements.items():
        if key in {"appointments", "appointment_room_options"}:
            # 哥哥，嵌套的旧约会仍保留完整记录，只在本轮预算够时发送。
            section_key = "arrangements" if key == "appointments" else "room_options"
            trace_key = f"intimacy.appointments.{section_key}"
            for index, item in enumerate(items):
                before = token_counter(render())
                section = background["intimacy"]["appointments"][section_key]
                section.append(item)
                after = token_counter(render())
                included = after <= optional_input_limit
                if not included:
                    section.pop()
                trace[f"{trace_key}:{index}"] = {
                    "source": trace_key,
                    "placement": "background",
                    "tokens": after - before,
                    "included": included,
                    "reason": "selected" if included else "total_budget",
                }
        else:
            for index, item in enumerate(items):
                add_nested_optional("intimacy", key, item, index)

    for turn in included_turns:
        coverage.remember(turn)
    for index, item in enumerate(past_moments):
        add_nested_optional("intimacy", "past_shared_moments", item, index)
    for key, items in relationship_history.items():
        for index, item in enumerate(items, start=relationship_history_start.get(key, 1)):
            add_nested_optional("intimacy", key, item, index)
    add_optional("knowledge_context", optional["knowledge_context"])

    recalled = optional["recalled_past_conversation"]
    if isinstance(recalled, list):
        for index, group in enumerate(conversation_exchanges(recalled)):
            if not relevance.wants_original(group):
                rejected("recalled_past_conversation", index, "not_requested")
                continue
            text = "\n".join(natural_text(turn) for turn in group)
            if all(coverage.repeated(turn) for turn in group):
                rejected("recalled_past_conversation", index, "duplicate_content")
                continue
            if relevance.terms and not relevance.matches(text):
                rejected("recalled_past_conversation", index, "unrelated_topic")
                continue
            before = token_counter(render())
            background.setdefault("recalled_past_conversation", []).extend(group)
            after = token_counter(render())
            included, reason = fits("recalled_past_conversation", after - before, after)
            if not included:
                del background["recalled_past_conversation"][-len(group) :]
                if not background["recalled_past_conversation"]:
                    background.pop("recalled_past_conversation")
            else:
                admitted("recalled_past_conversation", after - before, "", "")
                for turn in group:
                    coverage.remember(turn)
            trace[f"recalled_past_conversation:{index}"] = {
                "source": "recalled_past_conversation",
                "placement": "background",
                "tokens": after - before,
                "included": included,
                "reason": reason,
            }

    # Yuki 把重复的相同事件合成一次记录，次数单独保留，不误说只发生了一次。
    event_counts: dict[tuple[str, str], int] = {}
    merged_events: set[tuple[str, str]] = set()
    for event in scene_events:
        if isinstance(event, dict):
            identity = (str(event.get("event_type") or ""), str(event.get("summary") or ""))
            event_counts[identity] = event_counts.get(identity, 0) + 1
    for index, event in enumerate(scene_events):
        if isinstance(event, dict):
            identity = (str(event.get("event_type") or ""), str(event.get("summary") or ""))
            if identity in merged_events:
                if event.get("id"):
                    coverage.event_ids.add(str(event["id"]))
                rejected("scene.recent_personal_events", index, "merged_occurrences")
                continue
            if event_counts[identity] > 1:
                event = {**event, "occurrences": event_counts[identity]}
        add_nested_optional("scene", "recent_personal_events", event, index)
        if isinstance(event, dict) and trace[f"scene.recent_personal_events:{index}"]["included"]:
            merged_events.add(identity)
    if isinstance(personal_experience, dict):
        priority = (
            "emotion",
            "food_discomfort",
            "dispositions",
            "life_goals",
            "routine",
            "shared_appointments",
            "known_events",
            "beliefs",
        )
        for key in (*priority, *(item for item in personal_experience if item not in priority)):
            if key not in personal_experience or key in current_experience:
                continue
            value = personal_experience[key]
            if isinstance(value, list):
                for index, item in enumerate(value):
                    add_nested_optional("personal_experience", key, item, index)
            elif value is not None:
                add_nested_optional("personal_experience", key, value, None)

    for key in _OPTIONAL:
        if key not in {"knowledge_context", "open_commitments", "recalled_past_conversation"}:
            add_optional(key, optional[key])

    all_examples = [
        *({"source": "npc_card.dialogue_examples", "text": item} for item in examples),
        *(
            {"source": f"sillytavern_card.{item['source']}", "text": item["text"]}
            for item in imported_examples
        ),
    ]
    for index, example in enumerate(all_examples):
        before = token_counter(render())
        example_messages.append(
            {
                "role": "user",
                "content": _encode(
                    {
                        "dialogue_style_example": example["text"],
                        "note": "只参考语气，不是已发生的对话或世界事实",
                    }
                ),
            }
        )
        after = token_counter(render())
        included = after <= optional_input_limit
        if not included:
            example_messages.pop()
        trace[f"example:{index}"] = {
            "source": example["source"],
            "placement": "before_history",
            "tokens": after - before,
            "included": included,
        }

    messages = render()
    total = token_counter(messages)
    trace["total"] = {
        "source": "final_messages",
        "placement": "payload",
        "tokens": total,
        "included": True,
    }
    return NpcPrompt(messages, total, trace)


def build_deepseek_roleplay_payload(
    prompt: NpcPrompt,
    model: str,
    intent: SamplingIntent,
) -> dict[str, object]:
    """将标准采样意图转换成 DeepSeek 聊天补全可接受的字段。"""
    if intent.max_output_tokens < 1:
        raise ValueError("max_output_tokens 必须大于零")
    payload: dict[str, object] = {
        "model": model,
        "messages": prompt.messages,
        "max_tokens": intent.max_output_tokens,
        "stream": intent.stream,
    }
    if "reasoner" not in model.lower():
        # 哥哥，普通扮演关闭接口默认推理，避免短回复预算全被隐藏推理耗尽。
        payload["thinking"] = {"type": "disabled"}
        # 哥哥，对话里的真实选择也需要稳定 JSON；关闭推理后再约束对象格式。
        payload["response_format"] = {"type": "json_object"}
        for key in ("temperature", "top_p", "frequency_penalty", "presence_penalty"):
            value = getattr(intent, key)
            if value is not None:
                if key == "temperature":
                    value = min(2.0, max(0.0, value))
                elif key == "top_p":
                    value = min(1.0, max(0.0, value))
                else:
                    value = min(2.0, max(-2.0, value))
                payload[key] = value
    if intent.stop:
        payload["stop"] = list(intent.stop)
    return payload
