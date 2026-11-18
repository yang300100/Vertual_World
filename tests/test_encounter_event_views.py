"""哥哥，门外见闻与本人经历在生活事件、因果视图及共享经历中保持各自边界。"""

# ruff: noqa: F811

import json
from uuid import uuid4

import pytest
from test_appointment_rooms import door, room
from test_encounter_episodes import scene  # noqa: F401
from test_lived_world import town  # noqa: F401
from test_overnight_options import third_person

from world_engine.event_history import EventHistoryService
from world_engine.event_log import record_event
from world_engine.interiors import InteriorService
from world_engine.intimacy import IntimacyService
from world_engine.separate_stays import SeparateStayService


def round_trip(e, *, performer=None, policy="private"):
    performer = performer or e.npc
    room_name = "隔离验收的室内真实名称"
    rid = room(e, owner=performer, policy=policy, name=room_name)
    events = {}
    for operation in ("open", "enter", "exit"):
        if performer == e.player:
            response = door(e, rid, operation)
            assert response.status_code == 200, response.text
            result = response.json()
        else:
            # 哥哥，NPC 也执行与房门 API 相同的真实服务，逐门完成操作，不改位置伪造见闻。
            with e.db.write() as c:
                actor = c.execute("SELECT * FROM characters WHERE id=?", (performer,)).fetchone()
                result = InteriorService.door(c, actor, rid, operation, e.at)
        events[operation] = result["event_id"]
    return rid, room_name, events


def experience(e):
    response = e.client.get(f"/api/worlds/{e.wid}/player/experience")
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("policy", ["private", "public"])
def test_living_event_api_hides_witness_private_room_but_keeps_public_room_and_masks_unknown_name(
    scene, policy
):
    e = scene
    _, room_name, events = round_trip(e, policy=policy)
    with e.db.read() as c:
        raw = dict(c.execute("SELECT * FROM world_events WHERE id=?", (events["exit"],)).fetchone())
        npc_name = c.execute("SELECT name FROM characters WHERE id=?", (e.npc,)).fetchone()[0]
        assert room_name in raw["summary"] and npc_name in raw["summary"]
        assert (
            c.execute(
                "SELECT channel FROM event_observers WHERE event_id=? AND character_id=?",
                (events["exit"], e.player),
            ).fetchone()[0]
            == "witnessed"
        )
    viewed = next(item for item in experience(e)["events"] if item["id"] == events["exit"])
    assert npc_name not in viewed["summary"]
    assert viewed["payload"] == {}
    if policy == "private":
        assert room_name not in json.dumps(viewed, ensure_ascii=False)
        assert "离开了一处室内空间" in viewed["summary"]
    else:
        assert room_name in viewed["summary"]
    with e.db.read() as c:
        assert (
            dict(c.execute("SELECT * FROM world_events WHERE id=?", (events["exit"],)).fetchone())
            == raw
        )


def test_living_event_api_keeps_participant_real_private_room_fact_after_real_door_api(scene):
    e = scene
    rid, room_name, events = round_trip(e, performer=e.player)
    entries = {item["id"]: item for item in experience(e)["events"]}
    with e.db.read() as c:
        for operation in ("enter", "exit"):
            eid = events[operation]
            raw = c.execute("SELECT summary FROM world_events WHERE id=?", (eid,)).fetchone()[0]
            assert room_name in raw
            assert entries[eid]["summary"] == raw
            assert entries[eid]["payload"]["room_id"] == rid


def test_causal_api_and_living_embedded_causes_hide_witness_room_and_keep_participant_fact(scene):
    e = scene
    _, room_name, events = round_trip(e)
    with e.db.write() as c:
        consequence = record_event(
            c,
            world_id=e.wid,
            tick_id=str(uuid4()),
            occurred_at=e.at,
            event_type="action.socialize",
            actor_id=e.npc,
            target_id=e.player,
            location_id=e.loc["id"],
            summary="离开后在门外发生了一段双方实际知道的交谈。",
            payload={"dialogue": "我们在外面说话。"},
        )
        EventHistoryService.link(c, consequence, events["enter"])
        EventHistoryService.link(c, consequence, events["exit"])
        # 哥哥，门外的人只实际目击到离开；未见到的室内进入不能靠因果链补成已知。
        assert not c.execute(
            "SELECT 1 FROM character_event_knowledge WHERE character_id=? AND event_id=?",
            (e.player, events["enter"]),
        ).fetchone()
        participant = EventHistoryService.view(c, consequence, e.npc)
        participant_causes = {item["cause_event_id"]: item for item in participant["causes"]}
        assert set(participant_causes) == {events["enter"], events["exit"]}
        assert all(room_name in item["summary"] for item in participant_causes.values())
        raw_summary = c.execute(
            "SELECT summary FROM world_events WHERE id=?", (events["exit"],)
        ).fetchone()[0]
        npc_name = c.execute("SELECT name FROM characters WHERE id=?", (e.npc,)).fetchone()[0]
    response = e.client.get(f"/api/worlds/{e.wid}/player/events/{consequence}/causes")
    assert response.status_code == 200, response.text
    causes = response.json()["causes"]
    assert [item["cause_event_id"] for item in causes] == [events["exit"]]
    assert room_name not in json.dumps(response.json(), ensure_ascii=False)
    assert npc_name not in causes[0]["summary"]
    entry = next(item for item in experience(e)["events"] if item["id"] == consequence)
    assert entry["history"]["causes"] == causes
    assert room_name not in json.dumps(entry["history"], ensure_ascii=False)
    with e.db.read() as c:
        assert (
            c.execute("SELECT summary FROM world_events WHERE id=?", (events["exit"],)).fetchone()[
                0
            ]
            == raw_summary
        )


