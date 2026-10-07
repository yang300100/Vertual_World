"""哥哥，行动权限和位置不能只因为写了计划便被绕过。"""

from uuid import uuid4

import pytest
from test_collective_research import lab, research  # noqa: F401

from world_engine.materials import MaterialService as M


def test_moving_recipient_cannot_read_mail_at_home(lab):
    db, wid, actors, _, _, at = lab
    from datetime import timedelta

    from world_engine.collective_messages import MessageService
    from world_engine.movement import MovementService

    with db.write() as c:
        c.execute("INSERT INTO collective_contacts VALUES (?,?,.25)", (actors[0], actors[1]))
        mid = MessageService.send(c, actors[0], actors[1], at, text="送到家中的报告")
        MovementService().start(
            c,
            world_id=wid,
            character_id=actors[1],
            destination_longitude=2,
            destination_latitude=0,
            world_time=at,
        )
        c.execute("UPDATE characters SET current_location_id=NULL WHERE id=?", (actors[1],))
        MessageService.deliver(c, wid, at + timedelta(hours=1))
        assert (
            c.execute("SELECT delivered_at FROM collective_messages WHERE id=?", (mid,)).fetchone()[
                0
            ]
            is None
        )
        assert not c.execute(
            "SELECT 1 FROM collective_observations WHERE character_id=? AND kind='received_report'",
            (actors[1],),
        ).fetchone()


def test_collection_respects_real_inventory_capacity(lab):
    db, _, actors, batches, _, at = lab
    with db.write() as c:
        loc = c.execute(
            "SELECT lab_location_id FROM collective_people WHERE character_id=?", (actors[0],)
        ).fetchone()[0]
        item = c.execute("SELECT * FROM item_instances WHERE id=?", (batches[0],)).fetchone()
        output = M.move(c, item, 1, "ground", loc, actors[0])
        c.execute("UPDATE characters SET inventory_capacity=0 WHERE id=?", (actors[0],))
        with pytest.raises(ValueError, match="容量"):
            research().submit(
                c,
                actors[0],
                {
                    "reason": "本人收取实物",
                    "commands": [{"op": "collect", "batch_id": output, "quantity": 1}],
                },
                at,
            )
        assert (
            c.execute("SELECT container_type FROM item_instances WHERE id=?", (output,)).fetchone()[
                0
            ]
            == "ground"
        )


def test_npc_journey_does_not_teleport_or_start_next_command_early(lab):
    db, wid, actors, _, _, at = lab
    with db.write() as c:
        destination = str(uuid4())
        c.execute(
            "INSERT INTO locations(id,world_id,name,kind,longitude,latitude) VALUES (?,?,'邻近工坊','city',.02,0)",
            (destination, wid),
        )
        c.execute(
            "UPDATE collective_people SET lab_location_id=? WHERE character_id=?",
            (destination, actors[1]),
        )
        c.execute("INSERT INTO collective_contacts VALUES (?,?,2)", (actors[0], actors[1]))
        pid = research().submit(
            c,
            actors[0],
            {
                "reason": "本人决定前往同行工坊",
                "commands": [{"op": "move", "location_id": destination}, {"op": "inspect"}],
            },
            at,
        )
        assert (
            c.execute("SELECT longitude FROM characters WHERE id=?", (actors[0],)).fetchone()[0]
            == 0
        )
        assert (
            c.execute("SELECT COUNT(*) FROM character_movements WHERE status='moving'").fetchone()[
                0
            ]
            == 1
        )
        assert c.execute("SELECT COUNT(*) FROM collective_jobs").fetchone()[0] == 0
        assert (
            c.execute("SELECT status FROM collective_programs WHERE id=?", (pid,)).fetchone()[0]
            == "active"
        )


