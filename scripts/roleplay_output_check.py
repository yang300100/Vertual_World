"""对比 NPC 回复的两种输出契约。只读存档，不写入任何数据，不推进世界。

模式 json：现状 —— 走 `AgentModelBackend.complete(roleplay=True)`，请求带
           `response_format={"type":"json_object"}`，模型必须输出 JSON。
模式 free：拟改 —— 不带 `response_format`，模型自由说台词，再由 `salvage_reply`
           宽容解析（能认出 {"reply":…} 就取 reply，认不出就把整段当台词）。

同一个问题、同一份上下文，各跑一次，比较成功率和回复长度。

用法：
    .venv/Scripts/python.exe -X utf8 -m scripts.roleplay_output_check --npc '伊蕾娜·星绘'
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from pydantic import TypeAdapter

from scripts.roleplay_card_ab import build_service, salvage_reply
from world_engine.agent_llm import AgentModelBackend, build_agent_model_backend
from world_engine.config import Settings
from world_engine.decisions import NpcReply
from world_engine.repository import WorldRepository
from world_engine.roleplay import build_npc_reply_messages, npc_reply_system_prompt

DEFAULT_TURNS = (
    "你好，今天过得怎么样？",
    "附近有什么值得去的地方吗？",
    "你平时除了干活，还喜欢做什么？",
    "你觉得镇上的粮食够吃吗？",
)


def call_json_mode(backend: AgentModelBackend, context: dict) -> tuple[str, str, str]:
    """现状路径：结构化输出。返回 (台词, social_move, 备注)。"""
    completion = backend.complete(
        system_prompt=npc_reply_system_prompt(),
        user_payload=context,
        schema=TypeAdapter(NpcReply),
        label="json_mode",
        roleplay=True,
    )
    return completion.data.reply, completion.data.social_move, ""


def call_free_mode(backend: AgentModelBackend, context: dict) -> tuple[str, str, str]:
    """拟改路径：自由文本 + 宽容解析。返回 (台词, social_move, 备注)。"""
    response = backend.client.post(
        "chat/completions",
        json={
            "model": backend.model,
            "messages": build_npc_reply_messages(npc_reply_system_prompt(), context),
            "max_tokens": min(backend.max_tokens, 1200),
            "stream": False,
        },
    )
    response.raise_for_status()
    content = (response.json()["choices"][0]["message"].get("content") or "").strip()
    if not content:
        raise RuntimeError("模型正文为空")
    reply, social_move = salvage_reply(content)
    note = "解析出JSON" if content.lstrip().startswith(("{", "```")) else "纯文本"
    return reply, social_move, note


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--world", help="世界 ID；默认第一个世界")
    parser.add_argument("--npc", help="NPC ID 或姓名；默认第一个非玩家人物")
    parser.add_argument("--turns", action="append", help="一轮玩家输入，可重复")
    parser.add_argument("--output", type=Path, help="结果 JSON 路径")
    args = parser.parse_args()

    settings = Settings.from_env()
    backend = build_agent_model_backend(settings)
    if backend is None:
        raise SystemExit("未配置 DEEPSEEK_API_KEY")

    turns = tuple(args.turns) if args.turns else DEFAULT_TURNS
    service = build_service(settings)
    connection = sqlite3.connect(
        settings.database_path.resolve().as_uri() + "?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    rows: list[dict] = []
    try:
        connection.execute("BEGIN")
        world = connection.execute("SELECT id FROM worlds ORDER BY rowid LIMIT 1").fetchone()
        snapshot = WorldRepository().get_snapshot(connection, args.world or world["id"])
        player = next(c for c in snapshot.characters if c.is_player)
        npc = next(
            c for c in snapshot.characters
            if not c.is_player and (not args.npc or args.npc in {c.id, c.name})
        )
        print(f"NPC：{npc.name}（{npc.identity}）· 模型 {settings.deepseek_model}\n")
        for question in turns:
            context = service.build_npc_reply_context(
                connection, snapshot=snapshot, npc=npc, player=player,
                player_text=question, conversation=None,
            )
            row = {"question": question}
            for mode, caller in (("json", call_json_mode), ("free", call_free_mode)):
                try:
                    reply, move, note = caller(backend, context)
                    row[mode] = {"ok": True, "reply": reply, "social_move": move,
                                 "note": note, "length": len(reply)}
                except Exception as exc:  # 失败也是一种结果，如实记录
                    row[mode] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            rows.append(row)
            print(f"「{question}」")
            for mode in ("json", "free"):
                item = row[mode]
                if item["ok"]:
                    print(f"    {mode:>4} ✔ {item['length']:>3}字 [{item['note'] or item['social_move']}] {item['reply'][:60]}")
                else:
                    print(f"    {mode:>4} ✘ {item['error'][:70]}")
            print()
    finally:
        connection.close()
        backend.close()

    ok_json = sum(1 for r in rows if r["json"]["ok"])
    ok_free = sum(1 for r in rows if r["free"]["ok"])
    print("=" * 66)
    print(f"成功   json {ok_json}/{len(rows)}    free {ok_free}/{len(rows)}")
    for mode in ("json", "free"):
        lengths = [r[mode]["length"] for r in rows if r[mode]["ok"]]
        if lengths:
            print(f"平均字数 {mode} {sum(lengths) / len(lengths):.0f}")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已写入 {args.output}")


if __name__ == "__main__":
    main()
