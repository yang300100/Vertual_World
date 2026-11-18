"""哥哥，见面与重逢要来自真实在场，不靠姓名、关系分数或重复心跳补历史。"""

# ruff: noqa: F811
import json
import sqlite3
from datetime import timedelta
from uuid import uuid4

import pytest
from test_lived_world import post, town  # noqa: F401

from world_engine.database import Database
from world_engine.encounters import ENCOUNTER_SCHEMA, EncounterService
from world_engine.event_log import record_event
from world_engine.repository import to_iso


@pytest.fixture
def scene(town):
    e = town
    with e.db.write() as c:
        c.executescript(ENCOUNTER_SCHEMA)
        c.execute(
            "UPDATE characters SET longitude=?,latitude=?,current_room_id=NULL,space_layer='ground' "
            "WHERE id IN (?,?)",
            (e.loc["longitude"], e.loc["latitude"], e.npc, e.player),
        )
    return e


def observe(c, e, at=None):
    return EncounterService.observe_pair(c, e.wid, e.npc, e.player, at or e.at)


def current(c, e, at=None):
    return EncounterService.current(c, e.wid, e.npc, e.player, at or e.at)


def opportunity(c, e, at=None):
    return EncounterService.opportunity(c, e.wid, e.npc, e.player, at or e.at)


def position(c, e, *, far=False):
    c.execute("UPDATE characters SET longitude=? WHERE id=?",
              (e.loc["longitude"] + (0.001 if far else 0), e.npc))


def queue(c, e, *, at=None, **fields):
    values = {"id": str(uuid4()), "world_id": e.wid, "npc_id": e.npc,
              "player_id": e.player, "channel": "in_person", "reason": "遇见对方，可以自主决定交谈",
              "world_day": (at or e.at).date().isoformat(), **fields}
    c.execute("INSERT INTO npc_outreach_jobs(" + ",".join(values) + ") VALUES ("
              + ",".join("?" for _ in values) + ")", tuple(values.values()))
    return dict(c.execute("SELECT * FROM npc_outreach_jobs WHERE id=?", (values["id"],)).fetchone())


def attach(c, e, binding):
    job = queue(c, e)
    assert EncounterService.attach(c, job["id"], binding, e.at)
    return dict(c.execute("SELECT * FROM npc_outreach_jobs WHERE id=?", (job["id"],)).fetchone())


def fact(c, e, *, kind="action.socialize", actor=None, target=None, at=None, payload=None):
    return record_event(c, world_id=e.wid, tick_id=str(uuid4()), occurred_at=at or e.at,
                        event_type=kind, actor_id=actor or e.npc, target_id=target,
                        location_id=e.loc["id"], summary="一段有真实来源的共同见闻。",
                        payload=payload or {})


def damage_source(c, e, binding, damage):
    """哥哥，这些隔离故障都不该继续支持本次见面的交谈机会。"""
    values = (binding["source_event_id"], e.player)
    if damage == "missing_knowledge":
        c.execute("DELETE FROM character_event_knowledge WHERE event_id=? AND character_id=?", values)
    elif damage == "rumor":
        c.execute("UPDATE character_event_knowledge SET source_kind='rumor' "
                  "WHERE event_id=? AND character_id=?", values)
    elif damage == "future_knowledge":
        c.execute("UPDATE character_event_knowledge SET learned_world_time=? "
                  "WHERE event_id=? AND character_id=?",
                  (to_iso(e.at + timedelta(days=1)), *values))
    else:
        c.execute("UPDATE event_observers SET channel='heard' WHERE event_id=? AND character_id=?", values)


def test_schema_and_first_observation_do_not_create_people_relations_or_consent(scene):
    e = scene
    with e.db.write() as c:
        c.executescript(ENCOUNTER_SCHEMA)
        assert c.execute("SELECT COUNT(*) FROM encounter_episodes").fetchone()[0] == 0
        people = [tuple(r) for r in c.execute("SELECT * FROM characters ORDER BY id")]
        relationships = [tuple(r) for r in c.execute("SELECT * FROM relationships ORDER BY rowid")]
        binding = observe(c, e)
        assert binding and binding["source_event_id"]
        assert [tuple(r) for r in c.execute("SELECT * FROM characters ORDER BY id")] == people
        assert [tuple(r) for r in c.execute("SELECT * FROM relationships ORDER BY rowid")] == relationships
        assert not c.execute("SELECT 1 FROM companion_sessions WHERE status='active'").fetchone()
        for observer in (e.npc, e.player):
            assert EncounterService.context(c, binding, observer)["kind"] == "first"
            assert c.execute("SELECT 1 FROM character_event_knowledge WHERE character_id=? AND event_id=?",
                             (observer, binding["source_event_id"])).fetchone()


