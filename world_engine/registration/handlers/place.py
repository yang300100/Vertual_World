"""地点类世界元素处理器：聚落、建筑、巨构与世界观条目。"""

from __future__ import annotations

import json
from uuid import uuid4

from world_engine.registration.models import (
    BuildingSpec,
    Effect,
    ElementType,
    HandlerResult,
    LoreSpec,
    RegistrationContext,
    RegistrationPayload,
    RegistrationRejected,
    RegistrationStatus,
    SettlementSpec,
    StructureSpec,
    _clean_nonnegative_values,
    _entity_exists,
    _require_location,
)
from world_engine.repository import to_iso


class SettlementHandler:
    element_type = ElementType.SETTLEMENT

    def apply(
        self, context: RegistrationContext, payload: RegistrationPayload
    ) -> HandlerResult:
        if not isinstance(payload, SettlementSpec):
            raise TypeError("聚落处理器收到错误的数据类型")
        resources = _clean_nonnegative_values(payload.resources, "聚落资源")
        requirements = _clean_nonnegative_values(payload.requirements, "建设需求")
        existing = context.connection.execute(
            "SELECT 1 FROM locations WHERE world_id = ? AND name = ?",
            (context.world_id, payload.name.strip()),
        ).fetchone()
        if existing is not None:
            raise RegistrationRejected("当前世界已经存在同名地点")
        if payload.stage == "established" and context.requested_by_character_id is not None:
            raise RegistrationRejected("玩家或NPC创建聚落必须从 planned 阶段开始")

        if payload.stage == "planned":
            claim_radius = max(1.0, payload.area_radius_km)
            overlap = context.connection.execute(
                """
                SELECT 1 FROM land_claims
                WHERE world_id = ? AND status IN ('reserved', 'active')
                  AND ABS(longitude - ?) < ? AND ABS(latitude - ?) < ?
                """,
                (
                    context.world_id,
                    payload.longitude,
                    claim_radius / 111,
                    payload.latitude,
                    claim_radius / 111,
                ),
            ).fetchone()
            if overlap is not None:
                raise RegistrationRejected("目标地块已经存在有效的聚落土地权")
            project_id = str(uuid4())
            claim_id = str(uuid4())
            context.connection.execute(
                """
                INSERT INTO land_claims(
                    id, world_id, registration_id, claimant_character_id,
                    longitude, latitude, radius_km, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?)
                """,
                (
                    claim_id,
                    context.world_id,
                    context.registration_id,
                    context.requested_by_character_id,
                    payload.longitude,
                    payload.latitude,
                    claim_radius,
                    to_iso(context.now),
                    to_iso(context.now),
                ),
            )
            context.connection.execute(
                """
                INSERT INTO construction_projects(
                    id, world_id, registration_id, project_type, target_name, status,
                    location_id, longitude, latitude, progress, requirements_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'settlement', ?, 'planned', ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    project_id,
                    context.world_id,
                    context.registration_id,
                    payload.name.strip(),
                    context.source_event["location_id"],
                    payload.longitude,
                    payload.latitude,
                    json.dumps(requirements, ensure_ascii=False),
                    to_iso(context.now),
                    to_iso(context.now),
                ),
            )
            return HandlerResult(
                status=RegistrationStatus.APPROVED,
                summary=f"聚落“{payload.name}”已登记为规划项目，尚未形成城市。",
                entity_type="construction_project",
                entity_id=project_id,
                entity_name=payload.name.strip(),
                effects=[
                    Effect(
                        effect_type="settlement.planned",
                        entity_type="construction_project",
                        entity_id=project_id,
                        payload={"requirements": requirements},
                    ),
                    Effect(
                        effect_type="land.reserved",
                        entity_type="land_claim",
                        entity_id=claim_id,
                    ),
                ],
            )

        location_id = str(uuid4())
        context.connection.execute(
            """
            INSERT INTO locations(
                id, world_id, name, kind, resources_json, longitude, latitude,
                area_radius_km, area_priority
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 20)
            """,
            (
                location_id,
                context.world_id,
                payload.name.strip(),
                payload.settlement_kind,
                json.dumps(resources, ensure_ascii=False),
                payload.longitude,
                payload.latitude,
                payload.area_radius_km,
            ),
        )
        return HandlerResult(
            status=RegistrationStatus.APPLIED,
            summary=f"聚落“{payload.name}”已经登记为{payload.settlement_kind}。",
            entity_type="settlement",
            entity_id=location_id,
            entity_name=payload.name.strip(),
            location_id=location_id,
            effects=[
                Effect(
                    effect_type="settlement.created",
                    entity_type="settlement",
                    entity_id=location_id,
                    payload={"resources": resources},
                )
            ],
        )


class BuildingHandler:
    element_type = ElementType.BUILDING

    def apply(
        self, context: RegistrationContext, payload: RegistrationPayload
    ) -> HandlerResult:
        if not isinstance(payload, BuildingSpec):
            raise TypeError("建筑处理器收到错误的数据类型")
        location = _require_location(context.connection, context.world_id, payload.location_id)
        requirements = _clean_nonnegative_values(payload.requirements, "建设需求")
        if payload.stage == "completed" and context.requested_by_character_id is not None:
            raise RegistrationRejected("玩家或NPC创建建筑不能跳过施工阶段")
        if payload.owner_entity_id and not _entity_exists(
            context.connection, context.world_id, payload.owner_entity_id
        ):
            raise RegistrationRejected("建筑所有者不是当前世界中的已登记实体")
        exists = context.connection.execute(
            "SELECT 1 FROM buildings WHERE world_id = ? AND name = ?",
            (context.world_id, payload.name.strip()),
        ).fetchone()
        if exists is not None:
            raise RegistrationRejected("当前世界已经存在同名建筑")
        longitude = payload.longitude if payload.longitude is not None else location["longitude"]
        latitude = payload.latitude if payload.latitude is not None else location["latitude"]
        building_id = str(uuid4())
        context.connection.execute(
            """
            INSERT INTO buildings(
                id, world_id, registration_id, name, building_type, status,
                location_id, owner_entity_id, longitude, latitude, metadata_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                building_id,
                context.world_id,
                context.registration_id,
                payload.name.strip(),
                payload.building_type.strip(),
                payload.stage,
                payload.location_id,
                payload.owner_entity_id,
                longitude,
                latitude,
                json.dumps(payload.metadata, ensure_ascii=False),
                to_iso(context.now),
                to_iso(context.now),
            ),
        )
        effects = [
            Effect(
                effect_type="building.registered",
                entity_type="building",
                entity_id=building_id,
                payload={"stage": payload.stage},
            )
        ]
        if payload.stage != "completed":
            project_id = str(uuid4())
            context.connection.execute(
                """
                INSERT INTO construction_projects(
                    id, world_id, registration_id, project_type, target_name,
                    target_entity_id, status,
                    location_id, longitude, latitude, progress, requirements_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'building', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    context.world_id,
                    context.registration_id,
                    payload.name.strip(),
                    building_id,
                    payload.stage,
                    payload.location_id,
                    longitude,
                    latitude,
                    0.0 if payload.stage == "planned" else 0.1,
                    json.dumps(requirements, ensure_ascii=False),
                    to_iso(context.now),
                    to_iso(context.now),
                ),
            )
            effects.append(
                Effect(
                    effect_type="construction.started",
                    entity_type="construction_project",
                    entity_id=project_id,
                )
            )
        return HandlerResult(
            status=RegistrationStatus.APPLIED,
            summary=f"建筑“{payload.name}”已以{payload.stage}状态登记。",
            entity_type="building",
            entity_id=building_id,
            entity_name=payload.name.strip(),
            location_id=payload.location_id,
            effects=effects,
        )


class StructureHandler:
    element_type = ElementType.STRUCTURE

    def apply(
        self, context: RegistrationContext, payload: RegistrationPayload
    ) -> HandlerResult:
        if not isinstance(payload, StructureSpec):
            raise TypeError("巨构/遗迹处理器收到错误的数据类型")
        if payload.location_id is not None:
            _require_location(context.connection, context.world_id, payload.location_id)
        _clean_nonnegative_values(payload.requirements, "建设需求")
        if (
            payload.origin_mode == "constructed"
            and payload.status in {"active", "dormant", "ruined"}
            and context.requested_by_character_id is not None
        ):
            raise RegistrationRejected("人物不能把新建巨构直接登记为既成或历史状态")
        exists = context.connection.execute(
            "SELECT 1 FROM world_structures WHERE world_id = ? AND name = ?",
            (context.world_id, payload.name.strip()),
        ).fetchone()
        if exists is not None:
            raise RegistrationRejected("当前世界已经存在同名巨构或遗迹")
        structure_id = str(uuid4())
        provenance_status = (
            "claimed" if context.requested_by_character_id is not None else "canonical"
        )
        metadata = {**payload.metadata, "provenance_claim": payload.provenance_claim}
        context.connection.execute(
            """
            INSERT INTO world_structures(
                id, world_id, registration_id, name, structure_type, origin_mode,
                status, location_id, longitude, latitude, provenance_status,
                metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                structure_id,
                context.world_id,
                context.registration_id,
                payload.name.strip(),
                payload.structure_type.strip(),
                payload.origin_mode,
                payload.status,
                payload.location_id,
                payload.longitude,
                payload.latitude,
                provenance_status,
                json.dumps(metadata, ensure_ascii=False),
                to_iso(context.now),
                to_iso(context.now),
            ),
        )
        effects = [
            Effect(
                effect_type="structure.registered",
                entity_type="structure",
                entity_id=structure_id,
                payload={"provenance_status": provenance_status},
            )
        ]
        if payload.origin_mode == "constructed" and payload.status in {
            "planned",
            "constructing",
        }:
            project_id = str(uuid4())
            context.connection.execute(
                """
                INSERT INTO construction_projects(
                    id, world_id, registration_id, project_type, target_name,
                    target_entity_id, status, location_id, longitude, latitude,
                    progress, requirements_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'structure', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    context.world_id,
                    context.registration_id,
                    payload.name.strip(),
                    structure_id,
                    payload.status,
                    payload.location_id,
                    payload.longitude,
                    payload.latitude,
                    0.0 if payload.status == "planned" else 0.1,
                    json.dumps(payload.requirements, ensure_ascii=False),
                    to_iso(context.now),
                    to_iso(context.now),
                ),
            )
            effects.append(
                Effect(
                    effect_type="construction.started",
                    entity_type="construction_project",
                    entity_id=project_id,
                )
            )
        return HandlerResult(
            status=RegistrationStatus.APPLIED,
            summary=(
                f"{payload.origin_mode}的“{payload.name}”已登记；"
                f"来源解释状态为{provenance_status}。"
            ),
            entity_type="structure",
            entity_id=structure_id,
            entity_name=payload.name.strip(),
            location_id=payload.location_id,
            effects=effects,
        )


class LoreHandler:
    element_type = ElementType.LORE

    def apply(
        self, context: RegistrationContext, payload: RegistrationPayload
    ) -> HandlerResult:
        if not isinstance(payload, LoreSpec):
            raise TypeError("世界观处理器收到错误的数据类型")
        if (
            payload.knowledge_level == "author_canon"
            and context.requested_by_character_id is not None
        ):
            raise RegistrationRejected("人物提交的内容不能直接升级为作者正典")
        if payload.subject_entity_id and not _entity_exists(
            context.connection, context.world_id, payload.subject_entity_id
        ):
            raise RegistrationRejected("世界观条目引用的主题实体不存在")
        entry_id = str(uuid4())
        tags = list(dict.fromkeys(tag.strip() for tag in payload.tags if tag.strip()))
        context.connection.execute(
            """
            INSERT INTO knowledge_entries(
                id, world_id, registration_id, knowledge_level, title, content,
                subject_entity_id, author_character_id, source_event_id,
                confidence, tags_json, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
            """,
            (
                entry_id,
                context.world_id,
                context.registration_id,
                payload.knowledge_level,
                payload.title.strip(),
                payload.content.strip(),
                payload.subject_entity_id,
                context.requested_by_character_id,
                context.source_event["id"],
                payload.confidence,
                json.dumps(tags, ensure_ascii=False),
                to_iso(context.now),
                to_iso(context.now),
            ),
        )
        return HandlerResult(
            status=RegistrationStatus.APPLIED,
            summary=f"“{payload.title}”已登记为{payload.knowledge_level}。",
            entity_type="knowledge_entry",
            entity_id=entry_id,
            entity_name=payload.title.strip(),
            location_id=context.source_event["location_id"],
            effects=[
                Effect(
                    effect_type="knowledge.registered",
                    entity_type="knowledge_entry",
                    entity_id=entry_id,
                    payload={"knowledge_level": payload.knowledge_level},
                )
            ],
        )
