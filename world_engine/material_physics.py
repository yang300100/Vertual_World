"""哥哥，这里只描述材料的有限自然作用；人物方案不参与物理判定。"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace


@dataclass(frozen=True, slots=True)
class MaterialState:
    carrier: str = "grain"
    mass: float = 1
    particles: float = 0
    mobile: float = 0
    adsorbed: float = 0
    bound: float = 0
    water: float = 0
    viability: float = 1
    nutrition: float = 0
    ph: float = 7
    temperature: float = 20
    age_hours: float = 0
    porosity: float = 0.4
    structural_bond: float = 0
    strain: float = 0
    pigment_free: float = 0
    pigment_fixed: float = 0
    geometry_ratio: float = 1

    def __post_init__(self):
        for key, value in asdict(self).items():
            if key == "carrier":
                if not isinstance(value, str) or not value.strip():
                    raise ValueError("材料须有载体类型")
                continue
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError("材料量须是有限数值")
            if key not in {"temperature"} and value < 0:
                raise ValueError("材料量不能为负数")
        if not 0 <= self.ph <= 14 or not 0 <= self.viability <= 1:
            raise ValueError("酸碱度或种子活力超出物理范围")
        if any(
            not 0 <= getattr(self, key) <= 1 for key in ("porosity", "structural_bond", "strain")
        ):
            raise ValueError("孔隙、结构结合或形变超出范围")
        if not 0 < self.geometry_ratio <= 1000000:
            raise ValueError("材料几何比超出范围")
        if not -50 <= self.temperature <= 2000:
            raise ValueError("温度超出本材料模型范围")


EXTENSIVE = (
    "mass",
    "particles",
    "mobile",
    "adsorbed",
    "bound",
    "water",
    "nutrition",
    "pigment_free",
    "pigment_fixed",
)


def salt_total(state: MaterialState) -> float:
    return state.mobile + state.adsorbed + state.bound


def fraction(state: MaterialState, amount: float) -> MaterialState:
    if isinstance(amount, bool) or not math.isfinite(amount) or not 0 <= amount <= 1:
        raise ValueError("拆批比例超出范围")
    return replace(state, **{key: getattr(state, key) * amount for key in EXTENSIVE})


def combine(states: tuple[MaterialState, ...], *, carrier: str | None = None) -> MaterialState:
    if not states:
        raise ValueError("混合需要实际材料")
    amounts = {key: sum(getattr(s, key) for s in states) for key in EXTENSIVE}
    dry = sum(s.mass for s in states)
    wet = sum(s.mass + s.water for s in states)
    carriers = {s.carrier for s in states if s.mass > 0}
    return MaterialState(
        carrier=carrier or (states[0].carrier if len(carriers) <= 1 else "mixture"),
        **amounts,
        viability=sum(s.mass * s.viability for s in states) / dry if dry else 0,
        ph=sum((s.mass + s.water) * s.ph for s in states) / wet if wet else states[0].ph,
        temperature=sum((s.mass + s.water) * s.temperature for s in states) / wet
        if wet
        else states[0].temperature,
        # 哥哥，混合不能用新鲜批次给旧材料的观察历程归零。
        age_hours=max(s.age_hours for s in states),
        **{
            key: sum(s.mass * getattr(s, key) for s in states) / dry
            if dry
            else getattr(states[0], key)
            for key in ("porosity", "structural_bond", "strain", "geometry_ratio")
        },
    )


def advance(state: MaterialState, hours: float) -> MaterialState:
    _hours(hours)
    wetness = state.water / max(0.001, state.mass + state.water)
    acid = max(0, 7 - state.ph)
    adsorbed_rate = wetness * (0.0005 + acid * 0.003)
    bound_rate = wetness * (0.000001 + acid * 0.002)
    released = state.adsorbed * (-math.expm1(-hours * adsorbed_rate))
    unbound = state.bound * (-math.expm1(-hours * bound_rate))
    integrated_damage = hours * wetness * max(0, state.temperature - 12) * 0.00001
    if state.carrier == "seed":
        # 哥哥，湿润启动的种子会受到实际溶解负荷损伤，干燥休眠与加热伤害分开。
        def release_integral(rate):
            scaled = rate * hours
            if scaled < 1e-5:
                return hours * (scaled / 2 - scaled**2 / 6 + scaled**3 / 24)
            return hours + math.expm1(-scaled) / rate

        integrated_mobile = (
            state.mobile * hours
            + state.adsorbed * release_integral(adsorbed_rate)
            + state.bound * release_integral(bound_rate)
        )
        integrated_damage += wetness * integrated_mobile / max(0.001, state.mass)
    return replace(
        state,
        mobile=state.mobile + released + unbound,
        adsorbed=state.adsorbed - released,
        bound=state.bound - unbound,
        viability=state.viability * math.exp(-integrated_damage),
        age_hours=state.age_hours + hours,
    )


def _hours(hours):
    if (
        isinstance(hours, bool)
        or not isinstance(hours, (int, float))
        or not math.isfinite(hours)
        or hours < 0
    ):
        raise ValueError("处理时长须是非负有限数值")


CONTROLS = {
    "wash": {"water": (0, 1000000)},
    "heat": {"temperature": (0, 200), "power": (0.01, 10000)},
    "dry": {"ventilation": (0, 10)},
    "screen": {"mesh": (0.01, 1)},
    "filter": {"capacity": (0, 1000000), "porosity": (0.001, 1)},
    "adsorb": {},
    "mix": {},
    "bind": {},
    "settle": {},
    "store": {},
}


def process(
    state: MaterialState,
    operation: str,
    settings: dict[str, float],
    hours: float,
    auxiliaries: tuple[MaterialState, ...] = (),
) -> list[MaterialState]:
    _hours(hours)
    if operation not in CONTROLS or set(settings) - set(CONTROLS[operation]):
        raise ValueError("不存在该操作或材料控制量")
    for key, value in settings.items():
        low, high = CONTROLS[operation][key]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not low <= value <= high
        ):
            raise ValueError("材料控制量超出范围")
    if auxiliaries and operation not in {"wash", "mix", "adsorb", "bind", "filter"}:
        raise ValueError("本操作不接受辅助材料")
    before = [state, *auxiliaries]
    if operation == "wash":
        added = settings.get("water", 0)
        if auxiliaries:
            if added and not math.isclose(added, sum(x.water for x in auxiliaries)):
                raise ValueError("声明水量与实际辅助材料不符")
            liquid = combine(auxiliaries, carrier="water")
            added = liquid.water
        else:
            liquid = MaterialState(carrier="water", mass=0, water=added, viability=0)
            before.append(liquid)
        wet = state.water + added
        contact = added / max(0.001, wet + state.mass)
        released = state.mobile * contact * (-math.expm1(-0.7 * hours))
        particles = state.particles * contact * (-math.expm1(-0.9 * hours))
        loss = min(0.02, 0.005 * contact * hours)
        residue = fraction(state, 1 - loss)
        lost = fraction(state, loss)
        retained_water = min(wet, state.mass * 0.2)
        incoming_salt = liquid.mobile * retained_water / max(0.001, wet)
        product = replace(
            residue,
            water=retained_water,
            mobile=residue.mobile - released * (1 - loss) + incoming_salt,
            particles=residue.particles - particles * (1 - loss),
        )
        effluent = combine((lost, liquid), carrier="water")
        effluent = replace(
            effluent,
            water=wet - retained_water,
            mobile=effluent.mobile + released * (1 - loss) - incoming_salt,
            particles=effluent.particles + particles * (1 - loss),
        )
        outputs = [product, effluent]
    elif operation == "heat":
        target = settings.get("temperature", 60)
        power = settings.get("power", 1)
        lag = -math.expm1(-hours * 3 * power / max(0.1, state.mass + state.water))
        core = state.temperature + (target - state.temperature) * lag
        mean = (state.temperature + core) / 2
        damage = max(0, mean - 42) * hours * 0.08
        outputs = [replace(state, temperature=core, viability=state.viability * math.exp(-damage))]
    elif operation == "dry":
        evaporation = state.water * (-math.expm1(-hours * settings.get("ventilation", 1) * 0.15))
        outputs = [
            replace(state, water=state.water - evaporation),
            MaterialState(carrier="vapor", mass=0, water=evaporation, viability=0),
        ]
    elif operation == "screen":
        mesh = settings.get("mesh", 0.5)
        loss = min(0.2, 0.04 / mesh)
        product = fraction(state, 1 - loss)
        waste = fraction(state, loss)
        removed = product.particles * (1 - mesh) * (-math.expm1(-hours * 2))
        outputs = [
            replace(product, particles=product.particles - removed),
            replace(waste, carrier="screenings", particles=waste.particles + removed),
        ]
    elif operation == "filter":
        if not auxiliaries or any(
            x.carrier not in {"charcoal", "clay", "filter_media", "spent_media"}
            for x in auxiliaries
        ):
            raise ValueError("过滤需要真实滤材介质，提交容量不能创造物质")
        capacities = [max(0, x.mass * 0.1 - x.particles) for x in auxiliaries]
        cap = min(settings.get("capacity", sum(capacities)), sum(capacities))
        removed = min(
            cap, state.particles * (1 - settings.get("porosity", 0.5)) * (-math.expm1(-hours * 4))
        )
        outputs = [replace(state, particles=state.particles - removed)]
        remaining = removed
        for media, available in zip(auxiliaries, capacities, strict=True):
            captured = min(remaining, available)
            outputs.append(
                replace(media, carrier="spent_media", particles=media.particles + captured)
            )
            remaining -= captured
    elif operation == "settle":
        removed = state.particles * (-math.expm1(-hours * 0.1))
        outputs = [
            replace(state, particles=state.particles - removed),
            MaterialState(carrier="sediment", mass=0, particles=removed, viability=0),
        ]
    elif operation in {"adsorb", "bind"}:
        if len(auxiliaries) != 1:
            raise ValueError("必须提供一份真实吸附或结合材料")
        medium = auxiliaries[0]
        capacities = {"clay": 0.3, "charcoal": 0.8, "limestone": 0.6}
        capacity = max(
            0, medium.mass * capacities.get(medium.carrier, 0) - medium.adsorbed - medium.bound
        )
        transferred = min(capacity, state.mobile * (-math.expm1(-hours * 0.4)))
        if operation == "bind" and medium.carrier != "limestone":
            transferred = 0
        key = "adsorbed" if operation == "adsorb" else "bound"
        outputs = [
            replace(state, mobile=state.mobile - transferred),
            replace(medium, **{key: getattr(medium, key) + transferred}),
        ]
    elif operation == "mix":
        outputs = [combine((state, *auxiliaries))]
    else:
        outputs = [advance(state, hours)]
    # 哥哥，处理的时间也会触发自然返溶；store 已经推进，不能重复算时间。
    if operation != "store":
        outputs = [advance(x, hours) for x in outputs]
    for key in EXTENSIVE:
        if not math.isclose(
            sum(getattr(x, key) for x in before),
            sum(getattr(x, key) for x in outputs),
            abs_tol=1e-8,
            rel_tol=1e-10,
        ):
            # 盐的形态可以互转，只比较三种形态的总量。
            if key in {"mobile", "adsorbed", "bound"}:
                continue
            raise ValueError(f"材料过程未守恒：{key}")
    if not math.isclose(
        sum(salt_total(x) for x in before),
        sum(salt_total(x) for x in outputs),
        abs_tol=1e-8,
        rel_tol=1e-10,
    ):
        raise ValueError("盐的物质账未守恒")
    return outputs
