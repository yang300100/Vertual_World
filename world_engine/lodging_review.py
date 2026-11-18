"""哥哥，住房审价只产生协商结论，不替租客付款或替房主交房。"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from world_engine.contracts import ContractError, ContractService


def review(
    connection: sqlite3.Connection,
    world_id: str,
    renter: sqlite3.Row,
    owner: sqlite3.Row,
    terms: dict[str, Any],
    at: datetime,
) -> tuple[str, dict[str, Any] | None, str]:
    """按同一套规则审阅玩家或 NPC 的住房条款，保持只读。"""
    try:
        normalized = ContractService.normalize("lodging", {"duration_days": 1, **terms})
    except (ContractError, TypeError, ValueError) as exc:
        return "npc_rejected", None, str(exc)
    if not isinstance(normalized["room_id"], str):
        return "npc_rejected", None, "住房约定需要明确有效的房间编号。"
    room = connection.execute(
        """SELECT r.*, p.nightly_rate FROM life_rooms r
           JOIN room_rental_rates p ON p.room_id=r.id
           JOIN locations l ON l.id=r.location_id AND l.world_id=r.world_id
           WHERE r.id=? AND r.world_id=? AND r.owner_character_id=? AND l.is_active=1""",
        (normalized["room_id"], world_id, owner["id"]),
    ).fetchone()
    if room is None:
        return "npc_rejected", None, "该房间不属于对方或尚未开放出租。"
    if connection.execute(
        """SELECT 1 FROM contract_fulfillments
           WHERE world_id=? AND kind='lodging' AND asset_id=? AND status='active'""",
        (world_id, room["id"]),
    ).fetchone():
        return "npc_rejected", None, "房间已有有效租约。"

    price = int(room["nightly_rate"]) * normalized["duration_days"]
    if price > 1_000_000:
        return "npc_rejected", None, "完整租金超出可执行范围，不能形成住房约定。"
    if normalized["payment"] < price:
        return (
            "npc_countered",
            {**terms, "duration_days": normalized["duration_days"], "payment": price},
            "需要先确认完整租金，才会授予限期使用权。",
        )

    rows = connection.execute(
        """SELECT affinity, trust FROM relationships WHERE world_id=? AND (
               (source_character_id=? AND target_character_id=?) OR
               (source_character_id=? AND target_character_id=?))""",
        (world_id, owner["id"], renter["id"], renter["id"], owner["id"]),
    ).fetchall()
    score = (
        round(sum(int(row["affinity"]) + int(row["trust"]) for row in rows) / len(rows))
        if rows
        else 0
    )
    if score <= -35:
        return "npc_rejected", None, "当前关系不允许接受这项长期事务。"
    if score < 15:
        payment = normalized["payment"] * 2
        if payment > 1_000_000:
            return "npc_rejected", None, "反提案租金超出可执行范围，不能形成住房约定。"
        return (
            "npc_countered",
            {**terms, "duration_days": normalized["duration_days"], "payment": payment},
            "需要先接受房主提出的反提案，才能确认执行。",
        )
    return "npc_accepted", None, "事务尚未执行，等待提出者最终确认。"
