# ruff: noqa: F811
"""Yuki 验证重大消息沿真实接触传播，并成为收件人的个人计划。"""

import json
from datetime import timedelta
from uuid import uuid4

from test_lived_world import town  # noqa: F401

from world_engine.actions import ActionService
from world_engine.domain import ActionProposal, ActionType
from world_engine.engine import WorldEngine
from world_engine.society import SocietyService


def test_public_rumor_reaches_another_region_after_world_time_delay(town):
    e = town
    with e.db.write() as c:
        recipient = c.execute(
            "SELECT id FROM characters WHERE world_id=? AND is_player=0 AND id!=? LIMIT 1",
            (e.wid, e.npc),
        ).fetchone()["id"]
        remote_id = str(uuid4())
        c.execute(
            "INSERT INTO locations(id,world_id,name,kind,longitude,latitude) "
            "VALUES (?,?,?,?,?,?)",
            (remote_id, e.wid, "远方驿城", "city", e.loc["longitude"] + 1, e.loc["latitude"]),
        )
        c.execute(
            "UPDATE characters SET location_id=?,current_location_id=?,longitude=?,latitude=? "
            "WHERE id=?",
            (remote_id, remote_id, e.loc["longitude"] + 1, e.loc["latitude"], recipient),
        )
        c.execute(
            "INSERT INTO relationships(world_id,source_character_id,target_character_id,"
            "affinity,trust,updated_at) VALUES (?,?,?,?,?,?)",
            (e.wid, recipient, e.player, 30, 30, e.at.isoformat()),
        )
        news_id = ActionService._record_event(
            c, world_id=e.wid, tick_id=str(uuid4()), occurred_at=e.at,
            event_type="world.major_death", actor_id=e.npc, target_id=e.player,
            location_id=e.loc["id"], summary="集市发生致命冲突。", payload={},
        )
        SocietyService.tick(c, e.wid, e.at + timedelta(hours=1))
        assert c.execute(
            "SELECT 1 FROM character_event_knowledge WHERE character_id=? AND event_id=?",
            (recipient, news_id),
        ).fetchone() is None
        SocietyService.tick(c, e.wid, e.at + timedelta(days=1))
        learned = c.execute(
            "SELECT source_kind,source_character_id,confidence,hops,learned_world_time "
            "FROM character_event_knowledge WHERE character_id=? AND event_id=?",
            (recipient, news_id),
        ).fetchone()
        assert learned is not None
        assert (learned["source_kind"], learned["source_character_id"], learned["hops"]) == (
            "rumor", e.npc, 1,
        )
        assert learned["confidence"] < 1
        assert learned["learned_world_time"] == (e.at + timedelta(days=1)).isoformat()
        assert c.execute(
            "SELECT details FROM npc_todos WHERE character_id=? AND source_event_id=?",
            (recipient, news_id),
        ).fetchone()["details"].startswith("听到跨地区公共传闻")
        assert c.execute(
            "SELECT COUNT(*) FROM world_events WHERE event_type='social.public_rumor' "
            "AND actor_id=? AND target_id=?",
            (e.npc, recipient),
        ).fetchone()[0] == 1
        assert c.execute(
            "SELECT 1 FROM character_acquaintances WHERE observer_id=? AND subject_id=?",
            (recipient, e.npc),
        ).fetchone() is None
        SocietyService.tick(c, e.wid, e.at + timedelta(days=2))
        assert c.execute(
            "SELECT COUNT(*) FROM world_events WHERE event_type='social.public_rumor' "
            "AND actor_id=? AND target_id=?",
            (e.npc, recipient),
        ).fetchone()[0] == 1


def test_private_major_event_never_enters_public_rumor(town):
    e = town
    with e.db.write() as c:
        recipient = c.execute(
            "SELECT id FROM characters WHERE world_id=? AND is_player=0 AND id!=? LIMIT 1",
            (e.wid, e.npc),
        ).fetchone()["id"]
        remote_id = str(uuid4())
        c.execute(
            "INSERT INTO locations(id,world_id,name,kind,longitude,latitude) "
            "VALUES (?,?,?,?,?,?)",
            (remote_id, e.wid, "隐秘驿城", "city", e.loc["longitude"] + 1, e.loc["latitude"]),
        )
        c.execute(
            "UPDATE characters SET location_id=?,current_location_id=?,longitude=longitude+1 "
            "WHERE id=?", (remote_id, remote_id, recipient),
        )
        news_id = ActionService._record_event(
            c, world_id=e.wid, tick_id=str(uuid4()), occurred_at=e.at,
            event_type="world.major_death", actor_id=e.npc, target_id=e.player,
            location_id=e.loc["id"], summary="一件隐秘的大事。", payload={},
        )
        c.execute(
            "UPDATE world_events SET payload_json=? WHERE id=?",
            (json.dumps({"information_scope": "private"}), news_id),
        )
        SocietyService.tick(c, e.wid, e.at + timedelta(days=2))
        assert c.execute(
            "SELECT 1 FROM character_event_knowledge WHERE character_id=? AND event_id=?",
            (recipient, news_id),
        ).fetchone() is None


