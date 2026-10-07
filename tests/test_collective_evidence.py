"""哥哥，实际收信才取得同伴证据，引用与转发都须保留实测来源。"""

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from test_collective_cooperation import cooperation, proposal
from test_collective_research import lab, research  # noqa: F401

from world_engine.collective_messages import MessageService


def mail(c, actors, at, *, delay=2):
    c.execute("INSERT INTO collective_contacts VALUES (?,?,?)", (*actors, delay))
    c.execute("INSERT INTO collective_contacts VALUES (?,?,?)", (*reversed(actors), delay))
    oid = research().record_observation(
        c,
        actors[0],
        at,
        "亲自测得颗粒比例",
        {"kind": "measurement", "metric": "particles", "value": 0.25},
    )
    mid = MessageService.send(c, *actors, at, text="请独立评议", evidence_ids=[oid])
    return oid, mid


def hypothesis(oid):
    return {
        "explanation": "依据收到的读数提出可验证解释",
        "evidence_ids": [oid],
        "predictions": [{"metric": "particles", "low": 0, "high": 1}],
    }


@pytest.mark.parametrize("entry", ["hypothesis", "action", "proposal", "task"])
def test_delivered_original_id_is_usable_at_every_research_entry(lab, entry):
    db, wid, actors, batches, _, at = lab
    with db.write() as c:
        oid, _ = mail(c, actors, at)
        now = at + timedelta(hours=2)
        MessageService.deliver(c, wid, now)
        program = {"reason": "引用实际到达的同行读数", "commands": []}
        if entry == "hypothesis":
            program["hypotheses"] = [hypothesis(oid)]
        elif entry == "action":
            program["commands"] = [
                {
                    "op": "send",
                    "recipients": [actors[0]],
                    "text": "转发已收记录",
                    "evidence_ids": [oid],
                }
            ]
        else:
            body = proposal(actors, batches)
            if entry == "task":
                body["tasks"][1]["evidence_ids"] = [oid]
            else:
                body["evidence_ids"] = [oid]
            program["commands"] = [
                {
                    "op": "propose",
                    "text": body["reason"],
                    "tasks": body["tasks"],
                    "evidence_ids": body.get("evidence_ids", []),
                }
            ]
        pid = research().submit(c, actors[1], program, now)
        assert (
            c.execute("SELECT status FROM collective_programs WHERE id=?", (pid,)).fetchone()[0]
            == "completed"
        )


@pytest.mark.parametrize("entry", ["hypothesis", "send", "proposal", "task"])
def test_unreceived_original_id_is_rejected_even_if_other_person_knows_it(lab, entry):
    db, _, actors, batches, _, at = lab
    with db.write() as c:
        oid, _ = mail(c, actors, at)
        with pytest.raises(ValueError, match="证据|取得"):
            if entry == "hypothesis":
                research().submit(
                    c,
                    actors[1],
                    {"reason": "尚未收到", "hypotheses": [hypothesis(oid)], "commands": []},
                    at,
                )
            elif entry == "send":
                MessageService.send(
                    c, actors[1], actors[0], at, text="不能凭空转发", evidence_ids=[oid]
                )
            else:
                body = proposal(actors, batches)
                if entry == "task":
                    body["tasks"][1]["evidence_ids"] = [oid]
                else:
                    body["evidence_ids"] = [oid]
                cooperation().propose(c, actors[1], body, at)


@pytest.mark.parametrize("ref_type", ["original", "local_copy"])
def test_received_original_can_be_forwarded_with_unchanged_measurement(lab, ref_type):
    db, wid, actors, _, _, at = lab
    with db.write() as c:
        oid, _ = mail(c, actors, at)
        now = at + timedelta(hours=2)
        MessageService.deliver(c, wid, now)
        local = c.execute(
            "SELECT id FROM collective_observations WHERE character_id=? AND source_observation_id=?",
            (actors[1], oid),
        ).fetchone()[0]
        ref = oid if ref_type == "original" else local
        mid = MessageService.send(
            c, actors[1], actors[0], now, text="转述与原测量分开", evidence_ids=[ref]
        )
        content = json.loads(
            c.execute("SELECT content_json FROM collective_messages WHERE id=?", (mid,)).fetchone()[
                0
            ]
        )
        packet = content["evidence"][0]
        if ref_type == "local_copy":
            packet = packet["content"]["evidence"]
        assert packet["id"] == oid
        assert packet["content"] == {"kind": "measurement", "metric": "particles", "value": 0.25}
        assert packet["world_time"] == at.isoformat()
        MessageService.deliver(c, wid, now + timedelta(hours=2))
        received = research().context(c, actors[0], now + timedelta(hours=2))["observations"][-1]
        assert received["kind"] == "received_report"
        assert received["content"]["sender_id"] == actors[1]