def test_bulk_ground_handover_changes_ownership_without_putting_it_in_a_backpack(lab):
    db, wid, actors, batches, _, at = lab
    with db.write() as c:
        loc = c.execute(
            "SELECT lab_location_id FROM collective_people WHERE character_id=?", (actors[0],)
        ).fetchone()[0]
        c.execute(
            "UPDATE item_instances SET container_type='ground',container_id=? WHERE id=?",
            (loc, batches[0]),
        )
        research().submit(
            c,
            actors[0],
            {
                "reason": "本人在现场把这一堆材料交给同行",
                "commands": [
                    {
                        "op": "give",
                        "batch_id": batches[0],
                        "quantity": 100,
                        "recipients": [actors[1]],
                    }
                ],
            },
            at,
        )
        item = c.execute("SELECT * FROM item_instances WHERE id=?", (batches[0],)).fetchone()
        assert item["owner_character_id"] == actors[1] and item["container_type"] == "ground"
        assert M.audit(c, wid)["balanced"]


def test_public_ration_permission_does_not_grant_private_property_or_farming(lab):
    db, wid, actors, _, _, at = lab
    from test_collective_regions import material, region, region_service

    from world_engine.collective_production import ProductionService as P
    from world_engine.collective_types import CollectiveCommand
    from world_engine.material_physics import MaterialState

    with db.write() as c:
        rid, *_ = region(c, wid, actors[0], at)
        food = material(
            c,
            wid,
            actors[1],
            MaterialState(carrier="grain", mass=10, nutrition=200),
            10,
            category="food",
        )
        with pytest.raises(ValueError, match="权限"):
            P.ration(c, actors[1], rid, food, 10, at)
        region_service().grant(c, actors[0], rid, actors[1], ["ration"], "普通日常粮仓管理许可", at)
        P.ration(c, actors[1], rid, food, 10, at)
        assert (
            c.execute("SELECT container_type FROM item_instances WHERE id=?", (food,)).fetchone()[0]
            == "region_food"
        )
        seed = material(c, wid, actors[1], MaterialState(carrier="seed", mass=5, nutrition=100), 5)
        with pytest.raises(ValueError, match="权限"):
            P.start(
                c,
                actors[1],
                CollectiveCommand(op="plant", region_id=rid, batch_id=seed, quantity=5, hours=1),
                at,
            )
        view = research().context(c, actors[1], at)
        assert view["regions"][0]["permissions"] == ["ration"]


def test_collecting_bulk_respects_configured_actual_carrying_mass(lab):
    db, wid, actors, batches, _, at = lab
    with db.write() as c:
        loc = c.execute(
            "SELECT lab_location_id FROM collective_people WHERE character_id=?", (actors[0],)
        ).fetchone()[0]
        c.execute(
            "UPDATE item_instances SET container_type='ground',container_id=? WHERE id=?",
            (loc, batches[0]),
        )
        c.execute("INSERT INTO collective_carry_limits VALUES (?,10)", (actors[0],))
        with pytest.raises(ValueError, match="负重"):
            research().submit(
                c,
                actors[0],
                {
                    "reason": "背包槽位够但不能搬起整仓原粮",
                    "commands": [{"op": "collect", "batch_id": batches[0], "quantity": 100}],
                },
                at,
            )
        assert (
            c.execute(
                "SELECT container_type FROM item_instances WHERE id=?", (batches[0],)
            ).fetchone()[0]
            == "ground"
        )


def test_postal_route_has_finite_actual_cargo_load(lab):
    db, _, actors, batches, _, at = lab
    from world_engine.collective_messages import MessageService

    with db.write() as c:
        c.execute("INSERT INTO collective_contacts VALUES (?,?,2)", (actors[0], actors[1]))
        c.execute("INSERT INTO collective_route_limits VALUES (?,?,2)", (actors[0], actors[1]))
        with pytest.raises(ValueError, match="载荷"):
            MessageService.send(
                c,
                actors[0],
                actors[1],
                at,
                text="信使不能搬走整仓原粮",
                batch_id=batches[0],
                quantity=100,
            )
        mid = MessageService.send(
            c, actors[0], actors[1], at, text="仅寄小样", batch_id=batches[0], quantity=1
        )
        assert c.execute("SELECT cargo_id FROM collective_messages WHERE id=?", (mid,)).fetchone()[
            0
        ]


