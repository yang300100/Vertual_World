"""哥哥，编年者页面只引用当前世界的已有事实并保留确认步骤。"""
from datetime import timedelta
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_rides import ride_world  # noqa: F401
from test_vehicle_fuel_profile import fuel_world  # noqa: F401

from world_engine.api_elements import build_elements_router
from world_engine.api_vehicle_fuel_profile_options import build_vehicle_fuel_profile_options_router
from world_engine.repository import to_iso


def client(e):
    app = FastAPI()
    app.include_router(build_elements_router(e.db))
    app.include_router(build_vehicle_fuel_profile_options_router(e.db))
    return TestClient(app)


def test_options_offer_actual_source_and_complete_submit_confirm(fuel_world):
    e = fuel_world
    api = client(e)
    prefix = f"/api/worlds/{e.wid}"
    with e.db.read() as c:
        version = c.execute("SELECT version FROM worlds WHERE id=?", (e.wid,)).fetchone()[0]
        prior = c.total_changes
    options = api.get(prefix + "/vehicle-fuel/profile-options")
    assert options.status_code == 200
    payload = options.json()
    assert payload["vehicles"], payload
    vehicle = next(v for v in payload["vehicles"] if v["id"] == e.vehicle)
    assert e.source in [s["id"] for s in vehicle["sources"]]
    assert any(i["id"] == e.item_type for i in payload["item_types"])
    assert all(s["location_id"] == e.origin for s in vehicle["sources"])
    with e.db.read() as c:
        assert c.total_changes == prior == 0
        assert c.execute("SELECT version FROM worlds WHERE id=?", (e.wid,)).fetchone()[0] == version
    body = {"source_event_id": e.source, "idempotency_key": "fuel-profile:interface-test",
            "payload": {"element_type": "vehicle_fuel_profile", "vehicle_id": e.vehicle,
                        "item_type_id": e.item_type, "capacity_units": 12,
                        "rate_numerator": 3, "rate_denominator": 10}}
    response = api.post(prefix + "/registrations", json=body)
    assert response.status_code == 201
    proposal = response.json()
    assert proposal["status"] == "proposed" and proposal["source_event_id"] == e.source
    assert proposal["payload"] == body["payload"]
    with e.db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM vehicle_fuel_profiles").fetchone()[0] == 0
    confirmed = api.post(prefix + "/registrations/" + proposal["id"] + "/confirm")
    assert confirmed.status_code == 200 and confirmed.json()["status"] == "applied", confirmed.json()
    assert api.get(prefix + "/vehicle-fuel/profile-options").json()["vehicles"] == []


def test_options_exclude_future_source_and_in_use_vehicle(fuel_world):
    e = fuel_world
    api = client(e)
    url = f"/api/worlds/{e.wid}/vehicle-fuel/profile-options"
    with e.db.write() as c:
        c.execute("UPDATE world_events SET occurred_at=? WHERE id=?", (to_iso(e.at + timedelta(hours=1)), e.source))
    choices = api.get(url).json()["vehicles"]
    assert all(e.source not in [s["id"] for s in v["sources"]] for v in choices)
    with e.db.write() as c:
        c.execute("UPDATE characters SET active_vehicle_id=? WHERE id=?", (e.vehicle, e.player))
    assert api.get(url).json()["vehicles"] == []


def test_options_reject_same_time_source_before_vehicle_confirmation(fuel_world):
    e = fuel_world
    api = client(e)
    with e.db.write() as c:
        registration = c.execute("SELECT registration_id FROM vehicle_states WHERE vehicle_id=?", (e.vehicle,)).fetchone()[0]
        before = c.execute("SELECT source_event_id FROM element_registration_requests WHERE id=?", (registration,)).fetchone()[0]
        c.execute("UPDATE world_events SET payload_json=? WHERE id=?", (json.dumps({"vehicle_id": e.vehicle}), before))
    result = api.get(f"/api/worlds/{e.wid}/vehicle-fuel/profile-options").json()
    vehicle = next(v for v in result["vehicles"] if v["id"] == e.vehicle)
    assert before not in [source["id"] for source in vehicle["sources"]]
    assert e.source in [source["id"] for source in vehicle["sources"]]


def test_options_exclude_commodity_without_current_confirmation(fuel_world):
    e = fuel_world
    api = client(e)
    with e.db.write() as c:
        reg = c.execute("SELECT registration_id FROM world_item_profiles WHERE world_id=? AND item_type_id=?", (e.wid, e.item_type)).fetchone()[0]
        c.execute("UPDATE world_events SET occurred_at=? WHERE world_id=? AND tick_id=? AND event_type='world.element_registered'", (to_iso(e.at + timedelta(hours=1)), e.wid, reg))
    result = api.get(f"/api/worlds/{e.wid}/vehicle-fuel/profile-options").json()
    assert result["vehicles"] and e.item_type not in [item["id"] for item in result["item_types"]]


def test_actual_profile_interface_and_review_scripts():
    node = shutil.which("node")
    if not node:
        pytest.skip("本机没有 Node，无法验证真实前端脚本")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node, str(root / "tests/fixtures/vehicle_fuel_profile_ui_vm.cjs")],
                            cwd=root, capture_output=True, encoding="utf-8", check=True)
    assert json.loads(result.stdout.strip()) == {"status": "verified", "cases": 6, "method": "actual_UI_VM_mocked_API_no_browser"}