@pytest.mark.parametrize("ref_type", ["original", "local_copy"])
def test_delivered_record_cannot_be_referenced_at_a_time_before_delivery(lab, ref_type):
    db, wid, actors, _, _, at = lab
    with db.write() as c:
        oid, _ = mail(c, actors, at)
        MessageService.deliver(c, wid, at + timedelta(hours=2))
        local = c.execute(
            "SELECT id FROM collective_observations WHERE character_id=? AND source_observation_id=?",
            (actors[1], oid),
        ).fetchone()[0]
        ref = oid if ref_type == "original" else local
        with pytest.raises(ValueError, match="证据|取得"):
            research().submit(
                c,
                actors[1],
                {"reason": "不能使用未来到信", "hypotheses": [hypothesis(ref)], "commands": []},
                at + timedelta(hours=1),
            )


def test_received_prose_does_not_grant_access_to_a_mentioned_evidence_id(lab):
    db, wid, actors, _, _, at = lab
    with db.write() as c:
        oid, _ = mail(c, actors, at, delay=100)
        c.execute("UPDATE collective_contacts SET transit_hours=0")
        MessageService.send(c, *actors, at, text="只在文字中提到编号 " + oid)
        MessageService.deliver(c, wid, at)
        with pytest.raises(ValueError, match="证据|取得"):
            research().submit(
                c,
                actors[1],
                {"reason": "文字不是附带的实测", "hypotheses": [hypothesis(oid)], "commands": []},
                at,
            )


@pytest.mark.parametrize("ref_type", ["original", "local_copy"])
def test_received_evidence_is_usable_to_revise_known_tasks(lab, ref_type):
    db, wid, actors, batches, _, at = lab
    with db.write() as c:
        oid, _ = mail(c, actors, at)
        body = proposal(actors, batches)
        pid = cooperation().propose(c, actors[0], body, at)
        MessageService.send(c, *actors, at, text="请评议任务", proposal_id=pid, version=1)
        now = at + timedelta(hours=2)
        MessageService.deliver(c, wid, now)
        local = c.execute(
            "SELECT id FROM collective_observations WHERE character_id=? AND source_observation_id=?",
            (actors[1], oid),
        ).fetchone()[0]
        ref = oid if ref_type == "original" else local
        body["tasks"][1].update(hours=3, evidence_ids=[ref])
        cooperation().revise(c, actors[1], {**body, "proposal_id": pid, "version": 1}, now)
        spec = json.loads(
            c.execute(
                "SELECT spec_json FROM collective_proposal_versions WHERE proposal_id=? AND version=2",
                (pid,),
            ).fetchone()[0]
        )
        assert spec["tasks"][1]["evidence_ids"] == [ref]
        assert spec["tasks"][1]["hours"] == 3


def test_nested_forwarded_original_is_visible_without_fetching_sender_private_records(lab):
    db, wid, actors, _, _, at = lab
    with db.write() as c:
        oid, _ = mail(c, actors, at)
        now = at + timedelta(hours=2)
        MessageService.deliver(c, wid, now)
        local = c.execute(
            "SELECT id FROM collective_observations WHERE character_id=? AND source_observation_id=?",
            (actors[1], oid),
        ).fetchone()[0]
        third = str(uuid4())
        loc = c.execute(
            "SELECT lab_location_id FROM collective_people WHERE character_id=?", (actors[0],)
        ).fetchone()[0]
        c.execute(
            "INSERT INTO characters(id,world_id,name,identity,location_id,current_location_id,longitude,latitude,energy,satiety,money,health,skills_json,goals_json,created_at,updated_at) VALUES (?,?,?, '工坊居民',?,?,0,0,100,100,100,100,'[]','[]',?,?)",
            (third, wid, "丙收件人", loc, loc, at.isoformat(), at.isoformat()),
        )
        c.execute(
            "INSERT INTO collective_people(character_id,world_id,lab_location_id) VALUES (?,?,?)",
            (third, wid, loc),
        )
        c.execute("INSERT INTO collective_contacts VALUES (?,?,2)", (actors[1], third))
        MessageService.send(c, actors[1], third, now, text="带原证据的转发", evidence_ids=[local])
        MessageService.deliver(c, wid, now + timedelta(hours=2))
        assert not c.execute(
            "SELECT 1 FROM collective_observations WHERE character_id=? AND source_observation_id=?",
            (third, oid),
        ).fetchone()
        pid = research().submit(
            c,
            third,
            {
                "reason": "使用转发内实际可见的原始记录",
                "hypotheses": [hypothesis(oid)],
                "commands": [],
            },
            now + timedelta(hours=2),
        )
        assert (
            c.execute("SELECT status FROM collective_programs WHERE id=?", (pid,)).fetchone()[0]
            == "completed"
        )
