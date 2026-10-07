"""哥哥，恢复必须从实际粮食、活体生长和废物账里算出来。"""

import importlib.util
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from test_collective_research import lab  # noqa: F401

from world_engine.food import FoodService
from world_engine.material_physics import MaterialState
from world_engine.materials import MaterialService as M


def region_service():
    assert importlib.util.find_spec("world_engine.collective_regions"), "尚无真实地区恢复账"
    from world_engine.collective_regions import RegionService

    return RegionService


def test_small_exposures_accumulate_and_splitting_does_not_change_body_damage(lab):
    db, wid, actors, batches, _, at = lab
    with db.write() as c:
        region_service().ingestion(c, wid, at, batch_id=batches[0], dose=0.049, actor_id=actors[0])
        for _ in range(10):
            region_service().ingestion(
                c, wid, at, batch_id=batches[1], dose=0.0049, actor_id=actors[1]
            )
        region_service().tick(c, wid, at + timedelta(days=3))
        health = [
            c.execute("SELECT health FROM characters WHERE id=?", (a,)).fetchone()[0]
            for a in actors
        ]
        assert health[0] == health[1] < 100
        damage = [
            c.execute(
                "SELECT SUM(damage) FROM collective_exposures WHERE actor_id=?", (a,)
            ).fetchone()[0]
            for a in actors
        ]
        assert damage[0] == pytest.approx(damage[1]) and damage[1] > 0


def test_collecting_contaminated_waste_does_not_replace_real_sealing(lab):
    db, wid, actors, _, _, at = lab
    with db.write() as c:
        rid, *_ = region(c, wid, actors[0], at)
        loc = c.execute("SELECT location_id FROM collective_regions WHERE id=?", (rid,)).fetchone()[
            0
        ]
        waste = material(
            c,
            wid,
            actors[0],
            MaterialState(carrier="spent_media", mass=1, mobile=1),
            1,
            location=loc,
        )
        assert not region_service().view(c, wid, at)[0]["waste_ready"]
        item = c.execute("SELECT * FROM item_instances WHERE id=?", (waste,)).fetchone()
        M.move(c, item, 1, "character_inventory", actors[0], actors[0])
        assert not region_service().view(c, wid, at)[0]["waste_ready"]


@pytest.mark.parametrize(
    "container", ["warehouse", "collective_transit", "vehicle_inventory", "unknown_storage"]
)
def test_any_unsealed_waste_container_prevents_global_recovery(lab, container):
    db, wid, actors, _, _, at = lab
    with db.write() as c:
        region(c, wid, actors[0], at)
        waste = material(
            c, wid, actors[0], MaterialState(carrier="spent_media", mass=1, mobile=1), 1
        )
        item = c.execute("SELECT * FROM item_instances WHERE id=?", (waste,)).fetchone()
        M.move(c, item, 1, container, "remote-or-in-flight", actors[0])
        assert not region_service().view(c, wid, at)[0]["waste_ready"]
        item = c.execute("SELECT * FROM item_instances WHERE id=?", (waste,)).fetchone()
        M.move(c, item, 1, "character_inventory", actors[0], actors[0])
        assert not region_service().view(c, wid, at)[0]["waste_ready"]


def test_crop_growth_matches_coarse_and_fine_steps_under_changing_salt_stress(lab):
    db, wid, actors, _, _, at = lab
    from dataclasses import asdict

    from world_engine.collective_production import ProductionService as P
    from world_engine.collective_types import CollectiveCommand

    with db.write() as c:
        rid, soil, _, _ = region(c, wid, actors[0], at)
        poison = material(c, wid, actors[0], MaterialState(mass=0, mobile=5), 1)
        from dataclasses import replace

        M.rebalance(
            c,
            {
                poison: replace(M.state(c, poison), mobile=0),
                soil: replace(M.state(c, soil), mobile=5),
            },
        )
        seed = material(
            c, wid, actors[0], MaterialState(carrier="seed", mass=10, nutrition=200), 10
        )
        c.execute(
            "INSERT INTO character_skill_proficiencies(world_id,character_id,skill_name,proficiency,updated_at) VALUES (?,?, '农作',80,?)",
            (wid, actors[0], at.isoformat()),
        )
        jid = P.start(
            c,
            actors[0],
            CollectiveCommand(op="plant", region_id=rid, batch_id=seed, quantity=10, hours=1),
            at,
        )
        P._complete(
            c,
            c.execute("SELECT * FROM collective_domain_jobs WHERE id=?", (jid,)).fetchone(),
            at + timedelta(hours=1),
            True,
        )
        crop = c.execute("SELECT * FROM collective_crops").fetchone()
        c.execute("SAVEPOINT coarse_crop")
        P._grow(c, crop, at + timedelta(hours=2))
        coarse = asdict(M.state(c, crop["crop_item_id"]))
        c.execute("ROLLBACK TO coarse_crop")
        c.execute("RELEASE coarse_crop")
        for i in range(1, 5):
            current = c.execute("SELECT * FROM collective_crops").fetchone()
            P._grow(c, current, at + timedelta(hours=1 + i * 0.25))
        fine = asdict(M.state(c, crop["crop_item_id"]))
        assert coarse == pytest.approx(fine, rel=1e-10, abs=1e-10)


