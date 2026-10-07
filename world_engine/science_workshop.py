"""哥哥，工坊只准备有限的实物条件，人物目标、技能和研究选择都保留。"""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from world_engine.collective_research import CollectiveResearchService
from world_engine.craft_physics import PROFILE_DEFAULTS
from world_engine.material_physics import MaterialState
from world_engine.materials import MaterialService, encoded
from world_engine.repository import to_iso

MATERIALS = {
    "clay": ("常见黏土", 40),
    "metal": ("普通铁料", 30),
    "fiber": ("未染纤维", 40),
    "pigment": ("普通染料", 20),
    "limestone": ("石灰石", 20),
    "charcoal": ("木炭", 20),
    "water": ("工坊清水", 80),
}
INSTRUMENTS = {
    "strength": ("相对承重量器", 0.1),
    "leakage": ("渗漏量器", 0.001),
    "porosity": ("吸水比较量器", 0.01),
    "color_retention": ("洗涤色样量器", 0.01),
    "strain": ("形变量器", 0.01),
}
REFERENCES = {
    "clay": ("现有储水陶片", {"structural_bond": 0.55, "porosity": 0.25}),
    "metal": ("常规铁制试件", {"structural_bond": 0.5, "porosity": 0.1}),
    "fiber": (
        "传统染色纤维",
        {"structural_bond": 0.2, "pigment_free": 0.05, "pigment_fixed": 0.15},
    ),
}


def _rules(seed):
    result = {}
    for carrier in (*MATERIALS, "fuel", "mixture"):
        sample = int(hashlib.sha256(f"{seed}:{carrier}".encode()).hexdigest()[:8], 16)
        variation = 0.95 + sample / 0xFFFFFFFF * 0.1
        profile = dict(PROFILE_DEFAULTS)
        profile["strength_scale"] *= variation
        profile["heat_capacity"] *= variation
        if carrier in {"fiber", "pigment"}:
            profile.update(sinter_start=70, sinter_width=180, safe_temperature=250)
        elif carrier == "metal":
            profile.update(sinter_start=500, sinter_width=850, safe_temperature=1700)
        result[carrier] = profile
    return result


def profile_for(c, world_id, state):
    row = c.execute(
        "SELECT rules_json FROM science_workshops WHERE world_id=?", (world_id,)
    ).fetchone()
    if row is None:
        return dict(PROFILE_DEFAULTS)
    profiles = json.loads(row[0])
    return dict(profiles.get(state.carrier, PROFILE_DEFAULTS))


def _item(c, world_id, actor_id, site_id, name, quantity, *, category="material"):
    tid, iid = str(uuid4()), str(uuid4())
    c.execute(
        "INSERT INTO item_types(id,name,category,stack_limit,slot_size) VALUES (?,?,?,?,1)",
        (tid, name, category, quantity),
    )
    c.execute(
        "INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,"
        "quantity,condition,owner_character_id) VALUES (?,?,?,'ground',?,?,100,?)",
        (iid, world_id, tid, site_id, quantity, actor_id),
    )
    return iid


