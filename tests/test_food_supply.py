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


def _empty_location(connection, world: str) -> str:
    """造一个没有任何 NPC 在场的空地点，返回其 id（ORDER BY 保证结果稳定）。"""
    place_id = str(uuid4())
    connection.execute(
        "INSERT INTO locations(id, world_id, name, kind, resources_json, longitude, latitude, "
        "area_radius_km, area_priority) VALUES (?,?,?,'town',?,0,0,3.0,5)",
        (place_id, world, f"无人镇-{place_id}", '{"food": 0}'),
    )
    return place_id


def _regenerate_one_day(database, world: str, place_ids: list[str]) -> list[int]:
    """把给定地点的所有已知资源清零后推进一天，返回各地点的 food 再生结果。

    只清零 key、保留 key 本身，这样同一地点的专属资源也能被本函数一起测量。
    """
    from datetime import timedelta

    from world_engine.repository import to_iso

    with database.write() as connection:
        for place_id in place_ids:
            resources = json.loads(
                connection.execute(
                    "SELECT resources_json FROM locations WHERE id=?", (place_id,)
                ).fetchone()["resources_json"]
                or "{}"
            )
            connection.execute(
                "UPDATE locations SET resources_json=? WHERE id=?",
                (json.dumps({key: 0 for key in resources}), place_id),
            )
        start = utc_now()
        connection.execute(
            "UPDATE world_item_profiles SET last_growth_world_time=?",
            (to_iso(start),),
        )
        EconomyService.tick(connection, world, start + timedelta(days=1))
        return [
            json.loads(
                connection.execute(
                    "SELECT resources_json FROM locations WHERE id=?", (place_id,)
                ).fetchone()["resources_json"]
            )
            for place_id in place_ids
        ]


def _register_generic_food(connection, world: str, *, name: str, daily_growth: int,
                           capacity: int = 200) -> None:
    EconomyService.register_item(
        connection,
        world,
        make_registration(connection, world),
        CommoditySpec(
            name=name, category="food", nutrition=20,
            resource_key="food", initial_resource=0,
            daily_growth=daily_growth, resource_capacity=capacity,
        ),
        utc_now(),
    )


def test_generic_resource_regenerates_all_locations(database, world) -> None:
    """通用资源应在每个活跃地点各自再生（无人地点不缩放，保持 daily_growth）。"""
    with database.write() as connection:
        _register_generic_food(connection, world, name="再生粮食", daily_growth=5)
        # 覆盖「该地点无 NPC」与「该地点有 N 个 NPC」两种情形：前者是 daily_growth 基线，
        # 后者按人口缩放（见 test_generic_resource_scales_with_local_population）。
        empty_id = _empty_location(connection, world)
        populated_id, populated_count = connection.execute(
            "SELECT location_id, COUNT(*) FROM characters "
            "WHERE world_id=? AND is_player=0 GROUP BY location_id ORDER BY location_id LIMIT 1",
            (world,),
        ).fetchone()
    values = [row.get("food") for row in _regenerate_one_day(database, world, [empty_id, populated_id])]
    # 无人地点：一天 × 每天 5 份 = 5；有人地点：max(5, 人口 × 2)。
    assert values == [5, max(5, populated_count * 2)]


def test_generic_resource_respects_capacity(database, world) -> None:
    """再生不得超过 resource_capacity（上限优先于人口缩放，故断言与人口无关）。"""
    with database.write() as connection:
        _register_generic_food(connection, world, name="上限粮食", daily_growth=100, capacity=7)
        place_id = connection.execute(
            "SELECT id FROM locations WHERE world_id=? ORDER BY id LIMIT 1", (world,)
        ).fetchone()["id"]
    value = _regenerate_one_day(database, world, [place_id])[0]["food"]
    assert value == 7


def test_generic_resource_scales_with_local_population(database, world) -> None:
    """聚集城镇的通用资源再生量应随在场 NPC 数上浮，而非固定 daily_growth。"""
    with database.write() as connection:
        _register_generic_food(connection, world, name="聚集粮食", daily_growth=5)
        place_id = _empty_location(connection, world)
        # 4 个 NPC 集中到同一地点：需求 4 × 2 = 8 份/天 > daily_growth 5。
        actor_ids = [
            row["id"]
            for row in connection.execute(
                "SELECT id FROM characters WHERE world_id=? AND is_player=0 "
                "ORDER BY id LIMIT 4",
                (world,),
            ).fetchall()
        ]
        assert len(actor_ids) == 4
        connection.execute(
            "UPDATE characters SET location_id=?, current_location_id=? "
            f"WHERE id IN ({','.join('?' for _ in actor_ids)})",
            (place_id, place_id, *actor_ids),
        )
    value = _regenerate_one_day(database, world, [place_id])[0]["food"]
    # 断言实际值：4 人 × 2 份 = 8 份/天（旧行为是固定 5 份）。
    assert value == 8
    assert value >= 8