def test_continuous_presence_repeated_schema_and_restart_never_replay_a_meeting(scene):
    e = scene
    with e.db.write() as c:
        first = observe(c, e)
        again = observe(c, e, e.at + timedelta(minutes=1))
        assert first["episode_id"] == again["episode_id"]
        assert again["last_seen_at"] == to_iso(e.at + timedelta(minutes=1))
    reopened = Database(e.settings.database_path)
    reopened.initialize()
    with reopened.write() as c:
        for at in (e.at + timedelta(minutes=61), e.at + timedelta(days=1)):
            live = observe(c, e, at)
            assert live["episode_id"] == first["episode_id"]
            assert opportunity(c, e, at) is None
        assert c.execute("SELECT COUNT(*) FROM encounter_episodes").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM world_events WHERE id=?", (first["source_event_id"],)).fetchone()[0] == 1


def test_actual_separation_then_return_creates_new_source_and_each_person_recognizes_reunion(scene):
    e = scene
    with e.db.write() as c:
        first = observe(c, e)
        position(c, e, far=True)
        assert current(c, e, e.at + timedelta(minutes=1)) is None
        assert observe(c, e, e.at + timedelta(minutes=1)) is None
        position(c, e)
        second = observe(c, e, e.at + timedelta(minutes=2))
        assert first["episode_id"] != second["episode_id"]
        assert first["source_event_id"] != second["source_event_id"]
        for observer in (e.npc, e.player):
            context = EncounterService.context(c, second, observer)
            assert context["kind"] == "reunion"
            assert context["prior_seen_event_id"] == first["source_event_id"]
            assert first["source_event_id"] in [r["source_event_id"] for r in context["shared_history"]]


def test_position_change_event_closes_existing_scene_between_society_ticks(scene):
    e = scene
    with e.db.write() as c:
        first = observe(c, e)
        fact(c, e, kind="action.movement_started", at=e.at + timedelta(seconds=1))
        assert current(c, e, e.at + timedelta(seconds=1))["episode_id"] == first["episode_id"]
        position(c, e, far=True)
        fact(c, e, kind="action.movement_arrived", at=e.at + timedelta(seconds=2))
        assert c.execute("SELECT ended_at FROM encounter_episodes WHERE id=?",
                         (first["episode_id"],)).fetchone()[0] == to_iso(e.at + timedelta(seconds=2))
        position(c, e)
        fact(c, e, kind="action.movement_arrived", at=e.at + timedelta(seconds=3))
        assert c.execute("SELECT COUNT(*) FROM encounter_episodes").fetchone()[0] == 1
        second = observe(c, e, e.at + timedelta(seconds=3))
        assert second["episode_id"] != first["episode_id"]
        assert EncounterService.context(c, second, e.player)["kind"] == "reunion"
        assert EncounterService.context(c, first, e.player) is None


