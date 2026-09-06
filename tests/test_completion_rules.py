"""覆盖审查中复现的越界、数量丢失、超时和履约缺口。"""

import json
import threading
import time
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from world_engine.actions import ActionService
from world_engine.api import create_app
from world_engine.contracts import ContractError, ContractService
from world_engine.database import Database
from world_engine.domain import ActionProposal, ActionType
from world_engine.engine import WorldEngine
from world_engine.intent_effects import IntentEffectService
from world_engine.inventory import InventoryError, InventoryService
from world_engine.movement import MovementService
from world_engine.orchestration import MemoryCuratorAgent, ProposalCoordinator
from world_engine.repository import WorldRepository, from_iso, to_iso, utc_now


@pytest.fixture
def world(database):
    repo = WorldRepository()
    with database.write() as connection:
        wid = repo.create_world(
            connection, name="完善回归世界", minutes_per_tick=60, seed_demo=True
        )
        snapshot = repo.get_snapshot(connection, wid)
        actor, merchant = snapshot.characters[:2]
        connection.execute("UPDATE characters SET money=100,health=30 WHERE world_id=?", (wid,))
        connection.execute("UPDATE characters SET identity='商人' WHERE id=?", (merchant.id,))
        connection.execute(
            """INSERT INTO item_types(id,name,category,stack_limit,usable,heal)
               VALUES ('test-potion','测试药剂','consumable',3,1,10)"""
        )
    return wid, actor.id, merchant.id


def add_item(connection, world, holder, quantity=1, **kwargs):
    iid = str(uuid4())
    connection.execute(
        """INSERT INTO item_instances(id,world_id,item_type_id,container_id,container_type,
           quantity,condition) VALUES (?,?,'test-potion',?,?,?,?)""",
        (
            iid,
            world,
            holder,
            kwargs.get("container", "character_inventory"),
            quantity,
            kwargs.get("condition", 100),
        ),
    )
    return iid


def event(connection, world, actor=None, target=None, event_type="action.work", **kwargs):
    return ActionService._record_event(
        connection,
        world_id=world,
        tick_id=str(uuid4()),
        occurred_at=kwargs.get("at", utc_now()),
        event_type=event_type,
        actor_id=actor,
        target_id=target,
        location_id=kwargs.get("location"),
        summary="可核验事件",
        payload=kwargs.get("payload", {}),
    )


def test_stack_consumption_is_one_and_audited(database, world):
    wid, actor, _ = world
    with database.write() as connection:
        iid = add_item(connection, wid, actor, 3)
        outcome = ActionService().execute(
            connection,
            world_id=wid,
            tick_id="use",
            occurred_at=utc_now(),
            proposal=ActionProposal(
                actor_id=actor,
                action=ActionType.USE,
                reason="使用药剂",
                metadata={"item": "测试药剂"},
            ),
        )
        assert outcome.accepted
        assert (
            connection.execute("SELECT quantity FROM item_instances WHERE id=?", (iid,)).fetchone()[
                0
            ]
            == 2
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM inventory_changes WHERE source_event_id=?",
                (outcome.event_id,),
            ).fetchone()[0]
            == 1
        )


def test_purchase_requires_stock_and_space_preserves_money(database, world):
    wid, actor, seller = world
    service = IntentEffectService()
    with database.write() as connection:
        eid = event(connection, wid, actor, seller)
        match = service._PURCHASE.search("支付1铜币购买测试药剂")
        assert not service._purchase(connection, wid, eid, actor, seller, match).applied
        add_item(connection, wid, actor, 3, condition=80)
        add_item(connection, wid, actor, 3, condition=90)
        stock = add_item(connection, wid, seller, 3)
        assert not service._purchase(connection, wid, eid, actor, seller, match).applied
        assert (
            connection.execute("SELECT money FROM characters WHERE id=?", (actor,)).fetchone()[0]
            == 100
        )
        assert (
            connection.execute(
                "SELECT quantity FROM item_instances WHERE id=?", (stock,)
            ).fetchone()[0]
            == 3
        )


