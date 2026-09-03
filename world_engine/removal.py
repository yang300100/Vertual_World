from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from world_engine.elements import ElementLifecycleState, WorldElementCatalog
from world_engine.repository import from_iso, to_iso, utc_now

LOGGER = logging.getLogger("virtual-world.removal")


class RemovalReason(StrEnum):
    DESTROYED = "destroyed"
    RETIRED = "retired"
    REMOVED = "removed"


class RemovalStatus(StrEnum):
    VALIDATING = "validating"
    APPLIED = "applied"
    REJECTED = "rejected"
    FAILED = "failed"


class ElementRemovalSubmit(BaseModel):
    """删除请求只接收已发生事件，不能作为任意数据库删除接口。"""

    model_config = ConfigDict(extra="forbid")

    requested_by_character_id: str | None = None
    source_event_id: str
    idempotency_key: str = Field(min_length=8, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    target_element_type: Literal[
        "character", "location", "settlement", "building", "structure", "knowledge_entry", "vehicle"
    ]
    target_entity_id: str = Field(min_length=1, max_length=100)
    reason: RemovalReason = RemovalReason.DESTROYED
    details: str = Field(min_length=1, max_length=1000)


class RemovalEffectView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    effect_type: str
    entity_type: str | None = None
    entity_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ElementRemovalView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    world_id: str
    target_element_type: str
    target_entity_id: str
    requested_by_character_id: str | None = None
    source_event_id: str
    idempotency_key: str
    reason: RemovalReason
    details: str
    status: RemovalStatus
    rejection_reason: str | None = None
    input_world_version: int
    applied_world_version: int | None = None
    created_at: object
    updated_at: object
    effects: list[RemovalEffectView] = Field(default_factory=list)


class ElementRemovalNotFound(LookupError):
    pass


class ElementRemovalConflict(ValueError):
    pass


class ElementRemovalRejected(ValueError):
    pass


@dataclass(slots=True)
class RemovalEffect:
    effect_type: str
    entity_type: str | None = None
    entity_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


class WorldElementRemover:
    """统一元素删除器。

    删除的语义是“从活跃世界中移除并写入墓碑”，而非物理 DELETE；历史事件、
    注册来源、谱系和旧照片仍然可审计。每类元素在自身事实表内执行受限的状态转换。
    """

    def __init__(self) -> None:
        self.catalog = WorldElementCatalog()

    def submit(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        request: ElementRemovalSubmit,
    ) -> ElementRemovalView:
        world = connection.execute("SELECT * FROM worlds WHERE id = ?", (world_id,)).fetchone()
        if world is None:
            raise ElementRemovalNotFound("世界不存在")
        source_event = connection.execute(
            "SELECT * FROM world_events WHERE id = ? AND world_id = ?",
            (request.source_event_id, world_id),
        ).fetchone()
        if source_event is None:
            raise ElementRemovalNotFound("来源事件不存在或不属于当前世界")
        if request.requested_by_character_id is not None:
            requester = connection.execute(
                "SELECT id FROM characters WHERE id = ? AND world_id = ?",
                (request.requested_by_character_id, world_id),
            ).fetchone()
            if requester is None:
                raise ElementRemovalNotFound("申请人物不存在或不属于当前世界")
            if request.requested_by_character_id not in {
                source_event["actor_id"],
                source_event["target_id"],
            }:
                raise ElementRemovalRejected("申请人物不是来源事件的参与者")

        details = request.details.strip()
        existing = connection.execute(
            """
            SELECT * FROM element_removal_requests
            WHERE world_id = ? AND idempotency_key = ?
            """,
            (world_id, request.idempotency_key),
        ).fetchone()
        if existing is not None:
            expected = (
                request.target_element_type,
                request.target_entity_id,
                request.source_event_id,
                request.requested_by_character_id,
                request.reason.value,
                details,
            )
            actual = (
                existing["target_element_type"],
                existing["target_entity_id"],
                existing["source_event_id"],
                existing["requested_by_character_id"],
                existing["reason"],
                existing["details"],
            )
            if actual != expected:
                raise ElementRemovalConflict("幂等键已经用于不同的删除内容")
            return self.get(connection, world_id=world_id, removal_id=existing["id"])

        removal_id = str(uuid4())
        now = utc_now()
        connection.execute(
            """
            INSERT INTO element_removal_requests(
                id, world_id, target_element_type, target_entity_id,
                requested_by_character_id, source_event_id, idempotency_key,
                reason, details, status, input_world_version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'validating', ?, ?, ?)
            """,
            (
                removal_id,
                world_id,
                request.target_element_type,
                request.target_entity_id,
                request.requested_by_character_id,
                request.source_event_id,
                request.idempotency_key,
                request.reason.value,
                details,
                int(world["version"]),
                to_iso(now),
                to_iso(now),
            ),
        )
        connection.execute("SAVEPOINT element_removal_apply")
        try:
            target = self._ensure_catalog_target(
                connection,
                world_id=world_id,
                entity_type=request.target_element_type,
                entity_id=request.target_entity_id,
            )
            if target.lifecycle_state is not ElementLifecycleState.ACTIVE:
                raise ElementRemovalRejected("目标元素已经不在活跃世界中")
            effects = self._apply_target_removal(
                connection,
                world_id=world_id,
                target_element_type=request.target_element_type,
                target_entity_id=request.target_entity_id,
                reason=request.reason,
                world_time=from_iso(world["current_time"]),
                now=now,
            )
            event_id = self._record_event(
                connection,
                world_id=world_id,
                removal_id=removal_id,
                world_time=from_iso(world["current_time"]),
                actor_id=request.requested_by_character_id,
                location_id=source_event["location_id"],
                target_name=target.name,
                request=request,
                now=now,
            )
            self.catalog.synchronize_world(connection, world_id=world_id)
            self.catalog.mark_lifecycle(
                connection,
                world_id=world_id,
                entity_type=request.target_element_type,
                entity_id=request.target_entity_id,
                state=(
                    ElementLifecycleState.DESTROYED
                    if request.reason is RemovalReason.DESTROYED
                    else ElementLifecycleState.RETIRED
                ),
                source_event_id=event_id,
                metadata_update={"removal_id": removal_id, "details": details},
            )
            if request.target_element_type in {"location", "settlement"}:
                self._mark_location_aliases_destroyed(
                    connection,
                    world_id=world_id,
                    location_id=request.target_entity_id,
                    event_id=event_id,
                )
            self._persist_effects(
                connection,
                removal_id=removal_id,
                world_id=world_id,
                effects=effects,
                now=now,
            )
            connection.execute(
                "UPDATE worlds SET version = version + 1, updated_at = ? WHERE id = ?",
                (to_iso(now), world_id),
            )
            version = connection.execute(
                "SELECT version FROM worlds WHERE id = ?", (world_id,)
            ).fetchone()["version"]
            connection.execute(
                """
                UPDATE element_removal_requests
                SET status = 'applied', applied_world_version = ?, updated_at = ?
                WHERE id = ?
                """,
                (version, to_iso(now), removal_id),
            )
            connection.execute("RELEASE SAVEPOINT element_removal_apply")
        except ElementRemovalRejected as exc:
            connection.execute("ROLLBACK TO SAVEPOINT element_removal_apply")
            connection.execute("RELEASE SAVEPOINT element_removal_apply")
            connection.execute(
                """
                UPDATE element_removal_requests
                SET status = 'rejected', rejection_reason = ?, updated_at = ?
                WHERE id = ?
                """,
                (str(exc), to_iso(utc_now()), removal_id),
            )
        except Exception as exc:  # noqa: BLE001 - 副作用失败时必须留下审计记录。
            connection.execute("ROLLBACK TO SAVEPOINT element_removal_apply")
            connection.execute("RELEASE SAVEPOINT element_removal_apply")
            LOGGER.exception("世界元素删除失败：%s", removal_id)
            connection.execute(
                """
                UPDATE element_removal_requests
                SET status = 'failed', rejection_reason = ?, updated_at = ?
                WHERE id = ?
                """,
                (str(exc), to_iso(utc_now()), removal_id),
            )
        return self.get(connection, world_id=world_id, removal_id=removal_id)

    def get(
        self, connection: sqlite3.Connection, *, world_id: str, removal_id: str
    ) -> ElementRemovalView:
        row = connection.execute(
            "SELECT * FROM element_removal_requests WHERE id = ? AND world_id = ?",
            (removal_id, world_id),
        ).fetchone()
        if row is None:
            raise ElementRemovalNotFound("删除请求不存在")
        return self._view(connection, row)

    def list(
        self, connection: sqlite3.Connection, *, world_id: str, limit: int = 100
    ) -> list[ElementRemovalView]:
        rows = connection.execute(
            """
            SELECT * FROM element_removal_requests WHERE world_id = ?
            ORDER BY created_at DESC, id DESC LIMIT ?
            """,
            (world_id, max(1, min(limit, 500))),
        ).fetchall()
        return [self._view(connection, row) for row in rows]

    def _ensure_catalog_target(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        entity_type: str,
        entity_id: str,
    ):
        existing = self.catalog.get(
            connection,
            world_id=world_id,
            entity_type=entity_type,
            entity_id=entity_id,
        )
        if existing is not None:
            return existing
        source = self._read_source_entity(
            connection,
            world_id=world_id,
            entity_type=entity_type,
            entity_id=entity_id,
        )
        if source is None:
            raise ElementRemovalNotFound("目标元素不存在或不属于当前世界")
        self.catalog.upsert(
            connection,
            world_id=world_id,
            entity_type=entity_type,
            entity_id=entity_id,
            name=source["name"],
            source_kind="system",
        )
        target = self.catalog.get(
            connection,
            world_id=world_id,
            entity_type=entity_type,
            entity_id=entity_id,
        )
        if target is None:  # pragma: no cover - SQLite 插入成功后不应发生。
            raise ElementRemovalNotFound("目标元素目录写入失败")
        return target

    @staticmethod
    def _read_source_entity(
        connection: sqlite3.Connection,
        *,
        world_id: str,
        entity_type: str,
        entity_id: str,
    ) -> sqlite3.Row | None:
        queries = {
            "character": "SELECT id, name FROM characters WHERE world_id = ? AND id = ?",
            "location": "SELECT id, name FROM locations WHERE world_id = ? AND id = ?",
            "settlement": "SELECT id, name FROM locations WHERE world_id = ? AND id = ?",
            "building": "SELECT id, name FROM buildings WHERE world_id = ? AND id = ?",
            "structure": "SELECT id, name FROM world_structures WHERE world_id = ? AND id = ?",
            "knowledge_entry": (
                "SELECT id, title AS name FROM knowledge_entries "
                "WHERE world_id = ? AND id = ?"
            ),
            "vehicle": "SELECT id, name FROM vehicles WHERE world_id = ? AND id = ?",
        }
        return connection.execute(queries[entity_type], (world_id, entity_id)).fetchone()

    def _apply_target_removal(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        target_element_type: str,
        target_entity_id: str,
        reason: RemovalReason,
        world_time: datetime,
        now: object,
    ) -> list[RemovalEffect]:
        updated_at = to_iso(now)
        if target_element_type == "building":
            connection.execute(
                """
                UPDATE buildings SET status = 'ruined', updated_at = ?
                WHERE id = ? AND world_id = ?
                """,
                (updated_at, target_entity_id, world_id),
            )
            cancelled = connection.execute(
                """
                UPDATE construction_projects SET status = 'cancelled', updated_at = ?
                WHERE world_id = ? AND target_entity_id = ?
                  AND status IN ('planned', 'surveyed', 'constructing')
                """,
                (updated_at, world_id, target_entity_id),
            ).rowcount
            return [
                RemovalEffect("building.ruined", "building", target_entity_id),
                RemovalEffect(
                    "construction.destroyed",
                    "construction_project",
                    None,
                    {"cancelled_projects": cancelled, "refund": "none"},
                ),
            ]
        if target_element_type == "structure":
            connection.execute(
                """
                UPDATE world_structures SET status = 'ruined', updated_at = ?
                WHERE id = ? AND world_id = ?
                """,
                (updated_at, target_entity_id, world_id),
            )
            cancelled = connection.execute(
                """
                UPDATE construction_projects SET status = 'cancelled', updated_at = ?
                WHERE world_id = ? AND target_entity_id = ?
                  AND status IN ('planned', 'surveyed', 'constructing')
                """,
                (updated_at, world_id, target_entity_id),
            ).rowcount
            return [
                RemovalEffect("structure.ruined", "structure", target_entity_id),
                RemovalEffect(
                    "construction.destroyed",
                    "construction_project",
                    None,
                    {"cancelled_projects": cancelled, "refund": "none"},
                ),
            ]
        if target_element_type in {"location", "settlement"}:
            occupants = connection.execute(
                """
                SELECT name FROM characters
                WHERE world_id = ? AND current_location_id = ? AND health > 0
                """,
                (world_id, target_entity_id),
            ).fetchall()
            if occupants:
                names = "、".join(row["name"] for row in occupants[:3])
                raise ElementRemovalRejected(f"目标地点仍有在场人物（{names}），必须先撤离")
            connection.execute(
                "UPDATE locations SET is_active = 0 WHERE id = ? AND world_id = ?",
                (target_entity_id, world_id),
            )
            return [RemovalEffect("location.destroyed", target_element_type, target_entity_id)]
        if target_element_type == "character":
            row = connection.execute(
                "SELECT is_player FROM characters WHERE world_id = ? AND id = ?",
                (world_id, target_entity_id),
            ).fetchone()
            if row is None:
                raise ElementRemovalNotFound("人物不存在")
            if bool(row["is_player"]):
                raise ElementRemovalRejected("玩家人物由败北/复苏流程管理，不能被删除器移除")
            connection.execute(
                """
                UPDATE characters
                SET health = 0, activation_state = 'background',
                    activation_reason = 'destroyed', updated_at = ?
                WHERE id = ? AND world_id = ?
                """,
                (updated_at, target_entity_id, world_id),
            )
            return [RemovalEffect("character.deceased", "character", target_entity_id)]
        if target_element_type == "knowledge_entry":
            connection.execute(
                """
                UPDATE knowledge_entries SET status = 'retired', updated_at = ?
                WHERE id = ? AND world_id = ?
                """,
                (updated_at, target_entity_id, world_id),
            )
            return [RemovalEffect("knowledge.retired", "knowledge_entry", target_entity_id)]
        if target_element_type == "vehicle":
            from world_engine.movement import MovementService

            affected_movements = connection.execute(
                """
                SELECT character_id FROM character_movements
                WHERE world_id = ? AND vehicle_id = ? AND status = 'moving'
                ORDER BY started_at_world, id
                """,
                (world_id, target_entity_id),
            ).fetchall()
            movement_service = MovementService()
            for movement in affected_movements:
                movement_service.cancel(
                    connection,
                    world_id=world_id,
                    character_id=movement["character_id"],
                    world_time=world_time,
                    created_at=now,
                )
            connection.execute(
                """
                UPDATE vehicles SET is_available = 0, updated_at = ?
                WHERE id = ? AND world_id = ?
                """,
                (updated_at, target_entity_id, world_id),
            )
            detached = connection.execute(
                """
                UPDATE characters
                SET active_vehicle_id = NULL, movement_type = 'land',
                    movement_speed_kmh = 5, updated_at = ?
                WHERE world_id = ? AND active_vehicle_id = ?
                """,
                (updated_at, world_id, target_entity_id),
            ).rowcount
            return [
                RemovalEffect(
                    "vehicle.destroyed",
                    "vehicle",
                    target_entity_id,
                    {
                        "cancelled_movements": len(affected_movements),
                        "detached_characters": detached,
                    },
                )
            ]
        raise ElementRemovalRejected("当前元素类型尚未实现受控删除")

    def _mark_location_aliases_destroyed(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        location_id: str,
        event_id: str,
    ) -> None:
        aliases = connection.execute(
            """
            SELECT entity_type FROM world_element_catalog
            WHERE world_id = ? AND entity_id = ?
              AND entity_type IN ('location', 'settlement')
            """,
            (world_id, location_id),
        ).fetchall()
        for alias in aliases:
            self.catalog.mark_lifecycle(
                connection,
                world_id=world_id,
                entity_type=alias["entity_type"],
                entity_id=location_id,
                state=ElementLifecycleState.DESTROYED,
                source_event_id=event_id,
            )

    @staticmethod
    def _record_event(
        connection: sqlite3.Connection,
        *,
        world_id: str,
        removal_id: str,
        world_time: object,
        actor_id: str | None,
        location_id: str | None,
        target_name: str,
        request: ElementRemovalSubmit,
        now: object,
    ) -> str:
        event_id = str(uuid4())
        label = "毁灭" if request.reason is RemovalReason.DESTROYED else "退役"
        connection.execute(
            """
            INSERT INTO world_events(
                id, world_id, tick_id, occurred_at, event_type, actor_id,
                location_id, summary, importance, payload_json, created_at
            ) VALUES (?, ?, ?, ?, 'world.element_removed', ?, ?, ?, 'routine', ?, ?)
            """,
            (
                event_id,
                world_id,
                removal_id,
                to_iso(world_time),
                actor_id,
                location_id,
                f"世界元素“{target_name}”已被{label}：{request.details.strip()}",
                json.dumps(
                    {
                        "removal_id": removal_id,
                        "source_event_id": request.source_event_id,
                        "target_element_type": request.target_element_type,
                        "target_entity_id": request.target_entity_id,
                        "reason": request.reason.value,
                    },
                    ensure_ascii=False,
                ),
                to_iso(now),
            ),
        )
        return event_id

    @staticmethod
    def _persist_effects(
        connection: sqlite3.Connection,
        *,
        removal_id: str,
        world_id: str,
        effects: list[RemovalEffect],
        now: object,
    ) -> None:
        for effect in effects:
            connection.execute(
                """
                INSERT INTO element_removal_effects(
                    id, removal_id, world_id, effect_type, entity_type,
                    entity_id, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    removal_id,
                    world_id,
                    effect.effect_type,
                    effect.entity_type,
                    effect.entity_id,
                    json.dumps(effect.payload, ensure_ascii=False),
                    to_iso(now),
                ),
            )

    @staticmethod
    def _view(connection: sqlite3.Connection, row: sqlite3.Row) -> ElementRemovalView:
        effects = connection.execute(
            "SELECT * FROM element_removal_effects WHERE removal_id = ? ORDER BY created_at, id",
            (row["id"],),
        ).fetchall()
        return ElementRemovalView(
            id=row["id"],
            world_id=row["world_id"],
            target_element_type=row["target_element_type"],
            target_entity_id=row["target_entity_id"],
            requested_by_character_id=row["requested_by_character_id"],
            source_event_id=row["source_event_id"],
            idempotency_key=row["idempotency_key"],
            reason=RemovalReason(row["reason"]),
            details=row["details"],
            status=RemovalStatus(row["status"]),
            rejection_reason=row["rejection_reason"],
            input_world_version=int(row["input_world_version"]),
            applied_world_version=row["applied_world_version"],
            created_at=from_iso(row["created_at"]),
            updated_at=from_iso(row["updated_at"]),
            effects=[
                RemovalEffectView(
                    id=item["id"],
                    effect_type=item["effect_type"],
                    entity_type=item["entity_type"],
                    entity_id=item["entity_id"],
                    payload=json.loads(item["payload_json"]),
                )
                for item in effects
            ],
        )
