"""哥哥，公开契约要提前指出方法引用错误，同时保留直接实物指令。"""

import re

import pytest
from pydantic import ValidationError

from world_engine.science_types import MethodSpec, ScienceProgram


def method(step):
    return {
        "inputs": {"sample": "material", "fuel": "energy", "tool": "instrument"},
        "steps": [
            {"op": "store", "input": "sample", "result_key": "paste"},
            step,
        ],
    }


@pytest.mark.parametrize(
    ("step", "field"),
    [
        ({"op": "dry", "input": "$paste"}, "input"),
        ({"op": "mix", "input": "sample", "auxiliary_inputs": ["$paste"]}, "auxiliary_inputs"),
        ({"op": "heat", "input": "sample", "apparatus": "$paste"}, "apparatus"),
        ({"op": "heat", "input": "sample", "energy": "$paste"}, "energy"),
        ({"op": "assay", "input": "$paste", "metric": "strength", "instrument": "tool"}, "input"),
        (
            {"op": "assay", "input": "sample", "metric": "strength", "instrument": "$paste"},
            "instrument",
        ),
        (
            {
                "op": "use_method",
                "technology_id": "real-tech",
                "version": 1,
                "bindings": {"sample": "$paste"},
            },
            "bindings",
        ),
    ],
)
def test_missing_product_index_is_exposed_at_the_exact_method_field(step, field):
    with pytest.raises(ValidationError) as caught:
        MethodSpec.model_validate(method(step))
    errors = caught.value.errors()
    assert any(e["loc"][:2] == ("steps", 1) and field in e["loc"] for e in errors)

    schema = MethodSpec.model_json_schema()
    variant = next(
        value
        for value in schema["$defs"].values()
        if value.get("properties", {}).get("op", {}).get("const") == step["op"]
        or step["op"] in value.get("properties", {}).get("op", {}).get("enum", [])
    )
    reference = variant["properties"][field]
    if field == "auxiliary_inputs":
        reference = reference["items"]
    elif field == "bindings":
        reference = next(iter(reference["patternProperties"].values()))
    if "anyOf" in reference:
        reference = next(v for v in reference["anyOf"] if v.get("type") != "null")
    if "$ref" in reference:
        reference = schema["$defs"][reference["$ref"].rsplit("/", 1)[-1]]
    assert re.fullmatch(reference["pattern"], "$paste") is None
    assert re.fullmatch(reference["pattern"], "$paste:0")
    assert re.fullmatch(reference["pattern"], "sample")


def test_valid_method_reference_is_preserved_without_product_selection():
    parsed = MethodSpec.model_validate(method({"op": "dry", "input": "$paste:0", "energy": "fuel"}))
    assert parsed.steps[1].input == "$paste:0"
    assert parsed.steps[1].energy == "fuel"


@pytest.mark.parametrize("reference", ["$future:0", "$paste:16", "missing"])
def test_reference_shape_does_not_override_source_or_index_checks(reference):
    with pytest.raises(ValidationError):
        MethodSpec.model_validate(method({"op": "store", "input": reference}))


def test_direct_commands_keep_real_ids_and_outer_result_references():
    commands = [
        {"op": "craft", "operation": "dry", "input": "batch-uuid", "energy": "energy-uuid"},
        {"op": "assay", "input": "$outside", "metric": "strength", "instrument": "instrument-uuid"},
        {
            "op": "use_method",
            "technology_id": "tech-uuid",
            "version": 1,
            "bindings": {"sample": "batch-uuid"},
        },
    ]
    parsed = ScienceProgram.model_validate({"reason": "核对现场资源", "commands": commands})
    assert parsed.commands[0].input == "batch-uuid"
    assert parsed.commands[1].input == "$outside"
    assert parsed.commands[2].bindings == {"sample": "batch-uuid"}
