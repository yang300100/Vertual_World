"""哥哥，最终 NPC 提示只读取已经发生、本人已经知晓的经历，未来约定仍可商量。"""

# ruff: noqa: F811

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from test_encounter_episodes import scene  # noqa: F401
from test_encounter_event_views import round_trip
from test_lived_world import town  # noqa: F401
from test_overnight_options import third_person

from world_engine.appointments import AppointmentIntent, AppointmentService
from world_engine.engine import WorldEngine
from world_engine.event_log import record_event
from world_engine.intimacy import IntimacyService, RelationshipReflection
from world_engine.repository import WorldRepository, to_iso, utc_now
from world_engine.roleplay_pipeline import build_npc_prompt


def source(c, e, text, *, at=None):
    return record_event(
        c, world_id=e.wid, tick_id=str(uuid4()), occurred_at=at or e.at,
        event_type="action.socialize", actor_id=e.npc, target_id=e.player,
        location_id=e.loc["id"], summary=text, payload={"dialogue": text},
    )


def prompt(c, e, *, text="请按原话回忆铜铃交接的经历。"):
    engine = WorldEngine(e.db, e.settings, e.model)
    snapshot = WorldRepository().get_snapshot(c, e.wid)
    context = engine.dialogue_context.build(
        c, snapshot=snapshot, npc=snapshot.character_by_id(e.npc),
        player=snapshot.character_by_id(e.player), player_text=text,
        conversation=None, channel="in_person", interaction="当面交谈",
    )
    sent = build_npc_prompt("只根据本人当前已知的真实经历回答。", context)
    return context, json.dumps(sent.messages, ensure_ascii=False)


@pytest.mark.parametrize("boundary", ["future_event", "future_learning"])
def test_final_prompt_waits_for_source_and_own_learning_time(town, boundary):
    e = town
    secret = "铜铃隔离标记：本人已经完成交接。"
    with e.db.write() as c:
        eid = source(c, e, secret)
        assert secret in prompt(c, e)[1]
        future = to_iso(e.at + timedelta(days=1))
        if boundary == "future_event":
            c.execute("UPDATE world_events SET occurred_at=? WHERE id=?", (future, eid))
        else:
            c.execute(
                "UPDATE character_event_knowledge SET learned_world_time=? "
                "WHERE character_id=? AND event_id=?", (future, e.npc, eid),
            )
        context, sent = prompt(c, e)
        assert secret not in sent
        assert secret not in json.dumps(context, ensure_ascii=False)
        c.execute("UPDATE worlds SET current_time=? WHERE id=?", (future, e.wid))
        assert secret in prompt(c, e)[1]


def test_shared_experience_waits_for_both_participants_knowledge(town):
    e = town
    secret = "铜铃共同经历隔离标记。"
    with e.db.write() as c:
        eid = source(c, e, secret)
        assert secret in json.dumps(prompt(c, e)[0]["intimacy"]["shared_experiences"], ensure_ascii=False)
        c.execute(
            "UPDATE character_event_knowledge SET learned_world_time=? "
            "WHERE character_id=? AND event_id=?",
            (to_iso(e.at + timedelta(days=1)), e.player, eid),
        )
        context, _ = prompt(c, e)
        assert secret not in json.dumps(context["intimacy"]["shared_experiences"], ensure_ascii=False)
        # 哥哥，对方尚未知晓不抹去 NPC 本人已经知道的经历，只是不称为共同已知。
        assert secret in json.dumps(context["personal_experience"]["known_events"], ensure_ascii=False)


