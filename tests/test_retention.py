from __future__ import annotations

from datetime import timedelta

from world_engine.repository import WorldRepository, from_iso, to_iso, utc_now
from world_engine.retention import (
    archive_old_knowledge,
    knowledge_cutoff,
    prune_completed_memory_jobs,
)


def _create_world(database) -> str:
    with database.write() as connection:
        return WorldRepository().create_world(
            connection,
            name="保留策略世界",
            minutes_per_tick=60,
            seed_demo=True,
        )


def _seed_events(database, world_id: str, count: int) -> None:
    """造出若干事件；world_events 的触发器会同步生成等量的记忆任务。"""
    with database.write() as connection:
        for index in range(count):
            connection.execute(
                """
                INSERT INTO world_events(
                    id, world_id, tick_id, occurred_at, event_type, summary,
                    importance, payload_json, created_at
                ) VALUES (?, ?, ?, ?, 'action.idle', '测试事件', 'routine', '{}', ?)
                """,
                (f"evt-{index}", world_id, f"tick-{index}", to_iso(utc_now()), to_iso(utc_now())),
            )


def _memory_job_count(database, world_id: str, *, status: str | None = None) -> int:
    with database.read() as connection:
        if status is None:
            return connection.execute(
                "SELECT COUNT(*) FROM memory_jobs WHERE world_id = ?", (world_id,)
            ).fetchone()[0]
        return connection.execute(
            "SELECT COUNT(*) FROM memory_jobs WHERE world_id = ? AND status = ?",
            (world_id, status),
        ).fetchone()[0]


def test_completed_memory_jobs_are_pruned_but_pending_kept(database) -> None:
    """已完成的记忆任务应被清理，未完成的必须原样保留。"""
    world_id = _create_world(database)
    _seed_events(database, world_id, 10)
    with database.write() as connection:
        connection.execute(
            "UPDATE memory_jobs SET status='done' WHERE world_id = ? AND id LIKE 'memjob:evt-%'",
            (world_id,),
        )
        # 第一个事件的任务保持 pending，模拟尚未处理。
        connection.execute(
            "UPDATE memory_jobs SET status='pending' WHERE world_id = ? AND id = 'memjob:evt-0'",
            (world_id,),
        )

    assert _memory_job_count(database, world_id, status="done") == 9

    with database.write() as connection:
        deleted = prune_completed_memory_jobs(connection, world_id, keep_recent=0)

    assert deleted == 9
    assert _memory_job_count(database, world_id, status="done") == 0
    assert _memory_job_count(database, world_id, status="pending") == 1


def test_completed_memory_jobs_keep_recent_window(database) -> None:
    """保留窗口内的已完成任务不应被删除。"""
    world_id = _create_world(database)
    _seed_events(database, world_id, 8)
    with database.write() as connection:
        connection.execute(
            "UPDATE memory_jobs SET status='done' WHERE world_id = ?", (world_id,)
        )

    with database.write() as connection:
        deleted = prune_completed_memory_jobs(connection, world_id, keep_recent=3)

    assert deleted == 5
    assert _memory_job_count(database, world_id, status="done") == 3


def test_knowledge_archive_moves_records_without_losing_them(database) -> None:
    """归档把旧目击记录移入归档表，总量不丢、主表释放。"""
    world_id = _create_world(database)
    _seed_events(database, world_id, 6)
    with database.write() as connection:
        rows = connection.execute(
            "SELECT id FROM world_events WHERE world_id = ?", (world_id,)
        ).fetchall()
        observer = connection.execute(
            "SELECT id FROM characters WHERE world_id = ? LIMIT 1", (world_id,)
        ).fetchone()["id"]
        for row in rows:
            connection.execute(
                """
                INSERT OR IGNORE INTO event_observers(event_id, character_id, channel)
                VALUES (?, ?, 'participant')
                """,
                (row["id"], observer),
            )
        observer_total = connection.execute(
            "SELECT COUNT(*) FROM event_observers"
        ).fetchone()[0]
        connection.execute(
            "UPDATE world_events SET occurred_at = ? WHERE world_id = ?",
            (to_iso(utc_now() - timedelta(days=400)), world_id),
        )

    cutoff = knowledge_cutoff(utc_now(), 30)
    with database.write() as connection:
        archive_old_knowledge(connection, world_id, before_world_time=cutoff)
        remaining = connection.execute(
            "SELECT COUNT(*) FROM event_observers"
        ).fetchone()[0]
        archived = connection.execute(
            "SELECT COUNT(*) FROM event_observers_archive"
        ).fetchone()[0]

    assert observer_total > 0
    assert remaining == 0
    assert archived == observer_total


def test_knowledge_within_window_is_untouched(database) -> None:
    """保留窗口内的目击记录不能被归档。"""
    world_id = _create_world(database)
    _seed_events(database, world_id, 4)
    with database.write() as connection:
        row = connection.execute(
            "SELECT id FROM world_events WHERE world_id = ? LIMIT 1", (world_id,)
        ).fetchone()
        observer = connection.execute(
            "SELECT id FROM characters WHERE world_id = ? LIMIT 1", (world_id,)
        ).fetchone()["id"]
        connection.execute(
            """
            INSERT OR IGNORE INTO event_observers(event_id, character_id, channel)
            VALUES (?, ?, 'participant')
            """,
            (row["id"], observer),
        )

    cutoff = knowledge_cutoff(utc_now(), 30)
    with database.write() as connection:
        before = connection.execute("SELECT COUNT(*) FROM event_observers").fetchone()[0]
        archive_old_knowledge(connection, world_id, before_world_time=cutoff)
        after = connection.execute("SELECT COUNT(*) FROM event_observers").fetchone()[0]

    assert after == before


def test_world_time_cutoff_is_derived_from_retention_days() -> None:
    """截止时间应正好等于当前世界时间减去保留天数。"""
    now = from_iso(to_iso(utc_now()))
    cutoff = knowledge_cutoff(now, 10)
    assert from_iso(cutoff) == now - timedelta(days=10)
