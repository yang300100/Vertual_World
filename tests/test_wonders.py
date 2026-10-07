"""哥哥的奇观验收：走真实施工与时钟，不用生图替代空间证据。"""

from dataclasses import replace
from datetime import timedelta

import pytest

from world_engine.clock import WorldClockService
from world_engine.game_scene import GameSceneService
from world_engine.photos import PhotoCaptureRequest, PhotoService
from world_engine.proximity import same_room
from world_engine.repository import WorldRepository, from_iso, utc_now
from world_engine.society import SocietyService
from world_engine.wonders import WonderService, WonderSpec


@pytest.fixture
def wonder_world(database, settings):
    repo = WorldRepository()
    with database.write() as c:
        wid = repo.create_world(
            c, name="联合浮空城能力试验", minutes_per_tick=60, seed_maps=False, time_scale=60
        )
        loc = dict(
            c.execute("SELECT * FROM locations WHERE world_id=? ORDER BY name", (wid,)).fetchone()
        )
        people = c.execute(
            "SELECT * FROM characters WHERE world_id=? ORDER BY name", (wid,)
        ).fetchall()
        first, second, outsider = [p["id"] for p in people[:3]]
        for cid in (first, second):
            c.execute(
                "UPDATE characters SET longitude=?,latitu"
                "de=?,location_id=?,current_location_id=?"
                " WHERE id=?",
                (loc["longitude"], loc["latitude"], loc["id"], loc["id"], cid),
            )
        c.execute(
            "UPDATE characters SET longitude=?,latitude=? WHERE id=?",
            (loc["longitude"] + 5, loc["latitude"] + 5, outsider),
        )
        player = repo.create_player_character(
            c,
            world_id=wid,
            name="奇观旅人",
            identity="旅行者",
            location_id=loc["id"],
            traits=["好奇"],
            goal="参观奇观",
        )
        spec = WonderSpec(
            name="云间联合城",
            kind="floating_city",
            longitude=loc["longitude"],
            latitude=loc["latitude"],
            ground_location_id=loc["id"],
            altitude_m=600,
            radius_km=1,
            construction_hours=6,
        )
        invitation = WonderService.offer(
            c,
            wid,
            spec,
            [
                {"character_id": first, "polity": "维尔哈米", "authorized": True},
                {"character_id": second, "polity": "德雷霍斯特", "authorized": True},
                {"character_id": outsider, "polity": "拉津布", "authorized": False},
            ],
            from_iso(
                c.execute("SELECT w.current_time FROM worlds w WHERE id=?", (wid,)).fetchone()[0]
            ),
        )
    return {
        "db": database,
        "settings": settings,
        "wid": wid,
        "loc": loc,
        "first": first,
        "second": second,
        "outsider": outsider,
        "player": player.id,
        "invitation": invitation,
    }


def heartbeat(e, hours=1):
    return WorldClockService(e["db"], e["settings"]).heartbeat(
        e["wid"], elapsed_seconds=hours * 60, real_now=utc_now()
    )


def build(e):
    for _ in range(9):
        heartbeat(e)
        with e["db"].read() as c:
            rows = WonderService.view(c, e["wid"])
        if rows and rows[0]["status"] == "active":
            return rows[0]
    pytest.fail("正常心跳没有使奇观建成")


def test_autonomous_joint_construction_and_restart(wonder_world):
    e = wonder_world
    with e["db"].read() as c:
        assert WonderService.view(c, e["wid"]) == []
    heartbeat(e)
    with e["db"].read() as c:
        rows = WonderService.view(c, e["wid"])
        assert rows, {
            "decisions": [dict(r) for r in c.execute("SELECT * FROM wonder_decisions")],
            "knowledge": [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM character_event_knowledge WHERE event_id=?", (e["invitation"],)
                )
            ],
            "world": dict(c.execute("SELECT * FROM worlds WHERE id=?", (e["wid"],)).fetchone()),
        }
        row = rows[0]
        assert row["status"] == "constructing"
        assert set(row["sponsors"]) == {"维尔哈米", "德雷霍斯特"}
        assert not c.execute(
            "SELECT 1 FROM wonder_decisions WHERE character_id=? AND decision='join'",
            (e["outsider"],),
        ).fetchone()
    with e["db"].write() as c:
        with pytest.raises(ValueError, match="尚未建成"):
            WonderService.traverse(c, e["wid"], e["player"], row["id"], "enter", utc_now())
    city = build(e)
    assert city["location_id"] != e["loc"]["id"]
    e["db"].initialize()
    heartbeat(e)
    with e["db"].read() as c:
        assert len(WonderService.view(c, e["wid"])) == 1
        assert (
            c.execute(
                "SELECT COUNT(*) FROM world_events WHERE "
                "world_id=? AND event_type='world.wonder_"
                "completed'",
                (e["wid"],),
            ).fetchone()[0]
            == 1
        )
        assert (
            len(
                c.execute(
                    "SELECT * FROM wonder_decisions WHERE world_id=? AND decision='join'",
                    (e["wid"],),
                ).fetchall()
            )
            == 2
        )


