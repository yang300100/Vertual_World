"""可玩的地图视图使用真实位置与服务端移动。"""

# ruff: noqa: F811
import pytest
from test_lived_world import town  # noqa: F401


def test_scene_lists_every_npc_and_player(town):
    e = town
    result = e.client.get(f"/api/worlds/{e.wid}/game-scene")
    assert result.status_code == 200, result.text
    scene = result.json()
    assert scene["player"]["id"] == e.player
    assert e.npc in {actor["id"] for actor in scene["npcs"]}
    assert scene["map"]["min_longitude"] <= scene["player"]["longitude"] <= scene["map"]["max_longitude"]
    assert scene["map"]["min_latitude"] <= scene["player"]["latitude"] <= scene["map"]["max_latitude"]


def test_wasd_steer_is_server_controlled_and_ordered(town):
    e = town
    before = e.client.get(f"/api/worlds/{e.wid}/game-scene").json()["player"]
    response = e.client.post(
        f"/api/worlds/{e.wid}/player/steer",
        json={"dx": 1, "dy": 0, "sequence": 100},
    )
    assert response.status_code == 200, response.text
    after = e.client.get(f"/api/worlds/{e.wid}/game-scene").json()["player"]
    assert (before["longitude"], before["latitude"]) == (after["longitude"], after["latitude"])
    stale = e.client.post(
        f"/api/worlds/{e.wid}/player/steer", json={"dx": -1, "dy": 0, "sequence": 99},
    )
    assert stale.status_code == 409
    stop = e.client.post(
        f"/api/worlds/{e.wid}/player/steer/stop", json={"sequence": 101},
    )
    assert stop.status_code == 200, stop.text


def test_city_map_switches_without_changing_coordinates(town):
    from uuid import uuid4

    e = town
    lon, lat = e.loc["longitude"], e.loc["latitude"]
    with e.db.write() as c:
        c.execute("UPDATE locations SET kind='city',area_radius_km=5 WHERE id=?", (e.loc["id"],))
    local = e.client.get(f"/api/worlds/{e.wid}/game-scene").json()
    assert local["map"]["map_role"] == "local"
    with e.db.write() as c:
        c.execute(
            "INSERT INTO world_maps(id,world_id,name,asset_path,min_longitude,max_longitude,"
            "min_latitude,max_latitude,width_pixels,height_pixels,zoom_level,location_id,map_role) "
            "VALUES (?,?,?,?,?,?,?,?,?, ?,2,?,'detail')",
            (str(uuid4()), e.wid, "城镇详图", f"test/{uuid4()}.png",
             lon - .1, lon + .1, lat - .1, lat + .1, 512, 512, e.loc["id"]),
        )
    inside = e.client.get(f"/api/worlds/{e.wid}/game-scene").json()
    assert inside["map"]["map_role"] == "detail"
    assert inside["player"]["longitude"] == lon
    with e.db.write() as c:
        c.execute("UPDATE characters SET longitude=? WHERE id=?", (lon + 1, e.player))
    outside = e.client.get(f"/api/worlds/{e.wid}/game-scene").json()
    assert outside["map"]["map_role"] == "world"
    assert outside["player"]["longitude"] == lon + 1


def test_city_edge_uses_local_map_when_detail_does_not_cover_player(town):
    from uuid import uuid4

    e = town
    lon, lat = e.loc["longitude"], e.loc["latitude"]
    with e.db.write() as c:
        c.execute("UPDATE locations SET kind='city',area_radius_km=5 WHERE id=?", (e.loc["id"],))
        c.execute(
            "INSERT INTO world_maps(id,world_id,name,asset_path,min_longitude,max_longitude,"
            "min_latitude,max_latitude,width_pixels,height_pixels,zoom_level,location_id,map_role) "
            "VALUES (?,?,?,?,?,?,?,?,?, ?,2,?,'detail')",
            (str(uuid4()), e.wid, "城镇中心", f"test/{uuid4()}.png",
             lon - .005, lon + .005, lat - .005, lat + .005, 512, 512, e.loc["id"]),
        )
        c.execute("UPDATE characters SET longitude=? WHERE id=?", (lon + .02, e.player))
    scene = e.client.get(f"/api/worlds/{e.wid}/game-scene").json()
    assert scene["map"]["map_role"] == "local"
    assert scene["map"]["min_longitude"] <= scene["player"]["longitude"] <= scene["map"]["max_longitude"]


def test_paused_world_does_not_advance_wasd_position(town):
    e = town
    with e.db.write() as c:
        c.execute("UPDATE world_clock SET time_scale=0 WHERE world_id=?", (e.wid,))
    before = e.client.get(f"/api/worlds/{e.wid}/game-scene").json()["player"]
    started = e.client.post(
        f"/api/worlds/{e.wid}/player/steer",
        json={"dx": 1, "dy": 0, "sequence": 100},
    )
    assert started.status_code == 200, started.text
    e.clock.heartbeat(e.wid, elapsed_seconds=600)
    after = e.client.get(f"/api/worlds/{e.wid}/game-scene").json()["player"]
    assert (after["longitude"], after["latitude"]) == (before["longitude"], before["latitude"])


