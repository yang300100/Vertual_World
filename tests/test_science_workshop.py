"""哥哥，工坊只提供基础条件，不给人物写路线或提升技能。"""

import importlib
import importlib.util
import json

import pytest

pytest_plugins = ["test_collective_research"]


def workshop():
    assert importlib.util.find_spec("world_engine.science_workshop"), "尚无中立工坊条件"
    return importlib.import_module("world_engine.science_workshop")


def test_workshop_keeps_people_and_their_goals(lab):
    service = workshop()
    db, wid, actors, _, _, at = lab
    with db.write() as c:
        before = [
            tuple(r)
            for r in c.execute(
                "SELECT id,identity,skills_json,goals_json FROM characters "
                "WHERE world_id=? ORDER BY id",
                (wid,),
            )
        ]
        assets = service.seed_workshop(c, wid, at, seed=7)
        after = [
            tuple(r)
            for r in c.execute(
                "SELECT id,identity,skills_json,goals_json FROM characters "
                "WHERE world_id=? ORDER BY id",
                (wid,),
            )
        ]
        assert after == before
        assert set(assets["actors"]) == set(actors)
        assert c.execute("SELECT COUNT(*) FROM technologies").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM collective_programs").fetchone()[0] == 0


def test_prepare_is_idempotent_and_different_seed_is_rejected(lab):
    service = workshop()
    db, wid, _, _, _, at = lab
    with db.write() as c:
        first = service.seed_workshop(c, wid, at, seed=7)
        count = c.execute(
            "SELECT COUNT(*) FROM item_instances WHERE world_id=?", (wid,)
        ).fetchone()[0]
        second = service.seed_workshop(c, wid, at, seed=7)
        assert first == second
        assert (
            c.execute("SELECT COUNT(*) FROM item_instances WHERE world_id=?", (wid,)).fetchone()[0]
            == count
        )
        with pytest.raises(ValueError, match="种子|已准备"):
            service.seed_workshop(c, wid, at, seed=8)


def test_resources_have_actual_ownership_and_finite_energy(lab):
    service = workshop()
    db, wid, actors, _, _, at = lab
    with db.write() as c:
        assets = service.seed_workshop(c, wid, at, seed=7)
        for actor_id in actors:
            owned = assets["actors"][actor_id]
            assert owned["materials"] and owned["instruments"]
            for iid in [
                *owned["materials"].values(),
                *owned["instruments"].values(),
                owned["apparatus"],
                owned["energy"],
            ]:
                item = c.execute("SELECT * FROM item_instances WHERE id=?", (iid,)).fetchone()
                assert item["owner_character_id"] == actor_id
                assert item["container_id"] == owned["site_id"]
                assert item["quantity"] > 0
            energy = c.execute(
                "SELECT energy_per_unit FROM science_energy_stocks WHERE item_id=?",
                (owned["energy"],),
            ).fetchone()[0]
            assert 0 < energy < 100000


def test_initial_observations_reveal_tools_not_private_coefficients(lab):
    service = workshop()
    db, wid, actors, _, _, at = lab
    with db.write() as c:
        service.seed_workshop(c, wid, at, seed=7)
        entries = c.execute(
            "SELECT content_json FROM collective_observations WHERE character_id=?", (actors[0],)
        ).fetchall()
        text = json.dumps([json.loads(r[0]) for r in entries], ensure_ascii=False)
        assert "strength_scale" not in text and "sinter_start" not in text
        assert "instruments" in text


def test_existing_craft_reference_samples_are_not_new_technologies(lab):
    service = workshop()
    db, wid, actors, _, _, at = lab
    with db.write() as c:
        assets = service.seed_workshop(c, wid, at, seed=7)
        references = assets["actors"][actors[0]]["references"]
        assert {"clay", "metal", "fiber"} <= set(references)
        for iid in references.values():
            assert c.execute(
                "SELECT 1 FROM science_reference_samples WHERE item_id=? AND world_id=?", (iid, wid)
            ).fetchone()
        assert c.execute("SELECT COUNT(*) FROM technologies").fetchone()[0] == 0
