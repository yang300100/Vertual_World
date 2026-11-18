from __future__ import annotations

import math

WORLD_RADIUS_KM = 6371.0088


def nearby_coordinate_bounds(longitude: float, latitude: float, radius_km: float):
    """哥哥，先圈出球面邻域的保守范围，再用真实距离判断，极点和日期变更线也保留。"""
    if not math.isfinite(radius_km) or radius_km < 0:
        raise ValueError("邻域半径须为非负有限数")
    angular = min(math.pi, radius_km / WORLD_RADIUS_KM)
    delta = math.degrees(angular)
    epsilon = 1e-9
    south, north = max(-90, latitude - delta - epsilon), min(90, latitude + delta + epsilon)
    if south <= -90 or north >= 90:
        return south, north, [(-180.0, 180.0)]
    longitude_delta = (
        math.degrees(math.asin(min(1, math.sin(angular) / math.cos(math.radians(latitude)))))
        + epsilon
    )
    west, east = longitude - longitude_delta, longitude + longitude_delta
    if west < -180:
        ranges = [(-180.0, east), (west + 360, 180.0)]
    elif east > 180:
        ranges = [(west, 180.0), (-180.0, east - 360)]
    else:
        ranges = [(west, east)]
    return south, north, ranges


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
