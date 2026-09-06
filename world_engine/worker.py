from __future__ import annotations

import argparse
import logging
import signal
import threading
from collections.abc import Sequence

from world_engine.bounded_calls import submit_call
from world_engine.config import Settings
from world_engine.console import configure_console_encoding
from world_engine.database import Database
from world_engine.engine import ConcurrentWorldUpdateError, WorldEngine
from world_engine.repository import WorldRepository

LOGGER = logging.getLogger("virtual-world.worker")


class WorldWorker:
    """独立于Web进程的自动推进服务。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.database = Database(settings.database_path)
        self.repository = WorldRepository()
        self.engine = WorldEngine(self.database, settings)
        self.stop_event = threading.Event()
        self._memory_future = None

    def run_once(self, *, elapsed_seconds: float | None = None) -> int:
        with self.database.read() as connection:
            worlds = [
                item
                for item in self.repository.list_worlds(connection)
                if item.status == "running"
            ]
        completed = 0
        for world in worlds:
            if self.stop_event.is_set():
                break
            try:
                result = self.engine.heartbeat(
                    world.id,
                    elapsed_seconds=elapsed_seconds,
                )
                completed += 1
                LOGGER.info(
                    "世界 %s 心跳完成：%s -> %s，状态更新%s人，裁判=%s",
                    world.name,
                    result.previous_time.isoformat(),
                    result.current_time.isoformat(),
                    result.characters_updated,
                    "是" if result.adjudication else "否",
                )
            except ConcurrentWorldUpdateError:
                LOGGER.info("世界 %s 正由另一进程推进，本轮跳过", world.name)
            except Exception:
                LOGGER.exception("推进世界 %s 时发生错误", world.name)
        if self._memory_future is None or self._memory_future.done():
            world_ids = [world.id for world in worlds]

            def process_memories():
                for world_id in world_ids:
                    if self.stop_event.is_set():
                        break
                    self.engine.process_memory_jobs(world_id)

            self._memory_future = submit_call(process_memories)
        return completed

    def run_forever(self) -> None:
        reset_count = self.engine.reset_offline_baseline()
        self.engine.mark_worker_seen()
        LOGGER.info(
            "世界worker已启动，已暂停补算%s个世界，心跳间隔%s秒",
            reset_count,
            self.settings.worker_interval_seconds,
        )
        while not self.stop_event.is_set():
            if self.stop_event.wait(self.settings.worker_interval_seconds):
                break
            self.engine.mark_worker_seen()
            self.run_once()
        LOGGER.info("世界worker已停止")

    def stop(self, *_: object) -> None:
        self.stop_event.set()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="独立虚拟世界推进worker")
    parser.add_argument("--once", action="store_true", help="只推进一轮后退出")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    configure_console_encoding()
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    worker = WorldWorker(settings)
    worker.database.initialize()
    signal.signal(signal.SIGINT, worker.stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, worker.stop)
    if args.once:
        try:
            worker.engine.reset_offline_baseline()
            return 0 if worker.run_once(elapsed_seconds=0) >= 0 else 1
        finally:
            worker.engine.close()
    try:
        worker.run_forever()
        return 0
    finally:
        worker.engine.clear_worker_seen()
        worker.engine.close()


if __name__ == "__main__":
    raise SystemExit(main())