def test_growth_integrates_wet_soil_and_exact_source_exhaustion_across_arbitrary_boundaries():
    from dataclasses import replace

    from world_engine.collective_production import ProductionService as P

    plant = MaterialState(carrier="seed", mass=10, viability=0.9)
    soil = MaterialState(
        carrier="soil", mass=1000, water=500, mobile=5, adsorbed=10, bound=15, ph=4
    )
    source = MaterialState(carrier="growth_reserve", mass=0.03, nutrition=0.6)
    coarse = P.integrate_growth(plant, soil, source, 500, 1)
    grown = uptake = 0
    for duration in (0.173, 0.287, 0.54):
        g, u, v, soil = P.integrate_growth(plant, soil, source, 500, duration)
        source = replace(source, mass=max(0, source.mass - g))
        plant = replace(plant, viability=v)
        grown += g
        uptake += u
    assert grown == pytest.approx(coarse[0], abs=1e-10)
    assert uptake == pytest.approx(coarse[1], abs=1e-10)
    assert plant.viability == pytest.approx(coarse[2], abs=1e-10)
    assert soil.mobile == pytest.approx(coarse[3].mobile, abs=1e-10)


def test_completed_land_replacement_does_not_rewrite_the_previous_growth_interval(lab):
    from dataclasses import replace

    from world_engine.collective_production import ProductionService as P
    from world_engine.collective_types import CollectiveCommand
    from world_engine.life import LifeActivityService

    db, wid, actors, _, _, at = lab
    with db.write() as c:
        rid, old, source, _ = region(c, wid, actors[0], at)
        poison = material(c, wid, actors[0], MaterialState(mass=0, mobile=5), 1)
        M.rebalance(
            c,
            {
                poison: replace(M.state(c, poison), mobile=0),
                old: replace(M.state(c, old), mobile=5),
            },
        )
        clean = material(c, wid, actors[0], MaterialState(carrier="soil", mass=1000), 1000)
        seed = material(
            c, wid, actors[0], MaterialState(carrier="seed", mass=10, nutrition=200), 10
        )
        c.execute(
            "INSERT INTO character_skill_proficiencies(world_id,character_id,skill_name,proficiency,updated_at) VALUES (?,?,'农作',80,?)",
            (wid, actors[0], at.isoformat()),
        )
        P.start(
            c,
            actors[0],
            CollectiveCommand(op="plant", region_id=rid, batch_id=seed, quantity=10, hours=1),
            at,
        )
        activity = LifeActivityService.running(c, actors[0])
        LifeActivityService.finish(c, activity, at + timedelta(hours=1), "completed", "播种")
        P.tick(c, wid, at + timedelta(hours=1))
        crop = c.execute("SELECT * FROM collective_crops").fetchone()
        expected = P.integrate_growth(
            M.state(c, crop["crop_item_id"]),
            M.state(c, old),
            M.state(c, source),
            crop["target_growth_mass"],
            1,
        )[2]
        P.start(
            c,
            actors[0],
            CollectiveCommand(op="land", region_id=rid, batch_id=clean, quantity=1000, hours=1),
            at + timedelta(hours=1),
        )
        activity = LifeActivityService.running(c, actors[0])
        LifeActivityService.finish(c, activity, at + timedelta(hours=2), "completed", "换地")
        P.tick(c, wid, at + timedelta(hours=2))
        assert (
            c.execute("SELECT soil_id FROM collective_regions WHERE id=?", (rid,)).fetchone()[0]
            == clean
        )
        assert M.state(c, crop["crop_item_id"]).viability == pytest.approx(expected, abs=1e-10)


def material(c, wid, owner, state, quantity=100, category="material", location=None):
    tid, iid = str(uuid4()), str(uuid4())
    c.execute(
        "INSERT INTO item_types(id,name,category,stack_limit,slot_size) VALUES (?,?,?,?,1)",
        (tid, state.carrier, category, 1000000),
    )
    c.execute(
        "INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,owner_character_id,quantity,condition) VALUES (?,?,?,?,?,?,?,100)",
        (
            iid,
            wid,
            tid,
            "ground" if location else "character_inventory",
            location or owner,
            owner,
            quantity,
        ),
    )
    M.attach(c, iid, state)
    return iid


def region(c, wid, actor, at):
    s = region_service()
    loc = c.execute(
        "SELECT lab_location_id FROM collective_people WHERE character_id=?", (actor,)
    ).fetchone()[0]
    soil = material(c, wid, actor, MaterialState(carrier="soil", mass=1000), 1000, location=loc)
    source = material(
        c,
        wid,
        actor,
        MaterialState(carrier="growth_reserve", mass=1000, nutrition=20000),
        1000,
        location=loc,
    )
    water = material(
        c,
        wid,
        actor,
        MaterialState(carrier="water", mass=0, water=1000, viability=0),
        1000,
        location=loc,
    )
    rid = s.register(
        c,
        wid,
        loc,
        actor,
        at,
        population=10,
        daily_nutrition=200,
        soil_id=soil,
        growth_source_id=source,
        water_id=water,
    )
    return rid, soil, source, water


