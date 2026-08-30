from __future__ import annotations

from datetime import UTC, datetime, timedelta

from world_engine.iserra_time import (
    ERAS,
    MAIN_ERA,
    IserraCalendar,
    to_era,
    to_era_year,
    to_iserra_calendar,
)

BASE = datetime(2040, 4, 1, 8, 0, tzinfo=UTC)


def test_anchor_maps_to_h_24816_new_year() -> None:
    """引擎硬编码起始时间应对应 H 24816 年 1 月 1 日 08:00。"""
    result = to_iserra_calendar(BASE)
    assert result == IserraCalendar(24816, 1, 1, 8, 0, 0)


def test_one_month_progress_moves_to_month_two() -> None:
    result = to_iserra_calendar(BASE + timedelta(days=30))
    assert result == IserraCalendar(24816, 2, 1, 8, 0, 0)


def test_one_full_year_advances_h_year() -> None:
    result = to_iserra_calendar(BASE + timedelta(days=360))
    assert result == IserraCalendar(24817, 1, 1, 8, 0, 0)


def test_sub_day_shift_keeps_same_calendar_day() -> None:
    result = to_iserra_calendar(BASE + timedelta(hours=1))
    assert result == IserraCalendar(24816, 1, 1, 9, 0, 0)


def test_negative_offset_wraps_to_previous_year() -> None:
    """早于锚点的世界时间应正确落到上一年(12 月 30 日)。"""
    result = to_iserra_calendar(BASE - timedelta(days=1))
    assert result == IserraCalendar(24815, 12, 30, 8, 0, 0)


def test_within_month_counts_days() -> None:
    result = to_iserra_calendar(BASE + timedelta(days=30, hours=1))
    assert result == IserraCalendar(24816, 2, 1, 9, 0, 0)


def test_display_includes_h_era_prefix() -> None:
    assert to_iserra_calendar(BASE).display() == "H 24816 年 1 月 1 日 08:00:00"
    assert (
        to_iserra_calendar(BASE).display(include_seconds=False)
        == "H 24816 年 1 月 1 日 08:00"
    )


def test_naive_datetime_is_treated_as_utc() -> None:
    """无时区 datetime 应按 UTC 处理。"""
    naive = datetime(2040, 4, 1, 8, 0)
    result = to_iserra_calendar(naive)
    assert result == IserraCalendar(24816, 1, 1, 8, 0, 0)


def test_main_era_anchor_year() -> None:
    """主纪年河誓纪在锚点时刻应为第 327 年(H 24490 为元年)。"""
    assert to_era_year(BASE, "河誓纪") == 327
    assert to_era(BASE) == "河誓纪 327 年 1 月 1 日 08:00:00"


def test_main_era_next_year() -> None:
    assert to_era(BASE + timedelta(days=360), "河誓纪") == "河誓纪 328 年 1 月 1 日 08:00:00"


def test_era_table_complete_and_positive() -> None:
    """13 个政权纪元齐全;主纪年在表中;锚点时刻每个纪元纪年数都为正。"""
    assert len(ERAS) == 13
    assert MAIN_ERA in ERAS
    for name, origin in ERAS.items():
        assert isinstance(name, str) and name
        assert isinstance(origin, int)
        assert to_era_year(BASE, name) >= 1, f"{name} 纪年数不应为 0 或负"
