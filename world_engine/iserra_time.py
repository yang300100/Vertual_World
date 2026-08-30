"""伊瑟拉历换算(作者层展示用)。

引擎把世界时间存为连续 UTC datetime(秒级推进,无"年"概念)。本模块把
datetime 按伊瑟拉天文基准换算成 作者层环后纪年(H)的日历读数,供控制台展示。

伊瑟拉一年 360 天(开普勒自洽:行星约 1.16 AU 绕 1.6M☉ 双星,公转 ≈360.7 天,
取整 360),12 个月 × 30 天,每天 24 小时。基准见 `docs/worldbuilding/14-*`。

锚点约定:引擎硬编码起始世界时间 `datetime(2040,4,1,8,0,UTC)` 对应
**H 24816 年 1 月 1 日 08:00**(伊瑟拉新年)。从锚点起,每满 360 天为一年、
30 天为一个月、24 小时为一天。

本模块只做展示换算,不改变引擎推进逻辑;`worlds.current_time` 仍是 UTC datetime。

## 多元纪元法(朝代更替纪)

除作者层环后纪年 H 外,各政权还以近期重大事件为元年、用本政权意象命名当代纪元
(参见 `docs/worldbuilding/28-multi-era-calendar.md`)。控制台/CLI 以"主纪年"
(阿德伦·河誓纪)为主展示,数字远小于 H 2 万多年。换算规则:
当代纪年数 = 环后年号(H) − 该纪元元年 H + 1。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

# 锚点:引擎硬编码起始世界时间(repository.create_world)为 2040-04-01 08:00 UTC。
# 历法以锚点当日零点为"年/月/日"起点,08:00 自然作为当日已过时间被编码进偏移。
ISERRA_EPOCH_DAY_ZERO = datetime(2040, 4, 1, 0, 0, tzinfo=UTC)
ISERRA_EPOCH = datetime(2040, 4, 1, 8, 0, tzinfo=UTC)  # 展示用锚点时刻
ISERRA_EPOCH_H = 24816  # 锚点对应的环后纪年(H)

# 伊瑟拉天文基准(单位:秒)
_SECONDS_PER_DAY = 24 * 3600
_SECONDS_PER_MONTH = 30 * _SECONDS_PER_DAY
_SECONDS_PER_YEAR = 360 * _SECONDS_PER_DAY

# 各政权当代纪元:纪名 → 元年(环后纪年 H)。
# 当前纪年数 = 环后年号 − 元年 + 1,故数值落在几百~千余量级。
ERAS: dict[str, int] = {
    "河誓纪": 24490,   # 阿德伦议约王国(主角舞台)
    "炉谷纪": 24490,   # 赫塔尔工坊盟约
    "望镜纪": 24490,   # 伊莱湖林契约群
    "航灯纪": 23759,   # 诺赫兰东岸航盟
    "赭泉纪": 24490,   # 赤原誓团联盟
    "封河纪": 22604,   # 绿冠河林共同体
    "阶泉纪": 22174,   # 卡尔萨高原议盟
    "九井纪": 22174,   # 伊什拉绿洲路盟
    "潮门纪": 23759,   # 南潮城邦同盟
    "烬湾纪": 24490,   # 火弧祭盟
    "灯火纪": 23759,   # 中裂海航盟
    "东门纪": 24490,   # 东门群岛港邦
    "避潮纪": 23759,   # 黑潮岛盟
}

# 控制台/CLI 主打的主纪年(阿德伦·河誓纪)。
MAIN_ERA = "河誓纪"


@dataclass(frozen=True, slots=True)
class IserraCalendar:
    """一个作者层伊瑟拉历日期读数。"""

    year: int      # 环后纪年 H 年号
    month: int     # 1-12(每 30 天一月)
    day: int       # 1-30
    hour: int      # 0-23
    minute: int    # 0-59
    second: int    # 0-59

    def display(self, include_seconds: bool = True) -> str:
        """生成控制台展示串,如 `H 24816 年 1 月 1 日 08:00:00`。"""
        clock = f"{self.hour:02d}:{self.minute:02d}"
        if include_seconds:
            clock += f":{self.second:02d}"
        return f"H {self.year} 年 {self.month} 月 {self.day} 日 {clock}"


def to_iserra_calendar(value: datetime) -> IserraCalendar:
    """把世界时间 datetime 换算成伊瑟拉历(环后纪年 H 月日时)。

    对早于锚点的世界时间也稳健:Python 的 // 与 % 对负值做 floor/mod,
    因此能正确显示为更早的年份/月份。
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    else:
        value = value.astimezone(UTC)

    elapsed_seconds = (value - ISERRA_EPOCH_DAY_ZERO).total_seconds()

    year_offset = elapsed_seconds // _SECONDS_PER_YEAR
    year_remainder = elapsed_seconds % _SECONDS_PER_YEAR

    month = year_remainder // _SECONDS_PER_MONTH + 1
    month_remainder = year_remainder % _SECONDS_PER_MONTH

    day = month_remainder // _SECONDS_PER_DAY + 1
    day_remainder = month_remainder % _SECONDS_PER_DAY

    hour = day_remainder // 3600
    minute = (day_remainder % 3600) // 60
    second = day_remainder % 60

    return IserraCalendar(
        year=ISERRA_EPOCH_H + int(year_offset),
        month=int(month),
        day=int(day),
        hour=int(hour),
        minute=int(minute),
        second=int(second),
    )


def to_era_year(value: datetime, era_name: str = MAIN_ERA) -> int:
    """给定世界时间与当代纪名,返回其纪年数(当代纪元第 N 年)。"""
    return to_iserra_calendar(value).year - ERAS[era_name] + 1


def to_era(
    value: datetime,
    era_name: str = MAIN_ERA,
    include_seconds: bool = True,
) -> str:
    """生成某政体当代纪元的完整展示串,如 `河誓纪 327 年 1 月 1 日 08:00:00`。"""
    cal = to_iserra_calendar(value)
    era_year = cal.year - ERAS[era_name] + 1
    clock = f"{cal.hour:02d}:{cal.minute:02d}"
    if include_seconds:
        clock += f":{cal.second:02d}"
    return f"{era_name} {era_year} 年 {cal.month} 月 {cal.day} 日 {clock}"
