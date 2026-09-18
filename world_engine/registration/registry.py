"""世界元素注册表：统一的提案→校验→落库门面。"""

from __future__ import annotations

import json
import logging
import sqlite3
from uuid import uuid4

from world_engine.elements import WorldElementCatalog
from world_engine.registration.handlers import (
    ActivityRecipeHandler,
    BuildingHandler,
    CharacterArrivalHandler,
    CharacterBirthHandler,
    CommodityHandler,
    InteriorRoomHandler,
    LoreHandler,
    RoutinePlanHandler,
    SettlementHandler,
    StructureHandler,
    WorkplaceBudgetHandler,
)
from world_engine.registration.models import (
    REGISTRATION_PAYLOAD_ADAPTER,
    ElementRegistrationSubmit,
    ElementRegistrationView,
    ElementType,
    HandlerResult,
    RegistrationConflict,
    RegistrationContext,
    RegistrationEffectView,
    RegistrationHandler,
    RegistrationNotFound,
    RegistrationPayload,
    RegistrationRejected,
    RegistrationStatus,
)
from world_engine.repository import from_iso, to_iso, utc_now

LOGGER = logging.getLogger("virtual-world.registration")


class WorldElementRegistry:
    """统一注册门面；处理器只接受严格类型，不允许 Agent 或前端直接写事实表。"""

    def __init__(self) -> None:
        handlers: list[RegistrationHandler] = [
            CharacterBirthHandler(),
            CharacterArrivalHandler(),
            SettlementHandler(),
            BuildingHandler(),
            StructureHandler(),
            LoreHandler(),
            InteriorRoomHandler(),
            ActivityRecipeHandler(),
            CommodityHandler(),
            WorkplaceBudgetHandler(),
            RoutinePlanHandler(),
        ]
        self.handlers = {handler.element_type: handler for handler in handlers}
        self.catalog = WorldElementCatalog()

    def submit(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        request: ElementRegistrationSubmit,
        auto_apply: bool = True,
    ) -> ElementRegistrationView:
        world = connection.execute(
            "SELECT * FROM worlds WHERE id = ?", (world_id,)
        ).fetchone()
        if world is None:
            raise RegistrationNotFound("世界不存在")
        source_event = connection.execute(
            "SELECT * FROM world_events WHERE id = ? AND world_id = ?",
            (request.source_event_id, world_id),
        ).fetchone()
        if source_event is None:
            raise RegistrationNotFound("来源事件不存在或不属于当前世界")
        if request.requested_by_character_id is not None:
            requester = connection.execute(
                "SELECT id FROM characters WHERE id = ? AND world_id = ?",
                (request.requested_by_character_id, world_id),
            ).fetchone()
            if requester is None:
                raise RegistrationNotFound("申请人物不存在或不属于当前世界")

        payload_json = json.dumps(request.payload.model_dump(mode="json"), ensure_ascii=False)
        existing = connection.execute(
            """
            SELECT * FROM element_registration_requests
            WHERE world_id = ? AND idempotency_key = ?
            """,
            (world_id, request.idempotency_key),
        ).fetchone()
        if existing is not None:
            if (
                existing["payload_json"] != payload_json
                or existing["source_event_id"] != request.source_event_id
                or existing["requested_by_character_id"]
                != request.requested_by_character_id
            ):
                raise RegistrationConflict("幂等键已经用于不同的注册内容")
            return self.get(connection, world_id=world_id, registration_id=existing["id"])

        registration_id = str(uuid4())
        now = utc_now()
        element_type = ElementType(request.payload.element_type)
        connection.execute(
            """
            INSERT INTO element_registration_requests(
                id, world_id, element_type, requested_by_character_id,
                source_event_id, idempotency_key, schema_version, status,
                payload_json, input_world_version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 1, 'validating', ?, ?, ?, ?)
            """,
            (
                registration_id,
                world_id,
                element_type.value,
                request.requested_by_character_id,
                request.source_event_id,
                request.idempotency_key,
                payload_json,
                int(world["version"]),
                to_iso(now),
                to_iso(now),
            ),
        )
        context = RegistrationContext(
            connection=connection,
            registration_id=registration_id,
            world_id=world_id,
            world_time=from_iso(world["current_time"]),
            requested_by_character_id=request.requested_by_character_id,
            source_event=source_event,
            now=now,
        )
        if not auto_apply:
            connection.execute(
                """
                UPDATE element_registration_requests
                SET status = 'proposed', updated_at = ? WHERE id = ?
                """,
                (to_iso(now), registration_id),
            )
            return self.get(connection, world_id=world_id, registration_id=registration_id)
        return self._apply_existing(
            connection,
            context=context,
            payload=request.payload,
        )

    def confirm(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        registration_id: str,
    ) -> ElementRegistrationView:
        row = connection.execute(
            "SELECT * FROM element_registration_requests WHERE id = ? AND world_id = ?",
            (registration_id, world_id),
        ).fetchone()
        if row is None:
            raise RegistrationNotFound("注册请求不存在")
        if row["status"] != RegistrationStatus.PROPOSED.value:
            raise RegistrationConflict("只有待确认的注册请求可以确认")
        world = connection.execute(
            "SELECT * FROM worlds WHERE id = ?", (world_id,)
        ).fetchone()
        source_event = connection.execute(
            "SELECT * FROM world_events WHERE id = ? AND world_id = ?",
            (row["source_event_id"], world_id),
        ).fetchone()
        if world is None or source_event is None:
            raise RegistrationNotFound("注册请求的世界或来源事件不存在")
        payload = REGISTRATION_PAYLOAD_ADAPTER.validate_python(
            json.loads(row["payload_json"])
        )
        context = RegistrationContext(
            connection=connection,
            registration_id=registration_id,
            world_id=world_id,
            world_time=from_iso(world["current_time"]),
            requested_by_character_id=row["requested_by_character_id"],
            source_event=source_event,
            now=utc_now(),
        )
        return self._apply_existing(connection, context=context, payload=payload)

    def reject(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        registration_id: str,
        reason: str,
    ) -> ElementRegistrationView:
        row = connection.execute(
            "SELECT status FROM element_registration_requests WHERE id = ? AND world_id = ?",
            (registration_id, world_id),
        ).fetchone()
        if row is None:
            raise RegistrationNotFound("注册请求不存在")
        if row["status"] != RegistrationStatus.PROPOSED.value:
            raise RegistrationConflict("只有待确认的注册请求可以拒绝")
        connection.execute(
            """
            UPDATE element_registration_requests
            SET status = 'rejected', rejection_reason = ?, updated_at = ?
            WHERE id = ?
            """,
            (reason.strip() or "申请者拒绝该候选", to_iso(utc_now()), registration_id),
        )
        return self.get(connection, world_id=world_id, registration_id=registration_id)

    def _apply_existing(
        self,
        connection: sqlite3.Connection,
        *,
        context: RegistrationContext,
        payload: RegistrationPayload,
    ) -> ElementRegistrationView:
        registration_id = context.registration_id
        element_type = ElementType(payload.element_type)
        connection.execute("SAVEPOINT element_registration_apply")
        try:
            self._validate_requester_participation(context)
            connection.execute(
                "UPDATE element_registration_requests SET status = 'applying' WHERE id = ?",
                (registration_id,),
            )
            result = self.handlers[element_type].apply(context, payload)
            self._persist_result(connection, context, result)
            connection.execute("RELEASE SAVEPOINT element_registration_apply")
        except RegistrationRejected as exc:
            connection.execute("ROLLBACK TO SAVEPOINT element_registration_apply")
            connection.execute("RELEASE SAVEPOINT element_registration_apply")
            connection.execute(
                """
                UPDATE element_registration_requests
                SET status = 'rejected', rejection_reason = ?, updated_at = ?
                WHERE id = ?
                """,
                (str(exc), to_iso(utc_now()), registration_id),
            )
        except Exception as exc:  # noqa: BLE001 - 失败必须留审计记录且回滚局部副作用
            connection.execute("ROLLBACK TO SAVEPOINT element_registration_apply")
            connection.execute("RELEASE SAVEPOINT element_registration_apply")
            LOGGER.exception("世界元素注册失败：%s", registration_id)
            connection.execute(
                """
                UPDATE element_registration_requests
                SET status = 'failed', rejection_reason = ?, updated_at = ?
                WHERE id = ?
                """,
                (str(exc), to_iso(utc_now()), registration_id),
            )
        return self.get(
            connection,
            world_id=context.world_id,
            registration_id=registration_id,
        )

    def get(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        registration_id: str,
    ) -> ElementRegistrationView:
        row = connection.execute(
            """
            SELECT * FROM element_registration_requests
            WHERE id = ? AND world_id = ?
            """,
            (registration_id, world_id),
        ).fetchone()
        if row is None:
            raise RegistrationNotFound("注册请求不存在")
        return self._view(connection, row)

    def list(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        limit: int = 100,
        element_type: ElementType | None = None,
        status: RegistrationStatus | None = None,
    ) -> list[ElementRegistrationView]:
        world = connection.execute(
            "SELECT 1 FROM worlds WHERE id = ?", (world_id,)
        ).fetchone()
        if world is None:
            raise RegistrationNotFound("世界不存在")
        conditions = ["world_id = ?"]
        parameters: list[object] = [world_id]
        if element_type is not None:
            conditions.append("element_type = ?")
            parameters.append(element_type.value)
        if status is not None:
            conditions.append("status = ?")
            parameters.append(status.value)
        parameters.append(limit)
        rows = connection.execute(
            f"""
            SELECT * FROM element_registration_requests
            WHERE {' AND '.join(conditions)}
            ORDER BY created_at DESC LIMIT ?
            """,  # noqa: S608 - 条件片段只来自固定字符串
            parameters,
        ).fetchall()
        return [self._view(connection, row) for row in rows]

    @staticmethod
    def _validate_requester_participation(context: RegistrationContext) -> None:
        requester_id = context.requested_by_character_id
        if requester_id is None:
            return
        if requester_id not in {
            context.source_event["actor_id"],
            context.source_event["target_id"],
        }:
            raise RegistrationRejected("申请人物不是来源事件的参与者")

    def _persist_result(
        self,
        connection: sqlite3.Connection,
        context: RegistrationContext,
        result: HandlerResult,
    ) -> None:
        if result.entity_id and result.entity_type and result.entity_name:
            entity_sql="""INSERT INTO world_entities(
                id, world_id, entity_type, entity_id, name, registration_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)"""
            if result.entity_type=="npc_routine":
                entity_sql += " ON CONFLICT(world_id,entity_type,entity_id) DO UPDATE SET name=excluded.name,registration_id=excluded.registration_id"
            connection.execute(
                entity_sql,
                (
                    str(uuid4()),
                    context.world_id,
                    result.entity_type,
                    result.entity_id,
                    result.entity_name,
                    context.registration_id,
                    to_iso(context.now),
                ),
            )
            self.catalog.upsert(
                connection,
                world_id=context.world_id,
                entity_type=result.entity_type,
                entity_id=result.entity_id,
                name=result.entity_name,
                source_kind="registration",
                source_registration_id=context.registration_id,
                source_event_id=context.source_event["id"],
            )
        for effect in result.effects:
            connection.execute(
                """
                INSERT INTO element_registration_effects(
                    id, registration_id, world_id, effect_type,
                    entity_type, entity_id, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    context.registration_id,
                    context.world_id,
                    effect.effect_type,
                    effect.entity_type,
                    effect.entity_id,
                    json.dumps(effect.payload, ensure_ascii=False),
                    to_iso(context.now),
                ),
            )
        event_type = (
            "world.element_registered"
            if result.status is RegistrationStatus.APPLIED
            else "world.element_registration_approved"
        )
        connection.execute(
            """
            INSERT INTO world_events(
                id, world_id, tick_id, occurred_at, event_type, actor_id,
                location_id, summary, importance, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'routine', ?, ?)
            """,
            (
                str(uuid4()),
                context.world_id,
                context.registration_id,
                to_iso(context.world_time),
                event_type,
                context.requested_by_character_id,
                result.location_id or context.source_event["location_id"],
                result.summary,
                json.dumps(
                    {
                        "registration_id": context.registration_id,
                        "source_event_id": context.source_event["id"],
                        "entity_type": result.entity_type,
                        "entity_id": result.entity_id,
                    },
                    ensure_ascii=False,
                ),
                to_iso(context.now),
            ),
        )
        connection.execute(
            "UPDATE worlds SET version = version + 1, updated_at = ? WHERE id = ?",
            (to_iso(context.now), context.world_id),
        )
        applied_version = connection.execute(
            "SELECT version FROM worlds WHERE id = ?", (context.world_id,)
        ).fetchone()["version"]
        connection.execute(
            """
            UPDATE element_registration_requests
            SET status = ?, result_entity_id = ?, applied_world_version = ?,
                rejection_reason = NULL, updated_at = ?
            WHERE id = ?
            """,
            (
                result.status.value,
                result.entity_id,
                applied_version,
                to_iso(context.now),
                context.registration_id,
            ),
        )

    @staticmethod
    def _view(
        connection: sqlite3.Connection, row: sqlite3.Row
    ) -> ElementRegistrationView:
        effects = connection.execute(
            """
            SELECT * FROM element_registration_effects
            WHERE registration_id = ? ORDER BY created_at, id
            """,
            (row["id"],),
        ).fetchall()
        return ElementRegistrationView(
            id=row["id"],
            world_id=row["world_id"],
            element_type=ElementType(row["element_type"]),
            requested_by_character_id=row["requested_by_character_id"],
            source_event_id=row["source_event_id"],
            idempotency_key=row["idempotency_key"],
            schema_version=int(row["schema_version"]),
            status=RegistrationStatus(row["status"]),
            payload=json.loads(row["payload_json"]),
            result_entity_id=row["result_entity_id"],
            rejection_reason=row["rejection_reason"],
            input_world_version=int(row["input_world_version"]),
            applied_world_version=row["applied_world_version"],
            created_at=from_iso(row["created_at"]),
            updated_at=from_iso(row["updated_at"]),
            effects=[
                RegistrationEffectView(
                    id=effect["id"],
                    effect_type=effect["effect_type"],
                    entity_type=effect["entity_type"],
                    entity_id=effect["entity_id"],
                    payload=json.loads(effect["payload_json"]),
                    created_at=from_iso(effect["created_at"]),
                )
                for effect in effects
            ],
        )
