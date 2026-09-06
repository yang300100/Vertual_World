"""NPC 对话线程的持久化、按世界时间读取与上下文组装。"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import uuid4

from world_engine.domain import CharacterState, WorldSnapshot
from world_engine.geo import great_circle_distance_km
from world_engine.knowledge import WorldKnowledgeBase, search_dynamic_knowledge
from world_engine.player_inputs import parse_player_input
from world_engine.proximity import VISIBLE_PERSON_RADIUS_KM
from world_engine.repository import from_iso, to_iso, utc_now

_ASCII_WORD_PATTERN = re.compile(r"[a-z0-9_]{2,}", re.IGNORECASE)
_CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


@dataclass(frozen=True)
class NpcConversationContext:
    npc_id: str
    npc_name: str
    turns: tuple[str, ...]

    def planning_intent(self, player_intent: str) -> str:
        turns = "\n".join(self.turns)
        return (
            f"【连续对话对象：{self.npc_name}】\n"
            "以下为最近一小时内的真实对话，带有世界时间；本轮必须承接其话题。\n"
            f"{turns}\n"
            f"【本轮玩家原话】{player_intent}"
        )


@dataclass(frozen=True)
class NpcCharacterCard:
    """供对话使用的稳定角色底色；不是要求逐句复述的人设模板。"""

    public_role: str
    current_preoccupation: str
    private_tension: str
    social_boundary: str
    expression_notes: str
    speech_style: str
    initiative_notes: str
    preferred_address: str
    dialogue_examples: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "public_role": self.public_role,
            "current_preoccupation": self.current_preoccupation,
            "private_tension": self.private_tension,
            "social_boundary": self.social_boundary,
            "expression_notes": self.expression_notes,
            "speech_style": self.speech_style,
            "initiative_notes": self.initiative_notes,
            "preferred_address": self.preferred_address,
            "dialogue_examples": list(self.dialogue_examples),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> NpcCharacterCard:
        return cls(
            public_role=str(value.get("public_role") or "居民").strip()[:120],
            current_preoccupation=(
                str(value.get("current_preoccupation") or "处理眼前的事务").strip()[:240]
            ),
            private_tension=(
                str(value.get("private_tension") or "不愿轻易暴露自己的顾虑").strip()[:240]
            ),
            social_boundary=(
                str(value.get("social_boundary") or "会先判断谈话是否合适").strip()[:240]
            ),
            expression_notes=(
                str(value.get("expression_notes") or "按当下处境自然说话").strip()[:240]
            ),
            speech_style=(
                str(value.get("speech_style") or "使用符合身份的自然口语，避免重复套话")
                .strip()[:240]
            ),
            initiative_notes=(
                str(value.get("initiative_notes") or "必要时追问来意，并把话题带回自己关心的事务")
                .strip()[:240]
            ),
            preferred_address=(
                str(value.get("preferred_address") or "根据关系和场合自然称呼对方").strip()[:120]
            ),
            dialogue_examples=tuple(
                str(item).strip()[:300]
                for item in (value.get("dialogue_examples") or [])
                if str(item).strip()
            )[:4],
        )


class ConversationService:
    """对话记录是 NPC 的可读取记忆属性，但以专用表持久化。"""

    def __init__(
        self,
        max_context_chars: int = 12000,
        *,
        max_context_tokens: int = 3600,
        memory_top_k: int = 6,
        knowledge_top_k: int = 4,
        episode_turn_threshold: int = 6,
        episode_top_k: int = 4,
        knowledge_base: WorldKnowledgeBase | None = None,
    ) -> None:
        self.max_context_chars = max(1000, max_context_chars)
        self.max_context_tokens = max(800, max_context_tokens)
        self.memory_top_k = max(1, memory_top_k)
        self.knowledge_top_k = max(1, knowledge_top_k)
        normalized_threshold = max(4, episode_turn_threshold)
        self.episode_turn_threshold = (
            normalized_threshold
            if normalized_threshold % 2 == 0
            else normalized_threshold + 1
        )
        self.episode_top_k = max(1, episode_top_k)
        self.knowledge_base = knowledge_base

    @staticmethod
    def default_card(npc: CharacterState) -> NpcCharacterCard:
        """从已有角色资料确定性补全首张卡，不凭空添加具体世界事实。"""
        traits = set(npc.traits)
        role = (npc.identity or "居民").strip()
        concern = next((item.strip() for item in npc.goals if item.strip()), f"把{role}的事务办妥")
        if traits & {"警觉", "谨慎", "守序", "戒备", "孤僻"}:
            boundary = "陌生人问得太深时，会先追问来意或暂缓回答"
            expression = "先问清对象和缘由，再给有限而具体的答复"
        elif traits & {"热情", "温和", "仁慈", "健谈"}:
            boundary = "愿意帮忙，但不会替别人作出无法兑现的承诺"
            expression = "会接住对方的话，也常把话题带回自己关心的人或事"
        elif traits & {"直率", "务实", "专注", "坚韧"}:
            boundary = "讨厌空话，若被耽误会直接结束谈话"
            expression = "偏向先说可做与不可做的事，不绕远弯"
        else:
            boundary = "会先判断场合、关系与手头事务，再决定说多少"
            expression = "不急着下结论，会留下尚未说完的余地"
        if traits & {"热情", "健谈"}:
            speech_style = "语气亲切，句子稍长，会自然补充与话题有关的见闻"
            initiative = "愿意主动追问，也会提起自己正在关心的人或事"
            examples = ("你慢慢说，我听着；若正好知道，我不会让你白跑一趟。",)
        elif traits & {"警觉", "谨慎", "戒备", "孤僻"}:
            speech_style = "措辞克制，先确认来意，未知和不愿说的部分会明确保留"
            initiative = "发现问题涉及隐私或风险时，会反问来源或结束话题"
            examples = ("先说清楚你为什么问，我才好决定能告诉你多少。",)
        elif traits & {"直率", "务实", "专注"}:
            speech_style = "表达直接，偏好具体的人、物、时间与可执行事项"
            initiative = "会把空泛问题收束成可以回答或办理的具体问题"
            examples = ("先说要办什么事，能做与不能做我都直说。",)
        else:
            speech_style = "语气自然含蓄，会随关系和场合调整回答长短"
            initiative = "会在回答后留下一个与自身处境有关的追问或话题钩子"
            examples = ("这事得看眼下情形，你先把来意说清楚。",)
        trait_text = "、".join(npc.traits[:2]) or "职责"
        return NpcCharacterCard(
            public_role=role,
            current_preoccupation=concern,
            private_tension=f"在{trait_text}与眼前责任之间寻找不失分寸的做法",
            social_boundary=boundary,
            expression_notes=expression,
            speech_style=speech_style,
            initiative_notes=initiative,
            preferred_address="根据关系与场合自然称呼对方，不固定使用尊称",
            dialogue_examples=examples,
        )

    def get_card(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        npc: CharacterState,
        persist_default: bool = False,
    ) -> NpcCharacterCard:
        row = connection.execute(
            "SELECT card_json FROM npc_character_cards WHERE world_id = ? AND character_id = ?",
            (world_id, npc.id),
        ).fetchone()
        if row is not None:
            try:
                raw = json.loads(row["card_json"])
                if isinstance(raw, dict):
                    return NpcCharacterCard.from_dict(raw)
            except (json.JSONDecodeError, TypeError):
                pass
        card = self.default_card(npc)
        if persist_default:
            self.save_card(connection, world_id=world_id, npc_id=npc.id, card=card)
        return card

    @staticmethod
    def save_card(
        connection: sqlite3.Connection,
        *,
        world_id: str,
        npc_id: str,
        card: NpcCharacterCard,
    ) -> None:
        now = to_iso(utc_now())
        connection.execute(
            """
            INSERT INTO npc_character_cards(
                character_id, world_id, card_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(character_id) DO UPDATE SET
                card_json = excluded.card_json, updated_at = excluded.updated_at
            """,
            (npc_id, world_id, json.dumps(card.to_dict(), ensure_ascii=False), now, now),
        )

    def build_npc_reply_context(
        self,
        connection: sqlite3.Connection,
        *,
        snapshot: WorldSnapshot,
        npc: CharacterState,
        player: CharacterState,
        player_text: str,
        conversation: NpcConversationContext | None,
        channel: str = "in_person",
        interaction: str = "当面交谈",
        decision_details: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """按人物视角和预算组装一次对话所需的全部只读资料。"""
        card = self.get_card(connection, world_id=snapshot.world.id, npc=npc)
        memories = self._relevant_memories(
            connection,
            world_id=snapshot.world.id,
            npc_id=npc.id,
            query=" ".join((player_text, interaction, " ".join(npc.goals))),
        )
        todos = connection.execute(
            """
            SELECT title, details FROM npc_todos
            WHERE world_id = ? AND character_id = ? AND status IN ('open', 'doing')
            ORDER BY CASE status WHEN 'doing' THEN 0 ELSE 1 END, updated_at DESC LIMIT 3
            """,
            (snapshot.world.id, npc.id),
        ).fetchall()
        relation = {
            (item.source_character_id, item.target_character_id): {
                "affinity": item.affinity,
                "trust": item.trust,
            }
            for item in snapshot.relationships
        }
        location = snapshot.location_by_id(npc.current_location_id or npc.location_id)
        nearby = [
            {
                "id": item.id,
                "name": item.name,
                "identity": item.identity,
                "distance_m": round(
                    great_circle_distance_km(
                        npc.longitude, npc.latitude, item.longitude, item.latitude
                    ) * 1000
                ),
            }
            for item in snapshot.characters
            if item.id not in {npc.id, player.id}
            and great_circle_distance_km(
                npc.longitude, npc.latitude, item.longitude, item.latitude
            ) <= VISIBLE_PERSON_RADIUS_KM
        ][:8]
        scene = {
            "location": (
                {
                    "id": location.id,
                    "name": location.name,
                    "kind": location.kind,
                    "resources": location.resources,
                }
                if location
                else None
            ),
            "nearby_characters": nearby,
            "recent_personal_events": self._recent_personal_events(
                connection,
                world_id=snapshot.world.id,
                npc_id=npc.id,
                limit=6,
            ),
        }
        knowledge = self._retrieve_dialogue_knowledge(
            connection,
            snapshot=snapshot,
            npc=npc,
            player_text=player_text,
            location_id=location.id if location else npc.location_id,
        )
        recalled_turns = self._recall_past_conversation(
            connection,
            snapshot=snapshot,
            npc=npc,
            player_id=player.id,
            query=player_text,
        )
        episode_summaries = self._relevant_episode_summaries(
            connection,
            world_id=snapshot.world.id,
            npc_id=npc.id,
            player_id=player.id,
            query=player_text,
        )
        from world_engine.player_activities import PlayerActivityService

        activities = PlayerActivityService.recent_records(
            connection, snapshot.world.id, player.id, npc.id
        )
        base: dict[str, object] = {
            "channel": channel,
            "interaction": interaction,
            "npc": {
                "id": npc.id,
                "name": npc.name,
                "identity": npc.identity,
                "traits": npc.traits,
                "goals": npc.goals,
                "energy": npc.energy,
                "satiety": npc.satiety,
                "health": npc.health,
            },
            "npc_card": card.to_dict(),
            "player": {
                "id": player.id,
                "name": player.name,
                "identity": player.identity,
            },
            "relationship": {
                "npc_to_player": relation.get((npc.id, player.id), {"affinity": 0, "trust": 0}),
                "player_to_npc": relation.get((player.id, npc.id), {"affinity": 0, "trust": 0}),
            },
            "scene": scene,
            "recent_private_memories": memories,
            "open_commitments": [dict(row) for row in todos],
            "recent_conversation": list(conversation.turns) if conversation else [],
            "conversation_episodes": episode_summaries,
            "recalled_past_conversation": recalled_turns,
            "knowledge_context": knowledge,
            "decision": decision_details or {},
            "player_text": player_text,
            "player_activity_records": [
                {"title": row["title"], "status": row["status"],
                 "source_event_id": row["source_event_id"], "result": row["content"]["result"]}
                for row in activities[:6]
            ],
            "world_time": snapshot.world.current_time.isoformat(),
        }
        return self._apply_context_budget(base)

    @staticmethod
    def estimate_tokens(value: object) -> int:
        """使用偏保守的中英文估算，确保未知模型 tokenizer 下仍有输出余量。"""
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        cjk_count = len(_CJK_PATTERN.findall(text))
        non_cjk_count = max(0, len(text) - cjk_count)
        return max(1, cjk_count + math.ceil(non_cjk_count / 4))

    def _apply_context_budget(self, context: dict[str, object]) -> dict[str, object]:
        """保留人物与规则必需段，再按优先级逐项装入可裁剪上下文。"""
        optional_keys = (
            "recent_conversation",
            "player_activity_records",
            "conversation_episodes",
            "recent_private_memories",
            "knowledge_context",
            "open_commitments",
            "recalled_past_conversation",
        )
        result = dict(context)
        for key in optional_keys:
            result[key] = []
        trace: dict[str, int] = {}
        base_tokens = self.estimate_tokens(result)
        trace["mandatory"] = base_tokens
        remaining = max(0, self.max_context_tokens - base_tokens)
        for key in optional_keys:
            selected: list[object] = []
            raw_items = context.get(key)
            if not isinstance(raw_items, list):
                continue
            for item in raw_items:
                item_tokens = self.estimate_tokens(item)
                if item_tokens > remaining:
                    break
                selected.append(item)
                remaining -= item_tokens
            result[key] = selected
            trace[key] = sum(self.estimate_tokens(item) for item in selected)
        trace["total"] = self.max_context_tokens - remaining
        trace["limit"] = self.max_context_tokens
        result["budget_trace"] = trace
        return result

    def _relevant_memories(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        npc_id: str,
        query: str,
    ) -> list[dict[str, object]]:
        rows = connection.execute(
            """
            SELECT event_id, memory_type, summary, importance, confidence, created_at
            FROM character_memories
            WHERE world_id = ? AND character_id = ?
            ORDER BY created_at DESC LIMIT 80
            """,
            (world_id, npc_id),
        ).fetchall()
        query_tokens = self._search_tokens(query)
        ranked: list[tuple[float, int, sqlite3.Row]] = []
        for recency, row in enumerate(rows):
            memory_tokens = self._search_tokens(str(row["summary"]))
            overlap = len(query_tokens & memory_tokens)
            importance = int(row["importance"])
            score = overlap * 8 + importance * 1.5 + max(0, 8 - recency * 0.15)
            ranked.append((score, -recency, row))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [dict(item[2]) for item in ranked[: self.memory_top_k]]

    def _retrieve_dialogue_knowledge(
        self,
        connection: sqlite3.Connection,
        *,
        snapshot: WorldSnapshot,
        npc: CharacterState,
        player_text: str,
        location_id: str,
    ) -> list[dict[str, object]]:
        query = " ".join(
            item
            for item in (
                snapshot.world.name,
                player_text,
                npc.identity or "",
                " ".join(npc.goals),
            )
            if item
        )
        dynamic_hits = search_dynamic_knowledge(
            connection,
            world_id=snapshot.world.id,
            query=query,
            character_ids={npc.id},
            location_ids={location_id},
            limit=self.knowledge_top_k,
            max_total_chars=min(self.max_context_chars, 6000),
        )
        # 静态库当前混有作者资料和旧世界资料；对话只自动加入显式 always_include 的
        # 通用常识/护栏。地区公开知识应先以动态登记或显式世界范围进入运行时。
        guardrail_hits = []
        common_hits = []
        if self.knowledge_base is not None:
            region_scopes = self._location_scopes(snapshot, location_id)
            static_hits = self.knowledge_base.search(
                query,
                audiences={"guardrail", "character_common"},
                limit=self.knowledge_top_k * 2,
                max_total_chars=min(self.max_context_chars, 6000),
                world_scope=snapshot.world.name,
                region_scopes=region_scopes,
                at_time=snapshot.world.current_time,
            )
            guardrail_hits = [
                hit
                for hit in static_hits
                if hit.chunk.always_include and hit.chunk.audience == "guardrail"
            ]
            common_hits = [
                hit
                for hit in static_hits
                if hit.chunk.audience == "character_common"
                and (
                    hit.chunk.always_include
                    or hit.chunk.world_scopes
                    or hit.chunk.region_scopes
                )
            ]
        unique: dict[str, dict[str, object]] = {}
        for hit in [*guardrail_hits, *dynamic_hits, *common_hits]:
            if hit.chunk.id in unique:
                continue
            unique[hit.chunk.id] = hit.to_dict()
            if len(unique) >= self.knowledge_top_k:
                break
        return list(unique.values())

    @staticmethod
    def _location_scopes(snapshot: WorldSnapshot, location_id: str) -> set[str]:
        by_id = {location.id: location for location in snapshot.locations}
        scopes: set[str] = set()
        current = by_id.get(location_id)
        visited: set[str] = set()
        while current is not None and current.id not in visited:
            visited.add(current.id)
            scopes.update({current.id, current.name})
            current = by_id.get(current.parent_location_id) if current.parent_location_id else None
        return scopes

    @staticmethod
    def _recent_personal_events(
        connection: sqlite3.Connection,
        *,
        world_id: str,
        npc_id: str,
        limit: int,
    ) -> list[dict[str, object]]:
        return [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, event_type, occurred_at, summary, actor_id, target_id, location_id
                FROM world_events
                WHERE world_id = ? AND (actor_id = ? OR target_id = ?)
                ORDER BY occurred_at DESC, created_at DESC LIMIT ?
                """,
                (world_id, npc_id, npc_id, limit),
            ).fetchall()
        ]

    def _recall_past_conversation(
        self,
        connection: sqlite3.Connection,
        *,
        snapshot: WorldSnapshot,
        npc: CharacterState,
        player_id: str,
        query: str,
    ) -> list[str]:
        cutoff = to_iso(snapshot.world.current_time - timedelta(hours=1))
        rows = connection.execute(
            """
            SELECT t.event_id, t.turn_index, t.speaker_character_id, t.content, t.world_time,
                   t.message_kind
            FROM npc_conversation_turns t
            JOIN npc_conversation_sessions s ON s.id = t.session_id
            WHERE s.world_id = ? AND s.npc_character_id = ?
              AND s.counterpart_character_id = ? AND t.world_time < ?
            ORDER BY t.turn_index DESC LIMIT 100
            """,
            (snapshot.world.id, npc.id, player_id, cutoff),
        ).fetchall()
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in reversed(rows):
            grouped.setdefault(str(row["event_id"]), []).append(row)
        query_tokens = self._search_tokens(query)
        ranked: list[tuple[int, str, list[sqlite3.Row]]] = []
        for event_id, turns in grouped.items():
            text = " ".join(str(turn["content"]) for turn in turns)
            overlap = len(query_tokens & self._search_tokens(text))
            if overlap:
                ranked.append((overlap, event_id, turns))
        ranked.sort(key=lambda item: item[0], reverse=True)
        recalled: list[str] = []
        for _, _, turns in ranked[:3]:
            recalled.extend(
                f"[{from_iso(turn['world_time']).isoformat()}] "
                f"{'玩家行动' if turn['message_kind'] == 'action' else '玩家' if turn['speaker_character_id'] == player_id else 'NPC'}："
                f"{turn['content']}"
                for turn in turns
            )
        return recalled[:8]

    def _relevant_episode_summaries(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        npc_id: str,
        player_id: str,
        query: str,
    ) -> list[dict[str, object]]:
        rows = connection.execute(
            """
            SELECT e.* FROM npc_conversation_episodes e
            JOIN npc_conversation_sessions s ON s.id = e.session_id
            WHERE e.world_id = ? AND s.npc_character_id = ?
              AND s.counterpart_character_id = ?
            ORDER BY e.last_turn_index DESC LIMIT 40
            """,
            (world_id, npc_id, player_id),
        ).fetchall()
        query_tokens = self._search_tokens(query)
        ranked: list[tuple[float, int, sqlite3.Row]] = []
        for recency, row in enumerate(rows):
            episode_tokens = self._search_tokens(
                f"{row['summary']} {row['topic_terms_json']}"
            )
            overlap = len(query_tokens & episode_tokens)
            score = overlap * 10 + max(0, 8 - recency * 0.4)
            ranked.append((score, -recency, row))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        result: list[dict[str, object]] = []
        for _, _, row in ranked[: self.episode_top_k]:
            result.append(
                {
                    "first_turn_index": row["first_turn_index"],
                    "last_turn_index": row["last_turn_index"],
                    "summary": row["summary"],
                    "topic_terms": json.loads(row["topic_terms_json"]),
                    "open_questions": json.loads(row["open_questions_json"]),
                    "open_commitments": json.loads(row["open_commitments_json"]),
                    "source_event_ids": json.loads(row["source_event_ids_json"]),
                    "source_hash": row["source_hash"],
                }
            )
        return result

    @staticmethod
    def _search_tokens(text: str) -> set[str]:
        normalized = text.casefold()
        tokens = set(_ASCII_WORD_PATTERN.findall(normalized))
        cjk = "".join(_CJK_PATTERN.findall(normalized))
        tokens.update(cjk[index : index + 2] for index in range(max(0, len(cjk) - 1)))
        tokens.update(cjk[index : index + 3] for index in range(max(0, len(cjk) - 2)))
        return {token for token in tokens if token}

    @staticmethod
    def is_non_dialogue_intent(intent: str) -> bool:
        message = parse_player_input(intent)
        if message.kind != "auto":
            return message.kind == "action"
        return any(
            marker in intent
            for marker in (
                "攻击", "袭击", "砍", "杀", "打", "前往", "赶路", "动身", "休息",
                "睡", "吃", "喝", "捡", "拾", "使用", "装备", "开始工作", "去工作",
                "干活", "建造",
            )
        )

    @staticmethod
    def looks_like_follow_up(intent: str) -> bool:
        if parse_player_input(intent).kind == "speech":
            return True
        if ConversationService.is_non_dialogue_intent(intent):
            return False
        markers = ("是", "对", "嗯", "好", "那", "所以", "需要", "吗", "？", "?")
        return any(marker in intent for marker in markers)

    @staticmethod
    def looks_like_directed_dialogue(intent: str) -> bool:
        """区分“向人物说话”和“只是在陈述中提到人物”，避免误锁对话目标。"""
        markers = (
            "交谈",
            "对话",
            "聊",
            "问",
            "询问",
            "打听",
            "告诉",
            "跟他说",
            "向他说",
            "请教",
            "请求",
            "你好",
        )
        return any(marker in intent for marker in markers)

    def resolve_target(
        self,
        connection: sqlite3.Connection,
        *,
        snapshot: WorldSnapshot,
        player: CharacterState,
        intent: str,
        target_character_id: str | None,
    ) -> CharacterState | None:
        by_id = {item.id: item for item in snapshot.characters}
        if target_character_id:
            target = by_id.get(target_character_id)
            if target is None or target.is_player:
                raise ValueError("指定的对话对象不存在于当前世界")
            if great_circle_distance_km(
                player.longitude, player.latitude, target.longitude, target.latitude
            ) > VISIBLE_PERSON_RADIUS_KM:
                raise ValueError("指定的对话对象距离超过100米")
            return target
        named = next(
            (
                item
                for item in sorted(snapshot.characters, key=lambda value: -len(value.name))
                if not item.is_player and item.name in intent
            ),
            None,
        )
        if named is not None:
            # 这里只负责锁定“对话”对象。攻击、旅行等非对话意图仍由各自动作规则
            # 决定有效距离，不能因为对话限制而误拦截既有行动解析。
            if self.is_non_dialogue_intent(intent):
                return named
            if not self.looks_like_directed_dialogue(intent):
                return None
            if great_circle_distance_km(
                player.longitude, player.latitude, named.longitude, named.latitude
            ) > VISIBLE_PERSON_RADIUS_KM:
                raise ValueError(f"{named.name}不在你100米视线范围内")
            return named
        if not self.looks_like_follow_up(intent):
            return None
        cutoff = to_iso(snapshot.world.current_time - timedelta(hours=1))
        row = connection.execute(
            """
            SELECT npc_character_id FROM npc_conversation_sessions
            WHERE world_id = ? AND counterpart_character_id = ?
              AND last_world_time >= ? AND status = 'active'
            ORDER BY last_world_time DESC, updated_at DESC LIMIT 1
            """,
            (snapshot.world.id, player.id, cutoff),
        ).fetchone()
        if row is None:
            return None
        target = by_id.get(row["npc_character_id"])
        if target is None or great_circle_distance_km(
            player.longitude, player.latitude, target.longitude, target.latitude
        ) > VISIBLE_PERSON_RADIUS_KM:
            return None
        return target

    def load_context(
        self,
        connection: sqlite3.Connection,
        *,
        snapshot: WorldSnapshot,
        player_id: str,
        npc: CharacterState,
    ) -> NpcConversationContext | None:
        cutoff = to_iso(snapshot.world.current_time - timedelta(hours=1))
        session = connection.execute(
            """
            SELECT id FROM npc_conversation_sessions
            WHERE world_id = ? AND npc_character_id = ? AND counterpart_character_id = ?
              AND status = 'active' AND last_world_time >= ?
            """,
            (snapshot.world.id, npc.id, player_id, cutoff),
        ).fetchone()
        if session is None:
            return None
        rows = connection.execute(
            """
            SELECT speaker_character_id, content, world_time, message_kind
            FROM npc_conversation_turns
            WHERE session_id = ? AND world_time >= ?
            ORDER BY world_time, turn_index
            """,
            (session["id"], cutoff),
        ).fetchall()
        turns = [
            f"[{from_iso(row['world_time']).isoformat()}] "
            f"{'玩家行动' if row['message_kind'] == 'action' else '玩家' if row['speaker_character_id'] == player_id else 'NPC'}：{row['content']}"
            for row in rows
        ]
        while turns and sum(len(item) + 1 for item in turns) > self.max_context_chars:
            turns.pop(0)
        if not turns:
            return None
        return NpcConversationContext(
            npc_id=npc.id,
            npc_name=npc.name,
            turns=tuple(turns),
        )

    def record_exchange(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        npc_id: str,
        counterpart_id: str,
        event_id: str,
        world_time: object,
        player_text: str,
        npc_text: str,
        player_message_kind: str = "speech",
    ) -> str:
        """与行动事件同一事务写入双方原话，保证审计与对话记忆一致。"""
        now = to_iso(utc_now())
        world_time_iso = to_iso(world_time)
        row = connection.execute(
            """
            SELECT id FROM npc_conversation_sessions
            WHERE world_id = ? AND npc_character_id = ? AND counterpart_character_id = ?
            """,
            (world_id, npc_id, counterpart_id),
        ).fetchone()
        if row is None:
            session_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO npc_conversation_sessions(
                    id, world_id, npc_character_id, counterpart_character_id, status,
                    last_world_time, last_event_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)
                """,
                (session_id, world_id, npc_id, counterpart_id, world_time_iso, event_id, now, now),
            )
        else:
            session_id = row["id"]
            connection.execute(
                """
                UPDATE npc_conversation_sessions
                SET status = 'active', last_world_time = ?, last_event_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (world_time_iso, event_id, now, session_id),
            )
        next_turn = connection.execute(
            "SELECT COALESCE(MAX(turn_index), 0) + 1 "
            "FROM npc_conversation_turns WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
        for offset, speaker_id, listener_id, content in (
            (0, counterpart_id, npc_id, player_text.strip()),
            (1, npc_id, counterpart_id, npc_text.strip()),
        ):
            if not content:
                continue
            connection.execute(
                """
                INSERT INTO npc_conversation_turns(
                    id, session_id, world_id, event_id, turn_index,
                    speaker_character_id, listener_character_id, content, world_time, created_at,
                    message_kind
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()), session_id, world_id, event_id, next_turn + offset,
                    speaker_id, listener_id, content, world_time_iso, now,
                    player_message_kind if offset == 0 else "speech",
                ),
            )
        names = {
            str(row["id"]): str(row["name"])
            for row in connection.execute(
                "SELECT id, name FROM characters WHERE id IN (?, ?)",
                (npc_id, counterpart_id),
            ).fetchall()
        }
        npc_memory = (
            f"我与{names.get(counterpart_id, '对方')}交谈："
            f"对方说“{player_text.strip()[:350]}”，我回应“{npc_text.strip()[:350]}”。"
        )
        if player_message_kind == "action":
            npc_memory = (
                f"我目睹{names.get(counterpart_id, '对方')}的行动结果："
                f"{player_text.strip()[:350]}；我回应“{npc_text.strip()[:350]}”。"
            )
        connection.execute(
            """
            INSERT OR IGNORE INTO character_memories(
                id, world_id, character_id, event_id, memory_type,
                summary, importance, confidence, created_at
            ) VALUES (?, ?, ?, ?, 'experienced', ?, 6, 1.0, ?)
            """,
            (str(uuid4()), world_id, npc_id, event_id, npc_memory, now),
        )
        self._summarize_ready_episode(
            connection,
            session_id=session_id,
            world_id=world_id,
            player_id=counterpart_id,
            created_at=now,
        )
        return session_id

    def _summarize_ready_episode(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        world_id: str,
        player_id: str,
        created_at: str,
    ) -> None:
        """按固定回合区间生成本地提取式摘要，不调用模型也不覆盖原始回合。"""
        last_row = connection.execute(
            """
            SELECT COALESCE(MAX(last_turn_index), 0) AS last_turn_index
            FROM npc_conversation_episodes WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        last_index = int(last_row["last_turn_index"] if last_row else 0)
        rows = connection.execute(
            """
            SELECT event_id, turn_index, speaker_character_id, content, world_time
            FROM npc_conversation_turns
            WHERE session_id = ? AND turn_index > ?
            ORDER BY turn_index LIMIT ?
            """,
            (session_id, last_index, self.episode_turn_threshold),
        ).fetchall()
        if len(rows) < self.episode_turn_threshold:
            return
        lines = [
            f"{'玩家' if row['speaker_character_id'] == player_id else 'NPC'}："
            f"{str(row['content']).strip()[:240]}"
            for row in rows
        ]
        combined = "\n".join(lines)
        topics = self._episode_topic_terms(combined)
        questions = [
            line[:300]
            for line in lines
            if any(marker in line for marker in ("？", "?", "吗", "呢"))
        ][:4]
        commitments = [
            line[:300]
            for line in lines
            if any(
                marker in line
                for marker in ("约定", "答应", "承诺", "之后", "明天", "稍后", "记得", "会去")
            )
        ][:4]
        event_ids = list(dict.fromkeys(str(row["event_id"]) for row in rows))
        source_payload = [
            {
                "event_id": row["event_id"],
                "turn_index": row["turn_index"],
                "speaker": row["speaker_character_id"],
                "content": row["content"],
            }
            for row in rows
        ]
        source_hash = hashlib.sha256(
            json.dumps(source_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        connection.execute(
            """
            INSERT OR IGNORE INTO npc_conversation_episodes(
                id, session_id, world_id, first_turn_index, last_turn_index,
                summary, topic_terms_json, open_questions_json,
                open_commitments_json, source_event_ids_json, source_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                session_id,
                world_id,
                rows[0]["turn_index"],
                rows[-1]["turn_index"],
                combined[:2000],
                json.dumps(topics, ensure_ascii=False),
                json.dumps(questions, ensure_ascii=False),
                json.dumps(commitments, ensure_ascii=False),
                json.dumps(event_ids, ensure_ascii=False),
                source_hash,
                created_at,
            ),
        )

    @classmethod
    def _episode_topic_terms(cls, text: str) -> list[str]:
        candidates = cls._search_tokens(text)
        stop_words = {
            "玩家",
            "npc",
            "这个",
            "那个",
            "可以",
            "什么",
            "怎么",
            "我们",
            "你们",
        }
        ranked = sorted(
            (token for token in candidates if token not in stop_words),
            key=lambda token: (-text.count(token), -len(token), token),
        )
        return ranked[:12]


class DialogueContextAssembler:
    """对话上下文的只读门面，隔离调用方与会话存储/召回实现细节。"""

    def __init__(self, conversations: ConversationService) -> None:
        self.conversations = conversations

    def build(
        self,
        connection: sqlite3.Connection,
        *,
        snapshot: WorldSnapshot,
        npc: CharacterState,
        player: CharacterState,
        player_text: str,
        conversation: NpcConversationContext | None,
        channel: str,
        interaction: str,
        decision_details: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return self.conversations.build_npc_reply_context(
            connection,
            snapshot=snapshot,
            npc=npc,
            player=player,
            player_text=player_text,
            conversation=conversation,
            channel=channel,
            interaction=interaction,
            decision_details=decision_details,
        )


@dataclass(frozen=True)
class ScheduledDialogueSpeaker:
    character: CharacterState
    score: float
    reasons: tuple[str, ...]


class DialogueSpeakerScheduler:
    """按点名、职责、知识相关性、关系和发言冷却选择在场发言者。"""

    def __init__(self, *, max_speakers: int = 2) -> None:
        self.max_speakers = max(1, min(3, max_speakers))

    def select(
        self,
        connection: sqlite3.Connection,
        *,
        snapshot: WorldSnapshot,
        player: CharacterState,
        intent: str,
        participant_ids: list[str] | None = None,
    ) -> list[ScheduledDialogueSpeaker]:
        visible = [
            character
            for character in snapshot.characters
            if not character.is_player
            and character.health > 0
            and great_circle_distance_km(
                player.longitude,
                player.latitude,
                character.longitude,
                character.latitude,
            )
            <= VISIBLE_PERSON_RADIUS_KM
        ]
        if not visible:
            return []
        requested = set(participant_ids or [])
        visible_ids = {character.id for character in visible}
        if requested - visible_ids:
            raise ValueError("多人对话只能选择100米内仍在场的人物")
        relations = {
            str(row["source_character_id"]): (
                int(row["affinity"]),
                int(row["trust"]),
            )
            for row in connection.execute(
                """
                SELECT source_character_id, affinity, trust FROM relationships
                WHERE world_id = ? AND target_character_id = ?
                """,
                (snapshot.world.id, player.id),
            ).fetchall()
        }
        todos_by_character: dict[str, str] = {}
        for row in connection.execute(
            """
            SELECT character_id, title, details FROM npc_todos
            WHERE world_id = ? AND status IN ('open', 'doing')
            """,
            (snapshot.world.id,),
        ).fetchall():
            todos_by_character[str(row["character_id"])] = (
                todos_by_character.get(str(row["character_id"]), "")
                + f" {row['title']} {row['details']}"
            )
        last_target = connection.execute(
            """
            SELECT target_id FROM world_events
            WHERE world_id = ? AND actor_id = ? AND event_type = 'action.socialize'
            ORDER BY occurred_at DESC, created_at DESC LIMIT 1
            """,
            (snapshot.world.id, player.id),
        ).fetchone()
        last_target_id = str(last_target["target_id"]) if last_target else None
        query_tokens = ConversationService._search_tokens(intent)
        ranked: list[ScheduledDialogueSpeaker] = []
        for character in visible:
            score = 10.0
            reasons: list[str] = ["在场可见"]
            if character.id in requested:
                score += 120
                reasons.append("玩家指定")
            if character.name and character.name in intent:
                score += 100
                reasons.append("被明确点名")
            profile_text = " ".join(
                [
                    character.identity or "",
                    *character.traits,
                    *character.goals,
                    todos_by_character.get(character.id, ""),
                ]
            )
            overlap = len(query_tokens & ConversationService._search_tokens(profile_text))
            if overlap:
                score += overlap * 12
                reasons.append("职责或目标相关")
            affinity, trust = relations.get(character.id, (0, 0))
            relation_score = max(-20, min(20, (affinity + trust) / 10))
            score += relation_score
            if relation_score >= 5:
                reasons.append("关系较近")
            if character.id == last_target_id and character.id not in requested:
                score -= 18
                reasons.append("刚刚发言，进入冷却")
            ranked.append(
                ScheduledDialogueSpeaker(
                    character=character,
                    score=round(score, 3),
                    reasons=tuple(reasons),
                )
            )
        ranked.sort(key=lambda item: (-item.score, item.character.name, item.character.id))
        return ranked[: self.max_speakers]
