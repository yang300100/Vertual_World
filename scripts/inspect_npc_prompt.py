"""只读导出当前存档的一次 NPC 请求，不调用模型、不推进世界。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from world_engine.config import Settings
from world_engine.conversations import ConversationService
from world_engine.knowledge import WorldKnowledgeBase
from world_engine.repository import WorldRepository
from world_engine.roleplay import build_npc_reply_messages, npc_reply_system_prompt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--world", help="世界 ID；默认读取第一个世界")
    parser.add_argument("--npc", help="NPC ID 或姓名；默认读取第一个非玩家人物")
    parser.add_argument("--text", required=True, help="本轮玩家原话")
    parser.add_argument("--output", type=Path, required=True, help="导出的 JSON 路径")
    args = parser.parse_args()
    settings = Settings.from_env()
    knowledge = (
        WorldKnowledgeBase.from_paths(settings.knowledge_paths)
        if settings.knowledge_enabled else None
    )
    service = ConversationService(
        max_context_chars=settings.dialogue_context_max_chars,
        max_context_tokens=settings.dialogue_context_max_tokens,
        memory_top_k=settings.dialogue_memory_top_k,
        knowledge_top_k=settings.dialogue_knowledge_top_k,
        episode_turn_threshold=settings.dialogue_episode_turn_threshold,
        episode_top_k=settings.dialogue_episode_top_k,
        knowledge_base=knowledge,
    )
    connection = sqlite3.connect(settings.database_path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        world = connection.execute("SELECT id FROM worlds ORDER BY rowid LIMIT 1").fetchone()
        if not args.world and world is None:
            parser.error("存档中没有世界")
        snapshot = WorldRepository().get_snapshot(connection, args.world or world["id"])
        player = next((item for item in snapshot.characters if item.is_player), None)
        npc = next((item for item in snapshot.characters if not item.is_player and (
            not args.npc or args.npc in {item.id, item.name}
        )), None)
        if player is None or npc is None:
            parser.error("请先创建玩家，并选择当前世界中存在的 NPC")
        context = service.build_npc_reply_context(
            connection, snapshot=snapshot, npc=npc, player=player,
            player_text=args.text, conversation=None,
        )
        messages = build_npc_reply_messages(npc_reply_system_prompt(), context)
        result = {
            "npc_id": npc.id, "npc_name": npc.name,
            "budget_trace": context["budget_trace"],
            "estimated_request_tokens": service.estimate_tokens(messages),
            "max_output_tokens": min(settings.deepseek_max_output_tokens, 1200),
            "messages": messages,
        }
    finally:
        connection.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "messages"},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
