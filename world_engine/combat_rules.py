"""战斗规则的单一事实来源。

历史上 `actions.py`（玩家直接意图路径）与 `combat.py`（遭遇战路径）各自抄了一份
战斗数学，导致距离阈值、重要人物判定与位置回退语义悄悄分叉。
本模块把常量、判定与伤害公式集中到一处，两条结算路径共用，避免再次漂移。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Protocol

# 战斗交互距离阈值(公里)。撤退判定只看双方意图，不需要距离阈值。
COMBAT_RANGE_KM = 5.0

# 拥有这些头衔的人物倒台会记入重大历史事件。
IMPORTANT_IDENTITY_KEYWORDS: tuple[str, ...] = (
    "女王",
    "代表",
    "召集人",
    "记录官",
    "首席",
    "行誓者",
    "祭官",
    "灯判",
    "守潮",
    "调度官",
    "井见",
    "海议长",
    "港守",
    "传声人",
)

# 掌握这些技能的人物持械攻击时获得额外加成。
COMBAT_SKILL_KEYWORDS: tuple[str, ...] = ("剑术", "蛮力", "搏斗")
COMBAT_SKILL_BONUS = 4


class _RngLike(Protocol):
    """只需要 randint 的随机源，兼容 `random.Random` 与 `random` 模块。"""

    def randint(self, a: int, b: int) -> int: ...  # pragma: no cover - 协议声明


def is_important_character(row: sqlite3.Row) -> bool:
    """核心人物或持有要职头衔的人物，其死亡将载入史册。"""
    if int(row["is_core"]):
        return True
    identity = row["identity"] or ""
    return any(keyword in identity for keyword in IMPORTANT_IDENTITY_KEYWORDS)


def equipment_bonus(connection: sqlite3.Connection, row: sqlite3.Row, kind: str) -> int:
    """读取人物装备位的武器(攻击)或护符(防御)加成；掌握战斗技能再额外加攻。"""
    if kind == "attack":
        weapon = connection.execute(
            """
            SELECT it.attack_bonus AS b
            FROM item_instances ii JOIN item_types it ON it.id = ii.item_type_id
            WHERE ii.container_id = ? AND ii.container_type = 'character_equipment'
              AND it.category = 'weapon'
            """,
            (row["id"],),
        ).fetchone()
        bonus = int(weapon["b"]) if weapon and weapon["b"] is not None else 0
        skills_json = row["skills_json"] if "skills_json" in row.keys() else None
        if skills_json:
            try:
                skills = json.loads(skills_json)
            except (json.JSONDecodeError, TypeError):
                skills = []
            if any(keyword in COMBAT_SKILL_KEYWORDS for keyword in skills):
                bonus += COMBAT_SKILL_BONUS
        return bonus
    charm = connection.execute(
        """
        SELECT it.defense_bonus AS b
        FROM item_instances ii JOIN item_types it ON it.id = ii.item_type_id
        WHERE ii.container_id = ? AND ii.container_type = 'character_equipment'
          AND it.category = 'charm'
        """,
        (row["id"],),
    ).fetchone()
    return int(charm["b"]) if charm and charm["b"] is not None else 0


def current_location_id(row: sqlite3.Row) -> str | None:
    """人物当前所在位置：优先取移动中的临时位置，无则回退到注册地点。"""
    if "current_location_id" in row.keys() and row["current_location_id"]:
        return row["current_location_id"]
    return row["location_id"]


def location_kind(connection: sqlite3.Connection, location_id: str | None) -> str | None:
    if not location_id:
        return None
    row = connection.execute(
        "SELECT kind FROM locations WHERE id = ?", (location_id,)
    ).fetchone()
    return row["kind"] if row else None


def offensive_damage(
    rng: _RngLike, attacker: Any, *, attack_bonus: int, defense_bonus: int
) -> int:
    """先手一方的伤害。"""
    return max(
        1, rng.randint(10, 20) + int(attacker["energy"]) // 10 + attack_bonus - defense_bonus
    )


def counter_damage(
    rng: _RngLike, defender: Any, *, attack_bonus: int, defense_bonus: int
) -> int:
    """受击方存活时的反击伤害，低于先手伤害。"""
    return max(
        1, rng.randint(6, 16) + int(defender["energy"]) // 10 + attack_bonus - defense_bonus
    )