def test_starvation_has_actual_body_consequence_and_preserved_death_record(lab):
    db, wid, actors, _, _, at = lab
    from world_engine.clock import WorldClockService
    from world_engine.config import Settings

    with db.write() as c:
        c.execute("INSERT INTO material_modes VALUES (?,?)", (wid, '{"mode":"collective"}'))
        c.execute("UPDATE characters SET satiety=0,health=1 WHERE id=?", (actors[0],))
        c.execute(
            "INSERT INTO collective_food_decisions SELECT ?,id,'avoid','测试断粮',? FROM item_instances WHERE owner_character_id=?",
            (actors[0], at.isoformat(), actors[0]),
        )
    WorldClockService(db, Settings(database_path=db.path)).heartbeat(wid, elapsed_seconds=3600)
    with db.read() as c:
        assert (
            c.execute("SELECT health FROM characters WHERE id=?", (actors[0],)).fetchone()[0] == 0
        )
        assert (
            c.execute(
                "SELECT cause FROM collective_deaths WHERE actor_id=?", (actors[0],)
            ).fetchone()[0]
            == "starvation"
        )


def test_group_health_zero_records_deaths_but_keeps_daily_demand(lab):
    db, wid, actors, _, _, at = lab
    with db.write() as c:
        rid, *_ = region(c, wid, actors[0], at)
        c.execute("UPDATE collective_regions SET health=1 WHERE id=?", (rid,))
        region_service().tick(c, wid, at + timedelta(days=1))
        row = c.execute(
            "SELECT deaths,population,daily_nutrition FROM collective_regions WHERE id=?", (rid,)
        ).fetchone()
        assert tuple(row) == (10, 10, 200)


def test_loose_polluted_waste_leaches_but_clean_vapor_does_not_block_recovery(lab):
    db, wid, actors, _, _, at = lab
    with db.write() as c:
        rid, soil, _, water = region(c, wid, actors[0], at)
        loc = c.execute("SELECT location_id FROM collective_regions WHERE id=?", (rid,)).fetchone()[
            0
        ]
        waste = material(
            c,
            wid,
            actors[0],
            MaterialState(carrier="lab_waste", mass=5, particles=1, mobile=1),
            1,
            location=loc,
        )
        region_service().tick(c, wid, at + timedelta(hours=1))
        assert M.state(c, water).mobile > 0 and M.state(c, soil).particles > 0
        assert not region_service().view(c, wid, at + timedelta(hours=1))[0]["waste_ready"]
        assert M.audit(c, wid)["balanced"]
        M.consume(
            c,
            c.execute("SELECT * FROM item_instances WHERE id=?", (waste,)).fetchone(),
            1,
            "test-sealed",
        )
        material(
            c, wid, actors[0], MaterialState(carrier="vapor", mass=0, water=2), 1, location=loc
        )
        assert region_service().view(c, wid, at + timedelta(hours=1))[0]["waste_ready"]


@pytest.mark.parametrize("traveling", [False, True])
def test_absent_farmer_does_not_automatically_observe_crop_maturity(lab, traveling):
    db, wid, actors, _, _, at = lab
    from world_engine.collective_production import ProductionService as P

    with db.write() as c:
        rid, *_ = region(c, wid, actors[0], at)
        seed = material(
            c, wid, actors[0], MaterialState(carrier="seed", mass=10, nutrition=200), 10
        )
        c.execute(
            "INSERT INTO character_skill_proficiencies(world_id,character_id,skill_name,proficiency,updated_at) VALUES (?,?,'农作',80,?)",
            (wid, actors[0], at.isoformat()),
        )
        from world_engine.collective_types import CollectiveCommand

        jid = P.start(
            c,
            actors[0],
            CollectiveCommand(op="plant", region_id=rid, batch_id=seed, quantity=10, hours=1),
            at,
        )
        job = c.execute("SELECT * FROM collective_domain_jobs WHERE id=?", (jid,)).fetchone()
        P._complete(c, job, at + timedelta(hours=1), True)
        crop = c.execute("SELECT * FROM collective_crops").fetchone()
        loc = str(uuid4())
        c.execute(
            "INSERT INTO locations(id,world_id,name,kind,longitude,latitude) VALUES (?,?,'远方','city',1,1)",
            (loc, wid),
        )
        if traveling:
            from world_engine.life import LifeActivityService
            from world_engine.movement import MovementService

            LifeActivityService.advance(c, wid, at + timedelta(hours=1), "test-plant-complete")
            MovementService().start(
                c,
                world_id=wid,
                character_id=actors[0],
                destination_longitude=2,
                destination_latitude=0,
                world_time=at + timedelta(hours=1),
            )
            c.execute("UPDATE characters SET current_location_id=NULL WHERE id=?", (actors[0],))
        else:
            c.execute("UPDATE characters SET current_location_id=? WHERE id=?", (loc, actors[0]))
        P._grow(c, crop, at + timedelta(days=91))
        assert (
            c.execute(
                "SELECT COUNT(*) FROM collective_observations WHERE kind='crop_mature'"
            ).fetchone()[0]
            == 0
        )
        from world_engine.collective_research import CollectiveResearchService as R

        assert R.context(c, actors[0], at + timedelta(days=91))["crops"][0]["status"] == "unknown"


def test_register_processed_water_as_actual_downstream_after_real_labor(lab):
    db, wid, actors, _, _, at = lab
    from world_engine.collective_production import ProductionService as P
    from world_engine.collective_types import CollectiveCommand

    with db.write() as c:
        rid, *_ = region(c, wid, actors[0], at)
        replacement = material(
            c, wid, actors[0], MaterialState(carrier="water", mass=0, water=900), 900
        )
        command = CollectiveCommand(
            op="water", region_id=rid, batch_id=replacement, quantity=900, hours=1
        )
        jid = P.start(c, actors[0], command, at)
        assert (
            c.execute("SELECT water_id FROM collective_regions WHERE id=?", (rid,)).fetchone()[0]
            != replacement
        )
        job = c.execute("SELECT * FROM collective_domain_jobs WHERE id=?", (jid,)).fetchone()
        P._complete(c, job, at + timedelta(hours=1), True)
        assert (
            c.execute("SELECT water_id FROM collective_regions WHERE id=?", (rid,)).fetchone()[0]
            == replacement
        )


