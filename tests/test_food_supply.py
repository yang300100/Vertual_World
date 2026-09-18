"""通用食物资源的语义与播种。"""

from __future__ import annotations

import json
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


def test_harvest_works_at_any_town(database, world) -> None:
    """通用 profile 应让任意有 food 存量的地点都能采集。"""
    with database.write() as connection:
        registration_id = make_registration(connection, world)
        EconomyService.register_item(
            connection,
            world,
            registration_id,
            CommoditySpec(
                name="通用粮食", category="food", nutrition=20,
                resource_key="food", initial_resource=0,
                daily_growth=5, resource_capacity=200,
            ),
            utc_now(),
        )
        # 固定取两个不同地点，并记下坐标（harvest 要求角色与该地点相距够近）
        places = [
            dict(row)
            for row in connection.execute(
                "SELECT id, longitude, latitude FROM locations WHERE world_id=? ORDER BY id LIMIT 2",
                (world,),
            ).fetchall()
        ]
        assert len(places) == 2
        for place in places:
            connection.execute(
                "UPDATE locations SET resources_json=? WHERE id=?",
                ('{"food": 20}', place["id"]),
            )
        actor_id = connection.execute(
            "SELECT id FROM characters WHERE world_id=? LIMIT 1", (world,)
        ).fetchone()["id"]

    for place in places:
        with database.write() as connection:
            # 把角色放进该地点：坐标随之对齐，current_room_id 置空表示站在室外地面
            connection.execute(
                "UPDATE characters SET location_id=?, current_location_id=?, current_room_id=NULL, "
                "energy=100, longitude=?, latitude=? WHERE id=?",
                (place["id"], place["id"], place["longitude"], place["latitude"], actor_id),
            )
            actor = connection.execute(
                "SELECT * FROM characters WHERE id=?", (actor_id,)
            ).fetchone()
            assert EconomyService.harvest(connection, actor, utc_now(), food_only=True) is True


def test_generic_resource_regenerates_all_locations(database, world) -> None:
    """通用资源应在每个活跃地点各自再生，且不超过 resource_capacity。"""
    from datetime import timedelta

    from world_engine.repository import to_iso

    with database.write() as connection:
        registration_id = make_registration(connection, world)
        EconomyService.register_item(
            connection,
            world,
            registration_id,
            CommoditySpec(
                name="再生粮食", category="food", nutrition=20,
                resource_key="food", initial_resource=0,
                daily_growth=5, resource_capacity=12,
            ),
            utc_now(),
        )
        place_ids = [
            row["id"]
            for row in connection.execute(
                "SELECT id FROM locations WHERE world_id=? ORDER BY id LIMIT 2", (world,)
            ).fetchall()
        ]
        assert len(place_ids) == 2
        for place_id in place_ids:
            connection.execute(
                "UPDATE locations SET resources_json=? WHERE id=?", ('{"food": 0}', place_id)
            )
        start = utc_now()
        connection.execute(
            "UPDATE world_item_profiles SET last_growth_world_time=? WHERE resource_key='food'",
            (to_iso(start),),
        )
        EconomyService.tick(connection, world, start + timedelta(days=1))
        values = [
            json.loads(
                connection.execute(
                    "SELECT resources_json FROM locations WHERE id=?", (place_id,)
                ).fetchone()["resources_json"]
            ).get("food")
            for place_id in place_ids
        ]
    # 一天 × 每天 5 份 = 5，未触及上限 12
    assert values == [5, 5]


def test_generic_resource_respects_capacity(database, world) -> None:
    """再生不得超过 resource_capacity。"""
    from datetime import timedelta

    from world_engine.repository import to_iso

    with database.write() as connection:
        registration_id = make_registration(connection, world)
        EconomyService.register_item(
            connection,
            world,
            registration_id,
            CommoditySpec(
                name="上限粮食", category="food", nutrition=20,
                resource_key="food", initial_resource=0,
                daily_growth=100, resource_capacity=7,
            ),
            utc_now(),
        )
        place_id = connection.execute(
            "SELECT id FROM locations WHERE world_id=? ORDER BY id LIMIT 1", (world,)
        ).fetchone()["id"]
        connection.execute(
            "UPDATE locations SET resources_json=? WHERE id=?", ('{"food": 0}', place_id)
        )
        start = utc_now()
        connection.execute(
            "UPDATE world_item_profiles SET last_growth_world_time=? WHERE resource_key='food'",
            (to_iso(start),),
        )
        EconomyService.tick(connection, world, start + timedelta(days=1))
        value = json.loads(
            connection.execute(
                "SELECT resources_json FROM locations WHERE id=?", (place_id,)
            ).fetchone()["resources_json"]
        )["food"]
    assert value == 7



