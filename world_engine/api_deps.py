"""API 路由共享的请求前置条件。

`life_api` 与 `task_api` 曾各自实现了一份「取玩家角色与世界当前时间」的查询，
连 404 文案都不一致。这里收敛为唯一实现，路由工厂只需转发。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from fastapi import HTTPException

from world_engine.repository import from_iso


def require_world_time(connection: sqlite3.Connection, world_id: str) -> datetime:
    """读取世界当前时间；世界不存在时返回 404。"""
    world = connection.execute(
        'SELECT "current_time" FROM worlds WHERE id = ?', (world_id,)
    ).fetchone()
    if world is None:
        raise HTTPException(status_code=404, detail="世界不存在")
    return from_iso(world["current_time"])


def require_player(connection: sqlite3.Connection, world_id: str) -> sqlite3.Row:
    """读取玩家角色；世界中没有玩家时返回 404。"""
    player = connection.execute(
        "SELECT * FROM characters WHERE world_id = ? AND is_player = 1", (world_id,)
    ).fetchone()
    if player is None:
        raise HTTPException(status_code=404, detail="当前世界没有玩家角色")
    return player


def require_player_and_time(
    connection: sqlite3.Connection, world_id: str
) -> tuple[sqlite3.Row, datetime]:
    """一次性取出玩家角色与世界当前时间，供玩家操作端点使用。

    先校验世界存在，再校验玩家存在，保持与原有错误顺序一致。
    """
    world_time = require_world_time(connection, world_id)
    return require_player(connection, world_id), world_time
