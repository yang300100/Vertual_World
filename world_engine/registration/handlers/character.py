"""人物类世界元素处理器：出生与独立抵达。"""

from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

from world_engine.demographics import stable_npc_demographics
from world_engine.geo import great_circle_distance_km
from world_engine.registration.models import (
    CharacterArrivalSpec,
    CharacterBirthSpec,
    Effect,
    ElementType,
    HandlerResult,
    RegistrationContext,
    RegistrationPayload,
    RegistrationRejected,
    RegistrationStatus,
    _require_location,
)
from world_engine.repository import from_iso, to_iso


class CharacterBirthHandler:
    element_type = ElementType.CHARACTER_BIRTH

    def apply(
        self, context: RegistrationContext, payload: RegistrationPayload
    ) -> HandlerResult:
        if not isinstance(payload, CharacterBirthSpec):
            raise TypeError("人物出生处理器收到错误的数据类型")
        parent_ids = list(dict.fromkeys(payload.parent_character_ids))
        if len(parent_ids) != len(payload.parent_character_ids):
            raise RegistrationRejected("父母或监护人不能重复")
        placeholders = ",".join("?" for _ in parent_ids)
        parents = context.connection.execute(
            f"SELECT * FROM characters WHERE world_id = ? AND id IN ({placeholders})",  # noqa: S608
            (context.world_id, *parent_ids),
        ).fetchall()
        if len(parents) != len(parent_ids):
            raise RegistrationRejected("父母或监护人不属于当前世界")
        if any(int(parent["health"]) <= 0 for parent in parents):
            raise RegistrationRejected("已经死亡的人物不能登记新的出生关系")
        parent_species = {str(parent["species"] or "human") for parent in parents}
        profiles = {
            row["id"]: row
            for row in context.connection.execute(
                "SELECT * FROM species_profiles WHERE id IN ({})".format(
                    ",".join("?" for _ in parent_species)
                ),
                tuple(parent_species),
            ).fetchall()
        }
        if len(profiles) != len(parent_species):
            raise RegistrationRejected("父母或监护人的种族配置不存在")
        for parent in parents:
            birth = parent["birth_world_time"]
            if not birth:
                continue  # 旧数据没有出生时间，按已成年角色兼容处理。
            profile = profiles[str(parent["species"] or "human")]
            age_days = (context.world_time - from_iso(birth)).days
            if age_days < int(profile["adult_age_world_years"]) * 360:
                raise RegistrationRejected("父母或监护人尚未达到该种族的成年年龄")
        if len(parent_species) > 1:
            if payload.species is None:
                raise RegistrationRejected("跨种族后代必须明确指定已配置的后代种族")
            for profile in profiles.values():
                compatible = set(json.loads(profile["compatible_species_json"]))
                if payload.species not in compatible:
                    raise RegistrationRejected("父母种族配置不允许登记该后代种族")
        child_species = payload.species or next(iter(parent_species))
        child_profile = context.connection.execute(
            "SELECT * FROM species_profiles WHERE id = ?", (child_species,)
        ).fetchone()
        if child_profile is None:
            raise RegistrationRejected("后代种族配置不存在")
        participants = {context.source_event["actor_id"], context.source_event["target_id"]}
        if not participants.intersection(parent_ids):
            raise RegistrationRejected("来源事件没有任何一位父母或监护人参与")
        if (
            context.requested_by_character_id is not None
            and context.requested_by_character_id not in parent_ids
        ):
            raise RegistrationRejected("人物只能为自己参与的家庭关系提交后代登记")

        if payload.birth_state == "planned":
            plan_id = str(uuid4())
            due_at = context.world_time + timedelta(
                days=int(child_profile["gestation_world_days"])
            )
            context.connection.execute(
                """
                INSERT INTO family_plans(
                    id, world_id, registration_id, planned_child_name,
                    parent_character_ids_json, relation_type, status,
                    due_at_world, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'planned', ?, ?, ?)
                """,
                (
                    plan_id,
                    context.world_id,
                    context.registration_id,
                    payload.name.strip(),
                    json.dumps(parent_ids, ensure_ascii=False),
                    payload.relation_type,
                    to_iso(due_at),
                    to_iso(context.now),
                    to_iso(context.now),
                ),
            )
            return HandlerResult(
                status=RegistrationStatus.APPROVED,
                summary=f"{payload.name}的家庭登记计划已获准，尚未创建人物。",
                effects=[
                    Effect(
                        effect_type="lineage.planned",
                        entity_type="character",
                        entity_id=plan_id,
                        payload={
                            "name": payload.name,
                            "parent_character_ids": parent_ids,
                            "due_at_world": to_iso(due_at),
                        },
                    )
                ],
            )

        if payload.parent_registration_id is None:
            raise RegistrationRejected("出生登记必须引用已经到期的家庭计划")
        plan = context.connection.execute(
            """
            SELECT * FROM family_plans
            WHERE world_id = ? AND registration_id = ?
            """,
            (context.world_id, payload.parent_registration_id),
        ).fetchone()
        if plan is None:
            raise RegistrationRejected("引用的家庭计划不存在")
        if plan["status"] in {"completed", "cancelled"}:
            raise RegistrationRejected("家庭计划已经结束")
        if from_iso(plan["due_at_world"]) > context.world_time:
            raise RegistrationRejected("家庭计划尚未到达可登记出生的世界时间")
        if set(json.loads(plan["parent_character_ids_json"])) != set(parent_ids):
            raise RegistrationRejected("出生登记的父母与家庭计划不一致")
        if plan["planned_child_name"] != payload.name.strip():
            raise RegistrationRejected("出生登记姓名与家庭计划不一致")

        existing = context.connection.execute(
            "SELECT 1 FROM characters WHERE world_id = ? AND name = ?",
            (context.world_id, payload.name.strip()),
        ).fetchone()
        if existing is not None:
            raise RegistrationRejected("当前世界已经存在同名人物")

        location_id = (
            payload.location_id
            or parents[0]["current_location_id"]
            or parents[0]["location_id"]
        )
        location = _require_location(context.connection, context.world_id, location_id)
        character_id = str(uuid4())
        context.connection.execute(
            """
            INSERT INTO characters(
                id, world_id, name, location_id, energy, satiety, money, health,
                traits_json, goals_json, identity, species, gender, birth_world_time,
                is_player, is_pov, is_core,
                longitude, latitude, current_location_id,
                activation_state, activation_policy, activation_reason,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 100, 100, 0, 100, ?, ?, ?, ?, ?, ?, 0, 0, 0,
                      ?, ?, ?, 'background', 'distance', 'registered_birth', ?, ?)
            """,
            (
                character_id,
                context.world_id,
                payload.name.strip(),
                location_id,
                json.dumps(payload.traits, ensure_ascii=False),
                json.dumps(payload.goals, ensure_ascii=False),
                payload.identity.strip(),
                child_species,
                payload.gender,
                to_iso(context.world_time),
                location["longitude"],
                location["latitude"],
                location_id,
                to_iso(context.now),
                to_iso(context.now),
            ),
        )
        context.connection.execute(
            """
            INSERT INTO character_state_accumulators(
                character_id, world_id, satiety_residual, energy_residual, updated_at
            ) VALUES (?, ?, 0, 0, ?)
            """,
            (character_id, context.world_id, to_iso(context.now)),
        )
        for parent_id in parent_ids:
            context.connection.execute(
                """
                INSERT INTO character_lineages(
                    world_id, parent_character_id, child_character_id, relation_type,
                    registration_id, established_at_world, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    context.world_id,
                    parent_id,
                    character_id,
                    payload.relation_type,
                    context.registration_id,
                    to_iso(context.world_time),
                    to_iso(context.now),
                ),
            )
        context.connection.execute(
            """
            UPDATE family_plans
            SET status = 'completed', completed_registration_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (context.registration_id, to_iso(context.now), plan["id"]),
        )
        return HandlerResult(
            status=RegistrationStatus.APPLIED,
            summary=f"{payload.name}已经作为新人物登记进入世界。",
            entity_type="character",
            entity_id=character_id,
            entity_name=payload.name.strip(),
            location_id=location_id,
            effects=[
                Effect(
                    effect_type="character.created",
                    entity_type="character",
                    entity_id=character_id,
                    payload={"parent_character_ids": parent_ids},
                )
            ],
        )


