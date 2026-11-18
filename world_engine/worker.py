from __future__ import annotations

import argparse
import logging
import signal
import threading
from collections.abc import Iterator, Sequence
from datetime import timedelta

from world_engine.bounded_calls import submit_call
from world_engine.config import Settings
from world_engine.console import configure_console_encoding
from world_engine.database import Database
from world_engine.diagnostic_logging import configure_runtime_logging
from world_engine.domain import HeartbeatResult
from world_engine.engine import ConcurrentWorldUpdateError, WorldEngine
from world_engine.repository import WorldRepository, from_iso, utc_now
from world_engine.retention import apply_retention
from world_engine.world_pack import (
    check_pack_save,
    ensure_pack_save,
    ensure_pack_world,
    resolve_pack_settings,
)

LOGGER = logging.getLogger("virtual-world.worker")

# 每推进这么多轮做一次数据保留维护（按默认 60 秒心跳约合 1 小时）。
MAINTENANCE_EVERY_ROUNDS = 60


class WorldWorker:
    """独立于Web进程的自动推进服务。"""

    def __init__(self, settings: Settings) -> None:
        self.settings, self.world_pack = resolve_pack_settings(settings)
        self.database = Database(
            self.settings.database_path, legacy_world_defaults=self.world_pack is None
        )
        self.repository = WorldRepository()
        self.engine = WorldEngine(self.database, self.settings)
        self.stop_event = threading.Event()
        self._memory_future = None
        self._outreach_future = None
        self._rounds_since_maintenance = 0

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
                for result in self._world_heartbeats(world.id, elapsed_seconds):
                    LOGGER.info(
                        "世界 %s 心跳完成：%s -> %s，状态更新%s人，裁判=%s",
                        world.name,
                        result.previous_time.isoformat(),
                        result.current_time.isoformat(),
                        result.characters_updated,
                        "是" if result.adjudication else "否",
                    )
                completed += 1
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
        if self._outreach_future is None or self._outreach_future.done():
            def process_npc_contact():
                from world_engine.outreach import process_outreach
                for world in worlds:
                    if self.stop_event.is_set():
                        break
                    process_outreach(self.engine,world.id)
            self._outreach_future=submit_call(process_npc_contact)
        self._rounds_since_maintenance += 1
        if self._rounds_since_maintenance >= MAINTENANCE_EVERY_ROUNDS:
            self._rounds_since_maintenance = 0
            self._run_maintenance(worlds)
        return completed

    def _world_heartbeats(
        self, world_id: str, elapsed_seconds: float | None
    ) -> Iterator[HeartbeatResult]:
        """哥哥，逐段处理事件；中途停止时只提交已经结算的现实时间。"""
        from world_engine.collective_clock import next_boundary
        from world_engine.overnight import OvernightService
        from world_engine.collective_runner import is_collective_world
        from world_engine.science_runner import is_science_world

        real_end = utc_now()
        if elapsed_seconds is None:
            # 哥哥，现实时间在写事务内按最新基线量取，刷新与后台交错也不会重复扣算。
            progress = {}
            while True:
                result = self.engine.heartbeat(
                    world_id, real_now=real_end, _single_boundary=True,
                    _catchup_progress=progress,
                )
                yield result
                if progress.get("complete") or result.real_elapsed_seconds == 0 or self.stop_event.is_set():
                    return
        remaining = elapsed_seconds
        while True:
            with self.database.read() as connection:
                row = connection.execute(
                    "SELECT w.current_time,c.time_scale,c.last_heartbeat_real_time "
                    "FROM worlds w JOIN world_clock c ON c.world_id=w.id WHERE w.id=?",
                    (world_id,),
                ).fetchone()
                if row is None:
                    from world_engine.repository import WorldNotFoundError

                    raise WorldNotFoundError(world_id)
                if remaining is None:
                    baseline = row["last_heartbeat_real_time"]
                    remaining = (real_end - from_iso(baseline)).total_seconds() if baseline else 0
                remaining = max(0.0, float(remaining))
                step = remaining
                scale = float(row["time_scale"])
                if scale > 0 and remaining > 0 and (
                    is_collective_world(connection, world_id)
                    or is_science_world(connection, world_id)
                    or connection.execute("SELECT 1 FROM social_appointments WHERE world_id=? AND status IN ('pending','accepted','active') LIMIT 1",(world_id,)).fetchone()
                    or connection.execute("SELECT 1 FROM shared_preparation_plans WHERE world_id=? AND status IN ('pending','active') LIMIT 1",(world_id,)).fetchone()
                    or OvernightService.has_live(connection, world_id)
                ):
                    at = from_iso(row["current_time"])
                    boundary = next_boundary(connection, world_id, at,satiety_rate=self.settings.satiety_loss_per_world_hour)
                    step = min(step, (boundary - at).total_seconds() / scale)

            remaining = max(0.0, remaining - step)
            yield self.engine.heartbeat(
                world_id,
                real_now=real_end - timedelta(seconds=remaining),
                elapsed_seconds=step,
            )
            if remaining == 0 or self.stop_event.is_set():
                return

    def _run_maintenance(self, worlds) -> None:
        """定期清理已完成的事件衍生数据，避免数据库随运行无限膨胀。"""
        for world in worlds:
            if self.stop_event.is_set():
                return
            try:
                with self.database.write() as connection:
                    row = connection.execute(
                        'SELECT "current_time" AS current_time FROM worlds WHERE id = ?',
                        (world.id,),
                    ).fetchone()
                    if row is None:
                        continue
                    outcome = apply_retention(
                        connection,
                        world.id,
                        current_world_time=from_iso(row["current_time"]),
                        memory_job_keep_recent=self.settings.memory_job_keep_recent,
                        knowledge_retention_days=self.settings.knowledge_retention_days,
                    )
                if outcome.touched_anything:
                    LOGGER.info(
                        "世界 %s 数据保留：清理记忆任务%s条，归档知识%s条/目击%s条",
                        world.name,
                        outcome.memory_jobs_deleted,
                        outcome.knowledge_archived,
                        outcome.observers_archived,
                    )
            except Exception:
                LOGGER.exception("世界 %s 的数据保留维护失败", world.name)

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
    configure_runtime_logging(settings.debug_model_io)
    LOGGER.info(
        "模型 I/O 调试输出：%s",
        "已启用" if settings.debug_model_io else "未启用",
    )
    worker = WorldWorker(settings)
    if worker.world_pack is not None:
        check_pack_save(worker.database, worker.world_pack)
    worker.database.initialize()
    if worker.world_pack is not None:
        ensure_pack_save(worker.database, worker.world_pack)
        ensure_pack_world(worker.database, worker.settings, worker.world_pack)
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
