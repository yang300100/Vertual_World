"""哥哥，科技可以未知，物质和供能的账不能凭空变多。"""

import importlib
import importlib.util
from dataclasses import asdict

import pytest

from world_engine.material_physics import MaterialState, combine, fraction


def physics():
    assert importlib.util.find_spec("world_engine.craft_physics"), "尚无通用工艺自然响应"
    return importlib.import_module("world_engine.craft_physics")


def test_hot_processing_requires_actual_energy():
    craft = physics()
    with pytest.raises(ValueError, match="供能"):
        craft.process_craft(
            MaterialState(carrier="clay", mass=2),
            "heat",
            {"temperature": 900},
            (),
            hours=2,
            supplied_energy=0,
            profile={},
        )


def test_processing_cannot_create_water_or_pigment():
    craft = physics()
    state = MaterialState(carrier="fiber", mass=2, water=0.2, pigment_free=0.1, pigment_fixed=0.03)
    outputs = craft.process_craft(
        state, "dry", {"ventilation": 1}, (), hours=2, supplied_energy=1000, profile={}
    )
    assert sum(x.mass for x in outputs) == pytest.approx(2)
    assert sum(x.water for x in outputs) == pytest.approx(0.2)
    assert sum(x.pigment_free + x.pigment_fixed for x in outputs) == pytest.approx(0.13)


def test_fraction_and_combine_preserve_new_extensive_properties():
    physics()
    state = MaterialState(
        carrier="fiber", mass=10, pigment_free=2, pigment_fixed=1, porosity=0.4, structural_bond=0.2
    )
    part, remainder = fraction(state, 0.3), fraction(state, 0.7)
    assert part.pigment_free == pytest.approx(0.6)
    assert part.pigment_fixed == pytest.approx(0.3)
    restored = combine((part, remainder))
    assert restored.pigment_free == pytest.approx(2)
    assert restored.pigment_fixed == pytest.approx(1)
    assert restored.porosity == pytest.approx(0.4)
    assert restored.structural_bond == pytest.approx(0.2)


def test_structure_is_not_added_when_materials_are_combined():
    physics()
    a = MaterialState(carrier="clay", mass=1, structural_bond=0.2)
    b = MaterialState(carrier="clay", mass=3, structural_bond=0.8)
    assert combine((a, b)).structural_bond == pytest.approx(0.65)


def test_heat_response_repeats_for_same_physical_conditions():
    craft = physics()
    state = MaterialState(carrier="clay", mass=1, porosity=0.4)
    first = craft.process_craft(
        state, "heat", {"temperature": 900}, (), hours=2, supplied_energy=5000, profile={}
    )
    second = craft.process_craft(
        state, "heat", {"temperature": 900}, (), hours=2, supplied_energy=5000, profile={}
    )
    assert [asdict(x) for x in first] == [asdict(x) for x in second]
    assert first[0].structural_bond > state.structural_bond
    assert sum(x.mass for x in first) == pytest.approx(1)


def test_cooling_changes_temperature_and_preserves_sample():
    craft = physics()
    state = MaterialState(carrier="metal", mass=1, temperature=400, strain=0.2)
    outputs = craft.process_craft(
        state, "cool", {"temperature": 20}, (), hours=2, supplied_energy=0, profile={}
    )
    assert 20 <= outputs[0].temperature < 400
    assert sum(x.mass for x in outputs) == pytest.approx(1)


def test_salt_and_pigment_washing_preserves_discharge():
    craft = physics()
    state = MaterialState(carrier="fiber", mass=2, mobile=0.1, pigment_free=0.2, pigment_fixed=0.1)
    water = MaterialState(carrier="water", mass=0, water=2)
    outputs = craft.process_craft(
        state, "wash", {}, (water,), hours=2, supplied_energy=0, profile={}
    )
    assert sum(x.water for x in outputs) == pytest.approx(2)
    assert sum(x.mobile + x.adsorbed + x.bound for x in outputs) == pytest.approx(0.1)
    assert sum(x.pigment_free + x.pigment_fixed for x in outputs) == pytest.approx(0.3)
    assert outputs[0].pigment_free < 0.2