class CharacterArrivalHandler:
    """在来源事件约束下，为世界登记一名无血缘前置条件的 NPC。"""

    element_type = ElementType.CHARACTER_ARRIVAL

    def apply(
        self, context: RegistrationContext, payload: RegistrationPayload
    ) -> HandlerResult:
        if not isinstance(payload, CharacterArrivalSpec):
            raise TypeError("人物抵达处理器收到错误的数据类型")
        location = _require_location(
            context.connection, context.world_id, payload.location_id
        )
        species = context.connection.execute(
            "SELECT adult_age_world_years FROM species_profiles WHERE id = ?", (payload.species,)
        ).fetchone()
        if species is None:
            raise RegistrationRejected("NPC 的种族配置不存在")
        existing = context.connection.execute(
            "SELECT 1 FROM characters WHERE world_id = ? AND name = ?",
            (context.world_id, payload.name.strip()),
        ).fetchone()
        if existing is not None:
            raise RegistrationRejected("当前世界已经存在同名人物")
        if (payload.longitude is None) != (payload.latitude is None):
            raise RegistrationRejected("NPC 坐标必须同时提供经度和纬度")
        longitude = payload.longitude if payload.longitude is not None else location["longitude"]
        latitude = payload.latitude if payload.latitude is not None else location["latitude"]
        if great_circle_distance_km(
            longitude, latitude, location["longitude"], location["latitude"]
        ) > float(location["area_radius_km"]):
            raise RegistrationRejected("NPC 坐标超出所属地点范围")

        character_id = str(uuid4())
        demographic = stable_npc_demographics(
            identity_key=f"{context.world_id}|{payload.name.strip()}|{payload.species}",
            world_time=context.world_time,
            adult_age_world_years=int(species["adult_age_world_years"]),
        )
        gender = payload.gender or demographic.gender
        birth_world_time = payload.birth_world_time or demographic.birth_world_time
        context.connection.execute(
            """
            INSERT INTO characters(
                id, world_id, name, location_id, energy, satiety, money, health,
                traits_json, goals_json, identity, species, gender, birth_world_time,
                is_player, is_pov, is_core,
                longitude, latitude, current_location_id,
                activation_state, activation_policy, activation_reason,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 100, 100, 0, 100, ?, ?, ?, ?, ?, ?, 0, 0, 0,
                      ?, ?, ?, 'background', ?, 'registered_arrival', ?, ?)
            """,
            (
                character_id,
                context.world_id,
                payload.name.strip(),
                location["id"],
                json.dumps(payload.traits, ensure_ascii=False),
                json.dumps(payload.goals, ensure_ascii=False),
                payload.identity.strip(),
                payload.species,
                gender,
                to_iso(birth_world_time),
                longitude,
                latitude,
                location["id"],
                payload.activation_policy,
                to_iso(context.now),
                to_iso(context.now),
            ),
        )
        context.connection.execute(
            """
            INSERT INTO character_state_accumulators(
                character_id, world_id, satiety_residual, energy_residual, updated_at
            ) VALUES (?, ?, 0, 0, ?)
            """,
            (character_id, context.world_id, to_iso(context.now)),
        )
        return HandlerResult(
            status=RegistrationStatus.APPLIED,
            summary=f"{payload.name}作为独立 NPC 抵达了{location['name']}。",
            entity_type="character",
            entity_id=character_id,
            entity_name=payload.name.strip(),
            location_id=location["id"],
            effects=[
                Effect(
                    effect_type="character.arrived",
                    entity_type="character",
                    entity_id=character_id,
                    payload={
                        "identity": payload.identity,
                        "species": payload.species,
                        "activation_policy": payload.activation_policy,
                        "longitude": longitude,
                        "latitude": latitude,
                    },
                )
            ],
        )