def test_purchase_transfers_one_real_unit_and_owner(database, world):
    wid, actor, seller = world
    with database.write() as connection:
        iid = add_item(connection, wid, seller, 3)
        service = IntentEffectService()
        eid = event(connection, wid, actor, seller)
        assert service._purchase(
            connection, wid, eid, actor, seller, service._PURCHASE.search("支付1铜币购买测试药剂")
        ).applied
        assert (
            connection.execute("SELECT quantity FROM item_instances WHERE id=?", (iid,)).fetchone()[
                0
            ]
            == 2
        )
        item = InventoryService.find(connection, wid, actor, "测试药剂")
        assert item["quantity"] == 1 and item["owner_character_id"] == actor
        assert (
            connection.execute(
                "SELECT SUM(quantity) FROM item_instances WHERE world_id=?", (wid,)
            ).fetchone()[0]
            == 3
        )


def test_inventory_slot_size_is_enforced(database, world):
    wid, actor, seller = world
    with database.write() as connection:
        connection.execute("UPDATE item_types SET slot_size=3 WHERE id='test-potion'")
        add_item(connection, wid, seller)
        item = InventoryService.find(connection, wid, seller, "测试药剂")
        with pytest.raises(InventoryError):
            InventoryService.transfer(connection, item, actor)


def test_memory_requires_event_evidence_even_if_currently_visible():
    observer = SimpleNamespace(id="observer", location_id="here")
    ctx = SimpleNamespace(
        snapshot=SimpleNamespace(
            character_by_id=lambda cid: observer if cid == "observer" else None
        ),
        scene=SimpleNamespace(visible_characters=[observer]),
    )
    data = {
        "id": "event",
        "event_type": "action.work",
        "actor_id": "away",
        "summary": "异地事件",
        "location_id": "elsewhere",
    }
    curator = MemoryCuratorAgent()
    assert curator._run_rules(ctx, [data]) == []
    data["payload"] = {"witness_character_ids": ["observer"]}
    assert len(curator._run_rules(ctx, [data])) == 1


def test_single_and_parallel_timeout_return_without_waiting_for_thread():
    gate = threading.Event()
    coordinator = object.__new__(ProposalCoordinator)
    coordinator.timeout_seconds = 0.04
    coordinator.max_concurrency = 2
    try:
        started = time.monotonic()
        result = coordinator._run_single(lambda: gate.wait(2))
        assert result[1] == "timed_out"
        assert time.monotonic() - started < 0.6
        started = time.monotonic()
        results = coordinator._run_parallel(2, lambda _: gate.wait(2))
        assert all(result[1] == "timed_out" for result in results)
        assert time.monotonic() - started < 0.6
    finally:
        gate.set()


def test_memory_job_failure_retry_and_idempotency(database, settings, world, monkeypatch):
    wid, actor, _ = world
    engine = WorldEngine(database, settings)
    with database.write() as connection:
        connection.execute("DELETE FROM memory_jobs")
        eid = event(connection, wid, actor)
    original = MemoryCuratorAgent.run

    def fail(*args, **kwargs):
        raise RuntimeError("暂时失败")

    monkeypatch.setattr(MemoryCuratorAgent, "run", fail)
    assert engine.process_memory_jobs(wid, limit=1) == 0
    with database.write() as connection:
        job = connection.execute("SELECT * FROM memory_jobs WHERE event_id=?", (eid,)).fetchone()
        assert job["status"] == "failed" and job["attempt_count"] == 1
        connection.execute("UPDATE memory_jobs SET retry_at=NULL WHERE event_id=?", (eid,))
    monkeypatch.setattr(MemoryCuratorAgent, "run", original)
    assert engine.process_memory_jobs(wid, limit=1) == 1
    assert engine.process_memory_jobs(wid, limit=1) == 0
    with database.read() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM character_memories WHERE event_id=?", (eid,)
            ).fetchone()[0]
            == 1
        )


def start_contract(connection, world, kind, terms):
    wid, player, npc = world
    rid = str(uuid4())
    now = connection.execute('SELECT "current_time" FROM worlds WHERE id=?', (wid,)).fetchone()[0]
    connection.execute(
        """INSERT INTO long_term_operation_requests(id,world_id,requester_id,recipient_id,
           operation_type,terms_json,status,created_at,updated_at)
           VALUES (?,?,?,?,?,?,'npc_accepted',?,?)""",
        (rid, wid, player, npc, kind, json.dumps(terms), to_iso(utc_now()), to_iso(utc_now())),
    )
    request = connection.execute(
        "SELECT * FROM long_term_operation_requests WHERE id=?", (rid,)
    ).fetchone()
    ContractService.start(connection, request, terms, now)
    return connection.execute(
        "SELECT * FROM contract_fulfillments WHERE request_id=?", (rid,)
    ).fetchone()


