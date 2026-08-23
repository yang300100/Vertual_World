from __future__ import annotations

from datetime import UTC, datetime, timedelta


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
