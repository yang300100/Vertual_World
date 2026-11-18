"""哥哥，只在隔离世界检验 NPC 不能自行抹掉已发生的伤亡。"""
from datetime import timedelta
from uuid import uuid4

import pytest

from test_aboard_npc_selfcare import add_medicine
from test_explicit_player_actions import scenario  # noqa: F401
from test_rides import ride_world, current  # noqa: F401
from world_engine.actions import ActionService
from world_engine.domain import ActionProposal, ActionType
from world_engine.food import FoodService
from world_engine.inventory import InventoryError
from world_engine.decisions import NpcActionIntent
from world_engine.npc_stage_actions import settle_stage_action


def kill_npc(c, e, monkeypatch):
    # 哥哥，用真实攻击生成倒下来源，而不是只伪造一条死亡记录。
    monkeypatch.setattr("world_engine.actions.offensive_damage", lambda *a, **kw: 200)
    result = ActionService().execute(c, world_id=e.wid, tick_id=str(uuid4()), occurred_at=current(e, c),
        proposal=ActionProposal(actor_id=e.player, target_id=e.npc, action=ActionType.ATTACK, reason="隔离攻击"))
    assert result.accepted
    assert c.execute("SELECT health FROM characters WHERE id=?", (e.npc,)).fetchone()[0] == 0
    assert c.execute("SELECT 1 FROM world_events WHERE world_id=? AND target_id=? AND event_type IN ('world.major_death','action.target_down')", (e.wid, e.npc)).fetchone()


def medicine(c, e, item_id):
    return ActionService().execute(c, world_id=e.wid, tick_id=str(uuid4()), occurred_at=current(e, c),
        proposal=ActionProposal(actor_id=e.npc, action=ActionType.USE, reason="本人用药",
                                metadata={"item": "隔离药", "item_id": item_id}))


def test_real_death_cannot_self_use_medicine(ride_world, monkeypatch):
    e = ride_world
    with e.db.write() as c:
        item_id = add_medicine(c, e)
        kill_npc(c, e, monkeypatch)
        outcome = medicine(c, e, item_id)
        assert not outcome.accepted
        assert c.execute("SELECT health FROM characters WHERE id=?", (e.npc,)).fetchone()[0] == 0
        assert c.execute("SELECT quantity FROM item_instances WHERE id=?", (item_id,)).fetchone()[0] == 2
        assert c.execute("SELECT event_type FROM world_events WHERE id=?", (outcome.event_id,)).fetchone()[0] == "action.rejected"


def test_living_injured_npc_still_uses_own_medicine(ride_world):
    e = ride_world
    with e.db.write() as c:
        item_id = add_medicine(c, e)
        outcome = medicine(c, e, item_id)
        assert outcome.accepted
        assert c.execute("SELECT health FROM characters WHERE id=?", (e.npc,)).fetchone()[0] == 40
        assert c.execute("SELECT quantity FROM item_instances WHERE id=?", (item_id,)).fetchone()[0] == 1


def test_dead_npc_cannot_directly_consume_food_from_stale_actor(ride_world, monkeypatch):
    e = ride_world
    with e.db.write() as c:
        item_id = str(uuid4())
        c.execute("INSERT INTO item_types(id,name,category,stack_limit,usable,heal) VALUES (?,'隔离食物','food',10,1,0)", (item_id,))
        c.execute("INSERT INTO item_instances(id,world_id,item_type_id,container_id,container_type,owner_character_id,quantity,condition,spoils_world_time) VALUES (?,?,?,?,'character_inventory',?,2,100,?)", (item_id, e.wid, item_id, e.npc, e.npc, (current(e, c)+timedelta(days=1)).isoformat()))
        actor = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        food = c.execute("SELECT i.*,t.name,30 AS nutrition FROM item_instances i JOIN item_types t ON t.id=i.item_type_id WHERE i.id=?", (item_id,)).fetchone()
        kill_npc(c, e, monkeypatch)
        before = tuple(c.execute("SELECT health,satiety FROM characters WHERE id=?", (e.npc,)).fetchone())
        with pytest.raises(InventoryError, match="身体状态"):
            FoodService.eat(c, actor, food, current(e, c))
        assert tuple(c.execute("SELECT health,satiety FROM characters WHERE id=?", (e.npc,)).fetchone()) == before
        assert c.execute("SELECT quantity FROM item_instances WHERE id=?", (item_id,)).fetchone()[0] == 2


def test_old_npc_snapshot_cannot_give_item_after_death(ride_world, monkeypatch):
    e = ride_world
    with e.db.write() as c:
        item_id = add_medicine(c, e)
        actor = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        kill_npc(c, e, monkeypatch)
        with pytest.raises(ValueError, match="身体状态"):
            settle_stage_action(c, npc=actor, player_id=e.player, at=current(e, c),
                                action=NpcActionIntent(kind="give_item", target_id=item_id, quantity=1))
        item = c.execute("SELECT owner_character_id,quantity FROM item_instances WHERE id=?", (item_id,)).fetchone()
        assert tuple(item) == (e.npc, 2)


def test_living_npc_still_gives_own_item(ride_world):
    e = ride_world
    with e.db.write() as c:
        item_id = add_medicine(c, e)
        c.execute("UPDATE characters SET inventory_capacity=100 WHERE id=?", (e.player,))
        actor = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        result = settle_stage_action(c, npc=actor, player_id=e.player, at=current(e, c),
                                     action=NpcActionIntent(kind="give_item", target_id=item_id, quantity=1))
        assert result.status == "applied"
        assert c.execute("SELECT SUM(quantity) FROM item_instances WHERE item_type_id=? AND owner_character_id=?", (item_id, e.player)).fetchone()[0] == 1


def test_player_defeat_keeps_existing_recovery(database, scenario, monkeypatch):
    # 哥哥，玩家现有的战斗败北苏醒规则独立于 NPC 伤亡。
    engine, _, wid, player, npc, _ = scenario
    with database.write() as c:
        c.execute("UPDATE characters SET health=5 WHERE id=?", (player,))
    monkeypatch.setattr("world_engine.actions.offensive_damage", lambda *a, **kw: 1)
    monkeypatch.setattr("world_engine.actions.counter_damage", lambda *a, **kw: 200)
    result = engine.submit_player_intent(wid, "动作：攻击", target_character_id=npc)
    assert result.outcome.accepted
    with database.read() as c:
        assert c.execute("SELECT health FROM characters WHERE id=?", (player,)).fetchone()[0] == 60
        assert c.execute("SELECT 1 FROM world_events WHERE world_id=? AND event_type='world.player_defeat'", (wid,)).fetchone()
