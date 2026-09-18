"""建筑与聚落项目服务：按世界时间推进施工，并处理资源扣除与退款。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Literal
from uuid import uuid4

from world_engine.elements import WorldElementCatalog
from world_engine.registration.models import (
    RegistrationConflict,
    RegistrationNotFound,
    RegistrationRejected,
    SettlementSpec,
)
from world_engine.repository import to_iso, utc_now


class ConstructionProjectService:
    """按世界时间推进已开工项目；资源扣除仍留给后续经济系统。"""

    _duration_hours = {"settlement": 24 * 90, "building": 24 * 14, "structure": 24 * 180}

    def set_status(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        registration_id: str,
        status: Literal["surveyed", "constructing", "cancelled"],
    ) -> dict[str, object]:
        row = connection.execute(
            """
            SELECT * FROM construction_projects
            WHERE world_id = ? AND registration_id = ?
            """,
            (world_id, registration_id),
        ).fetchone()
        if row is None:
            raise RegistrationNotFound("注册请求没有对应建设项目")
        current = row["status"]
        allowed = {
            "planned": {"surveyed", "constructing", "cancelled"},
            "surveyed": {"constructing", "cancelled"},
            "constructing": {"cancelled"},
        }
        if status not in allowed.get(current, set()):
            raise RegistrationConflict(f"建设项目不能从{current}变为{status}")
        now = utc_now()
        if status == "constructing":
            self._consume_requirements(connection, row, now)
            if row["project_type"] == "settlement":
                connection.execute(
                    """
                    UPDATE land_claims SET status = 'active', updated_at = ?
                    WHERE registration_id = ?
                    """,
                    (to_iso(now), row["registration_id"]),
                )
        if status == "cancelled":
            self._refund_requirements(connection, row, now)
            if row["project_type"] == "settlement":
                connection.execute(
                    """
                    UPDATE land_claims SET status = 'released', updated_at = ?
                    WHERE registration_id = ?
                    """,
                    (to_iso(now), row["registration_id"]),
                )
        connection.execute(
            "UPDATE construction_projects SET status = ?, updated_at = ? WHERE id = ?",
            (status, to_iso(now), row["id"]),
        )
        WorldElementCatalog().synchronize_world(connection, world_id=world_id)
        return self.get(connection, world_id=world_id, registration_id=registration_id)

    @staticmethod
    def _consume_requirements(
        connection: sqlite3.Connection, row: sqlite3.Row, now: datetime
    ) -> None:
        requirements = json.loads(row["requirements_json"] or "{}")
        registration = connection.execute(
            """
            SELECT requested_by_character_id FROM element_registration_requests
            WHERE id = ?
            """,
            (row["registration_id"],),
        ).fetchone()
        requester_id = registration["requested_by_character_id"] if registration else None
        remaining = dict(requirements)
        money = int(remaining.pop("money", 0))
        if money:
            if requester_id is None:
                raise RegistrationConflict("无人申请的项目不能扣除人物资金")
            actor = connection.execute(
                "SELECT money FROM characters WHERE id = ? AND world_id = ?",
                (requester_id, row["world_id"]),
            ).fetchone()
            if actor is None or int(actor["money"]) < money:
                raise RegistrationConflict("申请人物资金不足，不能开工")
        location = None
        resources: dict[str, int] = {}
        if remaining:
            if row["location_id"] is None:
                raise RegistrationConflict("项目没有资源来源地点，不能开工")
            location = connection.execute(
                "SELECT resources_json FROM locations WHERE id = ? AND world_id = ?",
                (row["location_id"], row["world_id"]),
            ).fetchone()
            if location is None:
                raise RegistrationConflict("项目资源来源地点不存在")
            resources = json.loads(location["resources_json"] or "{}")
            missing = [key for key, amount in remaining.items() if resources.get(key, 0) < amount]
            if missing:
                raise RegistrationConflict(f"建设资源不足：{', '.join(sorted(missing))}")
        if money:
            connection.execute(
                "UPDATE characters SET money = money - ?, updated_at = ? WHERE id = ?",
                (money, to_iso(now), requester_id),
            )
        if remaining and location is not None:
            for key, amount in remaining.items():
                resources[key] -= amount
            connection.execute(
                "UPDATE locations SET resources_json = ? WHERE id = ?",
                (json.dumps(resources, ensure_ascii=False), row["location_id"]),
            )
        connection.execute(
            """
            INSERT INTO element_registration_effects(
                id, registration_id, world_id, effect_type, payload_json, created_at
            ) VALUES (?, ?, ?, 'construction.resources_committed', ?, ?)
            """,
            (
                str(uuid4()),
                row["registration_id"],
                row["world_id"],
                json.dumps(requirements, ensure_ascii=False),
                to_iso(now),
            ),
        )
        connection.execute(
            """
            INSERT INTO construction_escrow(
                project_id, world_id, requester_character_id, source_location_id,
                committed_resources_json, committed_money, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id) DO NOTHING
            """,
            (
                row["id"],
                row["world_id"],
                requester_id,
                row["location_id"],
                json.dumps(remaining, ensure_ascii=False),
                money,
                to_iso(now),
                to_iso(now),
            ),
        )

    @staticmethod
    def _refund_requirements(
        connection: sqlite3.Connection, row: sqlite3.Row, now: datetime
    ) -> None:
        escrow = connection.execute(
            "SELECT * FROM construction_escrow WHERE project_id = ?", (row["id"],)
        ).fetchone()
        if escrow is None:
            return
        ratio = max(0.0, 1.0 - float(row["progress"]))
        committed_resources = json.loads(escrow["committed_resources_json"] or "{}")
        refunded_resources = {
            key: int(amount * ratio) for key, amount in committed_resources.items()
        }
        refunded_money = int(int(escrow["committed_money"]) * ratio)
        if refunded_money and escrow["requester_character_id"]:
            connection.execute(
                "UPDATE characters SET money = money + ?, updated_at = ? WHERE id = ?",
                (refunded_money, to_iso(now), escrow["requester_character_id"]),
            )
        if refunded_resources and escrow["source_location_id"]:
            location = connection.execute(
                "SELECT resources_json FROM locations WHERE id = ?",
                (escrow["source_location_id"],),
            ).fetchone()
            if location is not None:
                resources = json.loads(location["resources_json"] or "{}")
                for key, amount in refunded_resources.items():
                    resources[key] = int(resources.get(key, 0)) + amount
                connection.execute(
                    "UPDATE locations SET resources_json = ? WHERE id = ?",
                    (json.dumps(resources, ensure_ascii=False), escrow["source_location_id"]),
                )
        connection.execute(
            """
            UPDATE construction_escrow
            SET refunded_resources_json = ?, refunded_money = ?, updated_at = ?
            WHERE project_id = ?
            """,
            (
                json.dumps(refunded_resources, ensure_ascii=False),
                refunded_money,
                to_iso(now),
                row["id"],
            ),
        )
        connection.execute(
            """
            INSERT INTO world_events(
                id, world_id, tick_id, occurred_at, event_type,
                summary, importance, payload_json, created_at
            ) VALUES (?, ?, ?, ?, 'world.construction_cancelled', ?, 'routine', ?, ?)
            """,
            (
                str(uuid4()),
                row["world_id"],
                row["registration_id"],
                to_iso(now),
                f"建设项目“{row['target_name']}”已取消并结算退款。",
                json.dumps(
                    {"refunded_resources": refunded_resources, "refunded_money": refunded_money},
                    ensure_ascii=False,
                ),
                to_iso(now),
            ),
        )

    def advance(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        world_delta_seconds: float,
        world_time: datetime,
        created_at: datetime,
    ) -> int:
        if world_delta_seconds <= 0:
            return 0
        rows = connection.execute(
            """
            SELECT p.*, r.payload_json, r.requested_by_character_id, r.source_event_id
            FROM construction_projects p
            JOIN element_registration_requests r ON r.id = p.registration_id
            WHERE p.world_id = ? AND p.status = 'constructing'
            ORDER BY p.created_at, p.id
            """,
            (world_id,),
        ).fetchall()
        updated = 0
        for row in rows:
            duration = self._duration_hours[row["project_type"]]
            progress = min(1.0, float(row["progress"]) + world_delta_seconds / 3600 / duration)
            if progress < 1.0:
                connection.execute(
                    "UPDATE construction_projects SET progress = ?, updated_at = ? WHERE id = ?",
                    (progress, to_iso(created_at), row["id"]),
                )
                updated += 1
                continue
            target_entity_id, location_id = self._complete(connection, row, world_time, created_at)
            connection.execute(
                """
                UPDATE construction_projects
                SET status = 'completed', progress = 1,
                    target_entity_id = ?, location_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (target_entity_id, location_id, to_iso(created_at), row["id"]),
            )
            self._record_completion_event(
                connection,
                row=row,
                world_time=world_time,
                created_at=created_at,
                entity_id=target_entity_id,
                location_id=location_id,
            )
            updated += 1
        if updated:
            WorldElementCatalog().synchronize_world(connection, world_id=world_id)
        return updated

    def list(self, connection: sqlite3.Connection, *, world_id: str) -> list[dict[str, object]]:
        return [
            dict(row)
            for row in connection.execute(
                """
                SELECT * FROM construction_projects
                WHERE world_id = ? ORDER BY created_at DESC
                """,
                (world_id,),
            ).fetchall()
        ]

    def get(
        self, connection: sqlite3.Connection, *, world_id: str, registration_id: str
    ) -> dict[str, object]:
        row = connection.execute(
            """
            SELECT * FROM construction_projects
            WHERE world_id = ? AND registration_id = ?
            """,
            (world_id, registration_id),
        ).fetchone()
        if row is None:
            raise RegistrationNotFound("建设项目不存在")
        return dict(row)

    @staticmethod
    def _complete(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        world_time: datetime,
        created_at: datetime,
    ) -> tuple[str | None, str | None]:
        entity_id = row["target_entity_id"]
        location_id = row["location_id"]
        if row["project_type"] == "building":
            connection.execute(
                "UPDATE buildings SET status = 'completed', updated_at = ? WHERE id = ?",
                (to_iso(created_at), entity_id),
            )
        elif row["project_type"] == "structure":
            connection.execute(
                "UPDATE world_structures SET status = 'active', updated_at = ? WHERE id = ?",
                (to_iso(created_at), entity_id),
            )
        else:
            payload = SettlementSpec.model_validate(json.loads(row["payload_json"]))
            existing = connection.execute(
                "SELECT id FROM locations WHERE world_id = ? AND name = ?",
                (row["world_id"], payload.name.strip()),
            ).fetchone()
            if existing is not None:
                raise RegistrationRejected("建设完成时发现同名聚落，不能覆盖现有地点")
            entity_id = str(uuid4())
            location_id = entity_id
            connection.execute(
                """
                INSERT INTO locations(
                    id, world_id, name, kind, resources_json, longitude, latitude,
                    area_radius_km, area_priority
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 20)
                """,
                (
                    entity_id,
                    row["world_id"],
                    payload.name.strip(),
                    payload.settlement_kind,
                    json.dumps(payload.resources, ensure_ascii=False),
                    payload.longitude,
                    payload.latitude,
                    payload.area_radius_km,
                ),
            )
            connection.execute(
                """
                INSERT INTO world_entities(
                    id, world_id, entity_type, entity_id, name, registration_id, created_at
                ) VALUES (?, ?, 'settlement', ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    row["world_id"],
                    entity_id,
                    payload.name.strip(),
                    row["registration_id"],
                    to_iso(created_at),
                ),
            )
            WorldElementCatalog().upsert(
                connection,
                world_id=row["world_id"],
                entity_type="settlement",
                entity_id=entity_id,
                name=payload.name.strip(),
                source_kind="registration",
                source_registration_id=row["registration_id"],
            )
            connection.execute(
                """
                INSERT INTO element_registration_effects(
                    id, registration_id, world_id, effect_type,
                    entity_type, entity_id, payload_json, created_at
                ) VALUES (?, ?, ?, 'settlement.created', 'settlement', ?, '{}', ?)
                """,
                (
                    str(uuid4()),
                    row["registration_id"],
                    row["world_id"],
                    entity_id,
                    to_iso(created_at),
                ),
            )
        return entity_id, location_id

    @staticmethod
    def _record_completion_event(
        connection: sqlite3.Connection,
        *,
        row: sqlite3.Row,
        world_time: datetime,
        created_at: datetime,
        entity_id: str | None,
        location_id: str | None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO world_events(
                id, world_id, tick_id, occurred_at, event_type, actor_id,
                location_id, summary, importance, payload_json, created_at
            ) VALUES (?, ?, ?, ?, 'world.construction_completed', ?, ?, ?, 'routine', ?, ?)
            """,
            (
                str(uuid4()),
                row["world_id"],
                row["registration_id"],
                to_iso(world_time),
                row["requested_by_character_id"],
                location_id,
                f"建设项目“{row['target_name']}”已经完成。",
                json.dumps(
                    {
                        "registration_id": row["registration_id"],
                        "entity_id": entity_id,
                    },
                    ensure_ascii=False,
                ),
                to_iso(created_at),
            ),
        )
