"""NPC 的稳定人口学属性：只为缺失字段生成，不覆盖既有角色设定。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class NpcDemographics:
    gender: str
    birth_world_time: datetime


def stable_npc_demographics(
    *,
    identity_key: str,
    world_time: datetime,
    adult_age_world_years: int,
) -> NpcDemographics:
    """按稳定键生成成年 NPC 的性别与生日，同一 NPC 重跑结果不变。"""
    digest = hashlib.sha256(identity_key.encode("utf-8")).digest()
    gender = "女性" if digest[0] % 2 == 0 else "男性"
    # 既有独立 NPC 默认均为成年人；生日分散在一年内，避免集中到同一天。
    age_years = max(1, int(adult_age_world_years)) + 1 + digest[1] % 48
    day_offset = int.from_bytes(digest[2:4], "big") % 365
    return NpcDemographics(
        gender=gender,
        birth_world_time=world_time - timedelta(days=age_years * 365 + day_offset),
    )


def age_years_at(birth_world_time: datetime | None, world_time: datetime) -> int | None:
    """以世界年（365日）计算完整年龄；生日缺失时明确保持未知。"""
    if birth_world_time is None:
        return None
    return max(0, int((world_time - birth_world_time).total_seconds() // (365 * 86400)))