def test_normal_meal_can_collect_one_portion_from_explicit_local_food_store(lab):
    db, wid, actors, batches, _, at = lab
    from world_engine.economy import EconomyService

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
            (actors[0], batches[0], "avoid", "隔离旧粮", at.isoformat()),
        )
        food = material(
            c,
            wid,
            actors[0],
            MaterialState(carrier="grain", mass=100, nutrition=2000),
            100,
            category="food",
            location=loc,
        )
        c.execute(
            "INSERT INTO collective_food_decisions VALUES (?,?,?,?,?)",
            (actors[0], food, "allow", "本人日常从粮柜取一餐", at.isoformat()),
        )
        c.execute("INSERT INTO collective_carry_limits VALUES (?,10)", (actors[0],))
        actor = c.execute("SELECT * FROM characters WHERE id=?", (actors[0],)).fetchone()
        assert EconomyService.food(c, actor, at=at)
        assert c.execute(
            "SELECT quantity,container_type FROM item_instances WHERE id=?", (food,)
        ).fetchone()[:] == (99, "ground")
        assert M.audit(c, wid)["balanced"]


def test_liquid_measurement_uses_real_water_volume_instead_of_zero_dry_mass():
    from world_engine.collective_research import CollectiveResearchService as R

    assert R._measurement(
        MaterialState(carrier="water", mass=0, water=100, mobile=1), "extractable_salt"
    ) == pytest.approx(0.01)


def test_tracked_food_uses_real_nutrition_and_records_delayed_exposure(lab):
    db, wid, actors, _, _, at = lab
    with db.write() as c:
        iid = material(
            c, wid, actors[0], MaterialState(mass=10, nutrition=15, mobile=1), 10, category="food"
        )
        actor = c.execute("SELECT * FROM characters WHERE id=?", (actors[0],)).fetchone()
        item = dict(c.execute("SELECT * FROM item_instances WHERE id=?", (iid,)).fetchone())
        item["nutrition"] = 999
        effect = FoodService.eat(c, actor, item, at)
        assert effect["nutrition"] == pytest.approx(1.5)
        assert c.execute("SELECT COUNT(*) FROM collective_exposures").fetchone()[0] == 1
        s = region_service()
        s.tick(c, wid, at + timedelta(hours=48))
        assert (
            c.execute("SELECT health FROM characters WHERE id=?", (actors[0],)).fetchone()[0] < 100
        )
        assert M.audit(c, wid)["balanced"]


def test_population_death_does_not_reduce_fixed_food_demand(lab):
    db, wid, actors, _, _, at = lab
    with db.write() as c:
        rid, *_ = region(c, wid, actors[0], at)
        c.execute("UPDATE collective_regions SET deaths=3 WHERE id=?", (rid,))
        s = region_service()
        s.tick(c, wid, at + timedelta(days=1))
        ledger = c.execute(
            "SELECT * FROM collective_food_days WHERE region_id=?", (rid,)
        ).fetchone()
        assert ledger["required_nutrition"] == 200 and ledger["delivered_nutrition"] == 0


def test_unsafe_ration_breaks_continuous_safe_days(lab):
    db, wid, actors, _, _, at = lab
    with db.write() as c:
        rid, *_ = region(c, wid, actors[0], at)
        food = material(
            c, wid, actors[0], MaterialState(mass=100, nutrition=2000), 100, category="food"
        )
        from world_engine.collective_production import ProductionService

        ProductionService.ration(c, actors[0], rid, food, 100, at)
        s = region_service()
        s.tick(c, wid, at + timedelta(days=2))
        assert s.view(c, wid, at + timedelta(days=2))[0]["safe_days"] == 2
        c.execute(
            "UPDATE item_instances SET container_type='ground',container_id=? WHERE container_type='region_food' AND container_id=?",
            (rid, rid),
        )
        unsafe = material(
            c, wid, actors[0], MaterialState(mass=20, nutrition=400, mobile=5), 20, category="food"
        )
        ProductionService.ration(c, actors[0], rid, unsafe, 20, at + timedelta(days=2))
        s.tick(c, wid, at + timedelta(days=3))
        assert s.view(c, wid, at + timedelta(days=3))[0]["safe_days"] == 0


def test_biomass_cannot_be_created_by_a_material_exchange(lab):
    db, _, _, batches, _, _ = lab
    with db.write() as c:
        with pytest.raises(ValueError, match="守恒"):
            M.rebalance(
                c, {batches[0]: MaterialState(mass=999, nutrition=2000, mobile=20, particles=1)}
            )


