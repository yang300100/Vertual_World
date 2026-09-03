"""按 NPC 身份配置可教授的基础技能；重复执行不会重复添加。"""
from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from world_engine.database import Database
from world_engine.repository import to_iso, utc_now

RULES = (
    (("医师",), "医治"),
    (("药", "草"), "草药辨识"),
    (("城镇执政",), "政务协调"),
    (("卫队", "巡夜"), "警戒"),
    (("粮仓",), "仓储管理"),
    (("集市",), "商贸记账"),
    (("铁匠", "马具"), "锻造"),
    (("石匠",), "建筑维修"),
    (("水务",), "水利维护"),
    (("矿石",), "矿石鉴定"),
    (("测绘", "路标"), "测绘"),
    (("渡口", "渔"), "航行"),
    (("织工",), "织造"),
    (("陶器",), "制陶"),
    (("书院教师", "书记", "书信"), "读写算术"),
)

database = Database(Path("data/world.db"))
database.initialize()
with database.write() as connection:
    world = connection.execute(
        """
        SELECT id, current_time FROM worlds
        WHERE status = 'running'
        ORDER BY updated_at DESC LIMIT 1
        """
    ).fetchone()
    if world is None:
        raise SystemExit("没有可运行世界")
    changed = []
    rows = connection.execute(
        """
        SELECT id, name, identity, skills_json FROM characters
        WHERE world_id = ? AND is_player = 0
        """,
        (world["id"],),
    ).fetchall()
    for row in rows:
        skills = list(json.loads(row["skills_json"] or "[]"))
        identity = row["identity"] or ""
        for markers, skill in RULES:
            if any(marker in identity for marker in markers) and skill not in skills:
                skills.append(skill)
        if skills != json.loads(row["skills_json"] or "[]"):
            connection.execute(
                "UPDATE characters SET skills_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(skills, ensure_ascii=False), to_iso(utc_now()), row["id"]),
            )
            changed.append({"id": row["id"], "name": row["name"], "skills": skills})
    if changed:
        event_id = str(uuid4())
        connection.execute(
            """
            INSERT INTO world_events(
                id, world_id, tick_id, occurred_at, event_type, summary,
                importance, payload_json, created_at
            ) VALUES (?, ?, ?, ?, 'world.npc_capabilities_configured', ?, 'routine', ?, ?)
            """,
            (
                event_id,
                world["id"],
                event_id,
                world["current_time"],
                f"系统为{len(changed)}名NPC配置了可教授技能。",
                json.dumps({"characters": changed}, ensure_ascii=False),
                to_iso(utc_now()),
            ),
        )
        connection.execute(
            "UPDATE worlds SET version = version + 1, updated_at = ? WHERE id = ?",
            (to_iso(utc_now()), world["id"]),
        )
print(json.dumps({"configured": changed}, ensure_ascii=False))
