"""通用食物资源的语义与播种。"""

from __future__ import annotations

from uuid import uuid4

import pytest

from world_engine.economy import CommoditySpec, EconomyService
from world_engine.repository import utc_now
from world_engine.seeder import create_iserra_world


@pytest.fixture
def world(database):
    return create_iserra_world(database)


def make_registration(connection, world_id: str, element_type: str = "commodity") -> str:
    """登记表要求 source_event_id 与 input_world_version 非空，因此先造一条事件。"""
    event_id = str(uuid4())
    connection.execute(
        """
        INSERT INTO world_events(
            id, world_id, tick_id, occurred_at, event_type, summary,
            importance, payload_json, created_at
        ) VALUES (?, ?, ?, ?, 'world.element_proposed', '为测试造的事件',
                  'routine', '{}', ?)
        """,
        (event_id, world_id, str(uuid4()), utc_now().isoformat(), utc_now().isoformat()),
    )
    version = int(
        connection.execute("SELECT version FROM worlds WHERE id=?", (world_id,)).fetchone()["version"]
    )
    registration_id = str(uuid4())
    connection.execute(
        """
        INSERT INTO element_registration_requests(
            id, world_id, element_type, source_event_id, idempotency_key,
            status, payload_json, input_world_version, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'applied', '{}', ?, ?, ?)
        """,
        (
            registration_id, world_id, element_type, event_id, registration_id,
            version, utc_now().isoformat(), utc_now().isoformat(),
        ),
    )
    return registration_id


def test_general_profile_registers_without_location(database, world) -> None:
    """resource_key 有值、resource_location_id 为 None ⇒ 通用资源，应当被接受。"""
    with database.write() as connection:
        registration_id = make_registration(connection, world)
        item_type_id = EconomyService.register_item(
            connection,
            world,
            registration_id,
            CommoditySpec(
                name="通用粮食",
                category="food",
                nutrition=20,
                price=3,
                resource_key="food",
                initial_resource=0,
                daily_growth=5,
                resource_capacity=200,
            ),
            utc_now(),
        )
        row = connection.execute(
            "SELECT resource_location_id, resource_key FROM world_item_profiles WHERE item_type_id=?",
            (item_type_id,),
        ).fetchone()
    assert row["resource_location_id"] is None
    assert row["resource_key"] == "food"


def test_location_without_key_is_rejected(database, world) -> None:
    """指定了资源地点却没给资源名 ⇒ 非法组合。"""
    with database.write() as connection:
        place_id = connection.execute(
            "SELECT id FROM locations WHERE world_id=? LIMIT 1", (world,)
        ).fetchone()["id"]
        registration_id = make_registration(connection, world)
        with pytest.raises(ValueError, match="资源名"):
            EconomyService.register_item(
                connection,
                world,
                registration_id,
                CommoditySpec(
                    name="缺资源名物品",
                    category="material",
                    resource_location_id=place_id,
                ),
                utc_now(),
            )