def test_shared_experience_keeps_participant_source_fact_and_masks_unknown_third_person(scene):
    e = scene
    third = third_person(e)
    with e.db.write() as c:
        third_name = c.execute("SELECT name FROM characters WHERE id=?", (third,)).fetchone()[0]
        source_text = f"双方实际交谈时明确提到室内事实与{third_name}这个尚未介绍的姓名。"
        eid = record_event(
            c,
            world_id=e.wid,
            tick_id=str(uuid4()),
            occurred_at=e.at,
            event_type="action.socialize",
            actor_id=e.player,
            target_id=e.npc,
            location_id=e.loc["id"],
            summary=source_text,
            payload={"dialogue": "我们谈到一段共同经历。"},
        )
        for observer, other in ((e.player, e.npc), (e.npc, e.player)):
            context = IntimacyService.context(c, e.wid, observer, other, e.at)
            item = next(
                row for row in context["shared_experiences"] if row["source_event_id"] == eid
            )
            assert "双方实际交谈时明确提到室内事实" in item["summary"]
            assert third_name not in item["summary"]
        assert (
            c.execute("SELECT summary FROM world_events WHERE id=?", (eid,)).fetchone()[0]
            == source_text
        )


def test_room_options_keep_unpermitted_private_door_askable_without_disclosing_its_name(scene):
    e = scene
    private_name = "尚未告知旅伴的私人房名"
    hidden = room(e, owner=e.player, name=private_name)
    public = room(e, owner=e.player, policy="public", name="公开旅舍会客厅")
    own = room(e, owner=e.npc, name="本人已知的私人住处")
    with e.db.read() as c:
        options = {
            item["id"]: item
            for item in SeparateStayService.room_options(c, e.wid, e.npc, e.player, e.at)
        }
        assert hidden in options
        assert options[hidden]["own_access"] is False
        assert options[hidden]["name"] == "私人房间的入口"
        assert options[public]["name"] == "公开旅舍会客厅"
        assert options[own]["name"] == "本人已知的私人住处"
        context = IntimacyService.context(c, e.wid, e.npc, e.player, e.at)
        assert private_name not in json.dumps(context, ensure_ascii=False)
        for key in ("separate_stay_rooms", "lodging_room_options"):
            candidate = next(item for item in context[key] if item["id"] == hidden)
            assert candidate["name"] == "私人房间的入口"
            assert candidate["own_access"] is False
        assert (
            c.execute("SELECT name FROM life_rooms WHERE id=?", (hidden,)).fetchone()[0]
            == private_name
        )


def test_actual_past_room_visit_keeps_known_name_after_access_is_revoked(scene):
    e = scene
    private_name = "本人曾实际进出的私人客房"
    rid = room(e, owner=e.npc, name=private_name)
    with e.db.write() as c:
        c.execute("INSERT INTO life_room_access VALUES (?,?)", (rid, e.player))
    for operation in ("open", "enter", "exit"):
        response = door(e, rid, operation)
        assert response.status_code == 200, response.text
    with e.db.write() as c:
        c.execute(
            "DELETE FROM life_room_access WHERE room_id=? AND character_id=?", (rid, e.player)
        )
        candidate = next(
            item
            for item in SeparateStayService.room_options(c, e.wid, e.player, e.npc, e.at)
            if item["id"] == rid
        )
        assert candidate["own_access"] is False
        assert candidate["name"] == private_name
        # 哥哥，移除本人对此事件的有效亲历知识后，不再用旧记录推定仍知晓房名。
        c.execute(
            "DELETE FROM character_event_knowledge WHERE character_id=? AND event_id IN "
            "(SELECT id FROM world_events WHERE world_id=? AND event_type LIKE 'action.interior_%' "
            "AND json_extract(payload_json,'$.room_id')=?)",
            (e.player, e.wid, rid),
        )
        unknown = next(
            item
            for item in SeparateStayService.room_options(c, e.wid, e.player, e.npc, e.at)
            if item["id"] == rid
        )
        assert unknown["name"] == "私人房间的入口"
        assert (
            c.execute("SELECT name FROM life_rooms WHERE id=?", (rid,)).fetchone()[0]
            == private_name
        )


def test_confirmed_separate_rooms_keep_shared_names_without_granting_partner_room_access(scene):
    from test_journey_room_appointments import current_time
    from test_separate_stays import accept, setup

    e = scene
    _, own, other, fields = setup(e)
    sid = accept(e, fields)
    at = current_time(e)
    with e.db.read() as c:
        for observer, partner, room_id in ((e.player, e.npc, other), (e.npc, e.player, own)):
            raw_name = c.execute("SELECT name FROM life_rooms WHERE id=?", (room_id,)).fetchone()[0]
            options = SeparateStayService.room_options(c, e.wid, observer, partner, at)
            candidate = next(item for item in options if item["id"] == room_id)
            assert candidate["name"] == raw_name
            assert candidate["own_access"] is False
            context = IntimacyService.context(c, e.wid, observer, partner, at)
            stay = next(item for item in context["separate_stays"] if item["id"] == sid)
            assert raw_name in json.dumps(stay, ensure_ascii=False)
        assert (
            c.execute("SELECT status FROM journey_separate_stays WHERE id=?", (sid,)).fetchone()[0]
            == "accepted"
        )
        assert (
            c.execute("SELECT COUNT(*) FROM contract_fulfillments WHERE kind='lodging'").fetchone()[
                0
            ]
            == 0
        )
