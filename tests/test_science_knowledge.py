"""哥哥，这里用测试元数据检查知识门槛；真实认证另走完整实验验收。"""

import importlib
import importlib.util
import json
from datetime import timedelta
from uuid import uuid4

import pytest

from world_engine.collective_messages import MessageService
from world_engine.collective_research import CollectiveResearchService as R
from world_engine.materials import encoded
from world_engine.repository import to_iso

pytest_plugins = ["science_fixtures"]


def knowledge():
    assert importlib.util.find_spec("world_engine.science_knowledge"), "尚无科研知识传播"
    return importlib.import_module("world_engine.science_knowledge")


def metadata(c, wid, sid, actor, at):
    cid, tid = str(uuid4()), str(uuid4())
    method = {
        "inputs": {"raw": "material", "gauge": "instrument"},
        "steps": [{"op": "assay", "input": "raw", "metric": "strength", "instrument": "gauge"}],
    }
    claim = {
        "metric": "strength",
        "direction": "increase",
        "minimum_delta": 1,
        "comparator_role": "raw",
    }
    c.execute(
        (
            "INSERT INTO technology_candidates(id,session_id,world_id,actor_id"
            ",name,problem,spec_json,created_at,updated_at) VALUES (?,?,?,?,?,"
            "?,?,?,?)"
        ),
        (
            cid,
            sid,
            wid,
            actor,
            "测试元数据",
            "仅测试传播权限",
            encoded({"method": method, "claim": claim}),
            to_iso(at),
            to_iso(at),
        ),
    )
    c.execute(
        (
            "INSERT INTO technologies(id,session_id,world_id,candidate_id,inve"
            "ntor_id,name,created_at) VALUES (?,?,?,?,?,?,?)"
        ),
        (tid, sid, wid, cid, actor, "测试元数据", to_iso(at)),
    )
    c.execute(
        "INSERT INTO technology_versions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (tid, wid, 1, encoded(method), encoded(claim), tid, tid, "{}", cid, 1, to_iso(at)),
    )
    c.execute(
        (
            "INSERT INTO technology_knowledge(world_id,actor_id,technology_id,"
            "version,evidence_ids_json,learned_at) VALUES (?,?,?,1,'[]',?)"
        ),
        (wid, actor, tid, to_iso(at)),
    )
    return tid


def test_new_session_does_not_grant_global_knowledge(science_lab):
    k = knowledge()
    db, wid, sid, actors, _, at = science_lab
    with db.write() as c:
        metadata(c, wid, sid, actors[0], at)
        assert k.known_methods(c, actors[1], at) == []


def test_method_only_arrives_with_actual_message(science_lab):
    k = knowledge()
    db, wid, sid, actors, _, at = science_lab
    with db.write() as c:
        tid = metadata(c, wid, sid, actors[0], at)
        c.execute("INSERT INTO collective_contacts VALUES (?,?,2)", tuple(actors))
        mid = k.share_method(c, actors[0], actors[1], tid, 1, at)
        with pytest.raises(ValueError, match="送达|收到"):
            k.receive_method(c, mid, at)
        assert k.known_methods(c, actors[1], at + timedelta(hours=1)) == []
        MessageService.deliver(c, wid, at + timedelta(hours=2))
        methods = k.known_methods(c, actors[1], at + timedelta(hours=2))
        assert len(methods) == 1 and methods[0]["version"] == 1
        assert methods[0]["source_message_id"] == mid
        assert methods[0]["propagation_depth"] == 1
        assert k.known_methods(c, actors[1], at) == []
        k.receive_method(c, mid, at + timedelta(hours=2))
        assert len(k.known_methods(c, actors[1], at + timedelta(hours=2))) == 1


def test_unknown_method_and_private_evidence_cannot_be_forwarded(science_lab):
    k = knowledge()
    db, wid, sid, actors, _, at = science_lab
    with db.write() as c:
        tid = metadata(c, wid, sid, actors[0], at)
        c.execute("INSERT INTO collective_contacts VALUES (?,?,1)", tuple(actors))
        c.execute("INSERT INTO collective_contacts VALUES (?,?,1)", tuple(reversed(actors)))
        with pytest.raises(ValueError, match="方法|知道"):
            k.share_method(c, actors[1], actors[0], tid, 1, at)
        oid = R.record_observation(c, actors[1], at, "私有记录", {"kind": "private"})
        with pytest.raises(ValueError, match="证据"):
            k.share_method(c, actors[0], actors[1], tid, 1, at, evidence_ids=(oid,))