def test_wasd_movement_obeys_character_speed(town):
    from world_engine.geo import great_circle_distance_km

    e = town
    before = e.client.get(f"/api/worlds/{e.wid}/game-scene").json()["player"]
    with e.db.read() as c:
        speed = c.execute("SELECT movement_speed_kmh FROM characters WHERE id=?",
                          (e.player,)).fetchone()[0]
    started = e.client.post(
        f"/api/worlds/{e.wid}/player/steer",
        json={"dx": 1, "dy": 0, "sequence": 100},
    )
    assert started.status_code == 200, started.text
    e.clock.heartbeat(e.wid, elapsed_seconds=60)
    after = e.client.get(f"/api/worlds/{e.wid}/game-scene").json()["player"]
    moved = great_circle_distance_km(before["longitude"], before["latitude"],
                                     after["longitude"], after["latitude"])
    assert 0 < moved <= speed / 60 + .02


def test_wasd_does_not_create_destination_or_route(town):
    e = town
    response = e.client.post(f"/api/worlds/{e.wid}/player/steer",
                             json={"dx": 1, "dy": 1, "sequence": 100})
    assert response.status_code == 200, response.text
    with e.db.read() as c:
        assert c.execute("SELECT count(*) FROM character_movements WHERE character_id=?",
                         (e.player,)).fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM world_events WHERE actor_id=? "
                         "AND event_type='action.route_planned'", (e.player,)).fetchone()[0] == 0
    assert response.json()["scene"]["steering"]["active"]


def test_direction_input_expires_and_does_not_keep_walking(town):
    e = town
    e.client.post(f"/api/worlds/{e.wid}/player/steer",
                  json={"dx": 1, "dy": 0, "sequence": 100})
    e.clock.heartbeat(e.wid, elapsed_seconds=60)
    first = e.client.get(f"/api/worlds/{e.wid}/game-scene").json()
    e.clock.heartbeat(e.wid, elapsed_seconds=60)
    second = e.client.get(f"/api/worlds/{e.wid}/game-scene").json()
    assert first["player"]["longitude"] == second["player"]["longitude"]
    assert not second["steering"]["active"]


def input_clock(e, monkeypatch):
    from world_engine.repository import to_iso, utc_now

    now = [utc_now()]
    monkeypatch.setattr("world_engine.engine_player_movement.utc_now", lambda: now[0])
    with e.db.write() as c:
        c.execute("UPDATE world_clock SET last_heartbeat_real_time=? WHERE world_id=?",
                  (to_iso(now[0]), e.wid))
    return now


