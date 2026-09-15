from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from fastapi.testclient import TestClient

from world_engine.api import create_app
from world_engine.database import Database


class _FakeNpcModelBackend:
    """用结构化模型替身覆盖 API 社交测试，避免预设文本参与测试。"""

    def complete(self, *, schema, **kwargs):  # type: ignore[no-untyped-def]
        return SimpleNamespace(data=schema.validate_python({"reply": "这是测试模型生成的回应。"}))


class _CapturingNpcModelBackend(_FakeNpcModelBackend):
    """记录跨渠道回复的统一上下文，确保 API 不再绕过角色卡和记忆管线。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.reply_contexts: list[dict[str, object]] = []

    name = "capturing-dialogue"

    def complete(self, *, schema, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        return super().complete(schema=schema, **kwargs)

    def respond_to_player(self, *, npc, player, context):  # type: ignore[no-untyped-def]
        from world_engine.decisions import NpcReply

        del npc, player
        self.reply_contexts.append(context)
        return NpcReply(reply="这是多人测试模型生成的回应。", social_move="answer")


def _use_fake_npc_model(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "world_engine.engine.build_agent_model_backend",
        lambda settings: _FakeNpcModelBackend(),
    )


def test_cross_channel_npc_replies_share_dialogue_context(
    settings, monkeypatch, tmp_path
) -> None:
    (tmp_path / "common.md").write_text(
        """<!-- rag: audience=character_common; always_include=true; tags=信笺,约定 -->
# 往来常识