def test_real_door_round_trip_between_ticks_creates_reunion_without_private_room_leak(scene):
    from test_appointment_rooms import door, room

    from world_engine.actions import ActionService
    from world_engine.conversations import ConversationService
    from world_engine.repository import WorldRepository
    from world_engine.roleplay import npc_reply_system_prompt
    from world_engine.roleplay_pipeline import build_npc_prompt
    from world_engine.society import SocietyService

    e = scene
    with e.db.write() as c:
        first = observe(c, e)
        job = attach(c, e, first)
    private_name = "哥哥的隔离私密书房"
    rid = room(e, owner=e.player, name=private_name)
    for operation in ("open", "enter", "exit"):
        response = door(e, rid, operation)
        assert response.status_code == 200, response.text
    with e.db.write() as c:
        assert current(c, e) is None
        assert c.execute("SELECT COUNT(*) FROM encounter_episodes").fetchone()[0] == 1
        second = observe(c, e)
        assert second["episode_id"] != first["episode_id"]
        view = EncounterService.context(c, second, e.npc)
        assert view["kind"] == "reunion"
        assert private_name not in json.dumps(view, ensure_ascii=False)
        assert private_name not in json.dumps(
            SocietyService.personal_context(c, e.wid, e.npc, e.at), ensure_ascii=False,
        )
        assert not EncounterService.current_job(c, job, e.at)
        exited = c.execute(
            "SELECT id,summary FROM world_events WHERE world_id=? AND actor_id=? "
            "AND event_type='action.interior_exit' ORDER BY rowid DESC LIMIT 1",
            (e.wid, e.player),
        ).fetchone()
        # 哥哥，旧记忆可能已有作者摘要；只读裁剪不能偷偷改掉真实历史。
        ActionService._record_memory(
            c, world_id=e.wid, character_id=e.npc, event_id=exited["id"],
            memory_type="experienced", summary=exited["summary"], importance=9,
        )
        service = ConversationService()
        memories = service._relevant_memories(
            c, world_id=e.wid, npc_id=e.npc, query="刚才离开房间的见闻",
        )
        assert private_name not in json.dumps(memories, ensure_ascii=False)
        assert private_name in c.execute(
            "SELECT summary FROM character_memories WHERE character_id=? AND event_id=?",
            (e.npc, exited["id"]),
        ).fetchone()[0]
        assert private_name in SocietyService.event_summary(
            c, e.wid, e.player, exited["id"], exited["summary"],
        )
        snapshot = WorldRepository().get_snapshot(c, e.wid)
        context = service.build_npc_reply_context(
            c, snapshot=snapshot, npc=snapshot.character_by_id(e.npc),
            player=snapshot.character_by_id(e.player), conversation=None,
            player_text="刚才有人离开房间了吗？我想回忆刚才。",
        )
        assert private_name not in json.dumps(context, ensure_ascii=False)
        unknown_player_name = snapshot.character_by_id(e.player).name
        assert unknown_player_name not in json.dumps(context, ensure_ascii=False)
        prompt = build_npc_prompt(npc_reply_system_prompt(), context)
        assert private_name not in json.dumps(prompt.messages, ensure_ascii=False)
        assert unknown_player_name not in json.dumps(prompt.messages, ensure_ascii=False)


@pytest.mark.parametrize("damage", ["missing_knowledge", "rumor", "future_knowledge", "heard_only"])
def test_both_participants_need_current_experienced_source_before_contact(scene, damage):
    e = scene
    with e.db.write() as c:
        binding = observe(c, e)
        job = attach(c, e, binding)
        damage_source(c, e, binding, damage)
        assert current(c, e) is None
        assert not EncounterService.current_job(c, job, e.at)
        assert EncounterService.context(c, binding, e.npc) is None
        assert observe(c, e) is None
        assert c.execute("SELECT COUNT(*) FROM encounter_episodes").fetchone()[0] == 1


def test_corrected_prior_knowledge_does_not_keep_stale_recognition(scene):
    e = scene
    with e.db.write() as c:
        first = observe(c, e)
        position(c, e, far=True)
        observe(c, e, e.at + timedelta(minutes=1))
        position(c, e)
        second = observe(c, e, e.at + timedelta(minutes=2))
        c.execute("UPDATE character_event_knowledge SET source_kind='rumor' "
                  "WHERE event_id=? AND character_id=?", (first["source_event_id"], e.npc))
        npc_view = EncounterService.context(c, second, e.npc)
        assert npc_view["kind"] == "first" and npc_view["prior_seen_event_id"] is None
        assert not npc_view["shared_history"]
        assert EncounterService.context(c, second, e.player)["kind"] == "reunion"