def test_expired_todo_is_not_injected_into_npc_decision(town, monkeypatch):
    e = town
    with e.db.write() as c:
        for title, due in (
            ("过期线索", e.at - timedelta(minutes=1)),
            ("仍有效的线索", e.at + timedelta(hours=1)),
        ):
            c.execute(
                "INSERT INTO npc_todos(id,world_id,character_id,title,due_world_time,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                (str(uuid4()), e.wid, e.npc, title, due.isoformat(),
                 e.at.isoformat(), e.at.isoformat()),
            )
    engine = WorldEngine(e.db, e.settings)
    captured = []

    def capture(snapshot, character_ids):
        captured.extend(item for item in snapshot.characters if item.id == e.npc)
        return []

    monkeypatch.setattr(engine, "_select_active_characters", capture)
    engine.adjudicate(e.wid, trigger="manual", character_ids=[e.npc])
    assert captured
    assert any("仍有效的线索" in goal for goal in captured[0].goals)
    assert all("过期线索" not in goal for goal in captured[0].goals)


def test_major_event_is_shared_in_person_and_creates_related_npc_plan(town):
    e = town
    with e.db.write() as c:
        recipient = c.execute(
            "SELECT id FROM characters WHERE world_id=? AND is_player=0 AND id!=? LIMIT 1",
            (e.wid, e.npc),
        ).fetchone()["id"]
        c.execute(
            "UPDATE characters SET longitude=longitude+1 WHERE id=?", (recipient,)
        )
        c.execute(
            "INSERT INTO relationships(world_id,source_character_id,target_character_id,"
            "affinity,trust,updated_at) VALUES (?,?,?,?,?,?)",
            (e.wid, recipient, e.player, 30, 30, e.at.isoformat()),
        )
        news_id = ActionService._record_event(
            c,
            world_id=e.wid,
            tick_id=str(uuid4()),
            occurred_at=e.at,
            event_type="world.major_death",
            actor_id=e.npc,
            target_id=e.player,
            location_id=e.loc["id"],
            summary="一位旅人在集市冲突中身亡。",
            payload={},
        )
        event = c.execute("SELECT payload_json FROM world_events WHERE id=?", (news_id,)).fetchone()
        assert json.loads(event["payload_json"])["information_scope"] == "public"
        assert c.execute(
            "SELECT 1 FROM character_event_knowledge WHERE character_id=? AND event_id=?",
            (recipient, news_id),
        ).fetchone() is None

        c.execute(
            "UPDATE characters SET current_location_id=?,longitude=?,latitude=? WHERE id=?",
            (e.loc["id"], e.loc["longitude"], e.loc["latitude"], recipient),
        )
        outcome = ActionService().execute(
            c,
            world_id=e.wid,
            tick_id=str(uuid4()),
            occurred_at=e.at,
            proposal=ActionProposal(
                actor_id=e.npc,
                target_id=recipient,
                action=ActionType.SOCIALIZE,
                reason="转告集市的大事",
                dialogue="集市刚发生了严重冲突。",
            ),
        )
        assert outcome.accepted
        knowledge = c.execute(
            "SELECT source_kind,source_character_id,hops FROM character_event_knowledge "
            "WHERE character_id=? AND event_id=?",
            (recipient, news_id),
        ).fetchone()
        assert knowledge is not None
        assert (knowledge["source_kind"], knowledge["source_character_id"], knowledge["hops"]) == (
            "heard", e.npc, 1,
        )
        plan = c.execute(
            "SELECT title,source_event_id FROM npc_todos WHERE character_id=? AND source_event_id=?",
            (recipient, news_id),
        ).fetchone()
        assert plan is not None
        assert plan["source_event_id"] == news_id
        assert "打听" in plan["title"]
        ActionService().execute(
            c,
            world_id=e.wid,
            tick_id=str(uuid4()),
            occurred_at=e.at,
            proposal=ActionProposal(
                actor_id=e.npc, target_id=recipient, action=ActionType.SOCIALIZE,
                reason="继续讨论集市事件",
            ),
        )
        assert c.execute(
            "SELECT COUNT(*) FROM npc_todos WHERE character_id=? AND source_event_id=?",
            (recipient, news_id),
        ).fetchone()[0] == 1