def test_no_auxiliary_no_water_appears():
    craft = physics()
    with pytest.raises(ValueError, match="水|辅助"):
        craft.process_craft(
            MaterialState(carrier="fiber", mass=1),
            "wash",
            {"water": 2},
            (),
            hours=1,
            supplied_energy=0,
            profile={},
        )


def test_assay_reports_resolution_without_hidden_coefficients():
    craft = physics()
    sample = MaterialState(carrier="clay", mass=1, structural_bond=0.5, porosity=0.3)
    result = craft.assay_craft(
        sample,
        "strength",
        {"precision": 0.5, "detection_limit": 0, "unit": "relative"},
        profile={"strength_scale": 100},
    )
    assert result["precision"] == 0.5
    assert result["unit"] == "relative"
    assert result["value"] % 0.5 == 0
    assert "strength_scale" not in result


@pytest.mark.parametrize("precision", [0, -1, True, float("nan")])
def test_invalid_instrument_cannot_produce_evidence(precision):
    craft = physics()
    with pytest.raises(ValueError):
        craft.assay_craft(
            MaterialState(mass=1),
            "strength",
            {"precision": precision, "detection_limit": 0},
            profile={},
        )


def test_unknown_operation_remains_unsupported():
    craft = physics()
    with pytest.raises(ValueError, match="支持|操作"):
        craft.process_craft(
            MaterialState(mass=1),
            "new_magic_effect",
            {},
            (),
            hours=1,
            supplied_energy=10,
            profile={},
        )


@pytest.mark.parametrize(
    "field,value",
    [("porosity", 1.5), ("structural_bond", -0.1), ("pigment_fixed", -1), ("strain", float("inf"))],
)
def test_impossible_craft_state_is_rejected(field, value):
    physics()
    with pytest.raises(ValueError):
        MaterialState(**{field: value})


def test_old_material_json_keeps_working_with_no_craft_fields():
    physics()
    state = MaterialState(**{"carrier": "grain", "mass": 1, "water": 0.1})
    assert state.mass == 1 and state.water == 0.1
    assert state.pigment_free == 0 and state.pigment_fixed == 0


def test_zero_time_cannot_heat_or_transform_structure():
    craft = physics()
    original = MaterialState(carrier="clay", mass=1)
    outputs = craft.process_craft(
        original, "heat", {"temperature": 900}, (), hours=0, supplied_energy=5000, profile={}
    )
    assert outputs[0].temperature == 20
    assert outputs[0].structural_bond == 0


def test_finite_heating_power_limits_core_temperature():
    craft = physics()
    original = MaterialState(carrier="clay", mass=1)
    weak = craft.process_craft(
        original,
        "heat",
        {"temperature": 900, "power": 0.01},
        (),
        hours=1,
        supplied_energy=5000,
        profile={},
    )[0]
    strong = craft.process_craft(
        original,
        "heat",
        {"temperature": 900, "power": 1},
        (),
        hours=1,
        supplied_energy=5000,
        profile={},
    )[0]
    assert 20 < weak.temperature < strong.temperature < 900


def test_heating_and_cooling_cannot_return_more_energy_than_supplied():
    craft = physics()
    original = MaterialState(carrier="clay", mass=1)
    required = craft.energy_required(original, "heat", {"temperature": 100}, 2, {})
    heated = craft.process_craft(
        original, "heat", {"temperature": 100}, (), hours=2, supplied_energy=required, profile={}
    )[0]
    cooled = craft.process_craft(
        heated, "cool", {"temperature": 20}, (), hours=2, supplied_energy=0, profile={}
    )[0]
    assert heated.temperature > original.temperature
    recovered = heated.temperature - cooled.temperature
    assert 0 < recovered < required
