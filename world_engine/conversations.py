"""兼容转发层：对话管线已拆到 `world_engine.conversation` 包。

保留本模块是为了让既有的 `from world_engine.conversations import ...` 写法继续可用，
同时避免同名包目录与旧模块文件互相遮蔽（混用可能加载到过期的
`__pycache__/conversations.cpython-*.pyc`）。
新代码请直接导入 `world_engine.conversation`。
"""

from __future__ import annotations

from world_engine.conversation import (
    _ASCII_WORD_PATTERN,
    _CJK_PATTERN,
    ConversationService,
    DialogueContextAssembler,
    DialogueSpeakerScheduler,
    NpcCharacterCard,
    NpcConversationContext,
    ScheduledDialogueSpeaker,
)

__all__ = [
    "_ASCII_WORD_PATTERN",
    "_CJK_PATTERN",
    "ConversationService",
    "DialogueContextAssembler",
    "DialogueSpeakerScheduler",
    "NpcCharacterCard",
    "NpcConversationContext",
    "ScheduledDialogueSpeaker",
]
