"""哥哥，这些测试核对守恒、迟发反应和规模，不安排正式 NPC 的解法。"""

import importlib.util
from dataclasses import asdict

import pytest


def physics():
    assert importlib.util.find_spec("world_engine.material_physics"), "尚无守恒材料物理能力"
    from world_engine import material_physics

    return material_physics


def test_wet_seed_damage_integrates_released_salt_independently_of_step_size():
    m = physics()
    seed = m.MaterialState(carrier="seed", mass=1, water=1, adsorbed=0.2, bound=0.1, ph=3)
    coarse = m.advance(seed, 1)
    fine = seed
    for _ in range(4):
        fine = m.advance(fine, 0.25)
    assert asdict(coarse) == pytest.approx(asdict(fine), rel=1e-12, abs=1e-12)


def test_leaching_records_contamination_in_actual_effluent():
    m = physics()
    grain = m.MaterialState(mass=10, particles=1, mobile=2, water=0, nutrition=100)
    outputs = m.process(grain, "wash", {"water": 20}, hours=2)
    assert len(outputs) == 2
    assert 0 < outputs[0].mobile < 2
    assert outputs[1].mobile > 0
    assert sum(x.mobile + x.adsorbed + x.bound for x in outputs) == pytest.approx(2)
    assert sum(x.particles for x in outputs) == pytest.approx(1)
    assert sum(x.mass for x in outputs) == pytest.approx(10)
    assert sum(x.water for x in outputs) == pytest.approx(20)
    assert grain.mobile == 2 and grain.water == 0


def test_heat_damages_seed_and_does_not_destroy_salt():
    m = physics()
    grain = m.MaterialState(mass=1, mobile=0.2, water=1, viability=1)
    heated = m.process(grain, "heat", {"temperature": 95}, hours=3)[0]
    assert heated.viability < 0.2
    assert m.salt_total(heated) == pytest.approx(0.2)


def test_larger_batch_has_colder_core_at_same_power_and_duration():
    m = physics()
    small = m.MaterialState(mass=1, water=1)
    large = m.MaterialState(mass=100, water=100)
    a = m.process(small, "heat", {"temperature": 100, "power": 1}, hours=1)[0]
    b = m.process(large, "heat", {"temperature": 100, "power": 1}, hours=1)[0]
    assert 20 < b.temperature < a.temperature < 100


def test_fine_filter_does_not_remove_dissolved_salts():
    m = physics()
    dirty = m.MaterialState(carrier="water", mass=0, water=10, particles=1, mobile=2)
    outputs = m.process(
        dirty,
        "filter",
        {"capacity": 10, "porosity": 0.05},
        hours=1,
        auxiliaries=(m.MaterialState(carrier="charcoal", mass=100, viability=0),),
    )
    assert outputs[0].particles < 0.1
    assert outputs[0].mobile == pytest.approx(2)
    assert sum(x.particles for x in outputs) == pytest.approx(1)


def test_filter_capacity_limits_removal():
    m = physics()
    dirty = m.MaterialState(carrier="water", mass=0, water=10, particles=10)
    outputs = m.process(
        dirty,
        "filter",
        {"capacity": 1, "porosity": 0.01},
        hours=1,
        auxiliaries=(m.MaterialState(carrier="charcoal", mass=10, viability=0),),
    )
    assert outputs[0].particles >= 9
    assert outputs[1].particles <= 1


def test_filter_cannot_create_capacity_and_loaded_media_cannot_reset():
    m = physics()
    dirty = m.MaterialState(carrier="water", mass=0, water=10, particles=10)
    with pytest.raises(ValueError, match="滤材|介质"):
        m.process(dirty, "filter", {"capacity": 1000000}, 4)
    media = m.MaterialState(carrier="charcoal", mass=10, viability=0)
    first = m.process(
        dirty, "filter", {"capacity": 1000000, "porosity": 0.001}, 4, auxiliaries=(media,)
    )
    assert first[0].particles == pytest.approx(9)
    assert first[1].particles == pytest.approx(1)
    second = m.process(
        dirty, "filter", {"capacity": 1000000, "porosity": 0.001}, 4, auxiliaries=(first[1],)
    )
    assert second[0].particles == pytest.approx(10)
    assert sum(x.mass for x in second) == pytest.approx(10)


def test_adsorbed_salt_returns_with_delay():
    m = physics()
    stored = m.MaterialState(mass=1, water=1, adsorbed=1, ph=4)
    immediate = m.advance(stored, 0)
    later = m.advance(stored, 48)
    assert immediate.mobile == 0
    assert later.mobile > 0.1
    assert m.salt_total(later) == pytest.approx(1)


def test_storage_integrates_identically_in_small_and_large_steps():
    m = physics()
    original = m.MaterialState(mass=3, water=2, adsorbed=1, bound=2, ph=4)
    large = m.advance(original, 72)
    small = original
    for _ in range(72):
        small = m.advance(small, 1)
    for field in ("mobile", "adsorbed", "bound", "viability", "age_hours"):
        assert getattr(small, field) == pytest.approx(getattr(large, field))