@pytest.mark.parametrize("boundary", ["distance", "layer", "room", "dead", "no_coordinates"])
def test_real_spatial_and_living_boundaries_invalidate_before_tick_or_model_delivery(scene, boundary):
    e = scene
    room_id = None
    if boundary == "room":
        proposal = post(e, "interior-layouts", {"idempotency_key": str(uuid4()), "room": {
            "name": "隔离见面边界房间", "location_id": e.loc["id"],
            "owner_character_id": e.npc, "fixtures": [],
        }}, 201)
        room_id = post(e, "registrations/" + proposal["registration"]["id"] + "/confirm")["result_entity_id"]
    with e.db.write() as c:
        binding = observe(c, e)
        job = attach(c, e, binding)
        if boundary == "distance":
            position(c, e, far=True)
        elif boundary == "layer":
            c.execute("UPDATE characters SET space_layer='underground' WHERE id=?", (e.npc,))
        elif boundary == "room":
            c.execute("UPDATE characters SET current_room_id=? WHERE id=?", (room_id, e.npc))
        elif boundary == "dead":
            c.execute("UPDATE characters SET health=0 WHERE id=?", (e.npc,))
        else:
            c.execute("UPDATE characters SET longitude=NULL WHERE id=?", (e.npc,))
        assert current(c, e) is None
        assert not EncounterService.current_job(c, job, e.at)
        assert observe(c, e) is None
        assert c.execute("SELECT ended_at FROM encounter_episodes WHERE id=?", (binding["episode_id"],)).fetchone()[0]


def test_invalid_world_role_and_self_pairs_cannot_manufacture_meetings(scene):
    e = scene
    with e.db.write() as c:
        for wid, npc, player in (("another-world", e.npc, e.player), (e.wid, e.player, e.npc), (e.wid, e.npc, e.npc)):
            assert EncounterService.observe_pair(c, wid, npc, player, e.at) is None
        assert c.execute("SELECT COUNT(*) FROM encounter_episodes").fetchone()[0] == 0


def test_relationship_scores_acquaintance_counts_and_remote_contact_do_not_invent_prior_meeting(scene):
    e = scene
    with e.db.write() as c:
        fact(c, e, kind="social.message_sent", target=e.player, at=e.at - timedelta(minutes=1))
        for observer, other in ((e.npc, e.player), (e.player, e.npc)):
            c.execute("INSERT INTO character_acquaintances(observer_id,subject_id,world_id,encounters,first_seen,last_seen) "
                      "VALUES (?,?,?,999,?,?) ON CONFLICT(observer_id,subject_id) DO UPDATE SET encounters=999",
                      (observer, other, e.wid, to_iso(e.at), to_iso(e.at)))
        binding = observe(c, e)
        for observer in (e.npc, e.player):
            assert EncounterService.context(c, binding, observer)["kind"] == "first"


def test_each_view_uses_own_witnessed_history_without_turning_rumor_or_future_into_recognition(scene):
    e = scene
    with e.db.write() as c:
        seen = fact(c, e, kind="action.work", at=e.at - timedelta(minutes=1))
        future = fact(c, e, kind="action.encounter", actor=e.player, target=e.npc, at=e.at + timedelta(days=1))
        binding = observe(c, e)
        npc = EncounterService.context(c, binding, e.npc)
        player = EncounterService.context(c, binding, e.player)
        assert npc["kind"] == "first" and npc["prior_seen_event_id"] is None
        assert player["kind"] == "reunion" and player["prior_seen_event_id"] == seen
        assert future not in json.dumps([npc, player])
        assert seen in [r["source_event_id"] for r in player["shared_history"]]


def test_prior_rumor_and_private_unshared_events_stay_out_of_shared_history(scene):
    e = scene
    with e.db.write() as c:
        rumor = fact(c, e, kind="action.encounter", target=e.player, at=e.at - timedelta(minutes=2))
        c.execute("UPDATE character_event_knowledge SET source_kind='rumor' WHERE event_id=?", (rumor,))
        private = fact(c, e, kind="private.note", at=e.at - timedelta(minutes=1))
        binding = observe(c, e)
        for observer in (e.npc, e.player):
            view = EncounterService.context(c, binding, observer)
            assert view["kind"] == "first"
            ids = [r["source_event_id"] for r in view["shared_history"]]
            assert rumor not in ids and private not in ids


def test_context_masks_unknown_names_and_omits_coordinates_or_private_recognition(scene):
    e = scene
    with e.db.write() as c:
        binding = observe(c, e)
        name = c.execute("SELECT name FROM characters WHERE id=?", (e.npc,)).fetchone()[0]
        view = EncounterService.context(c, binding, e.player)
        assert name not in json.dumps(view, ensure_ascii=False)
        assert {"longitude", "latitude", "npc_seen_event_id", "player_seen_event_id"}.isdisjoint(view)
        outsider = c.execute("SELECT id FROM characters WHERE world_id=? AND id NOT IN (?,?) LIMIT 1",
                             (e.wid, e.npc, e.player)).fetchone()[0]
        assert EncounterService.context(c, binding, outsider) is None