def test_layered_access_movement_and_limited_knowledge(wonder_world):
    from world_engine.actions import ActionService
    from world_engine.domain import ActionProposal, ActionType
    from world_engine.movement import MovementService
    from world_engine.spatial import SpatialContextService

    e = wonder_world
    city = build(e)
    with e["db"].write() as c:
        at = from_iso(
            c.execute("SELECT w.current_time FROM worlds w WHERE id=?", (e["wid"],)).fetchone()[0]
        )
        WonderService.traverse(c, e["wid"], e["player"], city["id"], "enter", at)
        WonderService.traverse(c, e["wid"], e["first"], city["id"], "enter", at)
        air = c.execute("SELECT * FROM characters WHERE id=?", (e["player"],)).fetchone()
        ground = c.execute("SELECT * FROM characters WHERE id=?", (e["second"],)).fetchone()
        assert not same_room(air, ground)
        SpatialContextService().update_world_contexts(c, e["wid"])
        scene = GameSceneService.snapshot(c, e["wid"])
        assert scene["map"]["location_id"] == city["location_id"]
        assert e["first"] in {npc["id"] for npc in scene["npcs"]}
        assert e["second"] not in {npc["id"] for npc in scene["npcs"]}
        assert city["id"] in {w["id"] for w in scene["wonders"]}
        interaction = ActionService().execute(
            c,
            world_id=e["wid"],
            tick_id="city-interaction-check",
            occurred_at=at,
            proposal=ActionProposal(
                actor_id=e["player"],
                action=ActionType.SOCIALIZE,
                target_id=e["first"],
                reason="向同在城内的居民打招呼，验证登城后的实际交互。",
            ),
        )
        assert interaction.accepted is True
        context = SocietyService.personal_context(c, e["wid"], e["first"], at)
        assert context["wonder_context"]["environment"]["kind"] == "floating_city"
        assert context["wonder_context"]["environment"]["altitude_m"] == 600
        unknown = WonderService.context(c, e["wid"], e["outsider"])
        assert unknown["known_wonders"] == []
        movement = MovementService().start(
            c,
            world_id=e["wid"],
            character_id=e["player"],
            destination_longitude=air["longitude"] + 0.001,
            destination_latitude=air["latitude"],
            world_time=at,
        )
        assert e["second"] not in movement.encountered_character_ids
        MovementService().advance(
            c,
            world_id=e["wid"],
            previous_time=at,
            current_time=at + timedelta(minutes=10),
            created_at=utc_now(),
        )
        moved = c.execute("SELECT * FROM characters WHERE id=?", (e["player"],)).fetchone()
        assert moved["current_location_id"] == city["location_id"]
        with pytest.raises(ValueError, match="边界"):
            MovementService().start(
                c,
                world_id=e["wid"],
                character_id=e["player"],
                destination_longitude=air["longitude"] + 1,
                destination_latitude=air["latitude"],
                world_time=at,
            )
        WonderService.traverse(c, e["wid"], e["player"], city["id"], "exit", at)
        assert GameSceneService.snapshot(c, e["wid"])["player"]["space_layer"] == "ground"


class NoImageClient:
    def generate(self, **kwargs):
        raise AssertionError("哥哥禁止调用生图")


def test_photo_directions_and_inside_context_without_generation(wonder_world):
    e = wonder_world
    photo = PhotoService(e["settings"], image_client=NoImageClient())
    city = build(e)
    with e["db"].write() as c:
        c.execute("UPDATE characters SET latitude=latitude-.03 WHERE id=?", (e["player"],))
        prepared = {}
        for direction in ("north", "east", "south", "west"):
            prepared[direction] = photo.prepare_capture(
                c,
                world_id=e["wid"],
                request=PhotoCaptureRequest(
                    photographer_character_id=e["player"], direction=direction
                ),
            )
        assert city["name"] in prepared["north"].prompt
        assert city["name"] not in prepared["south"].prompt
        assert "600" in prepared["north"].prompt
        assert e["second"] not in prepared["north"].context["included_character_ids"]
        c.execute(
            "UPDATE characters SET latitude=? WHERE id=?", (e["loc"]["latitude"], e["player"])
        )
        at = from_iso(
            c.execute("SELECT w.current_time FROM worlds w WHERE id=?", (e["wid"],)).fetchone()[0]
        )
        WonderService.traverse(c, e["wid"], e["player"], city["id"], "enter", at)
        for direction in ("north", "east", "south", "west"):
            inside = photo.prepare_capture(
                c,
                world_id=e["wid"],
                request=PhotoCaptureRequest(
                    photographer_character_id=e["player"], direction=direction
                ),
            )
            assert "浮于空中" in inside.prompt
            assert city["name"] in inside.prompt