def steer(e, sequence, dx=0, dy=0, stop=False):
    path = "player/steer/stop" if stop else "player/steer"
    payload = {"sequence": sequence} if stop else {"sequence": sequence, "dx": dx, "dy": dy}
    response = e.client.post(f"/api/worlds/{e.wid}/{path}", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["scene"]


def test_short_press_settles_old_direction_before_turn_and_stop(town, monkeypatch):
    from datetime import timedelta

    e = town
    now = input_clock(e, monkeypatch)
    start = steer(e, 100, dx=1)["player"]
    now[0] += timedelta(seconds=.5)
    corner = steer(e, 101, dy=1)["player"]
    assert corner["longitude"] > start["longitude"]
    assert corner["latitude"] == start["latitude"]
    now[0] += timedelta(seconds=.5)
    stopped = steer(e, 102, stop=True)
    assert stopped["player"]["longitude"] == corner["longitude"]
    assert stopped["player"]["latitude"] > corner["latitude"]
    assert not stopped["steering"]["active"]
    e.clock.heartbeat(e.wid, real_now=now[0] + timedelta(seconds=10))
    later = e.client.get(f"/api/worlds/{e.wid}/game-scene").json()
    assert later["player"] == stopped["player"]
    with e.db.read() as c:
        effect = c.execute("SELECT payload_json FROM action_effects WHERE actor_character_id=? "
                           "AND effect_type='action.steering_stopped'", (e.player,)).fetchone()
    import json
    changes = json.loads(effect[0])["changes"]
    assert "longitude" in changes and "latitude" in changes
    assert "items" not in changes


@pytest.mark.parametrize("dx,dy", [(1, 0), (1, 1), (.2, .2)])
def test_direction_speed_is_normalized_without_client_speed_override(town, monkeypatch, dx, dy):
    from datetime import timedelta

    from world_engine.geo import great_circle_distance_km

    e = town
    now = input_clock(e, monkeypatch)
    before = steer(e, 100, dx=dx, dy=dy)["player"]
    now[0] += timedelta(seconds=.5)
    after = steer(e, 101, stop=True)["player"]
    distance = great_circle_distance_km(before["longitude"], before["latitude"],
                                        after["longitude"], after["latitude"])
    with e.db.read() as c:
        speed = c.execute("SELECT movement_speed_kmh FROM characters WHERE id=?", (e.player,)).fetchone()[0]
    assert distance == pytest.approx(speed * .5 / 3600, rel=1e-5)


def test_direction_rejects_oversized_input(town):
    e = town
    response = e.client.post(f"/api/worlds/{e.wid}/player/steer",
                             json={"dx": 200, "dy": 200, "sequence": 100})
    assert response.status_code == 422
    with e.db.read() as c:
        assert not c.execute("SELECT 1 FROM player_steering WHERE world_id=?", (e.wid,)).fetchone()


def test_click_and_direction_control_are_mutually_exclusive(town, monkeypatch):
    e = town
    input_clock(e, monkeypatch)
    destination = {"destination_longitude": e.loc["longitude"] + .1,
                   "destination_latitude": e.loc["latitude"]}
    first = e.client.post(f"/api/worlds/{e.wid}/player/move", json=destination)
    assert first.status_code == 201, first.text
    steer(e, 100, dx=1)
    with e.db.read() as c:
        assert c.execute("SELECT status FROM character_movements WHERE character_id=?",
                         (e.player,)).fetchone()[0] == "cancelled"
    second = e.client.post(f"/api/worlds/{e.wid}/player/move", json=destination)
    assert second.status_code == 201, second.text
    scene = e.client.get(f"/api/worlds/{e.wid}/game-scene").json()
    assert not scene["steering"]["active"]
    # 延迟到达的松键请求只能结束方向输入，不能取消刚选择的目标行程。
    steer(e, 101, stop=True)
    with e.db.read() as c:
        assert c.execute("SELECT count(*) FROM character_movements WHERE character_id=? AND status='moving'",
                         (e.player,)).fetchone()[0] == 1


def test_steering_cannot_overlap_life_activity_or_change_transport(town, monkeypatch):
    from world_engine.life import LifeActivityError, LifeActivityService
    from world_engine.movement import MovementService

    e = town
    input_clock(e, monkeypatch)
    steer(e, 100, dx=1)
    with e.db.write() as c:
        actor = c.execute("SELECT * FROM characters WHERE id=?", (e.player,)).fetchone()
        with pytest.raises(LifeActivityError):
            LifeActivityService.start(c, actor, e.at, "rest", 60)
        with pytest.raises(ValueError, match="不能更换"):
            MovementService().select_transport(c, world_id=e.wid, character_id=e.player, vehicle_id=None)
    steer(e, 101, stop=True)
    with e.db.write() as c:
        actor = c.execute("SELECT * FROM characters WHERE id=?", (e.player,)).fetchone()
        LifeActivityService.start(c, actor, e.at, "rest", 60)
    response = e.client.post(f"/api/worlds/{e.wid}/player/steer",
                             json={"dx": 1, "dy": 0, "sequence": 102})
    assert response.status_code == 400, response.text


def test_direction_stops_at_water_without_routing_around_it(town, monkeypatch):
    from datetime import timedelta

    from test_routing import _cell

    from world_engine.navigation import TerrainService
    from world_engine.player_steering import PlayerSteeringService

    e = town
    now = input_clock(e, monkeypatch)
    with e.db.write() as c:
        c.execute("UPDATE world_clock SET time_scale=3600 WHERE world_id=?", (e.wid,))
    lon, lat = e.loc["longitude"], e.loc["latitude"]
    terrain = TerrainService.from_memory({
        0: _cell(0, lon, lat),
        1: _cell(1, lon + .002, lat, water="river"),
        2: _cell(2, lon + .004, lat),
    })
    monkeypatch.setattr("world_engine.player_steering._terrain", lambda _: terrain)
    monkeypatch.setattr("world_engine.routing.RoutePlanner._approved_dataset",
                        lambda *args: {"asset_root": "unused"})
    start = steer(e, 100, dx=1)["player"]
    now[0] += timedelta(seconds=.5)
    scene = steer(e, 101, stop=True)
    assert start["longitude"] <= scene["player"]["longitude"] < lon + .001
    assert scene["player"]["latitude"] == lat
    with e.db.read() as c:
        assert not PlayerSteeringService.active(c, e.player)
        assert c.execute("SELECT count(*) FROM character_movements WHERE character_id=?", (e.player,)).fetchone()[0] == 0


def test_restart_does_not_resume_old_direction_input(town, monkeypatch):
    e = town
    input_clock(e, monkeypatch)
    scene = steer(e, 100, dx=1)
    e.clock.reset_offline_baseline()
    e.clock.heartbeat(e.wid, elapsed_seconds=30)
    after = e.client.get(f"/api/worlds/{e.wid}/game-scene").json()
    assert after["player"]["longitude"] == scene["player"]["longitude"]
    assert not after["steering"]["active"]
