"""哥哥，接口只允许当前玩家操作自己的实际燃料。"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_vehicle_fuel_inventory import fuel_inventory, make_fuel  # noqa: F401
from test_vehicle_fuel_profile import fuel_world  # noqa: F401
from test_rides import ride_world  # noqa: F401
from world_engine.api_vehicle_fuel import build_vehicle_fuel_router


def client(e):
    app = FastAPI()
    app.include_router(build_vehicle_fuel_router(e.db))
    return TestClient(app)


def test_player_http_load_retry_unload_and_private_options(fuel_inventory):
    e = fuel_inventory
    api = client(e)
    prefix = f"/api/worlds/{e.wid}/vehicle-fuel"
    options = api.get(prefix + "/options")
    assert options.status_code == 200
    vehicle = options.json()["vehicles"][0]
    assert vehicle["vehicle_id"] == e.vehicle and vehicle["loadable"][0]["id"] == e.item
    payload = {"operation": "load", "vehicle_id": e.vehicle, "item_id": e.item,
               "quantity": 3, "idempotency_key": "player-load"}
    first = api.post(prefix + "/transfer", json=payload)
    assert first.status_code == 200 and first.json()["status"] == "applied"
    repeat = api.post(prefix + "/transfer", json=payload)
    assert repeat.status_code == 200 and repeat.json()["status"] == "already"
    assert repeat.json()["event_id"] == first.json()["event_id"]
    with e.db.read() as c:
        assert c.execute("SELECT quantity FROM item_instances WHERE id=?", (e.item,)).fetchone()[0] == 7
    loaded_id = first.json()["moved_item_id"]
    result = api.post(prefix + "/transfer", json={**payload, "operation": "unload", "item_id": loaded_id,
                                                       "idempotency_key": "player-unload"})
    assert result.status_code == 200 and result.json()["status"] == "applied"


def test_player_http_rejects_forged_actor_and_foreign_item(fuel_inventory):
    e = fuel_inventory
    with e.db.write() as c:
        foreign = make_fuel(c, e, e.npc, 2)
    api = client(e)
    url = f"/api/worlds/{e.wid}/vehicle-fuel/transfer"
    payload = {"operation": "load", "vehicle_id": e.vehicle, "item_id": foreign,
               "quantity": 1, "idempotency_key": "foreign"}
    assert api.post(url, json={**payload, "actor_id": e.npc}).status_code == 422
    assert api.post(url, json={**payload, "quantity": True}).status_code == 422
    assert api.post(url, json=payload).status_code == 409
    with e.db.read() as c:
        assert c.execute("SELECT quantity FROM item_instances WHERE id=?", (foreign,)).fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM vehicle_fuel_inventory_receipts").fetchone()[0] == 0