def test_api_map_access_and_free_preview(wonder_world):
    from fastapi.testclient import TestClient

    from world_engine.api import create_app

    e = wonder_world
    city = build(e)
    photo = PhotoService(e["settings"], image_client=NoImageClient())
    app = create_app(replace(e["settings"], image_api_key=None), photo_service_override=photo)
    with TestClient(app) as client:
        base = f"/api/worlds/{e['wid']}"
        result = client.get(base + "/game-scene")
        assert result.status_code == 200
        assert city["id"] in {item["id"] for item in result.json()["wonders"]}
        result = client.post(base + f"/wonders/{city['id']}/traverse", json={"direction": "enter"})
        assert result.status_code == 200, result.text
        assert result.json()["scene"]["player"]["space_layer"] == city["space_layer"]
        preview = client.post(
            base + "/photos/preview",
            json={"photographer_character_id": e["player"], "direction": "north"},
        )
        assert preview.status_code == 200, preview.text
        assert "浮于空中" in preview.json()["prompt"]
        assert preview.json()["generation_requested"] is False
        result = client.post(base + f"/wonders/{city['id']}/traverse", json={"direction": "exit"})
        assert result.status_code == 200, result.text


def test_unfinished_city_absent_from_photo_and_access_requires_presence(wonder_world):
    e = wonder_world
    heartbeat(e)
    service = PhotoService(e["settings"], image_client=NoImageClient())
    with e["db"].read() as c:
        preview = service.prepare_capture(
            c,
            world_id=e["wid"],
            request=PhotoCaptureRequest(photographer_character_id=e["player"], direction="north"),
        )
        assert "云间联合城" not in preview.prompt
    city = build(e)
    with e["db"].write() as c:
        at = from_iso(
            c.execute("SELECT w.current_time FROM worlds w WHERE id=?", (e["wid"],)).fetchone()[0]
        )
        with pytest.raises(ValueError, match="100米"):
            WonderService.traverse(c, e["wid"], e["outsider"], city["id"], "enter", at)
        from world_engine.life import LifeActivityService

        actor = c.execute("SELECT * FROM characters WHERE id=?", (e["player"],)).fetchone()
        LifeActivityService.start(c, actor, at, "rest", 60)
        with pytest.raises(ValueError, match="当前活动"):
            WonderService.traverse(c, e["wid"], e["player"], city["id"], "enter", at)


def test_ground_npc_receives_news_with_source_and_delay(wonder_world):
    e = wonder_world
    build(e)
    with e["db"].write() as c:
        at = from_iso(
            c.execute("SELECT w.current_time FROM worlds w WHERE id=?", (e["wid"],)).fetchone()[0]
        )
        assert WonderService.context(c, e["wid"], e["outsider"])["known_wonders"] == []
        SocietyService.spread_public_rumors(c, e["wid"], at + timedelta(hours=1))
        assert WonderService.context(c, e["wid"], e["outsider"])["known_wonders"] == []
        SocietyService.spread_public_rumors(c, e["wid"], at + timedelta(days=2))
        news = WonderService.context(c, e["wid"], e["outsider"])["known_wonders"]
        assert len(news) == 1
        assert news[0]["evidence"]["source_kind"] == "rumor"
        assert news[0]["evidence"]["source_character_id"] in {e["first"], e["second"]}
        assert news[0]["evidence"]["hops"] == 1
        assert news[0]["evidence"]["confidence"] < 1


def test_resident_chooses_to_move_in_on_normal_heartbeat(wonder_world):
    import json

    e = wonder_world
    with e["db"].write() as c:
        c.execute(
            "UPDATE characters SET goals_json=? WHERE id=?",
            (json.dumps(["希望入住云间联合城"], ensure_ascii=False), e["first"]),
        )
    city = build(e)
    with e["db"].read() as c:
        actor = c.execute("SELECT * FROM characters WHERE id=?", (e["first"],)).fetchone()
        assert actor["space_layer"] == city["space_layer"]
        assert actor["location_id"] == city["location_id"]
        assert c.execute(
            "SELECT 1 FROM world_events WHERE actor_id=? AND event_type='action.wonder_enter'",
            (e["first"],),
        ).fetchone()