def test_attach_is_idempotent_single_consideration_and_job_cleanup_does_not_reset_it(scene):
    e = scene
    with e.db.write() as c:
        binding = observe(c, e)
        job = attach(c, e, binding)
        assert EncounterService.attach(c, job["id"], binding, e.at)
        assert EncounterService.for_job(c, job, e.at)["episode_id"] == binding["episode_id"]
        assert opportunity(c, e) is None
        c.execute("UPDATE npc_outreach_jobs SET status='declined' WHERE id=?", (job["id"],))
        assert not EncounterService.current_job(c, job, e.at)
        c.execute("DELETE FROM npc_outreach_jobs WHERE id=?", (job["id"],))
        assert opportunity(c, e) is None
        later_job = queue(c, e)
        assert not EncounterService.attach(c, later_job["id"], binding, e.at)


@pytest.mark.parametrize("fields", [
    {"channel": "letter"}, {"reason": "有临近的约定需要优先商量"},
    {"attempts": 1}, {"status": "failed"}, {"world_day": "2029-12-31"},
])
def test_attach_preserves_existing_priority_channel_attempt_and_day_boundaries(scene, fields):
    e = scene
    with e.db.write() as c:
        binding = observe(c, e)
        job = queue(c, e, **fields)
        before = tuple(c.execute("SELECT * FROM npc_outreach_jobs WHERE id=?", (job["id"],)).fetchone())
        assert not EncounterService.attach(c, job["id"], binding, e.at)
        assert tuple(c.execute("SELECT * FROM npc_outreach_jobs WHERE id=?", (job["id"],)).fetchone()) == before
        assert opportunity(c, e)


def test_existing_source_expiry_new_episode_and_wrong_identity_cannot_rebind_or_deliver_old_job(scene):
    e = scene
    with e.db.write() as c:
        binding = observe(c, e)
        job = attach(c, e, binding)
        assert EncounterService.current_job(c, job, e.at + timedelta(minutes=59))
        assert not EncounterService.current_job(c, job, e.at + timedelta(minutes=60))
        assert current(c, e, e.at + timedelta(minutes=60))
        forged = {**job, "player_id": e.npc}
        assert EncounterService.for_job(c, forged, e.at) is None
        position(c, e, far=True)
        observe(c, e, e.at + timedelta(minutes=1))
        position(c, e)
        newer = observe(c, e, e.at + timedelta(minutes=2))
        assert newer["episode_id"] != binding["episode_id"]
        assert not EncounterService.current_job(c, job, e.at + timedelta(minutes=2))
        assert not EncounterService.attach(c, job["id"], binding, e.at + timedelta(minutes=2))
        assert not EncounterService.attach(c, job["id"], newer, e.at + timedelta(minutes=2))
        assert EncounterService.current_job(c, {**job, "reason": "普通事务"}, e.at)


def test_stale_time_and_damaged_event_do_not_replay_or_validate_an_episode(scene):
    e = scene
    with e.db.write() as c:
        binding = observe(c, e)
        observe(c, e, e.at + timedelta(minutes=2))
        assert observe(c, e, e.at + timedelta(minutes=1)) is None
        c.execute("UPDATE world_events SET payload_json='{}' WHERE id=?", (binding["source_event_id"],))
        assert current(c, e, e.at + timedelta(minutes=2)) is None
        assert observe(c, e, e.at + timedelta(minutes=2)) is None
        assert c.execute("SELECT COUNT(*) FROM encounter_episodes").fetchone()[0] == 1


def test_episode_insert_failure_rolls_back_source_event_and_derived_knowledge(scene):
    e = scene
    with e.db.write() as c:
        tables = ("world_events", "character_event_knowledge", "character_acquaintances")
        before = {table: c.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] for table in tables}
        c.execute("CREATE TEMP TRIGGER reject_encounter BEFORE INSERT ON encounter_episodes "
                  "BEGIN SELECT RAISE(ABORT,'隔离事务故障'); END")
        with pytest.raises(sqlite3.IntegrityError, match="隔离事务故障"):
            observe(c, e)
        after = {table: c.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] for table in tables}
        assert before == after
        assert not c.execute("SELECT 1 FROM encounter_episodes").fetchone()
        c.execute("DROP TRIGGER reject_encounter")
        assert observe(c, e)
