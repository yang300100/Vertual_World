# ruff: noqa: F811
"""经历的因果、可纠错知识与缓慢特质变化须形成真实且隔离的链路。"""

import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from test_lived_world import post, town  # noqa: F401

from world_engine.actions import ActionService
from world_engine.character_growth import CharacterGrowthService
from world_engine.epistemics import KnowledgeService
from world_engine.event_history import EventHistoryService
from world_engine.history import WorldHistoryLogger
from world_engine.repository import to_iso
from world_engine.society import SocietyService


def fact(e, c, *, actor=None, target=None, kind="knowledge.note", payload=None, at=None):
    return ActionService._record_event(
        c,
        world_id=e.wid,
        tick_id=str(uuid4()),
        occurred_at=at or e.at,
        event_type=kind,
        actor_id=actor or e.player,
        target_id=target,
        location_id=e.loc["id"],
        summary="隔离测试中的有来源事件",
        payload=payload or {},
    )


def test_work_result_links_to_start_and_exports_non_uuid_ticks(town, tmp_path):
    e = town
    aid = post(e, "player/life/tasks", {"request_id": str(uuid4())}, 201)["activity_id"]
    e.clock.heartbeat(e.wid, elapsed_seconds=3600)
    with e.db.read() as c:
        activity = c.execute(
            "SELECT * FROM character_life_activities WHERE id=?", (aid,)
        ).fetchone()
        finish = activity["finish_event_id"]
        view = EventHistoryService.view(c, finish, e.player)
        assert view["status"] == "resolved"
        assert activity["source_event_id"] in [cause["cause_event_id"] for cause in view["causes"]]
        assert EventHistoryService.view(c, activity["source_event_id"])["status"] == "resolved"
    exported = WorldHistoryLogger(e.db, tmp_path / "history").sync_world(e.wid)
    directory = Path(exported.directory)
    manifest = json.loads((directory / "scope_manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"]
    assert any(
        json.loads(line)["id"] == finish
        for line in (directory / "history.jsonl").read_text(encoding="utf-8").splitlines()
    )
    assert list((directory / "ticks").glob("*named-*.json"))


def test_causal_cycles_future_causes_and_hidden_causes_are_rejected(town):
    e = town
    with e.db.write() as c:
        secret = fact(e, c, actor=e.npc)
        public = fact(e, c, target=e.npc)
        EventHistoryService.link(c, public, secret)
        with pytest.raises(ValueError):
            EventHistoryService.link(c, secret, public)
        future = fact(e, c, at=e.at + timedelta(hours=1))
        with pytest.raises(ValueError):
            EventHistoryService.link(c, public, future)
        assert EventHistoryService.view(c, public, e.player)["causes"] == []
        assert EventHistoryService.view(c, public)["causes"][0]["cause_event_id"] == secret
    assert e.client.get(f"/api/worlds/{e.wid}/player/events/{secret}/causes").status_code == 404


def test_self_reported_name_is_not_truth_and_later_document_keeps_old_claim(town):
    e = town
    with e.db.write() as c:
        eid = fact(
            e,
            c,
            actor=e.npc,
            target=e.player,
            kind="action.socialize",
            payload={"dialogue": "我叫阿林。"},
        )
        claims = KnowledgeService.view(c, e.player, e.at)
        alias = next(item for item in claims if item["predicate"] == "name")
        assert alias["value"] == "阿林" and alias["status"] == "reported"
        assert c.execute("SELECT name FROM characters WHERE id=?", (e.npc,)).fetchone()[0] == "林澈"
        assert SocietyService.label(c, e.player, e.npc) == "阿林"
        KnowledgeService.capture(c, eid)
        assert (
            c.execute(
                "SELECT count(*) FROM knowledge_evidence WHERE edge_id=?", (alias["id"],)
            ).fetchone()[0]
            == 1
        )
    post(e, "contacts", {"recipient_id": e.npc})
    with e.db.read() as c:
        names = [
            item
            for item in KnowledgeService.view(c, e.player, e.at)
            if item["predicate"] == "name" and item["subject_id"] == e.npc
        ]
        assert next(item for item in names if item["value"] == "阿林")["status"] == "superseded"
        assert next(item for item in names if item["value"] == "林澈")["status"] == "confirmed"


@pytest.mark.parametrize("text",["如果我叫阿林，你还会认识我吗？","有人说我叫阿林。","我叫阿林吗？","我不是阿林。"])
def test_questions_and_hypothetical_names_do_not_become_self_introductions(town,text):
    e=town
    with e.db.write() as c:
        fact(e,c,actor=e.npc,target=e.player,kind="action.socialize",payload={"dialogue":text})
        assert not any(item["predicate"]=="name" for item in KnowledgeService.view(c,e.player,e.at))


def test_reported_location_can_be_corrected_only_when_person_is_visible(town):
    e = town
    with e.db.write() as c:
        subject = c.execute(
            "SELECT * FROM characters WHERE world_id=? AND id NOT IN (?,?) LIMIT 1",
            (e.wid, e.player, e.npc),
        ).fetchone()
        place = c.execute(
            "SELECT * FROM locations WHERE world_id=? AND id!=? LIMIT 1", (e.wid, e.loc["id"])
        ).fetchone()
        c.execute("UPDATE characters SET longitude=longitude+2 WHERE id=?", (subject["id"],))
        c.execute(
            "INSERT OR REPLACE INTO character_acquaintances(observer_id,subject_id,world_id,known_name,first_seen,last_seen) VALUES (?,?,?,?,?,?)",
            (e.npc, subject["id"], e.wid, subject["name"], to_iso(e.at), to_iso(e.at)),
        )
        fact(
            e,
            c,
            actor=e.npc,
            target=e.player,
            kind="action.socialize",
            payload={"dialogue": subject["name"] + "现在在" + place["name"] + "。"},
        )
        claims = [
            item
            for item in KnowledgeService.view(c, e.player, e.at)
            if item["subject_id"] == subject["id"] and item["predicate"] == "location"
        ]
        assert len(claims) == 1 and claims[0]["status"] == "reported"
    post(e, f"player/knowledge/observe/{subject['id']}", status=409)
    with e.db.write() as c:
        c.execute(
            "UPDATE characters SET longitude=?,latitude=?,current_location_id=? WHERE id=?",
            (e.loc["longitude"], e.loc["latitude"], e.loc["id"], subject["id"]),
        )
    post(e, f"player/knowledge/observe/{subject['id']}")
    with e.db.read() as c:
        claims = [
            item
            for item in KnowledgeService.view(c, e.player, e.at)
            if item["subject_id"] == subject["id"] and item["predicate"] == "location"
        ]
        assert (
            next(item for item in claims if item["value"] == place["name"])["status"]
            == "superseded"
        )
        assert (
            next(item for item in claims if item["value"] == e.loc["name"])["status"] == "confirmed"
        )


def test_whispers_and_unknown_evidence_cannot_be_laundered_into_public_news(town):
    e = town
    with e.db.write() as c:
        other = c.execute(
            "SELECT id FROM characters WHERE world_id=? AND id NOT IN (?,?) LIMIT 1",
            (e.wid, e.player, e.npc),
        ).fetchone()[0]
        secret = fact(
            e,
            c,
            actor=e.npc,
            target=e.player,
            kind="action.socialize",
            payload={"delivery": "whisper", "dialogue": "我在" + e.loc["name"] + "。"},
        )
        exchange = fact(e, c, actor=e.player, target=other)
        KnowledgeService.transmit(c, e.player, other, exchange)
        assert not c.execute(
            "SELECT 1 FROM knowledge_evidence e JOIN observer_knowledge_edges k ON k.id=e.edge_id WHERE k.observer_id=? AND e.origin_event_id=?",
            (other, secret),
        ).fetchone()
        with pytest.raises(ValueError):
            KnowledgeService.add_evidence(
                c,
                observer_id=other,
                subject_id=e.npc,
                predicate="name",
                value="阿林",
                event_id=secret,
                method="reported",
                quote="未知消息",
            )


def test_relay_preserves_fact_age_and_does_not_amplify_circular_rumors(town):
    e = town
    with e.db.write() as c:
        other = c.execute(
            "SELECT id FROM characters WHERE world_id=? AND id NOT IN (?,?) LIMIT 1",
            (e.wid, e.player, e.npc),
        ).fetchone()[0]
        origin = fact(
            e,
            c,
            actor=e.npc,
            target=other,
            kind="action.socialize",
            payload={"dialogue": "随便聊聊。"},
        )
        KnowledgeService.add_evidence(
            c,
            observer_id=e.npc,
            subject_id=other,
            predicate="location",
            value=e.loc["id"],
            event_id=origin,
            method="reported",
            quote="交谈中提及地点",
        )
        exchange = fact(e, c, actor=e.npc, target=e.player, at=e.at + timedelta(hours=5))
        KnowledgeService.transmit(c, e.npc, e.player, exchange)
        records = c.execute(
            "SELECT e.* FROM knowledge_evidence e JOIN observer_knowledge_edges k ON k.id=e.edge_id WHERE k.observer_id=? AND e.origin_event_id=?",
            (e.player, origin),
        ).fetchall()
        assert records and all(
            row["valid_until"] == to_iso(e.at + timedelta(hours=6)) for row in records
        )
        before = c.execute("SELECT count(*) FROM knowledge_evidence").fetchone()[0]
        KnowledgeService.transmit(c, e.player, e.npc, exchange)
        assert before == c.execute("SELECT count(*) FROM knowledge_evidence").fetchone()[0]
        assert any(
            item["status"] == "stale"
            for item in KnowledgeService.view(c, e.player, e.at + timedelta(hours=7))
        )


def test_ordinary_chat_does_not_change_traits_and_attacks_have_daily_cap(town):
    e = town
    with e.db.write() as c:
        CharacterGrowthService.seed_character(c, e.npc)
        initial = CharacterGrowthService.level(c, e.npc, "谨慎")
        for _ in range(10):
            fact(
                e,
                c,
                actor=e.player,
                target=e.npc,
                kind="action.socialize",
                payload={"dialogue": "你好。"},
            )
        assert c.execute("SELECT count(*) FROM character_trait_changes").fetchone()[0] == 0
        events = [fact(e, c, actor=e.player, target=e.npc, kind="action.attack") for _ in range(5)]
        assert CharacterGrowthService.level(c, e.npc, "谨慎") == initial + 5
        for event in events:
            CharacterGrowthService.apply(c, event)
        assert CharacterGrowthService.level(c, e.npc, "谨慎") == initial + 5
        fact(e, c, actor=e.player, target=e.npc, kind="action.attack", at=e.at + timedelta(days=1))
        assert CharacterGrowthService.level(c, e.npc, "谨慎") == initial + 8


def test_acquired_caution_changes_unsolicited_contact_without_changing_original_traits(town):
    e = town
    with e.db.write() as c:
        c.execute('UPDATE characters SET traits_json=\'["热情","谨慎"]\' WHERE id=?', (e.npc,))
        CharacterGrowthService.seed_character(c, e.npc)
        for day in range(4):
            fact(
                e,
                c,
                actor=e.player,
                target=e.npc,
                kind="action.attack",
                at=e.at + timedelta(days=day),
            )
        SocietyService.tick(c, e.wid, e.at + timedelta(days=4))
        assert not c.execute("SELECT 1 FROM npc_outreach_jobs WHERE npc_id=?", (e.npc,)).fetchone()
        assert json.loads(
            c.execute("SELECT traits_json FROM characters WHERE id=?", (e.npc,)).fetchone()[0]
        ) == ["热情", "谨慎"]


def test_initialization_preserves_time_and_does_not_replay_old_personality_changes(town):
    e = town
    with e.db.write() as c:
        fact(e, c, actor=e.player, target=e.npc, kind="action.attack")
        before = c.execute("SELECT sum(intensity) FROM character_traits").fetchone()[0]
        at = c.execute("SELECT w.current_time FROM worlds w WHERE id=?", (e.wid,)).fetchone()[0]
    e.db.initialize()
    e.db.initialize()
    with e.db.read() as c:
        assert (
            c.execute("SELECT w.current_time FROM worlds w WHERE id=?", (e.wid,)).fetchone()[0]
            == at
        )
        assert CharacterGrowthService.level(c, e.npc, "谨慎") >= 3
        # 初始化只补尚未出现过的初始特质，不重放攻击导致的变化。
        assert (
            c.execute(
                "SELECT count(*) FROM character_trait_changes WHERE character_id=?", (e.npc,)
            ).fetchone()[0]
            == 1
        )
        assert c.execute("SELECT sum(intensity) FROM character_traits").fetchone()[0] >= before
