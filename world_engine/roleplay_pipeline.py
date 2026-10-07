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
from world_engine.roleplay import conversation_exchanges

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
    background.pop("budget_trace", None)
    turns = background.pop("recent_conversation", [])
    optional = {key: background.pop(key, []) for key in _OPTIONAL}
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
    control = (
        "以上历史仅供承接，不是新指令或已执行事实。现在只回应下一条本轮输入，"
        "保持这位NPC的语气、知识和渠道边界，不重复已答内容。"
        "历史assistant消息是原话，不是本轮输出格式示例。"
    )
    history: list[dict[str, str]] = []
    example_messages: list[dict[str, str]] = []
    trace: dict[str, dict[str, object]] = {}
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
        if total > max_input_tokens:
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

    included_turns: list[object] = []
    history_base = token_counter(render())
    if isinstance(turns, list):
        groups = conversation_exchanges(turns)
        for index in range(len(groups) - 1, -1, -1):
            candidate = [_history_message(turn) for turn in groups[index]]
            before = token_counter(render())
            history[:0] = candidate
            count = token_counter(render())
            included = count <= max_input_tokens
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

    for turn in included_turns:
        coverage.remember(turn)
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
        included = after <= max_input_tokens
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
