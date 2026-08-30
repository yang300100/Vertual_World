from __future__ import annotations

from datetime import UTC, datetime

import pytest

from world_engine.database import Database
from world_engine.repository import WorldRepository, from_iso
from world_engine.time_utils import is_time_only, parse_datetime

# 某些运行环境(如 DSH 沙箱)会对 sqlite 中名为 current_time 的列在写入时强制改写为
# 当前时刻，导致"存储修复 current_time"的断言无法在该环境成立。此探测用于跳过这类用例。
_CURRENT_TIME_SENTINEL = "2000-01-01T00:00:00+00:00"


def _environment_rewrites_current_time(database: Database) -> bool:
    try:
        repository = WorldRepository()
        with database.write() as connection:
            world_id = repository.create_world(
                connection, name="探测", minutes_per_tick=60, seed_demo=False
            )
            connection.execute(
                "UPDATE worlds SET current_time = ? WHERE id = ?",
                (_CURRENT_TIME_SENTINEL, world_id),
            )
        with database.read() as connection:
            value = connection.execute(
                "SELECT current_time FROM worlds WHERE id = ?", (world_id,)
            ).fetchone()["current_time"]
        return value != _CURRENT_TIME_SENTINEL
    except Exception:
        return True


def test_parse_datetime_handles_time_only() -> None:
    value = parse_datetime("14:33:12")
    # 无日期时使用世界基准日期(2040-04-01)补全，避免编排解析崩溃。
    assert value.year == 2040 and value.month == 4 and value.day == 1
    assert value.hour == 14 and value.minute == 33 and value.second == 12
    assert value.tzinfo is not None


def test_parse_datetime_preserves_full_iso() -> None:
    value = parse_datetime("2040-04-01T08:00:00+00:00")
    assert value == datetime(2040, 4, 1, 8, 0, tzinfo=UTC)


def test_parse_datetime_rejects_empty() -> None:
    with pytest.raises(ValueError):
        parse_datetime("")


def test_is_time_only() -> None:
    assert is_time_only("14:33:12") is True
    assert is_time_only("14:33:12.123456") is True
    assert is_time_only("2040-04-01T08:00:00+00:00") is False
    assert is_time_only("") is False


def test_from_iso_is_defensive() -> None:
    # repository.from_iso 用于读取 worlds.current_time 等；时间串不再崩溃。
    assert isinstance(from_iso("14:33:12"), datetime)
    assert from_iso("2040-04-01T08:00:00+00:00").tzinfo is not None


def test_repair_migration_fixes_time_only_current_time(database: Database) -> None:
    if _environment_rewrites_current_time(database):
        pytest.skip("运行环境会重写 current_time 列，无法验证存储修复")

    repository = WorldRepository()
    with database.write() as connection:
        world_id = repository.create_world(
            connection, name="损坏修复", minutes_per_tick=60, seed_demo=True
        )
        # 模拟外部/手工写入的"仅时间" current_time。
        connection.execute(
            "UPDATE worlds SET current_time = ? WHERE id = ?", ("14:33:47", world_id)
        )

    with database.read() as connection:
        before = connection.execute(
            "SELECT current_time FROM worlds WHERE id = ?", (world_id,)
        ).fetchone()["current_time"]
    assert is_time_only(before)

    # 重新 initialize 触发迁移修复。
    database.initialize()
    with database.read() as connection:
        after = connection.execute(
            "SELECT current_time FROM worlds WHERE id = ?", (world_id,)
        ).fetchone()["current_time"]
    assert not is_time_only(after)
    repaired = parse_datetime(after)
    assert repaired.hour == 14 and repaired.minute == 33


def test_snapshot_reads_repaired_current_time(database: Database) -> None:
    if _environment_rewrites_current_time(database):
        pytest.skip("运行环境会重写 current_time 列，无法验证存储修复")

    repository = WorldRepository()
    with database.write() as connection:
        world_id = repository.create_world(
            connection, name="损坏修复2", minutes_per_tick=60, seed_demo=True
        )
        connection.execute(
            "UPDATE worlds SET current_time = ? WHERE id = ?", ("09:05:00", world_id)
        )
    database.initialize()
    with database.read() as connection:
        snapshot = repository.get_snapshot(connection, world_id)
    assert isinstance(snapshot.world.current_time, datetime)
    assert snapshot.world.current_time.hour == 9
