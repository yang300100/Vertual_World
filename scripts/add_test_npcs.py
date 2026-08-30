"""为玩家附近的城市插入若干测试 NPC，便于验证多 Agent 编排。

用法：
    python -m scripts.add_test_npcs [--db data/world.db] [--count 4]

NPC 设为持久激活(activation_policy=persistent, activation_state=active)，位于玩家地点
附近的小偏移处，带有明确的 identity/goals，从而被活跃 NPC Agent 及事件导演纳入观测。
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from world_engine.database import Database
from world_engine.repository import to_iso

NPC_TEMPLATES = [
    ("赫尔曼", "铁匠", ["为商队打造器具", "攒钱翻修工坊"], ["沉稳", "手艺好"]),
    ("巴尔德", "巡夜看守", ["守卫城门", "盯紧往来流民"], ["警觉", "话少"]),
    ("苏玛", "药草贩", ["采集河岸药材", "打听瘟疫传闻"], ["细心", "爱打听"]),
    ("奥尔加", "商会书记", ["核算岁入", "打听下游商路"], ["精算", "谨慎"]),
    ("老马库斯", "渡口船工", ["修整渡船", "接驳两岸货船"], ["爽朗", "踏实"]),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/world.db")
    parser.add_argument("--count", type=int, default=4)
    args = parser.parse_args()

    db = Database(Path(args.db).resolve())
    now = to_iso(datetime.now(UTC))

    player_name = ""
    location_id = ""
    base_longitude = 0.0
    base_latitude = 0.0
    inserted = 0

    with db.write() as connection:
        # 找到玩家，以玩家地点为中心放置 NPC。
        player = connection.execute(
            "SELECT id, name, world_id, location_id, longitude, latitude"
            " FROM characters WHERE is_player = 1 ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if player is None:
            raise SystemExit("当前世界还没有玩家角色，无法确定 NPC 放置位置")
        world_id = player["world_id"]
        location_id = player["location_id"]
        base_longitude = float(player["longitude"])
        base_latitude = float(player["latitude"])
        player_name = player["name"]

        # 避免与算法人重复：跳过已存在同名 NPC。
        existing = {
            row["name"] for row in connection.execute(
                "SELECT name FROM characters WHERE world_id = ? AND name LIKE ?",
                (world_id, "布景·%"),
            ).fetchall()
        }

        for index, (name, identity, goals, traits) in enumerate(NPC_TEMPLATES[: args.count]):
            if f"布景·{name}" in existing:
                continue
            character_id = str(uuid4())
            # 以玩家为中心的小偏移，保证在城镇可交互范围内且彼此不重叠。
            offset_longitude = ((index % 2) * 2 - 1) * 0.004 + (index * 0.001)
            offset_latitude = (index // 2) * 0.006 - 0.006
            longitude = base_longitude + offset_longitude
            latitude = base_latitude + offset_latitude
            connection.execute(
                """
                INSERT INTO characters(
                    id, world_id, name, location_id, energy, satiety, money, health,
                    traits_json, goals_json, skills_json, identity,
                    is_player, is_pov, is_core, longitude, latitude,
                    movement_type, movement_speed_kmh, current_location_id,
                    activation_state, activation_policy, activation_reason,
                    activation_radius_km, activation_probability,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 100, ?, ?, '[]', ?, 0, 0, 0, ?, ?,
                          'land', 5.0, ?, 'active', 'persistent', 'test_npc',
                          15.0, 1.0, ?, ?)
                """,
                (
                    character_id,
                    world_id,
                    f"布景·{name}",
                    location_id,
                    55 + (index * 7) % 30,
                    50 + (index * 5) % 30,
                    6 + index * 2,
                    json.dumps(traits, ensure_ascii=False),
                    json.dumps(goals, ensure_ascii=False),
                    identity,
                    longitude,
                    latitude,
                    location_id,
                    now,
                    now,
                ),
            )
            inserted += 1

    print(f"玩家：{player_name}（{location_id}）")
    print(
        f"已插入测试 NPC 数量：{inserted}，"
        f"位置围绕玩家坐标 ({base_longitude:.4f}, {base_latitude:.4f})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
