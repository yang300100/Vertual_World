"""回复上下文构建：按人物视角与预算组装一次对话所需的只读资料。"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import timedelta

from world_engine.conversation.types import NpcConversationContext
from world_engine.domain import CharacterState, WorldSnapshot
from world_engine.geo import great_circle_distance_km
from world_engine.knowledge import search_dynamic_knowledge
from world_engine.life import LifeActivityService
from world_engine.proximity import VISIBLE_PERSON_RADIUS_KM, same_room
from world_engine.repository import from_iso, to_iso
from world_engine.roleplay import conversation_exchanges

_ASCII_WORD_PATTERN = re.compile(r"[a-z0-9_]{2,}", re.IGNORECASE)
_CJK_PATTERN = re.compile(r"[㐀-䶿一-鿿]")


class ConversationContextMixin:
    """`ConversationService` 的回复上下文构建职责切片。"""

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
        decision_details = dict(decision_details or {})
        if channel == "letter":
            # 信笺只将该联系人真实来往的原话放入消息历史，保留远程感知边界。
            letters = decision_details.pop("recent_letters", [])
            letter_turns = [
                f"[{item['world_time']}] "
                f"{'NPC' if item['sender_id'] == npc.id else '玩家'}：{item['content']}"
                for item in letters
                if isinstance(item, dict)
                and item.get("sender_id") in {npc.id, player.id}
                and "world_time" in item and "content" in item
            ] if isinstance(letters, list) else []
            conversation = NpcConversationContext(npc.id, npc.name, tuple(letter_turns))
        elif conversation is None:
            conversation = self.load_context(
                connection, snapshot=snapshot, player_id=player.id, npc=npc,
            )
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
            and same_room(npc, item)
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
        if npc.current_room_id:
            from world_engine.interiors import InteriorService

            npc_row = connection.execute(
                "SELECT * FROM characters WHERE id=?", (npc.id,),
            ).fetchone()
            interior = InteriorService.scene(connection, npc_row)
            scene["room"] = interior["room"]
            scene["fixtures"] = [
                {"name": item["name"], "kind": item["kind"],
                 "open": item["open"], "occupied": item["occupied"]}
                for item in interior["fixtures"]
            ]
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
        from world_engine.society import SocietyService
        base["personal_experience"] = SocietyService.personal_context(
            connection, snapshot.world.id, npc.id, snapshot.world.current_time,
        )
        from world_engine.routines import RoutineService
        routine=RoutineService.view(connection,snapshot.world.id,npc.id)
        if routine:
            base["personal_experience"]["routine"]={"goal":routine["spec"]["goal"],"income_reserve":routine["spec"]["income_reserve"],"utc_offset_minutes":routine["spec"]["utc_offset_minutes"],"recent_occurrences":routine["recent_occurrences"][:3],"work_preference":routine["work_preference"]}
            weekly=[]
            for slot in routine["spec"]["slots"]:
                place=connection.execute("SELECT name FROM locations WHERE id=? AND world_id=?",(slot["location_id"],snapshot.world.id)).fetchone()
                weekly.append(f"星期编号{','.join(map(str,slot['weekdays']))}（0为周一），{slot['starts_at']}起{slot['duration_minutes']}分钟：{slot['name']}，地点{place[0] if place else '原地点已不可用'}")
            base["personal_experience"]["routine"]["enabled"]=routine["spec"]["enabled"]
            base["personal_experience"]["routine"]["weekly_slots"]=weekly
        base["personal_experience"]["shared_appointments"] = [dict(row) for row in connection.execute(
            "SELECT f.request_id,f.location_id,f.status,f.due_world_time,w.starts_world_time,w.ends_world_time "
            "FROM contract_fulfillments f LEFT JOIN appointment_windows w ON w.contract_id=f.request_id "
            "WHERE f.world_id=? AND f.requester_id=? AND f.recipient_id=? AND f.kind='appointment' AND f.status IN ('active','overdue') ORDER BY f.due_world_time LIMIT 5",(snapshot.world.id,player.id,npc.id),
        )]
        base["player"]["name"] = SocietyService.label(connection, npc.id, player.id)
        if channel != "letter" and same_room(npc, player) and great_circle_distance_km(
            npc.longitude, npc.latitude, player.longitude, player.latitude,
        ) <= VISIBLE_PERSON_RADIUS_KM:
            active = LifeActivityService.running(connection, player.id)
            if active:
                base["player_current_activity"] = LifeActivityService.view(
                    active, snapshot.world.current_time,
                )
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
        # 世界护栏不被长历史挤出；资料仍放在用户数据中，不提升为系统指令。
        knowledge = context.get("knowledge_context", [])
        result["knowledge_context"] = [
            item for item in knowledge
            if isinstance(item, dict) and item.get("audience") == "guardrail"
        ] if isinstance(knowledge, list) else []
        trace: dict[str, int] = {}
        base_tokens = self.estimate_tokens(result)
        trace["mandatory"] = base_tokens
        for key in optional_keys:
            selected = list(result[key])
            raw_items = context.get(key)
            if not isinstance(raw_items, list):
                continue
            groups = (
                list(reversed(conversation_exchanges(raw_items)))
                if key == "recent_conversation" else [[item] for item in raw_items]
            )
            for group in groups:
                if key == "knowledge_context" and group[0] in selected:
                    continue
                candidate = group + selected if key == "recent_conversation" else selected + group
                result[key] = candidate
                if self.estimate_tokens(result) > self.max_context_tokens:
                    result[key] = selected
                    if key == "recent_conversation":
                        break
                    continue
                selected = candidate
            result[key] = selected
            trace[key] = self.estimate_tokens(selected)
        trace["total"] = self.estimate_tokens(result)
        trace["limit"] = self.max_context_tokens
        trace["over_budget_tokens"] = max(0, trace["total"] - self.max_context_tokens)
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
        exchanges = conversation_exchanges(turns)
        while exchanges and sum(len(item) + 1 for group in exchanges for item in group) > (
            self.max_context_chars
        ):
            exchanges.pop(0)
        turns = [item for group in exchanges for item in group]
        if not turns:
            return None
        return NpcConversationContext(
            npc_id=npc.id,
            npc_name=npc.name,
            turns=tuple(turns),
        )


class DialogueContextAssembler:
    """对话上下文的只读门面，隔离调用方与会话存储/召回实现细节。"""

    def __init__(self, conversations: "ConversationContextMixin") -> None:
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