def seed_workshop(c, world_id, at, *, seed):
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("工坊种子须为整数")
    if c.execute("SELECT 1 FROM worlds WHERE id=?", (world_id,)).fetchone() is None:
        raise ValueError("工坊世界不存在")
    prior = c.execute(
        "SELECT seed,assets_json FROM science_workshops WHERE world_id=?", (world_id,)
    ).fetchone()
    if prior is not None:
        if prior["seed"] != seed:
            raise ValueError("工坊已准备，不可换种子重置实物或规律")
        return json.loads(prior["assets_json"])
    name = "workshop_" + uuid4().hex
    c.execute(f"SAVEPOINT {name}")
    try:
        assets = {"actors": {}, "opening_observation_ids": []}
        c.execute(
            "INSERT OR IGNORE INTO material_modes(world_id,policy_json) VALUES (?,?)",
            (world_id, encoded({"mode": "science"})),
        )
        actors = c.execute(
            "SELECT * FROM characters WHERE world_id=? AND is_player=0 AND health>0 ORDER BY id",
            (world_id,),
        ).fetchall()
        for actor in actors:
            aid = actor["id"]
            enrollment = c.execute(
                "SELECT lab_location_id FROM collective_people WHERE character_id=?", (aid,)
            ).fetchone()
            site = (
                enrollment[0]
                if enrollment
                else actor["current_location_id"] or actor["location_id"]
            )
            if (
                c.execute(
                    "SELECT 1 FROM locations WHERE id=? AND world_id=?", (site, world_id)
                ).fetchone()
                is None
            ):
                raise ValueError("人物工坊地点不属于本世界")
            c.execute(
                "INSERT OR IGNORE INTO collective_people(character_id,world_id,lab_location_id) "
                "VALUES (?,?,?)",
                (aid, world_id, site),
            )
            owned = {"site_id": site, "materials": {}, "instruments": {}, "references": {}}
            for carrier, (label, quantity) in MATERIALS.items():
                iid = _item(c, world_id, aid, site, label, quantity)
                state = MaterialState(
                    carrier=carrier,
                    mass=0 if carrier == "water" else quantity,
                    water=quantity if carrier == "water" else quantity * 0.05,
                    pigment_free=quantity * 0.2 if carrier == "pigment" else 0,
                )
                MaterialService.attach(c, iid, state)
                owned["materials"][carrier] = iid
            for carrier, (label, properties) in REFERENCES.items():
                quantity = 10
                iid = _item(c, world_id, aid, site, label, quantity)
                properties = {
                    key: value * quantity if key.startswith("pigment_") else value
                    for key, value in properties.items()
                }
                MaterialService.attach(
                    c, iid, MaterialState(carrier=carrier, mass=quantity, **properties)
                )
                origin = c.execute(
                    "SELECT id FROM material_ledger WHERE world_id=? "
                    "AND operation='origin' AND json_extract(item_ids_json,'$[0]')=?",
                    (world_id, iid),
                ).fetchone()[0]
                c.execute(
                    "INSERT INTO science_reference_samples VALUES (?,?,?,?,?)",
                    (iid, world_id, carrier, origin, to_iso(at)),
                )
                owned["references"][carrier] = iid
            for metric, (label, precision) in INSTRUMENTS.items():
                iid = _item(c, world_id, aid, site, label, 1, category="tool")
                c.execute(
                    "INSERT INTO collective_instruments(item_id,world_id,method,skill_name,"
                    "min_proficiency,duration_hours,detection_limit,precision) VALUES "
                    "(?,?,?,'日常观察',0,0.5,0,?)",
                    (iid, world_id, metric, precision),
                )
                owned["instruments"][metric] = iid
            apparatus = _item(c, world_id, aid, site, "普通手工与炉窑器具", 1, category="tool")
            c.execute(
                "INSERT INTO collective_apparatus(item_id,world_id,operations_json,capacity,"
                "throughput,max_power,skill_name,min_proficiency) VALUES (?,?,?,20,10,5,'工艺',0)",
                (
                    apparatus,
                    world_id,
                    encoded(
                        [
                            "wash",
                            "heat",
                            "cool",
                            "dry",
                            "mix",
                            "bind",
                            "adsorb",
                            "form",
                            "compact",
                            "store",
                        ]
                    ),
                ),
            )
            owned["apparatus"] = apparatus
            fuel = _item(c, world_id, aid, site, "有限工坊燃料", 20)
            MaterialService.attach(c, fuel, MaterialState(carrier="fuel", mass=20))
            c.execute(
                "INSERT INTO science_energy_stocks(item_id,world_id,energy_per_unit) "
                "VALUES (?,?,5000)",
                (fuel, world_id),
            )
            owned["energy"] = fuel
            assets["actors"][aid] = owned
            observation = CollectiveResearchService.record_observation(
                c,
                aid,
                at,
                "所在工坊的常见材料、手工器具和量器已开放，实物归属有记录",
                {
                    "kind": "workshop_opened",
                    **owned,
                    "note": "材料和器具需要本人实际到场使用，用完不会自行补充。",
                },
            )
            assets["opening_observation_ids"].append(observation)
        c.execute(
            "INSERT INTO science_workshops(world_id,seed,assets_json,rules_json,created_at) "
            "VALUES (?,?,?,?,?)",
            (world_id, seed, encoded(assets), encoded(_rules(seed)), to_iso(at)),
        )
        c.execute(f"RELEASE SAVEPOINT {name}")
        return assets
    except Exception:
        c.execute(f"ROLLBACK TO SAVEPOINT {name}")
        c.execute(f"RELEASE SAVEPOINT {name}")
        raise
