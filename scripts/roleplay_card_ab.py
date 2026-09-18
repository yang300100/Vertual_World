"""角色卡 A/B 对比：同一个 NPC、同一组对话，只替换角色卡，比较模型输出。

用于验证"角色卡的内容是否真的影响 NPC 的说话方式与话题走向"。

设计约束：
- 全程只读存档，不写入任何数据，不推进世界。
- 上下文复用真实链路（ConversationService.build_npc_reply_context →
  roleplay.build_npc_reply_messages），只把 npc_card 这一段替换成待比较的卡。
- 模型调用刻意绕开 AgentModelBackend.complete(roleplay=True)：后者会带上
  response_format=json_object，而当前推理模型在该模式下会把正文输出成空白
  （实测 3/3 失败，finish_reason 仍为 stop）。理由详见 call_npc_model。
- 需要 DEEPSEEK_API_KEY；每轮对话各产生一次模型调用，请自行控制轮数。

用法：
    .venv/Scripts/python.exe -X utf8 -m scripts.roleplay_card_ab \\
        --npc '伊蕾娜·星绘' \\
        --card-a run/roleplay-card-experiment/irena-card-BEFORE.json \\
        --output run/roleplay-card-experiment/ab-result.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from world_engine.agent_llm import AgentModelBackend, build_agent_model_backend
from world_engine.config import Settings
from world_engine.conversation import ConversationService, NpcCharacterCard
from world_engine.knowledge import WorldKnowledgeBase
from world_engine.repository import WorldRepository
from world_engine.roleplay import build_npc_reply_messages, npc_reply_system_prompt

# 粗略衡量"话题是否绕回本职工作"的词表。这不是精确判据，只用于两张卡之间的
# 横向对比；出现"路"这类多义字时会有噪声，看结果时以原文为准。
WORK_WORDS = ("星图", "测绘", "记录", "北境", "桩", "标", "图", "量", "路")

DEFAULT_TURNS = (
    "你好，今天过得怎么样？",
    "附近有什么值得去的地方吗？",
    "你平时除了干活，还喜欢做什么？",
)


def salvage_reply(content: str) -> tuple[str, str]:
    """从模型正文里取出台词与 social_move。

    模型有时会惯性输出 JSON 或包一层 ``` 围栏，有时直接说台词。两种都接受：
    能认出 {"reply": ...} 就取 reply，认不出就把整段文本当作台词。
    """
    text = content.strip()
    if text.startswith("```"):
        text = "\n".join(
            line for line in text.splitlines() if not line.strip().startswith("```")
        ).strip()
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            raw = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            raw = None
        if isinstance(raw, dict):
            reply = raw.get("reply")
            if isinstance(reply, str) and reply.strip():
                return reply.strip(), str(raw.get("social_move") or "answer")
    return text, "answer"


def call_npc_model(backend: AgentModelBackend, context: dict) -> tuple[str, str, dict]:
    """直发一次 NPC 回复请求，返回 (台词, social_move, usage)。

    刻意不走 `AgentModelBackend.complete(roleplay=True)`：
    后者会带上 `response_format={"type":"json_object"}`，而当前推理模型在该模式下
    会把正文输出成空白（实测 3/3 失败，finish_reason 仍是 stop）。
    去掉该模式后台词质量显著更高，代价是需要上面的宽容解析。
    """
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
    data = response.json()
    choice = data["choices"][0]
    content = (choice["message"].get("content") or "").strip()
    if not content:
        raise RuntimeError(
            f"模型正文为空（finish_reason={choice.get('finish_reason')}）"
        )
    reply, social_move = salvage_reply(content)
    return reply, social_move, data.get("usage") or {}


def build_service(settings: Settings) -> ConversationService:
    """按与线上一致的配置构造对话服务。"""
    knowledge = (
        WorldKnowledgeBase.from_paths(settings.knowledge_paths)
        if settings.knowledge_enabled
        else None
    )
    return ConversationService(
        max_context_chars=settings.dialogue_context_max_chars,
        max_context_tokens=settings.dialogue_context_max_tokens,
        memory_top_k=settings.dialogue_memory_top_k,
        knowledge_top_k=settings.dialogue_knowledge_top_k,
        episode_turn_threshold=settings.dialogue_episode_turn_threshold,
        episode_top_k=settings.dialogue_episode_top_k,
        knowledge_base=knowledge,
    )


def load_card(path: Path | None, *, fallback: NpcCharacterCard) -> NpcCharacterCard:
    """读取一张卡；未提供文件时用调用方给的兜底卡（通常是库里当前的卡）。"""
    if path is None:
        return fallback
    return NpcCharacterCard.from_dict(json.loads(path.read_text(encoding="utf-8")))