@pytest.mark.parametrize("boundary", ["future_event", "future_learning"])
def test_conversation_memory_episode_and_reflection_recheck_source_time(town, boundary):
    e = town
    secret = "铜铃派生经历隔离标记。"
    with e.db.write() as c:
        engine = WorldEngine(e.db, e.settings, e.model)
        event_ids = []
        for minutes in (120, 7, 6, 5):
            at = e.at - timedelta(minutes=minutes)
            eid = source(c, e, secret, at=at)
            event_ids.append(eid)
            engine.conversations.record_exchange(
                c, world_id=e.wid, npc_id=e.npc, counterpart_id=e.player,
                event_id=eid, world_time=at, player_text=secret, npc_text="这是该次实际经历的回应。",
            )
        npc = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        IntimacyService.reflect(
            c, npc, e.player, e.at, event_ids[-1],
            RelationshipReflection(feeling=secret, meaning=secret, significance="milestone"),
        )
        before, _ = prompt(c, e)
        for key in ("recent_private_memories", "recent_conversation", "conversation_episodes", "recalled_past_conversation"):
            assert secret in json.dumps(before[key], ensure_ascii=False), key
        for key in ("lasting_memories", "own_perspective"):
            assert secret in json.dumps(before["intimacy"][key], ensure_ascii=False), key
        future = to_iso(e.at + timedelta(days=1))
        for eid in event_ids:
            if boundary == "future_event":
                c.execute("UPDATE world_events SET occurred_at=? WHERE id=?", (future, eid))
            else:
                c.execute(
                    "UPDATE character_event_knowledge SET learned_world_time=? "
                    "WHERE character_id=? AND event_id=?", (future, e.npc, eid),
                )
        after, sent = prompt(c, e)
        assert secret not in sent
        assert secret not in json.dumps(after, ensure_ascii=False)


def test_current_source_does_not_make_future_derived_record_an_already_known_experience(town):
    e = town
    secret = "铜铃未来反思隔离标记。"
    with e.db.write() as c:
        eid = source(c, e, "这是当前真实发生的普通交谈。")
        future = e.at + timedelta(days=1)
        npc = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        IntimacyService.reflect(
            c, npc, e.player, future, eid,
            RelationshipReflection(feeling=secret, meaning=secret, significance="milestone"),
        )
        context, sent = prompt(c, e)
        assert context["intimacy"]["own_perspective"] == []
        assert context["intimacy"]["lasting_memories"] == []
        assert secret not in sent


def test_already_known_future_appointment_and_todo_remain_in_final_prompt(town):
    e = town
    commitment = "铜铃明日待办隔离标记"
    with e.db.write() as c:
        player = c.execute("SELECT * FROM characters WHERE id=?", (e.player,)).fetchone()
        npc = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        future = to_iso(e.at + timedelta(days=1))
        offered = AppointmentService.apply(
            c, player, npc, e.at,
            AppointmentIntent(
                operation="propose", kind="talk", location_id=e.loc["id"],
                meeting_world_time=future, statement="我们现在商量明天的铜铃交接。",
            ),
        )
        AppointmentService.apply(
            c, npc, player, e.at,
            AppointmentIntent(operation="accept", appointment_id=offered["appointment_id"], statement="我本人接受明天的见面。"),
        )
        c.execute(
            "INSERT INTO npc_todos(id,world_id,character_id,title,details,status,due_world_time,"
            "source_event_id,created_at,updated_at) VALUES (?,?,?,?,?,'open',?,?,?,?)",
            (str(uuid4()), e.wid, e.npc, commitment, "现在已知，明天才需要履行。", future,
             offered["event_id"], to_iso(utc_now()), to_iso(utc_now())),
        )
        context, sent = prompt(c, e, text="我们明天的铜铃交接约定是什么？")
        assert commitment in sent
        assert context["open_commitments"][0]["title"] == commitment
        arrangement = next(item for item in context["intimacy"]["appointments"]["arrangements"]
                           if item["id"] == offered["appointment_id"])
        assert arrangement["starts_at"] == future
        assert arrangement["status"] == "accepted"


def test_final_prompt_hides_witness_private_room_and_unknown_third_name(scene):
    e = scene
    _, private_name, _ = round_trip(e, performer=e.player)
    third = third_person(e)
    with e.db.write() as c:
        stranger_name = c.execute("SELECT name FROM characters WHERE id=?", (third,)).fetchone()[0]
        source(c, e, f"双方在外面交谈时记录了{stranger_name}，尚未介绍其姓名。")
        _, sent = prompt(c, e, text="说说刚才看见的进出和旁边的人。")
        assert private_name not in sent
        assert stranger_name not in sent
