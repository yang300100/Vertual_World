"""`ConversationService`：由各职责 Mixin 组合而成的对话门面。

方法签名、`self` 语义与调用方式与拆分前完全一致，调用点无需改动。
"""

from __future__ import annotations

from world_engine.conversation.cards import ConversationCardMixin
from world_engine.conversation.context import ConversationContextMixin
from world_engine.conversation.episodes import ConversationEpisodeMixin
from world_engine.conversation.routing import ConversationRoutingMixin
from world_engine.knowledge import WorldKnowledgeBase


class ConversationService(
    ConversationContextMixin,
    ConversationEpisodeMixin,
    ConversationCardMixin,
    ConversationRoutingMixin,
):
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