def test_crop_requires_actual_sowing_and_full_ninety_days(lab):
    db, wid, actors, _, _, at = lab
    with db.write() as c:
        rid, *_ = region(c, wid, actors[0], at)
        seed = material(
            c, wid, actors[0], MaterialState(carrier="seed", mass=10, nutrition=200), 10
        )
        c.execute(
            "INSERT INTO character_skill_proficiencies(world_id,character_id,skill_name,proficiency,updated_at) VALUES (?,?,?,80,?)",
            (wid, actors[0], "农作", at.isoformat()),
        )
        from world_engine.collective_production import ProductionService as P
        from world_engine.collective_types import CollectiveCommand

        jid = P.start(
            c,
            actors[0],
            CollectiveCommand(op="plant", region_id=rid, batch_id=seed, quantity=10, hours=1),
            at,
        )
        assert c.execute("SELECT COUNT(*) FROM collective_crops").fetchone()[0] == 0
        from world_engine.life import LifeActivityService

        activity = c.execute(
            "SELECT a.* FROM character_life_activities a JOIN collective_domain_jobs j ON j.activity_id=a.id WHERE j.id=?",
            (jid,),
        ).fetchone()
        LifeActivityService.finish(
            c, activity, at + timedelta(hours=1), "completed", "夹具实际播种完成"
        )
        P.tick(c, wid, at + timedelta(hours=1))
        crop = c.execute("SELECT * FROM collective_crops").fetchone()
        P.tick(c, wid, at + timedelta(days=89, hours=1))
        assert c.execute("SELECT status FROM collective_crops").fetchone()[0] == "growing"
        with pytest.raises(ValueError, match="成熟|90"):
            P.start(
                c,
                actors[0],
                CollectiveCommand(op="harvest", crop_id=crop["id"], hours=1),
                at + timedelta(days=89, hours=1),
            )
        P.tick(c, wid, at + timedelta(days=90, hours=1))
        assert c.execute("SELECT status FROM collective_crops").fetchone()[0] == "mature"
        assert (
            c.execute(
                "SELECT COUNT(*) FROM item_instances WHERE container_type='region_food'"
            ).fetchone()[0]
            == 0
        )
        assert M.audit(c, wid)["balanced"]


def test_food_quarantine_is_explicit_personal_knowledge_not_hidden_toxicity(lab):
    db, wid, actors, _, _, at = lab
    with db.write() as c:
        food = material(
            c, wid, actors[0], MaterialState(mass=5, nutrition=100, mobile=1), 5, category="food"
        )
        actor = c.execute("SELECT * FROM characters WHERE id=?", (actors[0],)).fetchone()
        item = dict(c.execute("SELECT * FROM item_instances WHERE id=?", (food,)).fetchone())
        item["nutrition"] = 20
        assert FoodService.eat(c, actor, item, at)["nutrition"] == 20
        from world_engine.collective_production import ProductionService as P
        from world_engine.collective_types import CollectiveCommand

        P.start(
            c,
            actors[0],
            CollectiveCommand(
                op="diet", batch_id=food, food_policy="avoid", text="本人已决定暂时隔离这批粮"
            ),
            at,
        )
        item = dict(c.execute("SELECT * FROM item_instances WHERE id=?", (food,)).fetchone())
        item["nutrition"] = 20
        with pytest.raises(ValueError, match="隔离|饮食"):
            FoodService.eat(c, actor, item, at)


def test_lab_waste_returns_to_real_floor_and_is_not_an_infinite_safe_sink(lab):
    db, wid, actors, batches, tool, at = lab
    from test_collective_research import experiment, research

    from world_engine.clock import WorldClockService
    from world_engine.config import Settings

    with db.write() as c:
        research().submit(c, actors[0], experiment(batches[0], tool), at)
    WorldClockService(db, Settings(database_path=db.path)).heartbeat(wid, elapsed_seconds=3600)
    with db.write() as c:
        research().tick(c, wid, at + timedelta(hours=1))
        region_service().tick(c, wid, at + timedelta(hours=1))
        waste = c.execute(
            "SELECT i.* FROM item_instances i JOIN item_types t ON t.id=i.item_type_id WHERE t.name='实验残留废物'"
        ).fetchone()
        assert waste and waste["container_type"] == "ground"
        assert M.state(c, waste["id"]).mobile == pytest.approx(0.2)
        assert M.audit(c, wid)["balanced"]


def test_finite_alternative_source_requires_actual_harvest_labor(lab):
    db, wid, actors, _, _, at = lab
    with db.write() as c:
        rid, _, _, _ = region(c, wid, actors[0], at)
        loc = c.execute("SELECT location_id FROM collective_regions WHERE id=?", (rid,)).fetchone()[
            0
        ]
        fish = material(
            c,
            wid,
            actors[0],
            MaterialState(carrier="fish", mass=50, nutrition=1000),
            50,
            category="food",
            location=loc,
        )
        c.execute("UPDATE item_instances SET container_type='natural_resource' WHERE id=?", (fish,))
        from world_engine.collective_production import ProductionService as P
        from world_engine.collective_types import CollectiveCommand

        jid = P.start(
            c,
            actors[0],
            CollectiveCommand(op="gather", batch_id=fish, region_id=rid, quantity=5, hours=1),
            at,
        )
        assert (
            c.execute("SELECT quantity FROM item_instances WHERE id=?", (fish,)).fetchone()[0] == 45
        )
        assert (
            c.execute(
                "SELECT COUNT(*) FROM item_instances WHERE container_type='character_inventory' AND item_type_id=(SELECT item_type_id FROM item_instances WHERE id=?)",
                (fish,),
            ).fetchone()[0]
            == 0
        )
        assert (
            c.execute("SELECT status FROM collective_domain_jobs WHERE id=?", (jid,)).fetchone()[0]
            == "working"
        )