正式约定在双方确认前都不能视为已经执行。
""",
        encoding="utf-8",
    )
    backend = _CapturingNpcModelBackend()
    monkeypatch.setattr(
        "world_engine.engine.build_agent_model_backend",
        lambda current_settings: backend,
    )
    monkeypatch.setattr(
        "world_engine.engine.build_decision_provider",
        lambda current_settings: backend,
    )
    configured = replace(settings, knowledge_paths=(tmp_path,), knowledge_enabled=True)
    with TestClient(create_app(configured)) as client:
        created = client.post("/api/worlds", json={"name": "统一对话世界"}).json()
        world_id = created["world"]["id"]
        npc = created["characters"][0]
        client.post(
            f"/api/worlds/{world_id}/player",
            json={"name": "旅人", "identity": "记录者", "location_id": npc["location_id"]},
        )
        contact = client.post(
            f"/api/worlds/{world_id}/contacts", json={"recipient_id": npc["id"]}
        )
        assert contact.status_code == 200
        letter = client.post(
            f"/api/worlds/{world_id}/messages",
            json={"recipient_id": npc["id"], "content": "还记得我们的约定吗？"},
        )
        assert letter.status_code == 200
        long_term = client.post(
            f"/api/worlds/{world_id}/long-term-requests",
            json={
                "recipient_id": npc["id"],
                "operation_type": "约定",
                "terms": {"payment": 0, "title": "明早见面"},
            },
        )
        assert long_term.status_code == 200

    assert [call["label"] for call in backend.calls] == [
        "contact_reply",
        "letter_reply",
        "long_term_reply",
    ]
    contexts = [call["user_payload"] for call in backend.calls]
    from world_engine.roleplay import npc_reply_system_prompt

    assert all(call["roleplay"] is True for call in backend.calls)
    assert all(
        call["system_prompt"] == npc_reply_system_prompt(include_topic=True)
        for call in backend.calls
    )
    assert [context["channel"] for context in contexts] == [
        "contact_request",
        "letter",
        "long_term_review",
    ]
    for context in contexts:
        assert context["npc_card"]["speech_style"]
        assert context["scene"]["location"]["id"] == npc["location_id"]
        assert context["budget_trace"]["total"] <= context["budget_trace"]["limit"]
        assert any("确认前" in item["content"] for item in context["knowledge_context"])
    letter_context = contexts[1]
    assert letter_context["recent_conversation"]
    assert "recent_letters" not in letter_context["decision"]


def test_group_dialogue_api_returns_locally_scheduled_speakers(
    settings, monkeypatch
) -> None:
    backend = _CapturingNpcModelBackend()
    monkeypatch.setattr(
        "world_engine.engine.build_agent_model_backend",
        lambda current_settings: backend,
    )
    monkeypatch.setattr(
        "world_engine.engine.build_decision_provider",
        lambda current_settings: backend,
    )
    with TestClient(create_app(settings)) as client:
        created = client.post("/api/worlds", json={"name": "多人对话API世界"}).json()
        world_id = created["world"]["id"]
        targets = created["characters"][:2]
        player_snapshot = client.post(
            f"/api/worlds/{world_id}/player",
            json={
                "name": "阿澈",
                "identity": "旅人",
                "location_id": targets[0]["location_id"],
            },
        ).json()
        player = next(item for item in player_snapshot["characters"] if item["is_player"])
        database = Database(settings.database_path)
        with database.write() as connection:
            for target in targets:
                connection.execute(
                    """
                    UPDATE characters
                    SET longitude = ?, latitude = ?, current_location_id = ?
                    WHERE id = ?
                    """,
                    (
                        player["longitude"],
                        player["latitude"],
                        player["location_id"],
                        target["id"],
                    ),
                )
        response = client.post(
            f"/api/worlds/{world_id}/player/group-dialogue",
            json={
                "intent": "你们觉得北门应该先查哪里？",
                "participant_ids": [target["id"] for target in targets],
                "max_speakers": 2,
            },
        )

    assert response.status_code == 201
    assert len(response.json()["replies"]) == 2
    assert {item["character_id"] for item in response.json()["replies"]} == {
        target["id"] for target in targets
    }
    assert [context["channel"] for context in backend.reply_contexts] == [
        "group_scene",
        "group_scene",
    ]


def test_api_creates_reads_and_ticks_world(settings) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        health = client.get("/api/health")
        created = client.post(
            "/api/worlds",
            json={"name": "API世界", "minutes_per_tick": 90, "seed_demo": True},
        )
        assert health.status_code == 200
        assert health.json()["service"] == "virtual-world-core"
        assert health.json()["server_time"].endswith("+00:00")
        assert health.json()["knowledge"] == {"enabled": True, "loaded_chunks": 0}
        assert created.status_code == 201

        world = created.json()
        world_id = world["world"]["id"]
        ticked = client.post(f"/api/worlds/{world_id}/tick")
        fetched = client.get(f"/api/worlds/{world_id}")
        events = client.get(f"/api/worlds/{world_id}/events")

    assert ticked.status_code == 200
    assert ticked.json()["current_version"] == 1
    assert fetched.status_code == 200
    assert fetched.json()["world"]["tick_count"] == 1
    assert events.status_code == 200
    assert len(events.json()) >= 3


def test_api_returns_404_for_missing_world(settings) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/api/worlds/not-found")
    assert response.status_code == 404


def test_api_exposes_clock_heartbeat_and_adjudication_controls(settings) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        created = client.post("/api/worlds", json={"name": "时钟API世界"}).json()
        world_id = created["world"]["id"]
        actor_id = created["characters"][0]["id"]

        speed = client.patch(
            f"/api/worlds/{world_id}/clock",
            json={"time_scale": 2.0, "operator": "test"},
        )
        heartbeat = client.post(f"/api/worlds/{world_id}/heartbeat")
        adjudication = client.post(
            f"/api/worlds/{world_id}/adjudicate",
            json={
                "trigger": "player_intervention",
                "character_ids": [actor_id],
            },
        )
        runs = client.get(f"/api/worlds/{world_id}/adjudications")

    assert speed.status_code == 200
    assert speed.json()["new_time_scale"] == 2.0
    assert heartbeat.status_code == 200
    assert heartbeat.json()["time_scale"] == 2.0
    assert adjudication.status_code == 200
    assert adjudication.json()["trigger"] == "player_intervention"
    assert len(adjudication.json()["outcomes"]) == 1
    assert runs.status_code == 200
    assert len(runs.json()) == 1


def test_api_creates_a_dedicated_player_character(settings) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        created = client.post("/api/worlds", json={"name": "玩家世界"}).json()
        world_id = created["world"]["id"]
        location_id = created["locations"][0]["id"]
        response = client.post(
            f"/api/worlds/{world_id}/player",
            json={
                "name": "阿澈",
                "identity": "远行学者",
                "location_id": location_id,
                "traits": ["谨慎", "好奇"],
                "goal": "记录陌生的遗迹",
            },
        )
        duplicate = client.post(
            f"/api/worlds/{world_id}/player",
            json={
                "name": "第二位玩家",
                "identity": "旅人",
                "location_id": location_id,
            },
        )

    assert response.status_code == 201
    player_characters = [
        item for item in response.json()["characters"] if item["is_player"]
    ]
    assert len(player_characters) == 1
    assert player_characters[0]["name"] == "阿澈"
    assert player_characters[0]["is_pov"] is True
    assert duplicate.status_code == 409


def test_log_scope_can_be_limited_to_the_player_participation(settings) -> None:
    """旅程日志必须优先返回玩家亲历事件，不能被其他人物的 routine 事件挤掉。"""
    app = create_app(settings)
    with TestClient(app) as client:
        created = client.post("/api/worlds", json={"name": "旅程日志世界"}).json()
        world_id = created["world"]["id"]
        npc = created["characters"][0]
        player_snapshot = client.post(
            f"/api/worlds/{world_id}/player",
            json={"name": "阿澈", "identity": "旅人", "location_id": npc["location_id"]},
        ).json()
        player = next(item for item in player_snapshot["characters"] if item["is_player"])
        database = Database(settings.database_path)
        with database.write() as connection:
            connection.executemany(
                """
                INSERT INTO world_events(
                    id, world_id, tick_id, occurred_at, event_type, actor_id, target_id,
                    summary, importance, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, 'routine', '{}', ?)
                """,
                [
                    ("player-log", world_id, "test:player", "2040-01-01T00:01:00+00:00", "action.socialize", player["id"], "玩家的行动", "2040-01-01T00:01:00+00:00"),
                    ("npc-log", world_id, "test:npc", "2040-01-01T00:02:00+00:00", "action.work", npc["id"], "NPC 的行动", "2040-01-01T00:02:00+00:00"),
                ],
            )
        logs = client.get(
            f"/api/worlds/{world_id}/events?scope=log&participant_id={player['id']}"
        )

    assert logs.status_code == 200
    assert [item["id"] for item in logs.json()] == ["player-log"]


def test_frontend_assets_are_served_by_fastapi(settings) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        root = client.get("/", follow_redirects=False)
        page = client.get("/ui/")
        styles = client.get("/ui/styles.css")
        script = client.get("/ui/app.js")
        favicon = client.get("/ui/favicon.svg")
        world_map = client.get("/world-assets/planet-master.png")

    assert root.status_code == 307
    assert root.headers["location"] == "/ui/"
    assert page.status_code == 200
    assert "伊瑟拉 · 旅人之书" in page.text
    assert 'id="player-name"' in page.text
    assert 'id="player-create-form"' in page.text
    assert 'id="player-intent-form"' in page.text
    assert 'id="location-map"' not in page.text
    assert 'id="room-map"' in page.text
    assert 'id="room-notebook"' in page.text
    assert 'id="map-camera"' in page.text
    assert 'id="map-context-name"' in page.text
    assert 'id="map-coordinate-form"' in page.text
    assert 'id="map-longitude-input"' in page.text
    assert 'id="map-latitude-input"' in page.text
    assert 'id="room-log"' in page.text
    assert 'id="player-transport"' in page.text
    assert 'id="nearby-characters"' in page.text
    assert 'id="heartbeat-button"' in page.text
    assert 'id="character-grid"' in page.text
    assert 'id="worker-status"' in page.text
    assert styles.status_code == 200
    assert "--copper-bright" in styles.text
    assert "[hidden]" in styles.text
    assert script.status_code == 200
    assert "/api/worlds/${state.worldId}/heartbeat" in script.text
    assert "function updateLiveClock()" in script.text
    assert "function runLiveFrame()" in script.text
    assert "function renderPlayerExperience" in script.text
    assert "function renderRelationshipGraph" in script.text
    assert "function zoomMap" in script.text
    assert "function updateMapPointScreenPositions" in script.text
    assert "function selectDisplayMap" in script.text
    assert "近期旅程日志" in page.text
    assert "function updateRouteOverlay" in script.text
    assert "elements.mapCoordinateForm.addEventListener" in script.text
    assert "function settlePredictedArrival" in script.text
    assert "/player/group-dialogue" in script.text
    assert "向在场众人说话" in script.text
    assert "function eventDetailText" in script.text
    assert "${speaker}说：“${payload.dialogue}”" in script.text
    assert "正在确认抵达" in script.text
    assert "let shouldExecute = false" in script.text
    assert "if (shouldExecute) executePlayerIntent(intent, directTargetId);" in script.text
    assert "mapContainsForDetailActivation(map, coordinate, nextScale)" in script.text
    assert "resolveZoomDetailMap(nextScale, focusCoordinate, { x: rect.left + focusX" in script.text
    assert "DETAIL_MAP_MIN_SCALE = 16" in script.text
    assert "DETAIL_MAP_ACTIVATION_PADDING_PX = 12" in script.text
    assert "DETAIL_MARKER_ACTIVATION_RADIUS_PX = 72" in script.text
    assert "function detailMapNearScreenPoint" in script.text
    assert "function enterDetailMap" in script.text
    assert "pointercancel" in script.text
    assert "activation_state" in script.text
    assert "/player/move" in script.text
    assert "function renderLongTermTerms" in script.text
    assert "data-long-term-remove" in script.text
    assert 'method: "DELETE"' in script.text
    assert "todoForm" not in script.text
    assert 'localStorage.getItem("iserra.notebook")' in script.text
    assert "virtual-world-intent:" in script.text
    assert favicon.status_code == 200
    assert "image/svg+xml" in favicon.headers["content-type"]
    assert world_map.status_code == 200
    assert world_map.headers["content-type"] == "image/png"
    assert "aspect-ratio: var(--map-aspect" in styles.text


def test_long_term_operations_can_be_removed_without_leaving_a_dossier(settings, monkeypatch) -> None:
    """玩家能结束自己提出的事务，且请求、派生待办和相关记忆一并消失。"""
    _use_fake_npc_model(monkeypatch)
    app = create_app(settings)
    with TestClient(app) as client:
        created = client.post("/api/worlds", json={"name": "社交事务API世界"}).json()
        world_id = created["world"]["id"]
        seeded_npc = next(item for item in created["characters"] if item["name"] == "林澈")
        square = next(item for item in created["locations"] if item["id"] == seeded_npc["location_id"])
        player_snapshot = client.post(
            f"/api/worlds/{world_id}/player",
            json={"name": "旅人", "identity": "记录者", "location_id": square["id"]},
        ).json()
        player = next(item for item in player_snapshot["characters"] if item["is_player"])
        npc = next(item for item in player_snapshot["characters"] if item["name"] == "林澈")

        contact = client.post(f"/api/worlds/{world_id}/contacts", json={"recipient_id": npc["id"]})
        contacts = client.get(f"/api/worlds/{world_id}/contacts")
        letter = client.post(
            f"/api/worlds/{world_id}/messages",
            json={"recipient_id": npc["id"], "content": "我想问问广场附近的情况。"},
        )
        messages = client.get(f"/api/worlds/{world_id}/messages?recipient_id={npc['id']}")
        player_todo = client.post(
            f"/api/worlds/{world_id}/todos",
            json={"character_id": npc["id"], "title": "替NPC安排行程"},
        )
        operation = client.post(
            f"/api/worlds/{world_id}/long-term-requests",
            json={
                "recipient_id": npc["id"], "operation_type": "约定",
                "terms": {"payment": 0, "title": "共同巡查广场", "details": "明日清晨"},
            },
        )
        confirmed = client.post(
            f"/api/worlds/{world_id}/long-term-requests/{operation.json()['id']}/confirm",
            json={"accept_counter_terms": True},
        )
        before_removal = client.get(f"/api/worlds/{world_id}/long-term-requests")
        removed = client.delete(f"/api/worlds/{world_id}/long-term-requests/{operation.json()['id']}")
        requests = client.get(f"/api/worlds/{world_id}/long-term-requests")
        todos = client.get(f"/api/worlds/{world_id}/todos")
        memories = client.get(f"/api/worlds/{world_id}/characters/{player['id']}/memories")

    assert contact.status_code == 200
    assert contact.json()["status"] == "accepted"
    assert contacts.status_code == 200 and contacts.json()[0]["recipient_id"] == npc["id"]
    assert letter.status_code == 200 and letter.json()["reply"] == "这是测试模型生成的回应。"
    assert messages.status_code == 200 and len(messages.json()) >= 4
    assert player_todo.status_code == 405
    assert operation.status_code == 200 and operation.json()["status"] == "npc_accepted"
    assert confirmed.status_code == 200 and confirmed.json()["status"] == "applied"
    assert before_removal.json()[0]["status"] == "applied"
    assert removed.status_code == 200 and removed.json()["status"] == "removed"
    assert requests.json() == []
    assert todos.json() == []
    assert not any("确认并执行" in memory["summary"] for memory in memories.json())


def test_contact_never_falls_back_to_a_preset_reply_without_a_model(settings) -> None:
    """未配置模型时，社交请求必须失败，不能写入固定 NPC 文本。"""
    app = create_app(settings)
    with TestClient(app) as client:
        created = client.post("/api/worlds", json={"name": "无模型社交世界"}).json()
        world_id = created["world"]["id"]
        npc = created["characters"][0]
        created_player = client.post(
            f"/api/worlds/{world_id}/player",
            json={"name": "旅人", "identity": "记录者", "location_id": npc["location_id"]},
        )
        response = client.post(
            f"/api/worlds/{world_id}/contacts", json={"recipient_id": npc["id"]}
        )
        contacts = client.get(f"/api/worlds/{world_id}/contacts")

    assert created_player.status_code == 201
    assert response.status_code == 503
    assert contacts.json() == []


def test_long_term_counterproposal_can_be_removed_and_identity_changes_are_blocked(
    settings, monkeypatch
) -> None:
    """反提案必须保留完整条款并可由玩家移除，身份性事务不得伪装为普通约定。"""
    _use_fake_npc_model(monkeypatch)
    app = create_app(settings)
    with TestClient(app) as client:
        created = client.post("/api/worlds", json={"name": "长期事务边界世界"}).json()
        world_id = created["world"]["id"]
        npc = created["characters"][0]
        location_id = npc["location_id"]
        player_snapshot = client.post(
            f"/api/worlds/{world_id}/player",
            json={"name": "旅人", "identity": "记录者", "location_id": location_id},
        ).json()
        player = next(item for item in player_snapshot["characters"] if item["is_player"])

        countered = client.post(
            f"/api/worlds/{world_id}/long-term-requests",
            json={
                "recipient_id": npc["id"], "operation_type": "委托",
                "terms": {"payment": 3, "title": "修复北门绞盘", "details": "日落前完成"},
            },
        )
        listed = client.get(f"/api/worlds/{world_id}/long-term-requests")
        removed_counter = client.delete(
            f"/api/worlds/{world_id}/long-term-requests/{countered.json()['id']}"
        )
        requests_after_removal = client.get(f"/api/worlds/{world_id}/long-term-requests")
        disguised_marriage = client.post(
            f"/api/worlds/{world_id}/long-term-requests",
            json={
                "recipient_id": npc["id"], "operation_type": "约定",
                "terms": {"payment": 0, "title": "与陌生人结婚", "details": "立刻成为夫妻"},
            },
        )
        removed_rejected = client.delete(
            f"/api/worlds/{world_id}/long-term-requests/{disguised_marriage.json()['id']}"
        )
        requests_after_rejected_removal = client.get(
            f"/api/worlds/{world_id}/long-term-requests"
        )

    assert player["id"]
    assert countered.status_code == 200 and countered.json()["status"] == "npc_countered"
    assert countered.json()["counter_terms"]["payment"] == 6
    assert listed.json()[0]["counter_terms"]["title"] == "修复北门绞盘"
    assert listed.json()[0]["counter_terms"]["details"] == "日落前完成"
    assert removed_counter.status_code == 200 and removed_counter.json()["status"] == "removed"
    assert requests_after_removal.json() == []
    assert disguised_marriage.status_code == 200
    assert disguised_marriage.json()["status"] == "npc_rejected"
    assert disguised_marriage.json()["npc_response"] == "这是测试模型生成的回应。"
    assert disguised_marriage.json()["system_notice"] == "提议涉及身份或权属变更，必须先走世界元素注册审议。"
    assert removed_rejected.status_code == 200 and removed_rejected.json()["status"] == "removed"
    assert requests_after_rejected_removal.json() == []


def test_initialize_removes_legacy_cancelled_long_term_records(settings, monkeypatch) -> None:
    """升级后的初始化不能让旧版的 cancelled 卷宗重新出现在玩家界面。"""
    _use_fake_npc_model(monkeypatch)
    app = create_app(settings)
    with TestClient(app) as client:
        created = client.post("/api/worlds", json={"name": "旧事务清理世界"}).json()
        world_id = created["world"]["id"]
        npc = created["characters"][0]
        client.post(
            f"/api/worlds/{world_id}/player",
            json={"name": "旅人", "identity": "记录者", "location_id": npc["location_id"]},
        )
        countered = client.post(
            f"/api/worlds/{world_id}/long-term-requests",
            json={
                "recipient_id": npc["id"], "operation_type": "委托",
                "terms": {"payment": 3, "title": "旧版反提案"},
            },
        ).json()

    database = Database(settings.database_path)
    with database.write() as connection:
        connection.execute(
            "UPDATE long_term_operation_requests SET status = 'cancelled' WHERE id = ?",
            (countered["id"],),
        )
    database.initialize()

    with TestClient(create_app(settings)) as client:
        requests = client.get(f"/api/worlds/{world_id}/long-term-requests")

    assert requests.status_code == 200
    assert requests.json() == []


def test_character_card_can_be_created_and_updated(settings) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        created = client.post("/api/worlds", json={"name": "角色卡世界", "seed_demo": True})
        world_id = created.json()["world"]["id"]
        npc_id = created.json()["characters"][0]["id"]
        default_card = client.get(
            f"/api/worlds/{world_id}/characters/{npc_id}/character-card"
        )
        updated = client.put(
            f"/api/worlds/{world_id}/characters/{npc_id}/character-card",
            json={
                "public_role": "粮仓管理员",
                "current_preoccupation": "核对今晨尚未入账的粮袋",
                "private_tension": "担心自己的疏漏会拖累赈济",
                "social_boundary": "不会在集市上谈具体账目",
                "expression_notes": "先问来意，再给出自己愿意承担的部分",
                "speech_style": "语句简短，习惯先报出数量再解释原因",
                "initiative_notes": "发现账目异常时会主动询问来货时间",
                "preferred_address": "称熟人为老朋友，称陌生人为客人",
                "dialogue_examples": ["先别急，告诉我是哪一批粮袋。"],
            },
        )
        fetched = client.get(f"/api/worlds/{world_id}/characters/{npc_id}/character-card")

    assert default_card.status_code == 200
    assert updated.status_code == 200
    assert fetched.json()["current_preoccupation"] == "核对今晨尚未入账的粮袋"
    assert fetched.json()["speech_style"] == "语句简短，习惯先报出数量再解释原因"
    assert fetched.json()["dialogue_examples"] == ["先别急，告诉我是哪一批粮袋。"]
