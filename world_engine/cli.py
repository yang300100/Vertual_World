from __future__ import annotations

import argparse
import atexit
import json
from collections.abc import Sequence

from world_engine.config import Settings
from world_engine.console import configure_console_encoding
from world_engine.database import Database
from world_engine.engine import WorldEngine
from world_engine.repository import WorldRepository


def _print_json(value: object) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="虚拟世界内核命令行")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="创建一个世界")
    create.add_argument("--name", required=True)
    create.add_argument("--minutes-per-tick", type=int)
    create.add_argument("--time-scale", type=float)
    create.add_argument("--empty", action="store_true", help="不创建演示人物与地点")

    subparsers.add_parser("list", help="列出所有世界")

    show = subparsers.add_parser("show", help="查看世界快照")
    show.add_argument("world_id")

    tick = subparsers.add_parser("tick", help="兼容入口：强制裁判但不推进时间")
    tick.add_argument("world_id")

    heartbeat = subparsers.add_parser("heartbeat", help="执行一次状态心跳")
    heartbeat.add_argument("world_id")
    heartbeat.add_argument(
        "--elapsed-seconds",
        type=float,
        help="管理与测试用途：覆盖实际经过秒数",
    )

    speed = subparsers.add_parser("speed", help="调整世界时间比例")
    speed.add_argument("world_id")
    speed.add_argument("time_scale", type=float)
    speed.add_argument("--operator", default="main_view")

    adjudicate = subparsers.add_parser("adjudicate", help="立即执行一次模型裁判")
    adjudicate.add_argument("world_id")
    adjudicate.add_argument(
        "--trigger",
        choices=["manual", "player_intervention"],
        default="manual",
    )
    adjudicate.add_argument("--character-id", action="append", dest="character_ids")

    events = subparsers.add_parser("events", help="查看世界事件")
    events.add_argument("world_id")
    events.add_argument("--limit", type=int, default=50)

    history = subparsers.add_parser("history", help="同步并查看世界历史日志位置")
    history.add_argument("world_id")

    memories = subparsers.add_parser("memories", help="查看人物记忆")
    memories.add_argument("world_id")
    memories.add_argument("character_id")
    memories.add_argument("--limit", type=int, default=50)

    adjudications = subparsers.add_parser("adjudications", help="查看模型裁判记录")
    adjudications.add_argument("world_id")
    adjudications.add_argument("--limit", type=int, default=50)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    configure_console_encoding()
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()
    database = Database(settings.database_path)
    database.initialize()
    repository = WorldRepository()
    engine = WorldEngine(database, settings)
    atexit.register(engine.close)

    if args.command == "create":
        with database.write() as connection:
            world_id = repository.create_world(
                connection,
                name=args.name,
                minutes_per_tick=args.minutes_per_tick or settings.minutes_per_tick,
                time_scale=(
                    args.time_scale
                    if args.time_scale is not None
                    else settings.default_time_scale
                ),
                adjudication_interval_minutes=settings.adjudication_interval_minutes,
                seed_demo=not args.empty,
            )
            _print_json(repository.get_snapshot(connection, world_id))
        return 0
    if args.command == "list":
        with database.read() as connection:
            worlds = repository.list_worlds(connection)
            _print_json([item.model_dump(mode="json") for item in worlds])
        return 0
    if args.command == "show":
        with database.read() as connection:
            _print_json(repository.get_snapshot(connection, args.world_id))
        return 0
    if args.command == "tick":
        _print_json(engine.tick(args.world_id))
        return 0
    if args.command == "heartbeat":
        _print_json(
            engine.heartbeat(
                args.world_id,
                elapsed_seconds=args.elapsed_seconds,
            )
        )
        return 0
    if args.command == "speed":
        _print_json(
            engine.set_time_scale(
                args.world_id,
                args.time_scale,
                operator=args.operator,
            )
        )
        return 0
    if args.command == "adjudicate":
        _print_json(
            engine.adjudicate(
                args.world_id,
                trigger=args.trigger,
                character_ids=args.character_ids,
            )
        )
        return 0
    if args.command == "events":
        with database.read() as connection:
            _print_json(repository.list_events(connection, args.world_id, args.limit))
        return 0
    if args.command == "history":
        _print_json(engine.sync_history(args.world_id))
        return 0
    if args.command == "memories":
        with database.read() as connection:
            _print_json(
                repository.list_memories(
                    connection, args.world_id, args.character_id, args.limit
                )
            )
        return 0
    if args.command == "adjudications":
        with database.read() as connection:
            _print_json(
                repository.list_adjudication_runs(
                    connection, args.world_id, args.limit
                )
            )
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
