"""多人对话的发言者调度：按点名、职责、关系与冷却挑选在场发言者。"""

from __future__ import annotations

import sqlite3

from world_engine.conversation.context import ConversationContextMixin
from world_engine.conversation.types import ScheduledDialogueSpeaker
from world_engine.domain import CharacterState, WorldSnapshot
from world_engine.geo import great_circle_distance_km
from world_engine.proximity import VISIBLE_PERSON_RADIUS_KM, same_room


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
        radius_km: float = VISIBLE_PERSON_RADIUS_KM,
    ) -> list[ScheduledDialogueSpeaker]:
        visible = [
            character
            for character in snapshot.characters
            if not character.is_player
            and character.health > 0
            and same_room(player, character)
            and great_circle_distance_km(
                player.longitude,
                player.latitude,
                character.longitude,
                character.latitude,
            )
            <= radius_km
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
        query_tokens = ConversationContextMixin._search_tokens(intent)
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
            overlap = len(
                query_tokens & ConversationContextMixin._search_tokens(profile_text)
            )
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
