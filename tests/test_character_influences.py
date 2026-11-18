"""哥哥，来源标注须保留个人例外，并守住登记、世界和认知边界。"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from world_engine.character_influences import annotate_registered_lore
from world_engine.conversation import ConversationService
from world_engine.knowledge import KnowledgeChunk, WorldKnowledgeBase, search_dynamic_knowledge
from world_engine.registration import ElementRegistrationSubmit, WorldElementRegistry
from world_engine.repository import WorldRepository, from_iso, to_iso, utc_now


@pytest.fixture
def lore_world(database):
    """用真实登记器建立隔离来源，哥哥不用碰正式存档。"""
    with database.write() as c:
        wid = WorldRepository().create_world(
            c, name="文化来源隔离世界", minutes_per_tick=60, seed_demo=True,
        )
        snapshot = WorldRepository().get_snapshot(c, wid)
        actor, other = snapshot.characters[:2]
        event = str(uuid4())
        c.execute(
            "INSERT INTO world_events(id,world_id,tick_id,occurred_at,event_type,actor_id,"
            "target_id,location_id,summary,importance,payload_json,created_at) "
            "VALUES (?,?,?,?,'test.lore_source',?,?,?,'本人记录了旅途见闻',"
            "'routine','{}',?)",
            (event, wid, str(uuid4()), to_iso(snapshot.world.current_time), actor.id,
             other.id, actor.location_id, to_iso(utc_now())),
        )
    return wid, event, actor.id, other.id, actor.location_id


def register_lore(c, world, level="public_lore", *, author=None, content=None, apply=True):
    wid, event, _actor, _other, _place = world
    result = WorldElementRegistry().submit(
        c,
        world_id=wid,
        auto_apply=apply,
        request=ElementRegistrationSubmit.model_validate({
            "source_event_id": event,
            "requested_by_character_id": author,
            "idempotency_key": str(uuid4()),
            "payload": {
                "element_type": "lore", "knowledge_level": level,
                "title": "旅途文化与个人记录", "content": content or "旅人相处先问明来意。",
                "confidence": 0.35,
            },
        }),
    )
    assert result.status == ("applied" if apply else "proposed")
    return result


def hit(entry_id, **extra):
    return {"id": f"dynamic:{entry_id}", "audience": "character_common", **extra}


def annotate(c, world, hits, *, character=None, locations=None):
    wid, _event, actor, _other, place = world
    return annotate_registered_lore(
        c, world_id=wid, character_id=character or actor,
        location_ids={place} if locations is None else locations, hits=hits,
    )


def test_real_registered_culture_and_personal_exception_keep_distinct_sources(database, lore_world):
    wid, event, actor, _other, place = lore_world
    with database.write() as c:
        culture = register_lore(c, lore_world, content="河边旅人常在问路前互报来意。")
        exception = register_lore(
            c, lore_world, "character_belief", author=actor,
            content="我虽知道河边旅人的习惯，仍喜欢先安静独行，不愿互报私事。",
        )
        local = register_lore(c, lore_world, "local_claim", author=actor)
        recalled = search_dynamic_knowledge(
            c, world_id=wid, query="旅人相处", character_ids={actor}, location_ids={place},
        )
        by_id = {item.chunk.id: item.to_dict() for item in recalled}
        public_hit = by_id[f"dynamic:{culture.result_entity_id}"]
        personal_hit = by_id[f"dynamic:{exception.result_entity_id}"]
        static = {"id": "static-common", "content": "本人可知的静态常识"}
        selected = [static, public_hit, personal_hit, hit(local.result_entity_id)]
        before = c.total_changes
        c.execute("PRAGMA query_only=ON")
        result = annotate(c, lore_world, selected)
        assert c.total_changes == before
        c.execute("PRAGMA query_only=OFF")

    assert result[0] is static
    assert [item["id"] for item in result] == [item["id"] for item in selected]
    assert "provenance" not in public_hit
    public, personal, saying = (item["provenance"] for item in result[1:])
    assert public["knowledge_level"] == "public_lore"
    assert personal["knowledge_level"] == "character_belief"
    assert personal["author_character_id"] == actor
    assert personal["confidence"] == 0.35
    assert personal["registration_id"] == exception.id
    assert personal["source_event_id"] == event
    assert saying["knowledge_level"] == "local_claim"
    assert saying["source_location_id"] == place
    assert result[2]["content"] == personal_hit["content"]
    assert "preference" not in personal
    assert "species" not in personal


def test_other_persons_belief_remote_claim_and_author_material_are_excluded(database, lore_world):
    _wid, _event, _actor, other, _place = lore_world
    with database.write() as c:
        private = register_lore(c, lore_world, "character_belief", author=other)
        local = register_lore(c, lore_world, "local_claim")
        canon = register_lore(c, lore_world, "author_canon")
        public = register_lore(c, lore_world)
        selected = [
            hit(private.result_entity_id), hit(local.result_entity_id),
            hit(canon.result_entity_id), hit(public.result_entity_id, audience="author_hidden"),
            hit(public.result_entity_id, audience="author_canon"),
        ]
        assert annotate(c, lore_world, selected, locations={"未亲临的地点"}) == []
        assert annotate(c, lore_world, [hit(private.result_entity_id)], character=other)


@pytest.mark.parametrize(
    "status", ["proposed", "validating", "approved", "applying", "failed", "rejected"],
)
def test_unapplied_registration_never_becomes_culture(database, lore_world, status):
    with database.write() as c:
        registered = register_lore(c, lore_world)
        c.execute("UPDATE element_registration_requests SET status=? WHERE id=?",
                  (status, registered.id))
        assert annotate(c, lore_world, [hit(registered.result_entity_id)]) == []


def test_pending_draft_requires_real_confirmation_before_retrieval(database, lore_world):
    wid, _event, _actor, _other, _place = lore_world
    with database.write() as c:
        draft = register_lore(c, lore_world, apply=False)
        assert c.execute("SELECT 1 FROM knowledge_entries WHERE registration_id=?",
                         (draft.id,)).fetchone() is None
        assert annotate(c, lore_world, [hit(draft.id)]) == []
        confirmed = WorldElementRegistry().confirm(c, world_id=wid, registration_id=draft.id)
        assert confirmed.status == "applied"
        result = annotate(c, lore_world, [hit(confirmed.result_entity_id)])
        assert result[0]["provenance"]["registration_id"] == draft.id


@pytest.mark.parametrize("status", ["disputed", "retired"])
def test_inactive_lore_is_not_revived_by_old_retrieval(database, lore_world, status):
    with database.write() as c:
        registered = register_lore(c, lore_world)
        c.execute("UPDATE knowledge_entries SET status=? WHERE id=?",
                  (status, registered.result_entity_id))
        assert annotate(c, lore_world, [hit(registered.result_entity_id)]) == []


def test_world_and_registration_provenance_must_match(database, lore_world):
    with database.write() as c:
        registered = register_lore(c, lore_world)
        other_world = WorldRepository().create_world(
            c, name="另一隔离世界", minutes_per_tick=60, seed_demo=True,
        )
        assert annotate_registered_lore(
            c, world_id=other_world, character_id=lore_world[2],
            location_ids={lore_world[4]}, hits=[hit(registered.result_entity_id)],
        ) == []
        c.execute("UPDATE element_registration_requests SET result_entity_id='另一实体' WHERE id=?",
                  (registered.id,))
        assert annotate(c, lore_world, [hit(registered.result_entity_id)]) == []


def test_dynamic_content_and_source_cannot_override_current_registration(database, lore_world):
    with database.write() as c:
        registered = register_lore(c, lore_world, content="地方说法不代表每个人都赞同。")
        stale = hit(
            registered.result_entity_id, content="某种族必定同意同行。", source="伪造来源",
            provenance={"knowledge_level": "author_canon", "confidence": 1}, score=1.5,
        )
        result = annotate(c, lore_world, [stale])[0]
    assert result["content"] == "地方说法不代表每个人都赞同。"
    assert result["source"] == "sqlite:knowledge_entries"
    assert result["score"] == 1.5
    assert result["provenance"]["confidence"] == 0.35
    assert stale["source"] == "伪造来源"


def test_dialogue_retrieval_preserves_registered_attribution_and_hides_author_material(
    database, lore_world,
):
    """哥哥，实际共享对话检索也必须收到来源，不能只验证独立标注器。"""
    wid, _event, actor, other, place = lore_world
    hidden_text = "作者秘密：某种族必定答应所有同行邀请。"
    service = ConversationService(knowledge_base=WorldKnowledgeBase([
        KnowledgeChunk(
            id="hidden-culture", source="作者档案", section="秘密文化",
            content=hidden_text, audience="author_hidden", always_include=True, tags=(),
        ),
    ]))
    with database.write() as c:
        public = register_lore(c, lore_world)
        own = register_lore(c, lore_world, "character_belief", author=actor)
        private = register_lore(c, lore_world, "character_belief", author=other)
        canon = register_lore(c, lore_world, "author_canon")
        snapshot = WorldRepository().get_snapshot(c, wid)
        result = service._retrieve_dialogue_knowledge(
            c, snapshot=snapshot, npc=snapshot.character_by_id(actor),
            player_text="旅人相处", location_id=place,
        )
    by_id = {item["id"]: item for item in result}
    assert set(by_id) == {f"dynamic:{public.result_entity_id}", f"dynamic:{own.result_entity_id}"}
    assert by_id[f"dynamic:{own.result_entity_id}"]["provenance"]["author_character_id"] == actor
    assert f"dynamic:{private.result_entity_id}" not in by_id
    assert f"dynamic:{canon.result_entity_id}" not in by_id
    assert all(item["content"] != hidden_text for item in result)


def test_future_registered_public_lore_waits_for_source_time(database, lore_world):
    """哥哥，真实登记成功也不能让人物预知尚未发生的来源事件。"""
    wid, event, actor, _other, place = lore_world
    with database.write() as c:
        world_time = from_iso(c.execute(
            "SELECT w.current_time FROM worlds w WHERE w.id=?", (wid,),
        ).fetchone()[0])
        future = to_iso(world_time + timedelta(days=1))
        c.execute("UPDATE world_events SET occurred_at=? WHERE id=?", (future, event))
        registered = register_lore(c, lore_world)
        selected = [item.to_dict() for item in search_dynamic_knowledge(
            c, world_id=wid, query="旅人相处", character_ids={actor}, location_ids={place},
        )]
        assert [item["id"] for item in selected] == [f"dynamic:{registered.result_entity_id}"]
        assert annotate(c, lore_world, selected) == []
        c.execute("UPDATE worlds SET current_time=? WHERE id=?", (future, wid))
        assert annotate(c, lore_world, selected)[0]["provenance"]["source_event_id"] == event


def test_observer_from_another_world_or_missing_observer_cannot_read_lore(database, lore_world):
    """哥哥，即便是公开文化条目，也只交给本世界存在的人物。"""
    with database.write() as c:
        registered = register_lore(c, lore_world)
        other_world = WorldRepository().create_world(
            c, name="异世界观察者测试", minutes_per_tick=60, seed_demo=True,
        )
        other_observer = WorldRepository().get_snapshot(c, other_world).characters[0].id
        for observer in (other_observer, "并不存在的人物"):
            assert annotate(c, lore_world, [hit(registered.result_entity_id)],
                            character=observer) == []
