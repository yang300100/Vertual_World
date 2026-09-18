"""NPC 对话线程的持久化、按世界时间读取与上下文组装。

原先是一个 1211 行的单文件模块，现按职责拆成包：

- `types`：`NpcConversationContext`、`NpcCharacterCard`、`ScheduledDialogueSpeaker`
- `cards`：`ConversationCardMixin` 角色卡读取、补全与持久化
- `context`：`ConversationContextMixin` 回复上下文构建与 `DialogueContextAssembler` 门面
- `episodes`：`ConversationEpisodeMixin` 回合记录、NPC 记忆与提取式摘要
- `routing`：`ConversationRoutingMixin` 对话判定与目标解析
- `scheduler`：`DialogueSpeakerScheduler` 多人发言选择
- `service`：`ConversationService` 组合各职责 Mixin

`ConversationService` 仍是一个类、方法签名与 `self` 语义不变；
`world_engine/conversations.py` 保留为薄转发层，既有导入路径保持可用。
"""

from __future__ import annotations

from world_engine.conversation.cards import ConversationCardMixin
from world_engine.conversation.context import (
    _ASCII_WORD_PATTERN,
    _CJK_PATTERN,
    ConversationContextMixin,
    DialogueContextAssembler,
)
from world_engine.conversation.episodes import ConversationEpisodeMixin
from world_engine.conversation.routing import ConversationRoutingMixin
from world_engine.conversation.scheduler import DialogueSpeakerScheduler
from world_engine.conversation.service import ConversationService
from world_engine.conversation.types import (
    NpcCharacterCard,
    NpcConversationContext,
    ScheduledDialogueSpeaker,
)

__all__ = [
    # 拆分前的 8 个顶层名字，全部按原路径重导出。
    "_ASCII_WORD_PATTERN",
    "_CJK_PATTERN",
    "ConversationService",
    "DialogueContextAssembler",
    "DialogueSpeakerScheduler",
    "NpcCharacterCard",
    "NpcConversationContext",
    "ScheduledDialogueSpeaker",
    # 新增的职责 Mixin 与模块级符号。
    "ConversationCardMixin",
    "ConversationContextMixin",
    "ConversationEpisodeMixin",
    "ConversationRoutingMixin",
]
