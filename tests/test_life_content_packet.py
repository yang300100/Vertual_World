# ruff: noqa: F811
"""内容模型只能交付结构化草案，目录导出和校验不写入游戏存档。"""

import json

import pytest
from test_lived_world import town  # noqa: F401

from scripts.prepare_life_content import export_packet, validate_draft


def draft(env, payload):
    return {
        "format_version": 1,
        "world_id": env.wid,
        "entries": [{"key": "entry-1", "source_note": "建议，待作者核对", "payload": payload}],
    }


def packet(env, tmp_path):
    return export_packet(env.settings.database_path, env.wid, [env.loc["id"]], tmp_path / "packet")


def test_export_contains_real_schemas_and_no_private_character_fields(town, tmp_path):
    e = town
    with e.db.read() as c:
        before = [tuple(row) for row in c.execute("SELECT w.id,w.version,w.current_time FROM worlds w")]
        count = c.execute("SELECT count(*) FROM world_events").fetchone()[0]
    catalogue = packet(e, tmp_path)
    assert catalogue["world"]["id"] == e.wid
    assert all(
        set(row) <= {"id", "name", "identity", "skills", "location_id"}
        for row in catalogue["characters"]
    )
    assert (tmp_path / "packet/填写说明.md").is_file()
    schema = json.loads(
        (tmp_path / "packet/schema-activity_recipe.json").read_text(encoding="utf-8")
    )
    assert schema["additionalProperties"] is False
    assert "check_difficulty" in schema["properties"]
    result = draft(e, {"element_type": "commodity", "name": "干粮建议", "category": "food"})
    assert validate_draft(result, catalogue) == 1
    with e.db.read() as c:
        assert before == [
            tuple(row) for row in c.execute("SELECT w.id,w.version,w.current_time FROM worlds w")
        ]
        assert count == c.execute("SELECT count(*) FROM world_events").fetchone()[0]


@pytest.mark.parametrize(
    "payload",
    [
        {
            "element_type": "commodity",
            "name": "干粮",
            "category": "food",
            "resource_location_id": "fake",
            "resource_key": "grain",
        },
        {
            "element_type": "commodity",
            "name": "干粮",
            "category": "food",
            "resource_owner_id": "fake",
        },
        {"element_type": "commodity", "name": "干粮", "category": "food", "initial_inventory": 20},
        {"element_type": "interior_room", "name": "客房", "location_id": "fake"},
        {
            "element_type": "activity_recipe",
            "name": "食物整理",
            "kind": "craft",
            "location_id": "fake",
            "duration_minutes": 30,
            "ingredients": [{"item_type_id": "fake", "quantity": 1}],
            "output_item_type_id": "fake",
        },
    ],
)
def test_draft_rejects_fabricated_references_and_unsupported_fields(town, tmp_path, payload):
    with pytest.raises(ValueError):
        validate_draft(draft(town, payload), packet(town, tmp_path))


def test_draft_rejects_reinitializing_wages_and_resource_sources(town, tmp_path):
    e = town
    catalogue = packet(e, tmp_path)
    with pytest.raises(ValueError, match="工资账户"):
        validate_draft(
            draft(
                e,
                {
                    "element_type": "workplace_budget",
                    "location_id": e.loc["id"],
                    "initial_funds": 5000,
                },
            ),
            catalogue,
        )
    with pytest.raises(ValueError, match="资源来源"):
        validate_draft(
            draft(
                e,
                {
                    "element_type": "commodity",
                    "name": "其他面包",
                    "category": "food",
                    "resource_location_id": e.loc["id"],
                    "resource_key": "bakery_supply",
                },
            ),
            catalogue,
        )


def test_draft_accepts_generic_resource_without_location(town, tmp_path):
    """通用资源（只填 resource_key、地点为 null）应通过校验，与引擎语义一致。"""
    assert (
        validate_draft(
            draft(
                town,
                {
                    "element_type": "commodity",
                    "name": "野地浆果",
                    "category": "food",
                    "nutrition": 20,
                    "price": 3,
                    "resource_key": "forage",
                    "initial_resource": 0,
                    "daily_growth": 5,
                    "resource_capacity": 200,
                },
            ),
            packet(town, tmp_path),
        )
        == 1
    )


def test_draft_rejects_location_without_resource_key(town, tmp_path):
    """有地点却没给资源名仍应被拦截——这是引擎唯一仍拒绝的组合。"""
    with pytest.raises(ValueError, match="资源名"):
        validate_draft(
            draft(
                town,
                {
                    "element_type": "commodity",
                    "name": "缺资源名物品",
                    "category": "material",
                    "resource_location_id": town.loc["id"],
                },
            ),
            packet(town, tmp_path),
        )


def test_export_requires_explicit_existing_location_and_never_creates_database(town, tmp_path):
    e = town
    with pytest.raises(ValueError):
        export_packet(e.settings.database_path, e.wid, [], tmp_path / "packet")
    with pytest.raises(ValueError):
        export_packet(e.settings.database_path, e.wid, ["unknown"], tmp_path / "packet")
    assert not (tmp_path / "packet").exists()