@pytest.mark.parametrize("direction", ["borrow", "lend"])
def test_loan_direction_repayment_and_duplicate_guard(database, world, direction):
    wid, player, npc = world
    with database.write() as connection:
        contract = start_contract(
            connection, world, "借贷", {"payment": 10, "direction": direction}
        )
        assert connection.execute("SELECT money FROM characters WHERE id=?", (player,)).fetchone()[
            0
        ] == (110 if direction == "borrow" else 90)
        ContractService.repay(connection, contract, contract["due_world_time"])
        assert (
            connection.execute("SELECT money FROM characters WHERE id=?", (player,)).fetchone()[0]
            == 100
        )
        updated = connection.execute(
            "SELECT * FROM contract_fulfillments WHERE request_id=?", (contract["request_id"],)
        ).fetchone()
        with pytest.raises(ContractError):
            ContractService.repay(connection, updated, contract["due_world_time"])


def test_overdue_debt_cannot_disappear_and_retries_when_funded(database, world):
    wid, player, npc = world
    with database.write() as connection:
        contract = start_contract(connection, world, "借贷", {"payment": 10})
        connection.execute("UPDATE characters SET money=0 WHERE id=?", (player,))
        assert ContractService.advance(connection, wid, contract["due_world_time"]) == 1
        with pytest.raises(ContractError):
            ContractService.cancel(connection, contract, contract["due_world_time"])
        connection.execute("UPDATE characters SET money=10 WHERE id=?", (player,))
        ContractService.advance(connection, wid, contract["due_world_time"])
        assert (
            connection.execute(
                "SELECT principal FROM contract_fulfillments WHERE request_id=?",
                (contract["request_id"],),
            ).fetchone()[0]
            == 0
        )


def test_work_escrow_needs_actual_evidence_and_pays_once(database, world):
    wid, player, npc = world
    with database.write() as connection:
        contract = start_contract(
            connection, world, "委托", {"payment": 8, "fulfillment_action": "work", "work_units": 2}
        )
        now = from_iso(contract["started_world_time"]) + timedelta(hours=1)
        assert ContractService.advance(connection, wid, to_iso(now)) == 0
        assert (
            connection.execute("SELECT money FROM characters WHERE id=?", (npc,)).fetchone()[0]
            == 100
        )
        # 出发或仍在路上的工作事件，不能作为实际工作完成的证据。
        event(connection, wid, npc, at=now, location=contract["location_id"])
        assert ContractService.advance(connection, wid, to_iso(now)) == 0
        for _ in range(2):
            event(
                connection,
                wid,
                npc,
                at=now,
                location=contract["location_id"],
                payload={"work_completed": True},
            )
        assert ContractService.advance(connection, wid, to_iso(now)) == 1
        assert ContractService.advance(connection, wid, to_iso(now)) == 0
        assert (
            connection.execute("SELECT money FROM characters WHERE id=?", (npc,)).fetchone()[0]
            == 108
        )


def test_unfinished_work_refunds_escrow(database, world):
    wid, player, _ = world
    with database.write() as connection:
        contract = start_contract(
            connection, world, "委托", {"payment": 8, "fulfillment_action": "work"}
        )
        ContractService.cancel(connection, contract, contract["started_world_time"])
        assert (
            connection.execute("SELECT money FROM characters WHERE id=?", (player,)).fetchone()[0]
            == 100
        )


def test_migration_preserves_existing_owner_and_quantity(database, world):
    wid, player, npc = world
    with database.write() as connection:
        iid = add_item(connection, wid, player, 3)
        connection.execute("UPDATE item_instances SET owner_character_id=? WHERE id=?", (npc, iid))
    database.initialize()
    with database.read() as connection:
        row = connection.execute("SELECT * FROM item_instances WHERE id=?", (iid,)).fetchone()
        assert row["quantity"] == 3 and row["owner_character_id"] == npc


