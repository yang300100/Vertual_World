from __future__ import annotations

import math

WORLD_RADIUS_KM = 6371.0088


def great_circle_distance_km(
    start_longitude: float,
    start_latitude: float,
    end_longitude: float,
    end_latitude: float,
) -> float:
    """按球面大圆距离计算两组世界经纬度之间的公里数。"""
    lat1 = math.radians(start_latitude)
    lat2 = math.radians(end_latitude)
    delta_latitude = lat2 - lat1
    delta_longitude = math.radians(end_longitude - start_longitude)
    value = (
        math.sin(delta_latitude / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_longitude / 2) ** 2
    )
    return 2 * WORLD_RADIUS_KM * math.asin(min(1.0, math.sqrt(value)))