def test_adsorption_moves_salt_into_limited_real_auxiliary():
    m = physics()
    water = m.MaterialState(carrier="water", mass=0, water=10, mobile=2)
    clay = m.MaterialState(carrier="clay", mass=1, nutrition=0)
    outputs = m.process(water, "adsorb", {}, hours=4, auxiliaries=(clay,))
    assert outputs[0].mobile < 2
    assert 0 < outputs[1].adsorbed <= 0.3
    assert sum(m.salt_total(x) for x in outputs) == pytest.approx(2)
    assert sum(x.mass for x in outputs) == pytest.approx(1)


def test_no_auxiliary_means_no_unlimited_adsorption():
    m = physics()
    water = m.MaterialState(carrier="water", mass=0, water=10, mobile=2)
    with pytest.raises(ValueError):
        m.process(water, "adsorb", {}, hours=4)


def test_screen_removes_particles_but_not_mobile_salt():
    m = physics()
    grain = m.MaterialState(mass=10, particles=1, mobile=2, nutrition=100)
    outputs = m.process(grain, "screen", {"mesh": 0.1}, hours=1)
    assert outputs[0].particles < 1
    assert outputs[0].mobile + outputs[1].mobile == pytest.approx(2)
    assert outputs[0].nutrition < 100
    assert sum(x.mass for x in outputs) == pytest.approx(10)


def test_ph_change_can_remobilize_previously_bound_contamination():
    m = physics()
    alkaline = m.MaterialState(carrier="soil", mass=10, water=2, bound=2, ph=8)
    acidic = m.MaterialState(carrier="soil", mass=10, water=2, bound=2, ph=4)
    assert m.advance(acidic, 72).mobile > m.advance(alkaline, 72).mobile


def test_sequence_of_drying_and_washing_changes_contact_outcome():
    m = physics()
    grain = m.MaterialState(mass=10, mobile=2, water=10)
    washed = m.process(grain, "wash", {"water": 2}, hours=1)[0]
    dry = m.process(grain, "dry", {"ventilation": 1}, hours=4)[0]
    dry_then_washed = m.process(dry, "wash", {"water": 2}, hours=1)[0]
    assert washed.mobile != pytest.approx(dry_then_washed.mobile)


def test_drying_records_water_in_vapor_output():
    m = physics()
    original = m.MaterialState(mass=1, water=5, mobile=0.2)
    outputs = m.process(original, "dry", {"ventilation": 1}, hours=8)
    assert outputs[0].water < 5
    assert outputs[1].carrier == "vapor"
    assert sum(x.water for x in outputs) == pytest.approx(5)
    assert sum(m.salt_total(x) for x in outputs) == pytest.approx(0.2)


@pytest.mark.parametrize(
    "field,value",
    [
        ("mass", -1),
        ("mobile", -1),
        ("water", float("nan")),
        ("ph", 15),
        ("viability", 2),
        ("particles", float("inf")),
    ],
)
def test_state_rejects_impossible_inputs(field, value):
    m = physics()
    with pytest.raises(ValueError):
        m.MaterialState(**{field: value})


@pytest.mark.parametrize(
    "settings,hours",
    [
        ({"water": -1}, 1),
        ({"water": 1}, -1),
        ({"water": float("inf")}, 1),
        ({"water": 1, "cure": 1}, 1),
    ],
)
def test_process_rejects_nonphysical_or_unknown_controls(settings, hours):
    m = physics()
    with pytest.raises(ValueError):
        m.process(m.MaterialState(mass=1), "wash", settings, hours)


def test_mix_preserves_constituents_and_weighted_seed_viability():
    m = physics()
    a = m.MaterialState(mass=3, mobile=0.3, viability=1, nutrition=30)
    b = m.MaterialState(mass=1, mobile=0.7, viability=0, nutrition=10)
    outputs = m.process(a, "mix", {}, hours=1, auxiliaries=(b,))
    assert len(outputs) == 1
    assert outputs[0].mass == 4
    assert outputs[0].mobile == pytest.approx(1)
    assert outputs[0].nutrition == 40
    assert outputs[0].viability == pytest.approx(0.75)
    assert asdict(a)["mass"] == 3


def test_washing_with_salty_water_can_contaminate_clean_retained_moisture():
    m = physics()
    grain = m.MaterialState(mass=10, mobile=0, nutrition=200)
    water = m.MaterialState(carrier="water", mass=0, water=20, mobile=2, viability=0)
    result = m.process(grain, "wash", {}, 1, auxiliaries=(water,))
    assert result[0].mobile > 0
    assert sum(m.salt_total(s) for s in result) == pytest.approx(2)


def test_mixing_soil_into_seed_does_not_create_more_plantable_seed_mass():
    m = physics()
    seed = m.MaterialState(carrier="seed", mass=1, viability=1)
    soil = m.MaterialState(carrier="soil", mass=100, viability=1)
    result = m.process(seed, "mix", {}, 1, auxiliaries=(soil,))[0]
    assert result.carrier == "mixture"


def test_wet_seed_exposure_has_a_measurable_germination_cost():
    m = physics()
    clean = m.MaterialState(carrier="seed", mass=1, water=1, mobile=0, viability=1)
    polluted = m.MaterialState(carrier="seed", mass=1, water=1, mobile=0.04, viability=1)
    assert m.advance(polluted, 72).viability < m.advance(clean, 72).viability * 0.5
