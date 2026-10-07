"""哥哥，这些是有限的工艺自然响应，不读取科技名称、人物目标或研究路线。"""

from __future__ import annotations

import math
from dataclasses import replace

from world_engine.material_physics import CONTROLS, EXTENSIVE, advance, process, salt_total

CRAFT_CONTROLS = {
    **CONTROLS,
    "heat": {"temperature": (-50, 2000), "power": (0.01, 10000)},
    "cool": {"temperature": (-50, 2000), "ventilation": (0, 10)},
    "form": {"geometry_ratio": (0.1, 20), "pressure": (0, 1000)},
    "compact": {"pressure": (0, 1000)},
}
CRAFT_METRICS = frozenset({"strength", "leakage", "porosity", "color_retention", "strain"})
PROFILE_DEFAULTS = {
    "heat_capacity": 1.0,
    "heat_loss": 0.2,
    "ambient_temperature": 20.0,
    "sinter_start": 350.0,
    "sinter_width": 650.0,
    "safe_temperature": 1500.0,
    "strength_scale": 100.0,
    "leakage_scale": 1.0,
}


def _number(value, label, low=0, high=1000000000):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not low <= value <= high
    ):
        raise ValueError(f"{label}须为范围内的有限数值")
    return value


def _profile(profile):
    if not isinstance(profile, dict) or set(profile) - set(PROFILE_DEFAULTS):
        raise ValueError("材料响应参数不支持")
    values = PROFILE_DEFAULTS | profile
    for key, value in values.items():
        _number(value, key, -50 if key == "ambient_temperature" else 0)
    if values["heat_capacity"] <= 0 or values["sinter_width"] <= 0:
        raise ValueError("热容量与响应宽度必须为正")
    return values


def _heated_temperature(state, settings, hours, profile):
    target = settings.get("temperature", 60)
    thermal_mass = max(0.1, state.mass * profile["heat_capacity"] + state.water * 4.2)
    response = -math.expm1(-hours * settings.get("power", 1) / thermal_mass)
    return state.temperature + (target - state.temperature) * response


def energy_required(state, operation, settings, hours, profile):
    """哥哥，执行器用同一需求预留真实能源，不能只给函数一个虚构的数字。"""
    p = _profile(profile)
    _number(hours, "工时", 0, 5760)
    if operation == "heat":
        target = _heated_temperature(state, settings, hours, p)
        increase = max(0, target - state.temperature)
        sensible = (state.mass * p["heat_capacity"] + state.water * 4.2) * increase
        return sensible * (1 + p["heat_loss"]) + sensible * hours * 0.01
    if operation == "cool":
        target = settings.get("temperature", p["ambient_temperature"])
        return max(0, p["ambient_temperature"] - target) * state.mass * p["heat_capacity"]
    if operation == "dry":
        evaporation = state.water * (-math.expm1(-hours * settings.get("ventilation", 1) * 0.15))
        return evaporation * 2300
    if operation in {"form", "compact"}:
        return state.mass * settings.get("pressure", 1) * hours * 0.02
    return 0.0


def _conserved(before, after):
    mutable_forms = {"mobile", "adsorbed", "bound", "pigment_free", "pigment_fixed"}
    for key in set(EXTENSIVE) - mutable_forms:
        if not math.isclose(
            sum(getattr(s, key) for s in before),
            sum(getattr(s, key) for s in after),
            rel_tol=1e-10,
            abs_tol=1e-8,
        ):
            raise ValueError(f"工艺物质账未守恒：{key}")
    for measure in (salt_total, lambda s: s.pigment_free + s.pigment_fixed):
        if not math.isclose(
            sum(measure(s) for s in before),
            sum(measure(s) for s in after),
            rel_tol=1e-10,
            abs_tol=1e-8,
        ):
            raise ValueError("工艺组分的总量未守恒")


