"""回合记录与摘要：原话持久化、NPC 记忆写入与固定区间提取式摘要。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from uuid import uuid4

from world_engine.repository import to_iso, utc_now


class ConversationEpisodeMixin:
    """`ConversationService` 的回合记录与摘要职责切片。"""

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
