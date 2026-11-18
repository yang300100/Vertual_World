"""哥哥，集体研究和科研测试共用独立工坊，不依赖测试收集顺序。"""

from uuid import uuid4

import pytest

from world_engine.collective_research import CollectiveResearchService
from world_engine.material_physics import MaterialState
from world_engine.materials import MaterialService
from world_engine.repository import WorldRepository, to_iso


@pytest.fixture
def lab(database):
    r = CollectiveResearchService
    with database.write() as c:
        wid = WorldRepository().create_world(
            c, name="阶段研究", minutes_per_tick=60, seed_demo=False, seed_maps=False
        )
        at = WorldRepository().get_snapshot(c, wid).world.current_time
        loc = str(uuid4())
        c.execute(
            "INSERT INTO locations(id,world_id,name,kind,longitude,latitude) "
            "VALUES (?,?,'试验工坊','city',0,0)",
            (loc, wid),
        )
        actors = []
        for name in ("甲工匠", "乙检验者"):
            aid = str(uuid4())
            actors.append(aid)
            c.execute(
                "INSERT INTO characters(id,world_id,name,identity,location_id,current_location_id,"
                "longitude,latitude,energy,satiety,money,health,skills_json,goals_json,"
                "created_at,updated_at) "
                "VALUES (?,?,?,'工坊居民',?,?,0,0,100,100,100,100,'[\"检验\"]','[]',?,?)",
                (aid, wid, name, loc, loc, to_iso(at), to_iso(at)),
            )
            c.execute(
                "INSERT INTO collective_people(character_id,world_id,lab_location_id) "
                "VALUES (?,?,?)",
                (aid, wid, loc),
            )
            c.execute("UPDATE characters SET inventory_capacity=12 WHERE id=?", (aid,))
            c.execute(
                "INSERT INTO character_skill_proficiencies"
                "(world_id,character_id,skill_name,proficiency,updated_at) VALUES (?,?,?,?,?)",
                (wid, aid, "检验", 80, to_iso(at)),
            )
        tid = str(uuid4())
        c.execute(
            "INSERT INTO item_types(id,name,category,stack_limit,slot_size) "
            "VALUES (?,'试验原粮','food',1000,1)",
            (tid,),
        )
        batches = []
        for aid in actors:
            iid = str(uuid4())
            batches.append(iid)
            c.execute(
                "INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,"
                "owner_character_id,quantity,condition) "
                "VALUES (?,?,?,'character_inventory',?,?,100,100)",
                (iid, wid, tid, aid, aid),
            )
            MaterialService.attach(
                c, iid, MaterialState(mass=100, mobile=20, particles=1, nutrition=2000)
            )
        tool_type, tool = str(uuid4()), str(uuid4())
        c.execute(
            "INSERT INTO item_types(id,name,category,stack_limit,slot_size) "
            "VALUES (?,'浸提检验器','tool',1,1)",
            (tool_type,),
        )
        c.execute(
            "INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,"
            "owner_character_id,quantity,condition) "
            "VALUES (?,?,?,'character_inventory',?,?,1,100)",
            (tool, wid, tool_type, actors[0], actors[0]),
        )
        c.execute(
            "INSERT INTO collective_instruments(item_id,world_id,method,skill_name,"
            "min_proficiency,duration_hours,detection_limit,precision) VALUES (?,?,?,?,?,?,?,?)",
            (tool, wid, "extractable_salt", "检验", 40, 1, 0.01, 0.01),
        )
        r.record_observation(
            c,
            actors[0],
            at,
            "看见灰雨落入原粮",
            {"kind": "eyewitness", "appearance": "粮食表面有灰色细粉"},
        )
    return database, wid, actors, batches, tool, at
