from __future__ import annotations

import pytest

from world_engine.geo import great_circle_distance_km
from world_engine.npc_generation import (
    generate_npc_positions,
    generate_npc_profiles,
    generate_town_duty_profiles,
)


def test_generation_is_deterministic_and_unique() -> None:
    first = generate_npc_profiles(seed="unit-test-seed", count=16)
    second = generate_npc_profiles(seed="unit-test-seed", count=16)

    assert first == second
    assert len({profile.name for profile in first}) == 16
    assert all(profile.identity and profile.traits and profile.goals for profile in first)
    assert len({profile.idempotency_suffix for profile in first}) == 16


def test_generation_respects_existing_names() -> None:
    baseline = generate_npc_profiles(seed="exclude-seed", count=3)
    result = generate_npc_profiles(
        seed="exclude-seed", count=3, excluded_names={baseline[0].name}
    )

    assert baseline[0].name not in {profile.name for profile in result}
    assert len(result) == 3


def test_generation_rejects_an_unsafe_batch_size() -> None:
    with pytest.raises(ValueError, match="1 到 100"):
        generate_npc_profiles(seed="bad-count", count=0)


def test_town_duty_generation_has_ten_distinct_essential_roles() -> None:
    profiles = generate_town_duty_profiles(seed="town-duty-seed")

    assert len(profiles) == 10
    assert len({profile.name for profile in profiles}) == 10
    assert {profile.identity for profile in profiles} == {
        "城镇执政官",
        "卫队长",
        "医师",
        "粮仓管理员",
        "集市司簿",
        "铁匠",
        "石匠",
        "水务员",
        "巡夜人",
        "书院教师",
    }


def test_town_duty_generation_only_fills_missing_roles_and_names() -> None:
    baseline = generate_town_duty_profiles(seed="town-duty-exclude")
    profiles = generate_town_duty_profiles(
        seed="town-duty-exclude",
        excluded_names={baseline[0].name},
        existing_identities={"城镇执政官", "医师"},
    )

    assert len(profiles) == 8
    assert baseline[0].name not in {profile.name for profile in profiles}
    assert {profile.identity for profile in profiles}.isdisjoint({"城镇执政官", "医师"})


def test_generated_npc_positions_are_unique_and_outside_visible_overlap() -> None:
    profiles = generate_npc_profiles(seed="position-seed", count=16)
    positions = generate_npc_positions(
        seed="position-seed",
        profiles=profiles,
        center_longitude=80.79,
        center_latitude=25.44,
    )

    assert len(positions) == 16
    assert len(set(positions.values())) == 16
    assert all(
        great_circle_distance_km(80.79, 25.44, longitude, latitude) > 0.01
        for longitude, latitude in positions.values()
    )