def test_waste_liner_capacity_cannot_be_invented_from_command(lab):
    db, wid, actors, _, _, at = lab
    with db.write() as c:
        rid, _, _, _ = region(c, wid, actors[0], at)
        waste = material(
            c, wid, actors[0], MaterialState(carrier="water", mass=0, water=200, mobile=20), 200
        )
        liner = material(c, wid, actors[0], MaterialState(carrier="clay", mass=1), 1)
        tid = c.execute("SELECT item_type_id FROM item_instances WHERE id=?", (liner,)).fetchone()[
            0
        ]
        c.execute("INSERT INTO collective_liners VALUES (?,100,0.00001,1,168,336)", (tid,))
        from world_engine.collective_production import ProductionService as P
        from world_engine.collective_types import CollectiveCommand

        with pytest.raises(ValueError, match="容量"):
            P.start(
                c,
                actors[0],
                CollectiveCommand(
                    op="dispose",
                    region_id=rid,
                    batch_id=waste,
                    quantity=200,
                    auxiliary_ids=[liner],
                    auxiliary_quantities={liner: 1},
                    hours=1,
                ),
                at,
            )


def test_unmaintained_waste_leaks_into_real_water_and_blocks_recovery(lab):
    db, wid, actors, _, _, at = lab
    with db.write() as c:
        rid, _, _, water = region(c, wid, actors[0], at)
        waste = material(
            c, wid, actors[0], MaterialState(carrier="water", mass=0, water=20, mobile=5), 20
        )
        sid = str(uuid4())
        c.execute(
            "INSERT INTO collective_waste_sites VALUES (?,?,?,?,?,NULL,?,?,?,?,?,0)",
            (
                sid,
                wid,
                rid,
                actors[0],
                100,
                0.01,
                at.isoformat(),
                at.isoformat(),
                at.isoformat(),
                336,
            ),
        )
        item = c.execute("SELECT * FROM item_instances WHERE id=?", (waste,)).fetchone()
        M.move(c, item, 20, "waste_site", sid, actors[0])
        region_service().tick(c, wid, at + timedelta(days=14))
        assert M.state(c, water).mobile > 0
        assert M.audit(c, wid)["balanced"]
        assert not region_service().view(c, wid, at + timedelta(days=14))[0]["waste_ready"]


def test_program_waits_for_actual_planting_before_using_crop_binding(lab):
    db, wid, actors, _, _, at = lab
    from test_collective_research import research

    from world_engine.collective_production import ProductionService as P
    from world_engine.life import LifeActivityService

    with db.write() as c:
        rid, *_ = region(c, wid, actors[0], at)
        seed = material(c, wid, actors[0], MaterialState(carrier="seed", mass=5, nutrition=100), 5)
        c.execute(
            "INSERT INTO character_skill_proficiencies(world_id,character_id,skill_name,proficiency,updated_at) VALUES (?,?,?,80,?)",
            (wid, actors[0], "农作", at.isoformat()),
        )
        pid = research().submit(
            c,
            actors[0],
            {
                "reason": "本人种植后再查看",
                "commands": [
                    {
                        "op": "plant",
                        "region_id": rid,
                        "batch_id": seed,
                        "quantity": 5,
                        "hours": 1,
                        "result_key": "crop",
                    },
                    {"op": "wait", "hours": 24},
                ],
            },
            at,
        )
        assert (
            c.execute("SELECT status FROM collective_programs WHERE id=?", (pid,)).fetchone()[0]
            == "active"
        )
        assert c.execute("SELECT COUNT(*) FROM collective_jobs").fetchone()[0] == 0
        activity = c.execute(
            "SELECT * FROM character_life_activities WHERE status='running'"
        ).fetchone()
        LifeActivityService.finish(
            c, activity, at + timedelta(hours=1), "completed", "夹具劳动完成"
        )
        P.tick(c, wid, at + timedelta(hours=1))
        binding = json.loads(
            c.execute(
                "SELECT bindings_json FROM collective_programs WHERE id=?", (pid,)
            ).fetchone()[0]
        )["crop"]
        assert c.execute("SELECT 1 FROM collective_crops WHERE id=?", (binding,)).fetchone()
        due = c.execute("SELECT due_at FROM collective_jobs").fetchone()[0]
        assert due == (at + timedelta(hours=25)).isoformat()