def test_eat_rejects_free_pickup_when_generic_profile_exists(database, world) -> None:
    """存在通用 food profile 时，_eat 必须要求走采集，而不是就地免费拿。"""
    from world_engine.actions import ActionRuleError, ActionService

    with database.write() as connection:
        registration_id = make_registration(connection, world)
        EconomyService.register_item(
            connection,
            world,
            registration_id,
            CommoditySpec(
                name="通用粮食", category="food", nutrition=20,
                resource_key="food", initial_resource=0,
                daily_growth=5, resource_capacity=200,
            ),
            utc_now(),
        )
        row = connection.execute(
            "SELECT id, longitude, latitude FROM locations WHERE world_id=? ORDER BY id LIMIT 1",
            (world,),
        ).fetchone()
        place_id = row["id"]
        connection.execute(
            "UPDATE locations SET resources_json=? WHERE id=?", ('{"food": 30}', place_id)
        )
        actor_id = connection.execute(
            "SELECT id FROM characters WHERE world_id=? LIMIT 1", (world,)
        ).fetchone()["id"]
        # 坐标必须与地点对齐，否则 _assert_near_location 会先抛「不在可交互范围内」
        connection.execute(
            "UPDATE characters SET location_id=?, current_location_id=?, current_room_id=NULL, "
            "money=100, longitude=?, latitude=? WHERE id=?",
            (place_id, place_id, row["longitude"], row["latitude"], actor_id),
        )
        actor = connection.execute("SELECT * FROM characters WHERE id=?", (actor_id,)).fetchone()
        with pytest.raises(ActionRuleError, match="登记库存"):
            ActionService()._eat(connection, actor, utc_now())


def test_food_places_include_generic_resource_locations(database, world) -> None:
    """存在通用 food profile 时，所有有存量的地点都应被视为可用餐地点。"""
    from world_engine.daily_life import DailyLifeService

    with database.write() as connection:
        registration_id = make_registration(connection, world)
        EconomyService.register_item(
            connection,
            world,
            registration_id,
            CommoditySpec(
                name="通用粮食", category="food", nutrition=20,
                resource_key="food", initial_resource=0,
                daily_growth=5, resource_capacity=200,
            ),
            utc_now(),
        )
        place_id = connection.execute(
            "SELECT id FROM locations WHERE world_id=? ORDER BY id LIMIT 1", (world,)
        ).fetchone()["id"]
        connection.execute(
            "UPDATE locations SET resources_json=? WHERE id=?", ('{"food": 30}', place_id)
        )
        actor_id = connection.execute(
            "SELECT id FROM characters WHERE world_id=? LIMIT 1", (world,)
        ).fetchone()["id"]
        places = DailyLifeService.food_places(
            connection, world, [place_id], actor_id=actor_id
        )
    assert places == [place_id]


def test_seed_writes_food_stock_to_every_town(database, world) -> None:
    """播种后每个 city/town 都应有 food 存量。"""
    from world_engine.food_supply import seed_food_supply

    with database.write() as connection:
        stats = seed_food_supply(connection, world)
        without = connection.execute(
            "SELECT COUNT(*) FROM locations WHERE world_id=? AND kind IN ('city','town') "
            "AND COALESCE(json_extract(resources_json,'$.food'),0) <= 0",
            (world,),
        ).fetchone()[0]
    assert stats.locations_seeded > 0
    assert without == 0


def test_initialize_backfills_food_supply(database, world) -> None:
    """已存在的世界在 initialize 后应自动获得食物供给。"""
    with database.write() as connection:
        connection.execute("UPDATE locations SET resources_json='{}' WHERE world_id=?", (world,))
        connection.execute(
            "DELETE FROM world_item_profiles WHERE world_id=? AND resource_key='food'", (world,)
        )
    database.initialize()
    with database.read() as connection:
        profiles = connection.execute(
            "SELECT COUNT(*) FROM world_item_profiles "
            "WHERE world_id=? AND resource_key='food' AND resource_location_id IS NULL",
            (world,),
        ).fetchone()[0]
        stock = connection.execute(
            "SELECT COUNT(*) FROM locations WHERE world_id=? "
            "AND COALESCE(json_extract(resources_json,'$.food'),0) > 0",
            (world,),
        ).fetchone()[0]
    assert profiles == 1
    assert stock > 0


def test_seed_is_idempotent(database, world) -> None:
    """重复播种不产生重复 profile，也不改变已有存量。"""
    from world_engine.food_supply import seed_food_supply

    with database.write() as connection:
        seed_food_supply(connection, world)
        first = connection.execute(
            "SELECT COUNT(*) FROM world_item_profiles WHERE world_id=? AND resource_key='food'",
            (world,),
        ).fetchone()[0]
    with database.write() as connection:
        seed_food_supply(connection, world)
        second = connection.execute(
            "SELECT COUNT(*) FROM world_item_profiles WHERE world_id=? AND resource_key='food'",
            (world,),
        ).fetchone()[0]
    assert first == second == 1
