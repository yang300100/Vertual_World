from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


class ElementLifecycleState(StrEnum):
    """所有世界元素共享的生命周期；专有状态仍留在各自的事实表。"""

    ACTIVE = "active"
    DESTROYED = "destroyed"
    RETIRED = "retired"


@dataclass(frozen=True, slots=True)
class CatalogElement:
    id: str
    world_id: str
    entity_type: str
    entity_id: str
    name: str
    lifecycle_state: ElementLifecycleState
    source_registration_id: str | None
    source_event_id: str | None
    metadata: dict[str, Any]


class WorldElementCatalog:
    """跨领域的元素身份/生命周期目录，不承载任意业务字段。"""

    def upsert(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        entity_type: str,
        entity_id: str,
        name: str,
        source_kind: str = "system",
        source_registration_id: str | None = None,
        source_event_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        lifecycle_state: ElementLifecycleState = ElementLifecycleState.ACTIVE,
    ) -> None:
        now = self._now_iso()
        connection.execute(
            """
            INSERT INTO world_element_catalog(
                id, world_id, entity_type, entity_id, name, lifecycle_state,
                source_kind, source_registration_id, source_event_id, metadata_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(world_id, entity_type, entity_id) DO UPDATE SET
                name = excluded.name,
                lifecycle_state = excluded.lifecycle_state,
                source_kind = CASE
                    WHEN world_element_catalog.source_kind = 'registration'
                    THEN world_element_catalog.source_kind
                    ELSE excluded.source_kind
                END,
                source_registration_id = COALESCE(
                    excluded.source_registration_id,
                    world_element_catalog.source_registration_id
                ),
                source_event_id = COALESCE(
                    excluded.source_event_id,
                    world_element_catalog.source_event_id
                ),
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at,
                destroyed_at = CASE
                    WHEN excluded.lifecycle_state = 'active' THEN NULL
                    ELSE COALESCE(world_element_catalog.destroyed_at, excluded.updated_at)
                END,
                destroyed_by_event_id = CASE
                    WHEN excluded.lifecycle_state = 'active' THEN NULL
                    ELSE world_element_catalog.destroyed_by_event_id
                END
            """,
            (
                str(uuid4()),
                world_id,
                entity_type,
                entity_id,
                name.strip(),
                lifecycle_state.value,
                source_kind,
                source_registration_id,
                source_event_id,
                json.dumps(metadata or {}, ensure_ascii=False),
                now,
                now,
            ),
        )

    def get(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        entity_type: str,
        entity_id: str,
    ) -> CatalogElement | None:
        row = connection.execute(
            """
            SELECT * FROM world_element_catalog
            WHERE world_id = ? AND entity_type = ? AND entity_id = ?
            """,
            (world_id, entity_type, entity_id),
        ).fetchone()
        return self._from_row(row) if row is not None else None

    def mark_lifecycle(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        entity_type: str,
        entity_id: str,
        state: ElementLifecycleState,
        source_event_id: str,
        metadata_update: dict[str, Any] | None = None,
    ) -> None:
        row = connection.execute(
            """
            SELECT metadata_json FROM world_element_catalog
            WHERE world_id = ? AND entity_type = ? AND entity_id = ?
            """,
            (world_id, entity_type, entity_id),
        ).fetchone()
        if row is None:
            raise LookupError("目标元素未进入统一目录")
        metadata = json.loads(row["metadata_json"])
        metadata.update(metadata_update or {})
        now = self._now_iso()
        connection.execute(
            """
            UPDATE world_element_catalog
            SET lifecycle_state = ?, metadata_json = ?, updated_at = ?,
                destroyed_at = CASE WHEN ? = 'active' THEN NULL ELSE ? END,
                destroyed_by_event_id = CASE WHEN ? = 'active' THEN NULL ELSE ? END
            WHERE world_id = ? AND entity_type = ? AND entity_id = ?
            """,
            (
                state.value,
                json.dumps(metadata, ensure_ascii=False),
                now,
                state.value,
                now,
                state.value,
                source_event_id,
                world_id,
                entity_type,
                entity_id,
            ),
        )

    def synchronize_world(self, connection: sqlite3.Connection, *, world_id: str) -> None:
        """把专用事实表的生命周期回写到目录，供旧存档和跨处理器状态变化复用。"""

        sources = (
            (
                "SELECT id, name, CASE WHEN health <= 0 "
                "THEN 'destroyed' ELSE 'active' END AS state "
                "FROM characters WHERE world_id = ?",
                "character",
            ),
            (
                "SELECT id, name, CASE WHEN is_active = 0 "
                "THEN 'destroyed' ELSE 'active' END AS state "
                "FROM locations WHERE world_id = ?",
                "location",
            ),
            (
                "SELECT id, name, CASE WHEN is_known = 0 THEN 'retired' ELSE 'active' END AS state "
                "FROM map_features WHERE world_id = ?",
                "map_feature",
            ),
            (
                "SELECT id, name, CASE WHEN is_available = 0 "
                "THEN 'destroyed' ELSE 'active' END AS state "
                "FROM vehicles WHERE world_id = ?",
                "vehicle",
            ),
            (
                "SELECT id, name, CASE WHEN status = 'ruined' "
                "THEN 'destroyed' ELSE 'active' END AS state "
                "FROM buildings WHERE world_id = ?",
                "building",
            ),
            (
                "SELECT id, name, CASE WHEN status = 'ruined' "
                "THEN 'destroyed' ELSE 'active' END AS state "
                "FROM world_structures WHERE world_id = ?",
                "structure",
            ),
            (
                "SELECT id, title AS name, CASE WHEN status = 'retired' "
                "THEN 'retired' ELSE 'active' END AS state "
                "FROM knowledge_entries WHERE world_id = ?",
                "knowledge_entry",
            ),
            (
                "SELECT id, target_name AS name, CASE WHEN status = 'cancelled' "
                "THEN 'retired' ELSE 'active' END AS state "
                "FROM construction_projects WHERE world_id = ?",
                "construction_project",
            ),
            (
                "SELECT id, name, 'active' AS state FROM world_maps WHERE world_id = ?",
                "world_map",
            ),
        )
        for query, entity_type in sources:
            rows = connection.execute(query, (world_id,)).fetchall()
            for row in rows:
                self.upsert(
                    connection,
                    world_id=world_id,
                    entity_type=entity_type,
                    entity_id=row["id"],
                    name=row["name"],
                    lifecycle_state=ElementLifecycleState(row["state"]),
                )

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _from_row(row: sqlite3.Row) -> CatalogElement:
        return CatalogElement(
            id=row["id"],
            world_id=row["world_id"],
            entity_type=row["entity_type"],
            entity_id=row["entity_id"],
            name=row["name"],
            lifecycle_state=ElementLifecycleState(row["lifecycle_state"]),
            source_registration_id=row["source_registration_id"],
            source_event_id=row["source_event_id"],
            metadata=json.loads(row["metadata_json"]),
        )