def test_forwarding_preserves_version_and_original_report(science_lab):
    k = knowledge()
    db, wid, sid, actors, _, at = science_lab
    with db.write() as c:
        tid = metadata(c, wid, sid, actors[0], at)
        c.execute("INSERT INTO collective_contacts VALUES (?,?,1)", tuple(actors))
        c.execute("INSERT INTO collective_contacts VALUES (?,?,1)", tuple(reversed(actors)))
        oid = R.record_observation(
            c, actors[0], at, "真实记录原文", {"kind": "eyewitness", "reading": 7.1}
        )
        k.share_method(c, actors[0], actors[1], tid, 1, at, evidence_ids=(oid,))
        MessageService.deliver(c, wid, at + timedelta(hours=1))
        mid = k.share_method(
            c, actors[1], actors[0], tid, 1, at + timedelta(hours=1), evidence_ids=(oid,)
        )
        data = json.loads(
            c.execute("SELECT content_json FROM collective_messages WHERE id=?", (mid,)).fetchone()[
                0
            ]
        )
        assert data["method_packet"]["propagation_depth"] == 2
        assert data["evidence"][0]["content"]["reading"] == 7.1


def test_long_history_keeps_current_method_and_excludes_future_or_hidden_records(science_lab):
    k = knowledge()
    db, wid, sid, actors, _, at = science_lab
    with db.write() as c:
        tid = metadata(c, wid, sid, actors[0], at)
        for _ in range(30):
            R.record_observation(c, actors[0], at, "旧日生活" * 200, {"kind": "old"})
        R.record_observation(
            c, actors[0], at + timedelta(hours=1), "未来不可见", {"kind": "future"}
        )
        context = k.science_context(c, actors[0], at, max_chars=12000)
        content = encoded(context)
        assert tid in content and len(content) <= 12000
        assert (
            "未来不可见" not in content
            and "strength_scale" not in content
            and "rules_json" not in content
        )
        assert context["context_diagnostics"]["dropped_old_records"] > 0


def test_real_knowledge_search_only_assembles_character_common(science_lab):
    k = knowledge()
    from world_engine.knowledge import KnowledgeChunk, WorldKnowledgeBase

    db, _, _, actors, _, at = science_lab
    kb = WorldKnowledgeBase(
        [
            KnowledgeChunk(
                "public", "common.md", "工坊", "工坊居民知道普通陶器", "character_common", False, ()
            ),
            KnowledgeChunk(
                "secret", "author.md", "工坊", "工坊居民秘密公式答案", "author_hidden", False, ()
            ),
        ]
    )
    with db.read() as c:
        context = k.science_context(c, actors[0], at, max_chars=20000, knowledge=kb)
    text = encoded(context)
    assert "普通陶器" in text and "秘密公式答案" not in text


def test_original_identity_skills_and_personal_memories_in_context(science_lab):
    from world_engine.event_log import record_event
    from world_engine.repository import to_iso

    db, wid, _, actors, _, at = science_lab
    with db.write() as c:
        c.execute("UPDATE characters SET skills_json='[\"织布\"]' WHERE id=?", (actors[0],))
        for actor, summary, date in (
            (actors[0], "本人曾经修补水袋", at),
            (actors[1], "别人私有记忆", at),
            (actors[0], "未来记忆", at + timedelta(hours=1)),
        ):
            eid = record_event(
                c,
                world_id=wid,
                tick_id="memory-test",
                occurred_at=date,
                event_type="character.observed",
                actor_id=actor,
                summary=summary,
                payload={},
            )
            c.execute(
                (
                    "INSERT INTO "
                    "character_memories(id,world_id,character_id,event_id,memory_type,summary"
                    ",importance,confidence,created_at) VALUES (?,?,?,?,?,?,1,1,?)"
                ),
                (eid, wid, actor, eid, "episodic", summary, to_iso(date)),
            )
        context = knowledge().science_context(c, actors[0], at, max_chars=40000)
    text = encoded(context)
    assert "织布" in text and "本人曾经修补水袋" in text
    assert "别人私有记忆" not in text and "未来记忆" not in text