def test_major_event_witness_with_personal_stake_forms_plan(town):
    e = town
    with e.db.write() as c:
        witness = c.execute(
            "SELECT id FROM characters WHERE world_id=? AND is_player=0 AND id!=? LIMIT 1",
            (e.wid, e.npc),
        ).fetchone()["id"]
        c.execute(
            "UPDATE characters SET current_location_id=?,longitude=?,latitude=?,"
            "activation_state='active' WHERE id=?",
            (e.loc["id"], e.loc["longitude"], e.loc["latitude"], witness),
        )
        c.execute("UPDATE characters SET goals_json='[]' WHERE id=?", (witness,))
        c.execute("UPDATE world_runtime SET tick_count=1 WHERE world_id=?", (e.wid,))
        c.execute(
            "INSERT INTO relationships(world_id,source_character_id,target_character_id,"
            "affinity,trust,updated_at) VALUES (?,?,?,?,?,?)",
            (e.wid, witness, e.player, 30, 30, e.at.isoformat()),
        )
        news_id = ActionService._record_event(
            c,
            world_id=e.wid,
            tick_id=str(uuid4()),
            occurred_at=e.at,
            event_type="world.major_death",
            actor_id=e.npc,
            target_id=e.player,
            location_id=e.loc["id"],
            summary="集市冲突致人死亡。",
            payload={},
        )
        assert c.execute(
            "SELECT source_kind FROM character_event_knowledge "
            "WHERE character_id=? AND event_id=?",
            (witness, news_id),
        ).fetchone()[0] == "experienced"
        assert c.execute(
            "SELECT 1 FROM npc_todos WHERE character_id=? AND source_event_id=?",
            (witness, news_id),
        ).fetchone() is not None
    result = WorldEngine(e.db, e.settings).adjudicate(
        e.wid, trigger="manual", character_ids=[witness]
    )
    assert any(item.actor_id == witness and item.action is ActionType.SOCIALIZE
               and item.accepted for item in result.outcomes)


def test_known_npcs_proactively_share_major_news_when_they_meet(town):
    e = town
    with e.db.write() as c:
        recipient = c.execute(
            "SELECT id FROM characters WHERE world_id=? AND is_player=0 AND id!=? LIMIT 1",
            (e.wid, e.npc),
        ).fetchone()["id"]
        c.execute("UPDATE characters SET longitude=longitude+1 WHERE id=?", (recipient,))
        for source, target in ((e.npc, recipient), (recipient, e.player)):
            c.execute(
                "INSERT INTO relationships(world_id,source_character_id,target_character_id,"
                "affinity,trust,updated_at) VALUES (?,?,?,?,?,?)",
                (e.wid, source, target, 30, 30, e.at.isoformat()),
            )
        news_id = ActionService._record_event(
            c,
            world_id=e.wid, tick_id=str(uuid4()), occurred_at=e.at,
            event_type="world.major_death", actor_id=e.npc, target_id=e.player,
            location_id=e.loc["id"], summary="集市发生致命冲突。", payload={},
        )
        SocietyService.tick(c, e.wid, e.at)
        assert c.execute(
            "SELECT 1 FROM character_event_knowledge WHERE character_id=? AND event_id=?",
            (recipient, news_id),
        ).fetchone() is None
        c.execute(
            "UPDATE characters SET current_location_id=?,longitude=?,latitude=? WHERE id=?",
            (e.loc["id"], e.loc["longitude"], e.loc["latitude"], recipient),
        )
    e.clock.heartbeat(e.wid, elapsed_seconds=60)
    with e.db.read() as c:
        learned = c.execute(
            "SELECT source_kind,hops FROM character_event_knowledge WHERE character_id=? AND event_id=?",
            (recipient, news_id),
        ).fetchone()
        assert learned is not None
        assert (learned["source_kind"], learned["hops"]) == ("heard", 1)
        assert c.execute(
            "SELECT COUNT(*) FROM world_events WHERE actor_id=? AND target_id=? "
            "AND event_type='social.news_shared'",
            (e.npc, recipient),
        ).fetchone()[0] == 1
    e.clock.heartbeat(e.wid, elapsed_seconds=60)
    with e.db.read() as c:
        assert c.execute(
            "SELECT COUNT(*) FROM world_events WHERE actor_id=? AND target_id=? "
            "AND event_type='social.news_shared'",
            (e.npc, recipient),
        ).fetchone()[0] == 1