def run_conversation(
    *,
    service: ConversationService,
    connection: sqlite3.Connection,
    snapshot,
    npc,
    player,
    card: NpcCharacterCard,
    turns: Sequence[str],
    backend: AgentModelBackend,
) -> list[dict]:
    """用同一张卡跑完整轮对话，返回逐轮记录。

    每轮都重新构造上下文（与真实链路一致），但把先前轮次的原话按存储格式
    追加进 recent_conversation，使多轮对话具备真实的承接关系。
    """
    transcript: list[dict] = []
    history: list[str] = []
    world_time = ""
    for index, player_text in enumerate(turns):
        context = service.build_npc_reply_context(
            connection,
            snapshot=snapshot,
            npc=npc,
            player=player,
            player_text=player_text,
            conversation=None,
        )
        # 只替换角色卡这一段，其余上下文保持真实产出的原样。
        context["npc_card"] = card.to_dict()
        context["recent_conversation"] = list(history)
        if index == 0:
            world_time = str(context.get("world_time") or "")
        reply_text, social_move, usage = call_npc_model(backend, context)
        history.append(f"[{world_time}] 玩家：{player_text}")
        history.append(f"[{world_time}] NPC：{reply_text}")
        transcript.append(
            {
                "turn": index + 1,
                "player_text": player_text,
                "reply": reply_text,
                "social_move": social_move,
                "work_word_hits": sum(reply_text.count(word) for word in WORK_WORDS),
                "reply_length": len(reply_text),
                "input_tokens": usage.get("prompt_tokens"),
                "output_tokens": usage.get("completion_tokens"),
            }
        )
    return transcript


def summarize(transcript: Sequence[dict]) -> dict:
    turns = len(transcript)
    return {
        "turns": turns,
        "total_work_word_hits": sum(item["work_word_hits"] for item in transcript),
        "turns_mentioning_work": sum(1 for item in transcript if item["work_word_hits"]),
        "avg_reply_length": (
            round(sum(item["reply_length"] for item in transcript) / turns, 1) if turns else 0
        ),
        "social_moves": [item["social_move"] for item in transcript],
        "total_output_tokens": sum(item["output_tokens"] or 0 for item in transcript),
        "estimated_cost_note": "token 数来自接口 usage 字段，费用请按所选模型的当前价目表自行换算",
    }


def render(label: str, transcript: Sequence[dict], summary: dict) -> None:
    print(f"\n{'=' * 62}\n{label}\n{'=' * 62}")
    for item in transcript:
        print(f"\n[{item['turn']}] 玩家：{item['player_text']}")
        print(f"    NPC：{item['reply']}")
        print(
            f"    （social_move={item['social_move']}"
            f" · 工作词 {item['work_word_hits']}"
            f" · {item['reply_length']} 字）"
        )
    print(
        f"\n  小结：{summary['turns_mentioning_work']}/{summary['turns']} 轮提到工作词"
        f" · 累计工作词 {summary['total_work_word_hits']}"
        f" · 平均 {summary['avg_reply_length']} 字"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--world", help="世界 ID；默认第一个世界")
    parser.add_argument("--npc", help="NPC ID 或姓名；默认第一个非玩家人物")
    parser.add_argument(
        "--card-a", type=Path, help="A 卡 JSON；不给则用存档里当前的卡"
    )
    parser.add_argument(
        "--card-b", type=Path, help="B 卡 JSON；不给则用存档里当前的卡"
    )
    parser.add_argument(
        "--turns",
        action="append",
        help="一轮玩家输入，可重复传入；不给则用内置的三轮测试句",
    )
    parser.add_argument("--output", type=Path, help="结果 JSON 路径")
    args = parser.parse_args()

    settings = Settings.from_env()
    backend = build_agent_model_backend(settings)
    if backend is None:
        raise SystemExit("未配置 DEEPSEEK_API_KEY，无法进行 A/B 对比")

    turns = tuple(args.turns) if args.turns else DEFAULT_TURNS
    service = build_service(settings)
    repository = WorldRepository()
    connection = sqlite3.connect(
        settings.database_path.resolve().as_uri() + "?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    result: dict = {"world": None, "npc": None, "turns": list(turns)}
    try:
        connection.execute("BEGIN")
        world = connection.execute("SELECT id FROM worlds ORDER BY rowid LIMIT 1").fetchone()
        if not args.world and world is None:
            parser.error("存档中没有世界")
        snapshot = repository.get_snapshot(connection, args.world or world["id"])
        player = next((item for item in snapshot.characters if item.is_player), None)
        npc = next(
            (
                item
                for item in snapshot.characters
                if not item.is_player and (not args.npc or args.npc in {item.id, item.name})
            ),
            None,
        )
        if player is None or npc is None:
            parser.error("请先创建玩家，并选择当前世界中存在的 NPC")

        current_card = service.get_card(
            connection, world_id=snapshot.world.id, npc=npc
        )
        card_a = load_card(args.card_a, fallback=current_card)
        card_b = load_card(args.card_b, fallback=current_card)

        result["world"] = snapshot.world.id
        result["npc"] = {"id": npc.id, "name": npc.name, "identity": npc.identity}
        result["turns"] = list(turns)

        runs = {}
        for label, card in (("A", card_a), ("B", card_b)):
            print(f"\n正在跑卡 {label} …（{len(turns)} 轮，各一次模型调用）")
            transcript = run_conversation(
                service=service,
                connection=connection,
                snapshot=snapshot,
                npc=npc,
                player=player,
                card=card,
                turns=turns,
                backend=backend,
            )
            summary = summarize(transcript)
            runs[label] = {
                "card": card.to_dict(),
                "transcript": transcript,
                "summary": summary,
            }
            render(f"卡 {label}", transcript, summary)
        result["runs"] = runs
    finally:
        connection.close()
        backend.close()

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n结果已写入 {args.output}")


if __name__ == "__main__":
    main()
