from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta

# 世界种子统一的基准日期(create_world 起始时刻)，用于修复仅含时间的 current_time。
WORLD_BASE_DATE = date(2040, 4, 1)
_TIME_ONLY_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2}(\.\d{1,6})?)?$")


def parse_datetime(value: str, *, base_date: date | None = None) -> datetime:
    """把时间字符串解析为 datetime。

    正常值为完整 ISO datetime；若遇到仅含时间(如 "14:33:47")的损坏值，
    则与基准日期(默认世界起始 2040-04-01，可用调用方指定 world_clock 日期)合并，
    避免编排/裁判入口因 fromisoformat 时间串而崩溃。
    """
    text = (value or "").strip()
    if not text:
        raise ValueError("空时间字符串")
    if _TIME_ONLY_RE.match(text):
        combine_date = base_date or WORLD_BASE_DATE
        return datetime.combine(combine_date, time.fromisoformat(text), tzinfo=UTC)
    return datetime.fromisoformat(text)


def is_time_only(value: str) -> bool:
    return bool(_TIME_ONLY_RE.match((value or "").strip()))


def next_adjudication_boundary(
    current_time: datetime, interval_minutes: int = 720
) -> datetime:
    """返回严格晚于当前时间的固定世界时间裁判边界。"""

    if interval_minutes <= 0 or 1440 % interval_minutes != 0:
        raise ValueError("裁判间隔必须是能够整除一天的正整数分钟数")
    aware_time = current_time if current_time.tzinfo else current_time.replace(tzinfo=UTC)
    midnight = aware_time.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed_minutes = (aware_time.astimezone(UTC) - midnight).total_seconds() / 60
    next_index = int(elapsed_minutes // interval_minutes) + 1
    return midnight + timedelta(minutes=next_index * interval_minutes)
