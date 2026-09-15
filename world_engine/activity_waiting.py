"""主动等候只跨过明确的空白时间，并在可计算的重要边界交回控制。"""

import json
from datetime import timedelta

from world_engine.geo import great_circle_distance_km
from world_engine.life import LifeActivityError
from world_engine.repository import from_iso


def waiting_boundary(connection, world_id, clock_row, request, settings):
    actor = connection.execute(
        "SELECT * FROM characters WHERE world_id=? AND is_player=1", (world_id,)
    ).fetchone()
    if actor is None:
        raise LifeActivityError("当前世界没有玩家")
    activity = connection.execute(
        "SELECT * FROM character_life_activities WHERE id=? AND world_id=? AND "
        "character_id=? AND status='running'",
        (request["activity_id"], world_id, actor["id"]),
    ).fetchone()
    if activity is None:
        raise LifeActivityError("活动已结束，请刷新后查看结果")
    if int(clock_row["version"]) != request["expected_version"]:
        raise LifeActivityError("世界状态已变化，请刷新后重新等候")
    if actor["health"] < activity["initial_health"] or great_circle_distance_km(
        actor["longitude"], actor["latitude"], activity["longitude"], activity["latitude"],
    ) > 0.001:
        raise LifeActivityError("活动条件已改变，请先处理伤势或位置变化")
    task_room = connection.execute(
        "SELECT room_id FROM activity_task_details WHERE activity_id=?", (activity["id"],),
    ).fetchone()
    if task_room and task_room["room_id"] != actor["current_room_id"]:
        raise LifeActivityError("活动所在房间已改变，请先结束这项活动")
    now = from_iso(clock_row["current_time"])
    next_adjudication = from_iso(clock_row["next_adjudication_world_time"])
    if next_adjudication <= now:
        raise LifeActivityError("当前有待处理的世界裁判，请先完成状态心跳或裁判再继续等候")
    if actor["health"] <= 0 or actor["satiety"] <= 5:
        raise LifeActivityError("身体状态需要处理，请先结束活动并补充食物或治疗")
    for encounter in connection.execute(
        "SELECT participants_json FROM combat_encounters WHERE world_id=? AND status='active'",
        (world_id,),
    ):
        if actor["id"] in json.loads(encounter["participants_json"]):
            raise LifeActivityError("战斗中不能跳过时间")
    candidates = [
        (from_iso(activity["ends_world_time"]), "当前活动结束"),
        (next_adjudication, "下一次世界裁判"),
        (now + timedelta(hours=2), "单次等候上限（两小时）"),
    ]
    deadline = connection.execute(
        "SELECT MIN(spoils_world_time) FROM item_instances WHERE container_type='activity_escrow' AND container_id=?",
        (activity["id"],),
    ).fetchone()[0]
    if deadline:
        candidates.append((from_iso(deadline), "预留食材到达变质时间"))
    for window in connection.execute(
        "SELECT w.starts_world_time FROM appointment_windows w JOIN contract_fulfillments f "
        "ON f.request_id=w.contract_id WHERE f.world_id=? AND f.status='active' "
        "AND (f.requester_id=? OR f.recipient_id=?)", (world_id,actor["id"],actor["id"]),
    ):
        start = from_iso(window[0])
        if start > now:
            candidates.append((start, "约定的见面时间到了"))
    for contract in connection.execute(
        "SELECT due_world_time FROM contract_fulfillments WHERE world_id=? AND status='active' "
        "AND (requester_id=? OR recipient_id=?)",
        (world_id, actor["id"], actor["id"]),
    ):
        due = from_iso(contract["due_world_time"])
        if due > now:
            candidates.append((due, "你的契约到期"))
    if not actor["current_room_id"]:
        for movement in connection.execute(
            "SELECT * FROM character_movements WHERE world_id=? AND status='moving'", (world_id,)
        ):
            if (
                great_circle_distance_km(
                    actor["longitude"],
                    actor["latitude"],
                    movement["destination_longitude"],
                    movement["destination_latitude"],
                )
                <= 0.1
            ):
                arrival = from_iso(movement["estimated_arrival_world"])
                if arrival > now:
                    candidates.append((arrival, "有人抵达附近"))
    satiety_rate = settings.satiety_loss_per_world_hour
    task = connection.execute(
        "SELECT spec_json FROM activity_task_details WHERE activity_id=?", (activity["id"],)
    ).fetchone()
    if task:
        spec = json.loads(task["spec_json"])
        satiety_rate += spec.get("satiety_cost", 0) * 60 / spec["duration_minutes"]
        rate = (
            spec["energy_cost"] * 60 / spec["duration_minutes"]
            - settings.energy_recovery_per_world_hour
        )
        if rate > 0:
            if actor["energy"] <= 5:
                raise LifeActivityError("精力需要处理，请先结束当前活动")
            candidates.append((now + timedelta(hours=(actor["energy"] - 5) / rate), "精力需要处理"))
    if satiety_rate > 0:
        candidates.append(
            (now + timedelta(hours=(actor["satiety"] - 5) / satiety_rate), "饱食度需要处理")
        )
    end, reason = min(candidates, key=lambda item: item[0])
    if end <= now:
        raise LifeActivityError("活动正在等待结算，请先刷新")
    return (end - now).total_seconds(), reason, actor["id"]