def test_lease_exclusive_access_and_expiry_restore_walking(database, world):
    wid, player, npc = world
    with database.write() as connection:
        now = to_iso(utc_now())
        connection.execute(
            """INSERT INTO vehicles(id,world_id,name,movement_type,speed_kmh,
               owner_character_id,created_at,updated_at)
               VALUES ('rental',?,'马车','land',30,?,?,?)""",
            (wid, npc, now, now),
        )
        contract = start_contract(connection, world, "租赁", {"vehicle_id": "rental", "payment": 5})
        assert ContractService.vehicle_for(connection, wid, npc, "rental") is None
        MovementService().select_transport(
            connection, world_id=wid, character_id=player, vehicle_id="rental"
        )
        assert ContractService.advance(connection, wid, contract["due_world_time"]) == 1
        assert ContractService.vehicle_for(connection, wid, player, "rental") is None
        actor = connection.execute("SELECT * FROM characters WHERE id=?", (player,)).fetchone()
        assert actor["active_vehicle_id"] is None and actor["movement_speed_kmh"] == 5
        assert ContractService.vehicle_for(connection, wid, npc, "rental") is not None


def test_npc_review_releases_write_lock_and_rechecks_version(settings, monkeypatch):
    class Backend:
        name = "concurrent-review-test"

        def complete(self, *, schema, **kwargs):
            # 在模型调用期间模拟另一条真实写请求；旧代码在此持有写锁。
            with Database(settings.database_path).write() as connection:
                connection.execute("UPDATE worlds SET version=version+1")
            return SimpleNamespace(data=schema.validate_python({"reply": "同意商议。"}))

        def close(self):
            return None

    monkeypatch.setattr("world_engine.engine.build_agent_model_backend", lambda _: Backend())
    with TestClient(create_app(settings)) as client:
        snapshot = client.post("/api/worlds", json={"name": "并发回复"}).json()
        wid = snapshot["world"]["id"]
        npc = snapshot["characters"][0]
        client.post(
            f"/api/worlds/{wid}/player", json={"name": "旅人", "location_id": npc["location_id"]}
        )
        response = client.post(
            f"/api/worlds/{wid}/long-term-requests",
            json={
                "recipient_id": npc["id"],
                "operation_type": "约定",
                "terms": {"payment": 0},
            },
        )
        assert response.status_code == 409
        assert client.get(f"/api/worlds/{wid}/long-term-requests").json() == []


def test_loan_api_confirmation_list_and_repayment(settings, monkeypatch):
    class Backend:
        name = "loan-review-test"

        def complete(self, *, schema, **kwargs):
            return SimpleNamespace(data=schema.validate_python({"reply": "我们按条款来办。"}))

        def close(self):
            return None

    monkeypatch.setattr("world_engine.engine.build_agent_model_backend", lambda _: Backend())
    with TestClient(create_app(settings)) as client:
        snapshot = client.post("/api/worlds", json={"name": "借贷全流程"}).json()
        wid = snapshot["world"]["id"]
        npc = snapshot["characters"][0]
        created = client.post(
            f"/api/worlds/{wid}/player",
            json={
                "name": "旅人",
                "location_id": npc["location_id"],
            },
        ).json()
        player = next(row for row in created["characters"] if row["is_player"])
        review = client.post(
            f"/api/worlds/{wid}/long-term-requests",
            json={
                "recipient_id": npc["id"],
                "operation_type": "借贷",
                "terms": {"payment": 3, "direction": "borrow", "duration_days": 7},
            },
        ).json()
        rid = review["id"]
        response = client.post(
            f"/api/worlds/{wid}/long-term-requests/{rid}/confirm",
            json={"accept_counter_terms": True},
        )
        assert response.status_code == 200, response.text
        contract = client.get(f"/api/worlds/{wid}/long-term-requests").json()[0]["fulfillment"]
        assert contract["status"] == "active"
        assert from_iso(contract["due_world_time"]) - from_iso(
            contract["started_world_time"]
        ) == timedelta(days=7)
        assert contract["borrower_id"] == player["id"]
        response = client.post(f"/api/worlds/{wid}/long-term-requests/{rid}/repay")
        assert response.status_code == 200
        assert client.post(f"/api/worlds/{wid}/long-term-requests/{rid}/repay").status_code == 409
        with Database(settings.database_path).read() as connection:
            assert (
                connection.execute(
                    "SELECT money FROM characters WHERE id=?", (player["id"],)
                ).fetchone()[0]
                == player["money"]
            )