def test_disposal_and_maintenance_need_completed_labor_and_real_liners(lab):
    db, wid, actors, _, _, at = lab
    from world_engine.collective_production import ProductionService as P
    from world_engine.collective_types import CollectiveCommand
    from world_engine.life import LifeActivityService

    with db.write() as c:
        rid, *_ = region(c, wid, actors[0], at)
        waste = material(
            c, wid, actors[0], MaterialState(carrier="water", mass=0, water=20, mobile=2), 20
        )
        liner = material(c, wid, actors[0], MaterialState(carrier="clay", mass=3), 3)
        tid = c.execute("SELECT item_type_id FROM item_instances WHERE id=?", (liner,)).fetchone()[
            0
        ]
        c.execute("INSERT INTO collective_liners VALUES (?,100,0.000001,1,168,336)", (tid,))
        P.start(
            c,
            actors[0],
            CollectiveCommand(
                op="dispose",
                region_id=rid,
                batch_id=waste,
                quantity=20,
                auxiliary_ids=[liner],
                auxiliary_quantities={liner: 1},
                hours=1,
            ),
            at,
        )
        assert c.execute("SELECT COUNT(*) FROM collective_waste_sites").fetchone()[0] == 0
        activity = c.execute(
            "SELECT * FROM character_life_activities WHERE status='running'"
        ).fetchone()
        LifeActivityService.finish(
            c, activity, at + timedelta(hours=1), "completed", "封存实际完成"
        )
        P.tick(c, wid, at + timedelta(hours=1))
        site = c.execute("SELECT * FROM collective_waste_sites").fetchone()
        assert (
            c.execute("SELECT container_type FROM item_instances WHERE id=?", (waste,)).fetchone()[
                0
            ]
            == "waste_site"
        )
        assert (
            c.execute("SELECT quantity FROM item_instances WHERE id=?", (liner,)).fetchone()[0] == 2
        )
        P.start(
            c,
            actors[0],
            CollectiveCommand(
                op="maintain", region_id=rid, site_id=site["id"], auxiliary_ids=[liner], hours=1
            ),
            at + timedelta(days=7),
        )
        activity = c.execute(
            "SELECT * FROM character_life_activities WHERE status='running'"
        ).fetchone()
        LifeActivityService.finish(
            c, activity, at + timedelta(days=7, hours=1), "completed", "维护实际完成"
        )
        P.tick(c, wid, at + timedelta(days=7, hours=1))
        assert (
            c.execute("SELECT last_maintained_at FROM collective_waste_sites").fetchone()[0]
            == (at + timedelta(days=7, hours=1)).isoformat()
        )
        assert M.audit(c, wid)["balanced"]


def test_harvest_keeps_real_seed_and_finite_growth_balance(lab):
    db, wid, actors, _, _, at = lab
    from world_engine.collective_production import ProductionService as P
    from world_engine.collective_types import CollectiveCommand
    from world_engine.life import LifeActivityService

    with db.write() as c:
        rid, *_ = region(c, wid, actors[0], at)
        seed = material(
            c, wid, actors[0], MaterialState(carrier="seed", mass=10, nutrition=200), 10
        )
        c.execute(
            "INSERT INTO character_skill_proficiencies(world_id,character_id,skill_name,proficiency,updated_at) VALUES (?,?,?,80,?)",
            (wid, actors[0], "农作", at.isoformat()),
        )
        P.start(
            c,
            actors[0],
            CollectiveCommand(op="plant", region_id=rid, batch_id=seed, quantity=10, hours=1),
            at,
        )
        activity = c.execute(
            "SELECT * FROM character_life_activities WHERE status='running'"
        ).fetchone()
        LifeActivityService.finish(c, activity, at + timedelta(hours=1), "completed", "播种完成")
        P.tick(c, wid, at + timedelta(hours=1))
        P.tick(c, wid, at + timedelta(days=90, hours=1))
        crop = c.execute("SELECT * FROM collective_crops").fetchone()
        P.start(
            c,
            actors[0],
            CollectiveCommand(op="harvest", crop_id=crop["id"], hours=1),
            at + timedelta(days=90, hours=1),
        )
        activity = c.execute(
            "SELECT * FROM character_life_activities WHERE status='running'"
        ).fetchone()
        LifeActivityService.finish(
            c, activity, at + timedelta(days=90, hours=2), "completed", "收获完成"
        )
        P.tick(c, wid, at + timedelta(days=90, hours=2))
        crop = c.execute("SELECT * FROM collective_crops").fetchone()
        assert crop["status"] == "harvested"
        assert M.state(c, crop["seed_id"]).carrier == "seed"
        assert M.state(c, crop["seed_id"]).viability == 1
        assert M.state(c, crop["food_id"]).mass == pytest.approx(484.5)
        assert M.audit(c, wid)["balanced"]


def test_automatic_meal_uses_tracked_harvest_without_legacy_food_profile(lab):
    db, wid, actors, batches, _, at = lab
    from world_engine.economy import EconomyService

    with db.write() as c:
        c.execute("INSERT INTO material_modes(world_id) VALUES (?)", (wid,))
        c.execute(
            "UPDATE item_instances SET container_type='ground',container_id=(SELECT lab_location_id FROM collective_people WHERE character_id=?) WHERE id=?",
            (actors[0], batches[0]),
        )
        food = material(
            c,
            wid,
            actors[0],
            MaterialState(carrier="grain", mass=5, nutrition=100),
            5,
            category="food",
        )
        c.execute("UPDATE characters SET satiety=20 WHERE id=?", (actors[0],))
        actor = c.execute("SELECT * FROM characters WHERE id=?", (actors[0],)).fetchone()
        assert EconomyService.food(c, actor, at=at)
        assert (
            c.execute("SELECT quantity FROM item_instances WHERE id=?", (food,)).fetchone()[0] == 4
        )
        c.execute(
            "INSERT INTO collective_food_decisions VALUES (?,?,?,?,?)",
            (actors[0], food, "avoid", "本人隔离", at.isoformat()),
        )
        actor = c.execute("SELECT * FROM characters WHERE id=?", (actors[0],)).fetchone()
        assert EconomyService.food(c, actor, at=at) is None