def test_food_policy_cannot_be_erased_by_merging_into_allowed_batch(lab):
    db, _, actors, batches, _, at = lab
    from world_engine.repository import to_iso

    with db.write() as c:
        new = M.split(
            c,
            c.execute("SELECT * FROM item_instances WHERE id=?", (batches[0],)).fetchone(),
            1,
            str(uuid4()),
        )
        c.execute(
            "INSERT INTO collective_food_decisions VALUES (?,?,?,?,?)",
            (actors[0], new, "avoid", "本人保留隔离样品", to_iso(at)),
        )
        with pytest.raises(ValueError, match="合并"):
            M.merge(
                c,
                c.execute("SELECT * FROM item_instances WHERE id=?", (new,)).fetchone(),
                c.execute("SELECT * FROM item_instances WHERE id=?", (batches[0],)).fetchone(),
                1,
            )


def test_work_uses_explicit_actual_location_after_journey(lab):
    db, wid, actors, batches, _, at = lab
    from test_collective_cooperation import cooperation

    with db.write() as c:
        actual = c.execute(
            "SELECT lab_location_id FROM collective_people WHERE character_id=?", (actors[0],)
        ).fetchone()[0]
        other = str(uuid4())
        c.execute(
            "INSERT INTO locations(id,world_id,name,kind,longitude,latitude) VALUES (?,?,'本人远方原工坊','city',1,1)",
            (other, wid),
        )
        c.execute(
            "UPDATE collective_people SET lab_location_id=? WHERE character_id=?",
            (other, actors[0]),
        )
        s = cooperation()
        pid = s.propose(
            c,
            actors[0],
            {
                "reason": "已旅行抵达后的实际现场工作",
                "tasks": [
                    {
                        "key": "here",
                        "worker_id": actors[0],
                        "location_id": actual,
                        "operation": "store",
                        "batch_id": batches[0],
                        "quantity": 1,
                        "hours": 1,
                    }
                ],
            },
            at,
        )
        s.accept(
            c,
            actors[0],
            {"proposal_id": pid, "version": 1, "task_keys": ["here"], "reason": "本人审阅并同意"},
            at,
        )
        s.start(c, actors[0], {"proposal_id": pid, "version": 1}, at)
        assert (
            c.execute(
                "SELECT status FROM collective_work_orders WHERE proposal_id=?", (pid,)
            ).fetchone()[0]
            == "working"
        )


def test_region_permission_cannot_instantly_notify_a_distant_recipient(lab):
    db, wid, actors, _, _, at = lab
    from test_collective_regions import region, region_service

    with db.write() as c:
        rid, *_ = region(c, wid, actors[0], at)
        other = str(uuid4())
        c.execute(
            "INSERT INTO locations(id,world_id,name,kind,longitude,latitude) VALUES (?,?,'远方工坊','city',1,1)",
            (other, wid),
        )
        c.execute("UPDATE characters SET current_location_id=? WHERE id=?", (other, actors[1]))
        with pytest.raises(ValueError, match="现场|实际"):
            region_service().grant(
                c, actors[0], rid, actors[1], ["ration"], "应通过真实交流传达许可", at
            )
        assert not c.execute(
            "SELECT 1 FROM collective_region_permissions WHERE actor_id=?", (actors[1],)
        ).fetchone()


def test_actual_instrument_can_be_handed_over_at_the_shared_site(lab):
    db, _, actors, _, tool, at = lab
    with db.write() as c:
        research().submit(
            c,
            actors[0],
            {
                "reason": "本人把完整真实测量器交给同行",
                "commands": [{"op": "give", "batch_id": tool, "recipients": [actors[1]]}],
            },
            at,
        )
        item = c.execute("SELECT * FROM item_instances WHERE id=?", (tool,)).fetchone()
        assert item["owner_character_id"] == actors[1] and item["container_type"] == "ground"
        assert (
            c.execute(
                "SELECT COUNT(*) FROM collective_instruments WHERE item_id=?", (tool,)
            ).fetchone()[0]
            == 1
        )


