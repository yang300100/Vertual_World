"""哥哥，契约可以共享文本，但不能漏掉约束、描述或默认值。"""

import importlib
import json
from copy import deepcopy

from world_engine.materials import encoded
from world_engine.science_runner import SYSTEM
from world_engine.science_types import ScienceProgram


def prompt_schema():
    start = SYSTEM.index('{"$defs":')
    return json.JSONDecoder().raw_decode(SYSTEM[start:])[0]


def expanded_contract(schema):
    """独立展开本项目的本地引用；只忽略JSON Schema展示元数据。"""
    definitions = schema.get("$defs", {})

    def expand(node):
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            assert set(node) == {"$ref"}
            assert node["$ref"].startswith("#/$defs/")
            return expand(definitions[node["$ref"][8:]])
        result = {}
        for key, value in node.items():
            if key in {"$defs", "title", "discriminator"}:
                continue
            if key in {"properties", "patternProperties", "dependentSchemas"}:
                result[key] = {name: expand(child) for name, child in value.items()}
            elif key in {"anyOf", "oneOf", "allOf", "prefixItems"}:
                result[key] = [expand(child) for child in value]
            elif key in {
                "items",
                "additionalProperties",
                "propertyNames",
                "not",
                "if",
                "then",
                "else",
                "contains",
                "unevaluatedProperties",
                "unevaluatedItems",
                "contentSchema",
            }:
                result[key] = expand(value)
            else:
                result[key] = value
        return result

    return expand(schema)


def test_full_known_context_fits_the_remaining_budget_without_truncation():
    # 第三轮保存的完整资料为18509字节；消息包装和输出上限也必须完整保留。
    reserve = 18509 + len(SYSTEM.encode("utf-8")) + 1024 + 8192
    assert reserve <= 42906


def test_every_science_constraint_and_description_survives_presentation():
    assert expanded_contract(prompt_schema()) == expanded_contract(
        ScienceProgram.model_json_schema()
    )


def compact(schema):
    module = importlib.import_module("world_engine.science_prompt_schema")
    return module.compact_prompt_schema(schema)


def test_property_names_and_default_data_are_not_treated_as_schema_metadata():
    schema = {
        "type": "object",
        "title": "展示标题",
        "properties": {
            "title": {"type": "string", "default": "保留字段"},
            "discriminator": {
                "type": "object",
                "default": {"$ref": "数据引用", "title": "数据标题"},
            },
            "first": {"type": "string", "minLength": 1, "maxLength": 100},
            "second": {"type": "string", "minLength": 1, "maxLength": 100},
        },
        "required": ["title", "discriminator"],
        "additionalProperties": False,
    }
    before = deepcopy(schema)
    result = compact(schema)
    assert schema == before
    assert expanded_contract(result) == {
        "type": "object",
        "properties": {
            "title": {"type": "string", "default": "保留字段"},
            "discriminator": {
                "type": "object",
                "default": {"$ref": "数据引用", "title": "数据标题"},
            },
            "first": {"type": "string", "minLength": 1, "maxLength": 100},
            "second": {"type": "string", "minLength": 1, "maxLength": 100},
        },
        "required": ["title", "discriminator"],
        "additionalProperties": False,
    }


def test_all_presented_references_resolve_and_result_is_deterministic():
    original = ScienceProgram.model_json_schema()
    first = compact(original)
    second = compact(original)
    assert encoded(first) == encoded(second)
    assert first == prompt_schema()
    assert expanded_contract(first) == expanded_contract(original)
