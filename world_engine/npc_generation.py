"""用本地词条组合生成可审计的基础 NPC 档案，不调用语言模型。"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class GeneratedNpcProfile:
    """尚未写入世界的 NPC 档案；由注册器完成最终落档。"""

    name: str
    identity: str
    traits: tuple[str, ...]
    goals: tuple[str, ...]

    @property
    def idempotency_suffix(self) -> str:
        """由稳定内容导出的短标识，供批量登记安全重跑。"""
        raw = "|".join((self.name, self.identity, *self.traits, *self.goals))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


_FAMILY_NAMES = (
    "阿斯特", "贝洛", "晨溪", "岚桥", "雾杉", "维林", "银砧", "月湾",
    "暮石", "罗文", "晴港", "霜铃", "星绘", "岩栎", "白帆", "织风",
)

_GIVEN_NAMES = (
    "艾琳", "洛恩", "梅芙", "赛拉", "托林", "伊娅", "卡洛", "诺安",
    "瑟芙", "莱奥", "薇拉", "欧林", "菲恩", "妲芙", "雷恩", "米拉",
)

_ROLES = (
    ("草药采集人", ("耐心", "熟悉山野"), ("辨认附近可用药草",)),
    ("路标修补匠", ("务实", "细致"), ("修复通往邻镇的旧路标",)),
    ("旅店侍者", ("热情", "记性好"), ("记住旅人的故事与需求",)),
    ("布料织工", ("安静", "专注"), ("找到更耐用的染料",)),
    ("渡口看守", ("谨慎", "守时"), ("让过河的人平安抵岸",)),
    ("矿石鉴定师", ("好奇", "讲原则"), ("记录本地矿脉的变化",)),
    ("巡游星图测绘员", ("克制", "细致"), ("补全道路与星图记录",)),
    ("书信递送员", ("可靠", "健谈"), ("按时把家书送到目的地",)),
    ("陶器匠", ("温和", "手稳"), ("烧制更结实的储水陶罐",)),
    ("马具修理师", ("直率", "耐心"), ("替旅人修好磨损的马具",)),
    ("雨具商贩", ("机敏", "乐观"), ("赶在雨季前备齐货物",)),
    ("河岸渔人", ("坚韧", "寡言"), ("找到不伤鱼群的捕鱼法",)),
)

# 第一阶段的每座城镇都要具备这十项可见事务。它们是基础服务槽位，
# 不等同于对该城镇人口、官制或经济规模的完整断言。
_TOWN_DUTY_ROLES = (
    ("城镇执政官", ("稳重", "善于协调"), ("处理城镇公共事务与纠纷",)),
    ("卫队长", ("警觉", "守序"), ("安排巡逻并保护居民",)),
    ("医师", ("仁慈", "冷静"), ("照料伤病与储备常用药材",)),
    ("粮仓管理员", ("谨慎", "公正"), ("核对粮食储备并安排赈济",)),
    ("集市司簿", ("精明", "守信"), ("记录摊位、税费与商贸纠纷",)),
    ("铁匠", ("专注", "直率"), ("维护农具、车具与日常器械",)),
    ("石匠", ("坚韧", "细致"), ("检修房屋、桥梁与公共设施",)),
    ("水务员", ("耐心", "负责"), ("维护饮水、排水与河岸设施",)),
    ("巡夜人", ("沉着", "可靠"), ("守望夜间街巷并报告异常",)),
    ("书院教师", ("博学", "温和"), ("教授读写算术并保管地方记录",)),
)


def generate_npc_profiles(
    *,
    seed: str,
    count: int,
    excluded_names: Iterable[str] = (),
) -> list[GeneratedNpcProfile]:
    """以固定种子生成不重名的 NPC；同输入得到完全相同的结果。"""
    if not 1 <= count <= 100:
        raise ValueError("一次只能生成 1 到 100 名 NPC")
    excluded = {name.strip() for name in excluded_names if name.strip()}
    candidates = [
        f"{family}·{given}"
        for family in _FAMILY_NAMES
        for given in _GIVEN_NAMES
    ]
    random.Random(f"{seed}:names").shuffle(candidates)
    role_order = list(_ROLES)
    random.Random(f"{seed}:roles").shuffle(role_order)

    profiles: list[GeneratedNpcProfile] = []
    for index, name in enumerate(candidates):
        if name in excluded:
            continue
        identity, traits, goals = role_order[index % len(role_order)]
        profiles.append(
            GeneratedNpcProfile(
                name=name,
                identity=identity,
                traits=traits,
                goals=goals,
            )
        )
        if len(profiles) == count:
            return profiles
    raise ValueError("可用的词条组合不足，无法生成指定数量的 NPC")


def generate_town_duty_profiles(
    *,
    seed: str,
    excluded_names: Iterable[str] = (),
    existing_identities: Iterable[str] = (),
) -> list[GeneratedNpcProfile]:
    """为一个城镇补齐尚未具备的第一阶段基础事务岗位。"""
    excluded = {name.strip() for name in excluded_names if name.strip()}
    occupied_identities = {
        identity.strip() for identity in existing_identities if identity.strip()
    }
    duties = [role for role in _TOWN_DUTY_ROLES if role[0] not in occupied_identities]
    candidates = [
        f"{family}·{given}"
        for family in _FAMILY_NAMES
        for given in _GIVEN_NAMES
    ]
    random.Random(f"{seed}:town-duty-names").shuffle(candidates)

    profiles: list[GeneratedNpcProfile] = []
    candidate_iter = iter(name for name in candidates if name not in excluded)
    for identity, traits, goals in duties:
        try:
            name = next(candidate_iter)
        except StopIteration as exc:
            raise ValueError("可用的词条组合不足，无法补齐城镇事务 NPC") from exc
        profiles.append(
            GeneratedNpcProfile(
                name=name,
                identity=identity,
                traits=traits,
                goals=goals,
            )
        )
    return profiles


def generate_npc_positions(
    *,
    seed: str,
    profiles: Iterable[GeneratedNpcProfile],
    center_longitude: float,
    center_latitude: float,
    minimum_distance_m: float = 18.0,
    maximum_distance_m: float = 220.0,
) -> dict[str, tuple[float, float]]:
    """在同一地点范围内生成稳定且互不重叠的 NPC 坐标。"""
    items = list(profiles)
    if not items:
        return {}
    if minimum_distance_m <= 10 or maximum_distance_m <= minimum_distance_m:
        raise ValueError("NPC 坐标分布范围必须大于 10 米且上限大于下限")
    rng = random.Random(f"{seed}:positions")
    base_angle = rng.random() * math.tau
    latitude_scale = 111_320.0
    longitude_scale = latitude_scale * max(0.1, math.cos(math.radians(center_latitude)))
    positions: dict[str, tuple[float, float]] = {}
    golden_angle = math.pi * (3 - math.sqrt(5))
    for index, profile in enumerate(items):
        proportion = math.sqrt((index + 0.5) / len(items))
        radius_m = minimum_distance_m + proportion * (maximum_distance_m - minimum_distance_m)
        angle = base_angle + index * golden_angle
        east_m = math.cos(angle) * radius_m
        north_m = math.sin(angle) * radius_m
        positions[profile.name] = (
            center_longitude + east_m / longitude_scale,
            center_latitude + north_m / latitude_scale,
        )
    return positions