def test_economy_gather_uses_finite_source_instead_of_numeric_regeneration(lab):
    db, wid, actors, _, _, at = lab
    from world_engine.economy import EconomyService

    with db.write() as c:
        c.execute("INSERT INTO material_modes(world_id) VALUES (?)", (wid,))
        rid, *_ = region(c, wid, actors[0], at)
        loc = c.execute("SELECT location_id FROM collective_regions WHERE id=?", (rid,)).fetchone()[
            0
        ]
        fish = material(
            c,
            wid,
            actors[0],
            MaterialState(carrier="fish", mass=10, nutrition=200),
            10,
            category="food",
            location=loc,
        )
        c.execute("UPDATE item_instances SET container_type='natural_resource' WHERE id=?", (fish,))
        actor = c.execute("SELECT * FROM characters WHERE id=?", (actors[0],)).fetchone()
        assert EconomyService.harvest(c, actor, at, food_only=True)
        assert (
            c.execute("SELECT quantity FROM item_instances WHERE id=?", (fish,)).fetchone()[0] == 9
        )


def test_repeated_gather_is_npc_declared_and_each_round_uses_a_body(lab):
    db, wid, actors, _, _, at = lab
    from world_engine.collective_production import ProductionService as P
    from world_engine.collective_types import CollectiveCommand
    from world_engine.life import LifeActivityService

    with db.write() as c:
        rid, *_ = region(c, wid, actors[0], at)
        loc = c.execute("SELECT location_id FROM collective_regions WHERE id=?", (rid,)).fetchone()[
            0
        ]
        fish = material(
            c,
            wid,
            actors[0],
            MaterialState(carrier="fish", mass=10, nutrition=200),
            10,
            category="food",
            location=loc,
        )
        c.execute("UPDATE item_instances SET container_type='natural_resource' WHERE id=?", (fish,))
        P.start(
            c,
            actors[0],
            CollectiveCommand(
                op="gather",
                region_id=rid,
                batch_id=fish,
                quantity=1,
                hours=1,
                repeat_count=3,
                repeat_interval_hours=24,
            ),
            at,
        )
        activity = c.execute(
            "SELECT * FROM character_life_activities WHERE status='running'"
        ).fetchone()
        LifeActivityService.finish(
            c, activity, at + timedelta(hours=1), "completed", "首次采集完成"
        )
        P.tick(c, wid, at + timedelta(hours=1))
        P.tick(c, wid, at + timedelta(hours=23))
        assert (
            c.execute("SELECT quantity FROM item_instances WHERE id=?", (fish,)).fetchone()[0] == 9
        )
        P.tick(c, wid, at + timedelta(hours=24))
        assert (
            c.execute("SELECT quantity FROM item_instances WHERE id=?", (fish,)).fetchone()[0] == 8
        )
        assert (
            c.execute(
                "SELECT COUNT(*) FROM character_life_activities WHERE status='running'"
            ).fetchone()[0]
            == 1
        )
        assert M.audit(c, wid)["balanced"]


def test_region_context_discloses_local_resources_without_secret_pollution(lab):
    db, wid, actors, _, _, at = lab
    from test_collective_research import research

    with db.write() as c:
        rid, soil, source, water = region(c, wid, actors[0], at)
        text = json.dumps(research().context(c, actors[0], at))
        assert all(iid in text for iid in (rid, soil, source, water))
        assert "salt_limit" not in text and "mobile" not in text and "growth_reserve" in text


def test_quarantined_batch_split_preserves_owners_decision(lab):
    db, wid, actors, batches, _, at = lab
    with db.write() as c:
        c.execute(
            "INSERT INTO collective_food_decisions VALUES (?,?,?,?,?)",
            (actors[0], batches[0], "avoid", "本人隔离这批来源", at.isoformat()),
        )
        item = c.execute("SELECT * FROM item_instances WHERE id=?", (batches[0],)).fetchone()
        new = M.split(c, item, 2, str(uuid4()))
        decision = c.execute(
            "SELECT policy FROM collective_food_decisions WHERE actor_id=? AND batch_id=?",
            (actors[0], new),
        ).fetchone()
        assert decision and decision[0] == "avoid"


def test_dead_seed_cannot_convert_environment_reserve_into_full_crop(lab):
    db, wid, actors, _, _, at = lab
    from world_engine.collective_production import ProductionService as P
    from world_engine.collective_types import CollectiveCommand
    from world_engine.life import LifeActivityService

    with db.write() as c:
        rid, _, source, _ = region(c, wid, actors[0], at)
        seed = material(
            c,
            wid,
            actors[0],
            MaterialState(carrier="seed", mass=10, nutrition=200, viability=0),
            10,
        )
        c.execute(
            "INSERT INTO character_skill_proficiencies(world_id,character_id,skill_name,proficiency,updated_at) VALUES (?,?,?,80,?)",
            (wid, actors[0], "农作", at.isoformat()),
        )
        P.start(
            c,
            actors[0],
            CollectiveCommand(op="plant", region_id=rid, batch_id=seed, quantity=10, hours=1),
            at,
        )
        activity = c.execute(
            "SELECT * FROM character_life_activities WHERE status='running'"
        ).fetchone()
        LifeActivityService.finish(
            c, activity, at + timedelta(hours=1), "completed", "播种实际完成"
        )
        P.tick(c, wid, at + timedelta(hours=1))
        P.tick(c, wid, at + timedelta(days=90, hours=1))
        assert c.execute("SELECT grown_mass FROM collective_crops").fetchone()[0] == 0
        assert M.state(c, source).mass == 1000