def test_postal_instrument_transport_preserves_identity_and_waits_for_delivery(lab):
    db, wid, actors, _, tool, at = lab
    from datetime import timedelta

    from world_engine.collective_messages import MessageService

    with db.write() as c:
        c.execute("INSERT INTO collective_contacts VALUES (?,?,2)", (actors[0], actors[1]))
        mid = MessageService.send(
            c, actors[0], actors[1], at, text="借用这台完整检验器", batch_id=tool, quantity=1
        )
        assert (
            c.execute("SELECT container_type FROM item_instances WHERE id=?", (tool,)).fetchone()[0]
            == "collective_transit"
        )
        MessageService.deliver(c, wid, at + timedelta(hours=2))
        item = c.execute("SELECT * FROM item_instances WHERE id=?", (tool,)).fetchone()
        assert item["owner_character_id"] == actors[1] and item["container_type"] == "ground"
        assert (
            c.execute("SELECT cargo_id FROM collective_messages WHERE id=?", (mid,)).fetchone()[0]
            is None
        )


def test_traveler_can_eat_carried_food_without_teleporting_to_a_store(lab):
    db, wid, actors, _, _, at = lab
    from world_engine.collective_clock import basic_life
    from world_engine.movement import MovementService

    with db.write() as c:
        c.execute("INSERT INTO material_modes VALUES (?,?)", (wid, '{"mode":"collective"}'))
        c.execute("UPDATE characters SET satiety=60 WHERE id=?", (actors[0],))
        MovementService().start(
            c,
            world_id=wid,
            character_id=actors[0],
            destination_longitude=2,
            destination_latitude=0,
            world_time=at,
        )
        basic_life(c, wid, at)
        assert (
            c.execute("SELECT satiety FROM characters WHERE id=?", (actors[0],)).fetchone()[0] > 60
        )
        assert (
            c.execute(
                "SELECT COUNT(*) FROM character_movements WHERE character_id=? AND status='moving'",
                (actors[0],),
            ).fetchone()[0]
            == 1
        )


def test_traveler_cannot_fetch_a_portion_from_the_home_store(lab):
    db, wid, actors, batches, _, at = lab
    from world_engine.collective_clock import basic_life
    from world_engine.movement import MovementService

    with db.write() as c:
        c.execute("INSERT INTO material_modes VALUES (?,?)", (wid, '{"mode":"collective"}'))
        loc = c.execute(
            "SELECT lab_location_id FROM collective_people WHERE character_id=?", (actors[0],)
        ).fetchone()[0]
        c.execute(
            "UPDATE item_instances SET container_type='ground',container_id=? WHERE id=?",
            (loc, batches[0]),
        )
        c.execute(
            "INSERT INTO collective_food_decisions VALUES (?,?,?,?,?)",
            (actors[0], batches[0], "allow", "仅允许从实际本地粮柜取饭", at.isoformat()),
        )
        MovementService().start(
            c,
            world_id=wid,
            character_id=actors[0],
            destination_longitude=2,
            destination_latitude=0,
            world_time=at,
        )
        c.execute(
            "UPDATE characters SET satiety=60,longitude=1,current_location_id=NULL WHERE id=?",
            (actors[0],),
        )
        basic_life(c, wid, at)
        assert (
            c.execute("SELECT satiety FROM characters WHERE id=?", (actors[0],)).fetchone()[0] == 60
        )


def test_borrowed_instrument_return_cannot_teleport_to_a_distant_owner(lab):
    db, wid, actors, _, tool, at = lab
    from world_engine import collective_resources as resources

    with db.write() as c:
        loc = c.execute(
            "SELECT lab_location_id FROM collective_people WHERE character_id=?", (actors[0],)
        ).fetchone()[0]
        other = str(uuid4())
        c.execute(
            "INSERT INTO locations(id,world_id,name,kind,longitude,latitude) VALUES (?,?,'器具主人远方住所','city',1,1)",
            (other, wid),
        )
        c.execute("UPDATE characters SET current_location_id=? WHERE id=?", (other, actors[0]))
        c.execute(
            "UPDATE item_instances SET container_type='collective_work_tool',container_id='test-order' WHERE id=?",
            (tool,),
        )
        resources.return_tool(
            c, tool, {"container_type": "character_inventory", "container_id": actors[0]}, loc
        )
        item = c.execute("SELECT * FROM item_instances WHERE id=?", (tool,)).fetchone()
        assert item["container_type"] == "ground" and item["container_id"] == loc