def process_craft(state, operation, settings, auxiliaries, hours, supplied_energy, profile):
    if operation not in CRAFT_CONTROLS or set(settings) - set(CRAFT_CONTROLS[operation]):
        raise ValueError("不支持该工艺操作或参数")
    for key, value in settings.items():
        _number(value, key, *CRAFT_CONTROLS[operation][key])
    _number(hours, "工时", 0, 5760)
    _number(supplied_energy, "供能")
    p = _profile(profile)
    need = energy_required(state, operation, settings, hours, p)
    if supplied_energy + 1e-8 < need:
        raise ValueError("实际供能不足，不能执行本次工艺")
    if auxiliaries and operation in {"heat", "cool", "form", "compact"}:
        raise ValueError("此操作不接受辅助材料")
    if operation == "heat":
        if settings.get("temperature", 60) < state.temperature:
            raise ValueError("降温须使用实际冷却过程")
        target = _heated_temperature(state, settings, hours, p)
        rate = max(0, target - p["sinter_start"]) / p["sinter_width"]
        increment = (1 - state.structural_bond) * (-math.expm1(-rate * hours))
        damage = max(0, target - p["safe_temperature"]) * hours / 1000
        outputs = [
            advance(
                replace(
                    state,
                    temperature=target,
                    structural_bond=min(1, state.structural_bond + increment),
                    porosity=max(0.001, state.porosity * (1 - 0.6 * increment)),
                    strain=min(1, state.strain + damage),
                ),
                hours,
            )
        ]
    elif operation == "cool":
        target = settings.get("temperature", p["ambient_temperature"])
        if target > state.temperature:
            raise ValueError("冷却目标不能高于原样品")
        rate = settings.get("ventilation", 1) * 0.8 / max(0.1, state.mass * p["heat_capacity"])
        temp = target + (state.temperature - target) * math.exp(-hours * rate)
        drop = max(0, state.temperature - temp)
        outputs = [
            advance(
                replace(
                    state,
                    temperature=temp,
                    strain=min(1, state.strain + drop / max(1, hours) * 0.0001),
                ),
                hours,
            )
        ]
    elif operation in {"form", "compact"}:
        pressure = settings.get("pressure", 1)
        outputs = [
            advance(
                replace(
                    state,
                    geometry_ratio=settings.get("geometry_ratio", state.geometry_ratio),
                    porosity=max(0.001, state.porosity * math.exp(-pressure * hours * 0.002)),
                    strain=min(1, state.strain + pressure * hours * 0.0001),
                ),
                hours,
            )
        ]
    else:
        if operation == "wash" and not auxiliaries:
            raise ValueError("洗涤必须使用真实辅助水源")
        outputs = process(state, operation, settings, hours, auxiliaries=tuple(auxiliaries))
        if operation == "wash":
            source, effluent = outputs
            ratio = sum(x.water for x in auxiliaries) / max(
                0.001, state.mass + state.water + sum(x.water for x in auxiliaries)
            )
            free = source.pigment_free * ratio * (-math.expm1(-hours * 0.4))
            fixed = source.pigment_fixed * ratio * (-math.expm1(-hours * 0.001))
            outputs = [
                replace(
                    source,
                    pigment_free=source.pigment_free - free,
                    pigment_fixed=source.pigment_fixed - fixed,
                ),
                replace(
                    effluent,
                    pigment_free=effluent.pigment_free + free,
                    pigment_fixed=effluent.pigment_fixed + fixed,
                ),
            ]
        elif operation == "bind":
            capacity = auxiliaries[0].mass * 0.1 if auxiliaries else 0
            fixed = min(capacity, outputs[0].pigment_free * (-math.expm1(-hours * 0.4)))
            outputs[0] = replace(
                outputs[0],
                pigment_free=outputs[0].pigment_free - fixed,
                pigment_fixed=outputs[0].pigment_fixed + fixed,
            )
    _conserved((state, *auxiliaries), outputs)
    return tuple(outputs)


def assay_craft(state, metric, instrument, profile):
    precision = _number(instrument.get("precision"), "量器精度")
    detection = _number(instrument.get("detection_limit", 0), "检测限")
    if precision <= 0:
        raise ValueError("量器精度须为正")
    p = _profile(profile)
    pigment = state.pigment_free + state.pigment_fixed
    values = {
        "strength": p["strength_scale"]
        * (0.05 + state.structural_bond)
        * (1 - state.porosity) ** 2
        * (1 - state.strain)
        / state.geometry_ratio,
        "leakage": p["leakage_scale"]
        * state.porosity**3
        * state.geometry_ratio
        / (state.structural_bond + 0.05),
        "porosity": state.porosity,
        "color_retention": state.pigment_fixed / pigment if pigment else 0,
        "strain": state.strain,
    }
    if metric not in values:
        raise ValueError("量器不支持这个测量指标")
    raw = values[metric]
    return {
        "value": None if raw < detection else round(raw / precision) * precision,
        "unit": instrument.get("unit", "relative"),
        "precision": precision,
        "detection_limit": detection,
    }