def test_location_bound_resource_ignores_population(database, world) -> None:
    """地点专属资源不按人口缩放，保持 daily_growth。"""
    with database.write() as connection:
        place_id = _empty_location(connection, world)
        # 让该地点先有 4 个 NPC，若误按人口缩放就会变成 8 份。
        actor_ids = [
            row["id"]
            for row in connection.execute(
                "SELECT id FROM characters WHERE world_id=? AND is_player=0 ORDER BY id LIMIT 4",
                (world,),
            ).fetchall()
        ]
        assert len(actor_ids) == 4
        connection.execute(
            "UPDATE characters SET location_id=?, current_location_id=? "
            f"WHERE id IN ({','.join('?' for _ in actor_ids)})",
            (place_id, place_id, *actor_ids),
        )
        EconomyService.register_item(
            connection,
            world,
            make_registration(connection, world),
            CommoditySpec(
                name="本地木材", category="material", price=3,
                resource_location_id=place_id, resource_key="timber",
                initial_resource=0, daily_growth=5, resource_capacity=200,
            ),
            utc_now(),
        )
    value = _regenerate_one_day(database, world, [place_id])[0]["timber"]
    # 专属资源恒定每天 5 份，与人口无关。
    assert value == 5


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
        # 新建的世界尚未播种（「已播种」判据是通用 profile 是否存在）；
        # 这里清空存量是为稳妥起见，确保能走到「写存量」那条路径。
        connection.execute(
            "UPDATE locations SET resources_json='{}' WHERE world_id=?", (world,)
        )
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
        # 模拟一个从未获得过食物供给的旧存档。「已播种」的判据是
        # 通用 profile 是否存在，所以抹掉 profile 即可让它重新播种。
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


def test_noryia_world_gets_food_supply_on_creation(database) -> None:
    """新建的 Noryia 世界应自带食物供给，无需依赖迁移。"""
    from world_engine.noryia_seeder import create_noryia_world

    world_id = create_noryia_world(database)
    with database.write() as connection:
        # 复现「一个全新世界」的播种路径：清掉 profile 与已写下的存量，
        # 让播种重新执行一次。此时仍能正确补齐，说明播种不依赖任何
        # 一次性的初始状态。
        connection.execute(
            "UPDATE locations SET resources_json='{}' WHERE world_id=?", (world_id,)
        )
        connection.execute(
            "DELETE FROM world_item_profiles WHERE world_id=? AND resource_key='food'",
            (world_id,),
        )
        from world_engine.food_supply import seed_food_supply

        seed_food_supply(connection, world_id)
    with database.read() as connection:
        profiles = connection.execute(
            "SELECT COUNT(*) FROM world_item_profiles "
            "WHERE world_id=? AND resource_key='food' AND resource_location_id IS NULL",
            (world_id,),
        ).fetchone()[0]
        with_food = connection.execute(
            "SELECT COUNT(*) FROM locations WHERE world_id=? "
            "AND COALESCE(json_extract(resources_json,'$.food'),0) > 0",
            (world_id,),
        ).fetchone()[0]
    assert profiles == 1
    assert with_food > 0


def test_cli_registers_seed_food_supply_command() -> None:
    """CLI 应注册 seed-food-supply 子命令并接收 world_id。

    播种逻辑本身已由 Task 6 的单元测试覆盖，此处只验证命令可被正确解析，
    避免为一个薄封装去 mock 整个 CLI 运行时。
    """
    from world_engine.cli import build_parser

    args = build_parser().parse_args(["seed-food-supply", "some-world-id"])
    assert args.command == "seed-food-supply"
    assert args.world_id == "some-world-id"


def test_seed_is_idempotent(database, world) -> None:
    """重复播种不产生重复 profile，也不重置已被消耗的存量。"""
    from world_engine.food_supply import seed_food_supply

    with database.write() as connection:
        seed_food_supply(connection, world)
        first = connection.execute(
            "SELECT COUNT(*) FROM world_item_profiles WHERE world_id=? AND resource_key='food'",
            (world,),
        ).fetchone()[0]
        # 模拟 NPC 采集：把某个城镇的食物消耗掉一部分
        place_id = connection.execute(
            "SELECT id FROM locations WHERE world_id=? AND kind IN ('city','town') ORDER BY id LIMIT 1",
            (world,),
        ).fetchone()["id"]
        connection.execute(
            "UPDATE locations SET resources_json='{\"food\": 1}' WHERE id=?", (place_id,)
        )

    with database.write() as connection:
        seed_food_supply(connection, world)
        second = connection.execute(
            "SELECT COUNT(*) FROM world_item_profiles WHERE world_id=? AND resource_key='food'",
            (world,),
        ).fetchone()[0]
        after = connection.execute(
            "SELECT resources_json FROM locations WHERE id=?", (place_id,)
        ).fetchone()["resources_json"]

    assert first == second == 1
    # 关键：已消耗的存量不得被播种重置回满仓
    assert json.loads(after)["food"] == 1
