"""固定的可调共振器模型；这里没有人物策略或灾害解决流程。"""

import math

CONTROL_BOUNDS = {"feed": (0.0, 1.2), "tune": (-1.0, 1.0), "screen": (0.0, 1.0)}
DEFAULT_CONTROLS = {"feed": 1.0, "tune": 0.0, "screen": 0.0}


def controls_changed(current, changes):
    result = dict(current)
    if not changes or set(changes) - set(CONTROL_BOUNDS):
        raise ValueError("只能调节装置确实存在的旋钮")
    for key, value in changes.items():
        low, high = CONTROL_BOUNDS[key]
        if isinstance(value, bool) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError("旋钮位置超出物理范围")
        result[key] = float(value)
    return result


def response(controls, physical, forcing):
    feed, tune, screen = (controls[k] for k in ("feed", "tune", "screen"))
    resonance = math.exp(-(((tune - physical["center"]) / physical["width"]) ** 2))
    noise = forcing * feed * (resonance * (1 - 0.9 * screen) + 0.035)
    nominal = feed * (1 - 0.08 * abs(tune)) * (1 - 0.12 * screen)
    return {"local_noise": round(noise, 6), "nominal_service": round(nominal, 6)}


def coupled_response(controls, physical, forcing, background, hazard_limit):
    result = response(controls, physical, forcing)
    hazard = max(result["local_noise"], background)
    return {
        **result,
        "background_noise": round(background, 6),
        "hazard": round(hazard, 6),
        "service": round(result["nominal_service"] / (1 + 2 * max(0, hazard - hazard_limit)), 6),
    }