def test_npc_arrival_in_floating_city_inherits_layer_and_prompt(wonder_world):
    from world_engine.registration import WorldElementRegistry
    from world_engine.registration.models import CharacterArrivalSpec, ElementRegistrationSubmit
    from world_engine.roleplay_pipeline import build_npc_prompt

    e = wonder_world
    city = build(e)
    with e["db"].write() as c:
        registration = WorldElementRegistry().submit(
            c,
            world_id=e["wid"],
            request=ElementRegistrationSubmit(
                source_event_id=city["completed_event_id"],
                idempotency_key="wonder-resident-one",
                payload=CharacterArrivalSpec(
                    name="云城居民", identity="浮空城居民", location_id=city["location_id"]
                ),
            ),
        )
        resident = c.execute(
            "SELECT * FROM characters WHERE id=?", (registration.result_entity_id,)
        ).fetchone()
        assert resident["space_layer"] == city["space_layer"]
        assert resident["altitude_m"] == 600
        at = from_iso(
            c.execute("SELECT w.current_time FROM worlds w WHERE id=?", (e["wid"],)).fetchone()[0]
        )
        context = SocietyService.personal_context(c, e["wid"], resident["id"], at)
        prompt = build_npc_prompt(
            "以角色身份回答。", {"personal_experience": context, "player_text": "你住在哪里？"}
        )
        assert "浮于空中" in str(prompt.messages)
        assert "600" in str(prompt.messages)


def test_wasd_stays_on_platform_and_cross_layer_dialogue_is_rejected(wonder_world):
    from world_engine.actions import ActionService
    from world_engine.domain import ActionProposal, ActionType
    from world_engine.player_steering import PlayerSteeringService

    e = wonder_world
    city = build(e)
    with e["db"].write() as c:
        at = from_iso(
            c.execute("SELECT w.current_time FROM worlds w WHERE id=?", (e["wid"],)).fetchone()[0]
        )
        WonderService.traverse(c, e["wid"], e["player"], city["id"], "enter", at)
        denied = ActionService().execute(
            c,
            world_id=e["wid"],
            tick_id="layer-check",
            occurred_at=at,
            proposal=ActionProposal(
                actor_id=e["player"],
                action=ActionType.SOCIALIZE,
                target_id=e["second"],
                reason="检查楼上楼下是否被误判成同一场景",
            ),
        )
        assert denied.accepted is False
        actor = c.execute("SELECT * FROM characters WHERE id=?", (e["player"],)).fetchone()
        c.execute("UPDATE characters SET movement_speed_kmh=500 WHERE id=?", (e["player"],))
        actor = c.execute("SELECT * FROM characters WHERE id=?", (e["player"],)).fetchone()
        PlayerSteeringService.set_direction(c, actor, (1, 0), 1, at, utc_now())
        PlayerSteeringService.advance(c, e["wid"], at + timedelta(minutes=10), utc_now())
        player = c.execute("SELECT * FROM characters WHERE id=?", (e["player"],)).fetchone()
        assert player["space_layer"] == city["space_layer"]
        assert player["current_location_id"] == city["location_id"]
        assert WonderService.contains(
            c, e["wid"], city["space_layer"], player["longitude"], player["latitude"]
        )


def test_unknown_npc_destination_context_and_doors_do_not_leak_city(wonder_world):
    from world_engine.interiors import InteriorService
    from world_engine.npc_stage_actions import travel_destinations

    e = wonder_world
    city = build(e)
    with e["db"].read() as c:
        unknown = c.execute("SELECT * FROM characters WHERE id=?", (e["outsider"],)).fetchone()
        assert city["location_id"] not in {item["id"] for item in travel_destinations(c, unknown)}
        ground = c.execute("SELECT * FROM characters WHERE id=?", (e["second"],)).fetchone()
        assert not InteriorService.near_location(c, ground, city["location_id"])


def test_conversation_budget_keeps_current_wonder_environment(wonder_world):
    from world_engine.conversations import ConversationService
    from world_engine.roleplay_pipeline import build_npc_prompt

    e = wonder_world
    city = build(e)
    with e["db"].write() as c:
        at = from_iso(
            c.execute("SELECT current_time FROM worlds WHERE id=?", (e["wid"],)).fetchone()[0]
        )
        WonderService.traverse(c, e["wid"], e["first"], city["id"], "enter", at)
        environment = WonderService.context(c, e["wid"], e["first"])
    service = ConversationService(max_context_tokens=800)
    history = [
        line
        for i in range(12)
        for line in (
            f"[2040-04-01T10:{i:02}:00+00:00] 玩家：" + "旧问" * 140,
            f"[2040-04-01T10:{i:02}:00+00:00] NPC：" + "旧答" * 140,
        )
    ]
    context = service._apply_context_budget(
        {
            "npc": {"name": "云城居民"},
            "player_text": "今天感觉如何？",
            "personal_experience": {"wonder_context": environment},
            "recent_conversation": history,
        }
    )
    assert context["personal_experience"]["wonder_context"] == environment
    assert context["budget_trace"]["total"] <= 800
    prompt = build_npc_prompt("以角色身份回答。", context)
    assert "浮于空中" in str(prompt.messages)
    assert "600" in str(prompt.messages)
