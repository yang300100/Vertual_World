"""SillyTavern 角色卡导入后必须能进入主要 NPC 的对话管线。"""

from __future__ import annotations

import base64
import binascii
import json
import struct
import zlib
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from world_engine.api import create_app
from world_engine.api_player import SillyTavernCardImportRequest, _parse_uploaded_sillytavern_card
from world_engine.roleplay_pipeline import build_npc_prompt
from world_engine.sillytavern_cards import (
    CardImportError,
    character_book_review_text,
    extract_character_book_fragments,
    parse_sillytavern_card,
    select_character_book_person,
)


def _card_json() -> bytes:
    return json.dumps({
        "spec": "chara_card_v2",
        "data": {
            "name": "卡片里的名字",
            "description": "她是谨慎的档案管理员。",
            "personality": "说话简洁，遇到陌生人会先确认来意。",
            "scenario": "{{char}} 正在整理档案；{{user}} 来询问。",
            "first_mes": "有什么事需要查？",
            "mes_example": "<START>\n{{user}}: 可以查档吗？\n{{char}}: 先说查哪一年。",
            "system_prompt": "优先扮演档案管理员的口吻。",
            "post_history_instructions": "保持简洁。",
            "creator_notes": "只供作者查看。",
        },
    }, ensure_ascii=False).encode("utf-8")


def _chunk(kind: bytes, data: bytes) -> bytes:
    crc = binascii.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def _card_png(kind: bytes = b"tEXt") -> bytes:
    encoded = base64.b64encode(_card_json())
    if kind == b"zTXt":
        metadata = b"chara\x00\x00" + zlib.compress(encoded)
    elif kind == b"iTXt":
        metadata = b"chara\x00\x00\x00\x00\x00" + encoded
    else:
        metadata = b"chara\x00" + encoded
    header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    pixel = zlib.compress(b"\x00\xff\xcc\xaa\xff")
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header)
            + _chunk(kind, metadata) + _chunk(b"IDAT", pixel) + _chunk(b"IEND", b""))


@pytest.mark.parametrize("filename,data", [
    ("npc.json", _card_json()),
    ("npc.png", _card_png()),
    ("npc.png", _card_png(b"zTXt")),
    ("npc.png", _card_png(b"iTXt")),
])
def test_import_parser_preserves_roleplay_fields(filename: str, data: bytes) -> None:
    card = parse_sillytavern_card(data, filename)
    assert card["name"] == "卡片里的名字"
    assert card["personality"] == "说话简洁，遇到陌生人会先确认来意。"
    assert "{{char}}" in card["scenario"]
    assert "creator_notes" not in card


def test_import_parser_rejects_corrupt_png_and_plain_json() -> None:
    with pytest.raises(CardImportError):
        parse_sillytavern_card(_card_png()[:-8], "npc.png")
    with pytest.raises(CardImportError):
        parse_sillytavern_card(b'{"name":"empty"}', "npc.json")


def test_imported_card_keeps_world_identity_and_shapes_dialogue() -> None:
    imported = parse_sillytavern_card(_card_json(), "npc.json")
    prompt = build_npc_prompt("世界规则优先。", {
        "npc": {"name": "世界里的阿月"},
        "player": {"name": "旅人"},
        "npc_card": {"public_role": "本地职务", "sillytavern_card": imported},
        "player_text": "能查档吗？",
    }, max_input_tokens=10000)
    contents = [item["content"] for item in prompt.messages]
    assert any("世界里的阿月 正在整理档案；旅人 来询问" in text for text in contents)
    assert any("先说查哪一年" in text for text in contents)
    assert any("优先扮演档案管理员" in text for text in contents)
    assert not any("只供作者查看" in text for text in contents)
    assert prompt.messages[0]["content"].startswith("世界规则优先。")
    assert prompt.messages[-1]["role"] == "user"


def test_card_example_turns_are_budgeted_as_complete_groups() -> None:
    imported = parse_sillytavern_card(_card_json(), "npc.json")
    imported["mes_example"] = (
        "<START>\n{{user}}: 第一问\n{{char}}: 第一答"
        "\n<START>\n{{user}}: 第二问\n{{char}}: 第二答"
    )
    prompt = build_npc_prompt("规则", {
        "npc": {"name": "阿月"}, "player": {"name": "旅人"},
        "npc_card": {"sillytavern_card": imported}, "player_text": "现在呢？",
    }, max_input_tokens=10000)
    examples = [item["content"] for item in prompt.messages
                if "dialogue_style_example" in item["content"]]
    assert len(examples) == 3
    assert any("第一问" in item and "第二问" not in item for item in examples)
    assert any("第二问" in item and "第一问" not in item for item in examples)


def test_opening_line_is_not_repeated_after_conversation_started() -> None:
    imported = parse_sillytavern_card(_card_json(), "npc.json")
    prompt = build_npc_prompt("规则", {
        "npc": {"name": "阿月"}, "player": {"name": "旅人"},
        "npc_card": {"sillytavern_card": imported},
        "recent_conversation": [
            "[2026-09-07T10:00:00+00:00] 玩家：之前说过的事情",
            "[2026-09-07T10:00:00+00:00] NPC：我记得",
        ],
        "player_text": "后来呢？",
    }, max_input_tokens=10000)
    contents = " ".join(item["content"] for item in prompt.messages)
    assert "有什么事需要查" not in contents
    assert "我记得" in contents


def test_import_api_accepts_existing_npc_and_persists_card(settings) -> None:
    app = create_app(settings)
    data_url = "data:application/json;base64," + base64.b64encode(_card_json()).decode()
    with TestClient(app) as client:
        created = client.post("/api/worlds", json={"name": "导入卡世界", "seed_demo": True}).json()
        world_id = created["world"]["id"]
        core = next(item for item in created["characters"] if item["is_core"])
        noncore = next(item for item in created["characters"] if not item["is_core"]
                       and not item["is_player"])
        endpoint = f"/api/worlds/{world_id}/characters/{{}}/sillytavern-card"
        bound_noncore = client.post(endpoint.format(noncore["id"]), json={
            "file_name": "npc.json", "data_url": data_url,
        })
        imported = client.post(endpoint.format(core["id"]), json={
            "file_name": "npc.json", "data_url": data_url,
        })
        fetched = client.get(
            f"/api/worlds/{world_id}/characters/{core['id']}/character-card"
        )
        revised = client.put(
            f"/api/worlds/{world_id}/characters/{core['id']}/character-card",
            json={
                "public_role": "档案管理员", "current_preoccupation": "整理旧档案",
                "private_tension": "怕漏掉索引", "social_boundary": "不会透露别人的私信",
                "expression_notes": "先确认年份",
            },
        )
        removed = client.delete(endpoint.format(core["id"]))
        after_remove = client.get(
            f"/api/worlds/{world_id}/characters/{core['id']}/character-card"
        )
    assert bound_noncore.status_code == 200
    assert bound_noncore.json()["sillytavern_card"]["name"] == "卡片里的名字"
    assert imported.status_code == 200
    assert fetched.json()["sillytavern_card"]["description"] == "她是谨慎的档案管理员。"
    assert fetched.json()["public_role"] != "卡片里的名字"
    assert revised.json()["sillytavern_card"]["name"] == "卡片里的名字"
    assert removed.status_code == 200
    assert "sillytavern_card" not in after_remove.json()
    assert after_remove.json()["public_role"] == "档案管理员"


def test_import_card_creates_core_npc_at_selected_location(settings) -> None:
    app = create_app(settings)
    data_url = "data:application/json;base64," + base64.b64encode(_card_json()).decode()
    with TestClient(app) as client:
        created = client.post("/api/worlds", json={"name": "新人物世界", "seed_demo": True}).json()
        world_id = created["world"]["id"]
        location_id = created["locations"][0]["id"]
        result = client.post(
            f"/api/worlds/{world_id}/characters/from-sillytavern",
            json={
                "file_name": "npc.json", "data_url": data_url,
                "identity": "档案管理员", "location_id": location_id,
            },
        )
        assert result.status_code == 201, result.text
        npc = next(item for item in client.get(f"/api/worlds/{world_id}").json()["characters"]
                   if item["id"] == result.json()["character_id"])
        card = client.get(
            f"/api/worlds/{world_id}/characters/{npc['id']}/character-card"
        ).json()
    assert npc["name"] == "卡片里的名字"
    assert npc["identity"] == "档案管理员"
    assert npc["current_location_id"] == location_id
    assert npc["is_core"] is True
    assert card["sillytavern_card"]["personality"] == "说话简洁，遇到陌生人会先确认来意。"


def test_preview_card_and_import_edited_world_compatible_content(settings) -> None:
    app = create_app(settings)
    data_url = "data:application/json;base64," + base64.b64encode(_card_json()).decode()
    with TestClient(app) as client:
        created = client.post(
            "/api/worlds", json={"name": "改写角色卡世界", "seed_demo": True},
        ).json()
        world_id = created["world"]["id"]
        before = len(created["characters"])
        preview = client.post(
            f"/api/worlds/{world_id}/characters/sillytavern-preview",
            json={"file_name": "npc.json", "data_url": data_url},
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["scenario"].startswith("{{char}}")
        assert len(client.get(f"/api/worlds/{world_id}").json()["characters"]) == before
        imported = client.post(
            f"/api/worlds/{world_id}/characters/from-sillytavern",
            json={
                "file_name": "npc.json", "data_url": data_url,
                "identity": "本地档案管理员", "location_id": created["locations"][0]["id"],
                "card_overrides": {
                    "name": "本地阿月",
                    "description": "她在本地档案馆工作。",
                    "scenario": "{{char}} 正在整理本地文书。",
                    "system_prompt": "",
                },
            },
        )
        assert imported.status_code == 201, imported.text
        character_id = imported.json()["character_id"]
        card = client.get(
            f"/api/worlds/{world_id}/characters/{character_id}/character-card"
        ).json()["sillytavern_card"]
    assert imported.json()["name"] == "本地阿月"
    assert card["description"] == "她在本地档案馆工作。"
    assert card["scenario"] == "{{char}} 正在整理本地文书。"
    assert card["system_prompt"] == ""


def test_png_card_avatar_and_removal_fallback(settings, tmp_path) -> None:
    configured = replace(settings, media_directory=tmp_path / "media")
    png = _card_png()
    payload = {
        "file_name": "npc.png",
        "data_url": "data:image/png;base64," + base64.b64encode(png).decode(),
    }
    with TestClient(create_app(configured)) as client:
        created = client.post("/api/worlds", json={"name": "卡面头像世界"}).json()
        world_id = created["world"]["id"]
        npc_id = created["characters"][0]["id"]
        bound = client.post(
            f"/api/worlds/{world_id}/characters/{npc_id}/sillytavern-card", json=payload,
        )
        assert bound.status_code == 200, bound.text
        character = next(item for item in client.get(f"/api/worlds/{world_id}").json()["characters"]
                         if item["id"] == npc_id)
        assert character["portrait_url"].startswith("/world-media/")
        assert client.get(character["portrait_url"]).content == png
        removed = client.delete(f"/api/worlds/{world_id}/characters/{npc_id}/sillytavern-card")
        assert removed.status_code == 200
        character = next(item for item in client.get(f"/api/worlds/{world_id}").json()["characters"]
                         if item["id"] == npc_id)
        assert character["portrait_url"] is None


def test_card_overrides_reject_unknown_fields(settings) -> None:
    data_url = "data:application/json;base64," + base64.b64encode(_card_json()).decode()
    with TestClient(create_app(settings)) as client:
        created = client.post("/api/worlds", json={"name": "卡片字段校验"}).json()
        world_id = created["world"]["id"]
        result = client.post(
            f"/api/worlds/{world_id}/characters/from-sillytavern",
            json={
                "file_name": "npc.json", "data_url": data_url,
                "identity": "管理员", "location_id": created["locations"][0]["id"],
                "card_overrides": {"creator_notes": "不得修改"},
            },
        )
        assert result.status_code == 422


def test_binding_edited_card_preserves_existing_npc_identity(settings) -> None:
    data_url = "data:application/json;base64," + base64.b64encode(_card_json()).decode()
    with TestClient(create_app(settings)) as client:
        created = client.post("/api/worlds", json={"name": "绑定卡片编辑"}).json()
        world_id = created["world"]["id"]
        npc = created["characters"][0]
        result = client.post(
            f"/api/worlds/{world_id}/characters/{npc['id']}/sillytavern-card",
            json={
                "file_name": "npc.json", "data_url": data_url,
                "card_overrides": {"description": "改成符合本地世界的档案员。"},
            },
        )
        after = client.get(f"/api/worlds/{world_id}").json()
    assert result.status_code == 200, result.text
    assert result.json()["sillytavern_card"]["description"] == "改成符合本地世界的档案员。"
    updated_npc = next(item for item in after["characters"] if item["id"] == npc["id"])
    assert updated_npc["name"] == npc["name"]


def test_invalid_card_does_not_create_npc(settings) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        created = client.post(
            "/api/worlds", json={"name": "拒绝坏卡世界", "seed_demo": True},
        ).json()
        world_id = created["world"]["id"]
        before = len(created["characters"])
        result = client.post(
            f"/api/worlds/{world_id}/characters/from-sillytavern",
            json={
                "file_name": "npc.json",
                "data_url": "data:application/json;base64," + base64.b64encode(b"{}").decode(),
                "identity": "档案管理员", "location_id": created["locations"][0]["id"],
            },
        )
        after = len(client.get(f"/api/worlds/{world_id}").json()["characters"])
    assert result.status_code == 422
    assert after == before


def test_import_card_rejects_blank_identity_and_foreign_location(settings) -> None:
    app = create_app(settings)
    data_url = "data:application/json;base64," + base64.b64encode(_card_json()).decode()
    with TestClient(app) as client:
        first = client.post("/api/worlds", json={"name": "地点甲", "seed_demo": True}).json()
        second = client.post("/api/worlds", json={"name": "地点乙", "seed_demo": True}).json()
        world_id = first["world"]["id"]
        base = {"file_name": "npc.json", "data_url": data_url, "identity": "档案管理员",
                "location_id": first["locations"][0]["id"]}
        blank = client.post(f"/api/worlds/{world_id}/characters/from-sillytavern",
                            json={**base, "identity": "   "})
        foreign = client.post(f"/api/worlds/{world_id}/characters/from-sillytavern",
                              json={**base, "location_id": second["locations"][0]["id"]})
        after = len(client.get(f"/api/worlds/{world_id}").json()["characters"])
    assert blank.status_code == 422
    assert foreign.status_code == 404
    assert after == len(first["characters"])


def _multi_person_card() -> bytes:
    return json.dumps({"spec": "chara_card_v3", "data": {
        "name": "群像卡", "description": "", "first_mes": "大家都在场。",
        "character_book": {"entries": [
            {"comment": "阿月", "keys": ["阿月"], "enabled": True,
             "content": (
                 "# SFW - 人物设定\nname: 阿月\n# 人物背景\n在档案馆工作。"
                 "\n# 性格与行为\n谨慎。\n# NSFW 设置\n不应导入"
             )},
            {"comment": "阿星", "keys": ["阿星"], "enabled": True,
             "content": (
                 "# SFW - 人物设定\nname: 阿星\n# 人物背景\n在码头工作。\n# 性格与行为\n直率。"
             )},
            {"comment": "阿月 性格补充", "keys": ["阿月"], "enabled": True,
             "content": "# SFW - 人物设定\n# 沟通特征\n说话简短。"},
            {"comment": "世界规则", "keys": [], "constant": True,
             "content": "阿月与阿星住在同一个城镇。"},
        ]},
    }}, ensure_ascii=False).encode("utf-8")


def test_freeform_worldbook_profile_uses_labeled_lines_without_sfw_headings() -> None:
    source = {"character_book": {"entries": [
        {"comment": "阿月", "keys": [], "enabled": True,
         "content": (
             "<角色资料>\n姓名: 阿月\n身份: 档案馆记录员\n发色: 黑色\n"
             "性格: 谨慎，遇到陌生人先确认来意\n场景: 正在整理文书\n"
             "罩杯: 不应提取\n</角色资料>"
         )},
        {"comment": "阿星", "keys": [], "enabled": True,
         "content": "姓名: 阿星\n身份: 码头工人\n性格: 直率"},
    ]}}

    card, review = select_character_book_person(source, "chara_card_v3", "阿月", [])

    assert "档案馆记录员" in card["description"]
    assert "黑色" in card["description"]
    assert card["personality"] == "谨慎，遇到陌生人先确认来意"
    assert card["scenario"] == "正在整理文书"
    assert "不应提取" not in json.dumps(card, ensure_ascii=False)
    assert "码头工人" not in json.dumps(card, ensure_ascii=False)
    assert review == [0]


def test_named_wrapper_keeps_freeform_person_description() -> None:
    source = {"character_book": {"entries": [
        {"comment": "阿月", "keys": [], "enabled": True,
         "content": (
             "<阿月>\n<档案>\n姓名: 阿月\n身份: 航线记录员\n</档案>\n"
             "她从小研究潮汐，习惯把每次出航的观察写进笔记。\n"
             "她对陌生人谨慎，却愿意分享核实过的消息。\n</阿月>\n"
             "<阿星>\n她在码头经营船队。\n</阿星>"
         )},
    ]}}

    card, _ = select_character_book_person(source, "chara_card_v3", "阿月", [])

    assert "航线记录员" in card["description"]
    assert "研究潮汐" in card["description"]
    assert "核实过的消息" in card["description"]
    assert "码头经营船队" not in json.dumps(card, ensure_ascii=False)


def test_rich_named_wrapper_does_not_wait_for_model_extraction() -> None:
    source = {"character_book": {"entries": [{
        "comment": "阿月", "keys": [], "enabled": True,
        "content": "<阿月>\n姓名: 阿月\n" + "她谨慎地记录每天的潮汐变化。" * 25 + "\n</阿月>",
    }]}}

    def unexpected_extractor(person_name, content):
        raise AssertionError("已有完整人物正文时不应调用模型")

    card, _ = select_character_book_person(
        source, "chara_card_v3", "阿月", [], extractor=unexpected_extractor,
    )

    assert len(card["description"]) > 200


def test_alternative_sfw_headings_are_read_without_nsfw_text() -> None:
    source = {"character_book": {"entries": [{
        "comment": "阿月", "keys": ["阿月"], "enabled": True,
        "content": (
            "# SFW\n姓名: 阿月\n## 职业\n档案馆记录员。\n"
            "## 性格\n谨慎，回答前会核对资料。\n"
            "# NSFW\n不应导入的内容。"
        ),
    }]}}

    card, _ = select_character_book_person(source, "chara_card_v3", "阿月", [])

    assert card["description"] == "档案馆记录员。"
    assert card["personality"] == "谨慎，回答前会核对资料。"
    assert "不应导入" not in json.dumps(card, ensure_ascii=False)


def test_freeform_extraction_keeps_only_quotes_from_selected_entry() -> None:
    class Backend:
        def complete(self, **kwargs):
            schema = kwargs["schema"]
            return SimpleNamespace(data=schema.validate_python({"fragments": [
                {"field": "description", "quote": "她在档案馆记录航线。"},
                {"field": "personality", "quote": "她习惯先核对来意。"},
                {"field": "description", "quote": "她在码头经营船队。"},
            ]}))

    fields = extract_character_book_fragments(
        Backend(), person_name="阿月",
        content="姓名: 阿月\n她在档案馆记录航线。她习惯先核对来意。",
    )

    assert fields == {
        "description": "她在档案馆记录航线。",
        "personality": "她习惯先核对来意。",
    }


def test_minor_adult_entry_is_kept_for_manual_review_without_model_call() -> None:
    class Backend:
        def complete(self, **kwargs):
            raise AssertionError("不应发送该条目")

    fields = extract_character_book_fragments(
        Backend(), person_name="阿月",
        content="姓名: 阿月\n年龄: 16岁\n# NSFW\n此段由玩家自行核对。",
    )

    assert fields == {}


def test_minor_freeform_review_only_shows_basic_labeled_fields() -> None:
    text = "姓名: 阿月\n年龄: 16岁\n身份: 档案馆学徒\n未分类的私密正文。"

    review = character_book_review_text(text)

    assert "档案馆学徒" in review
    assert "私密正文" not in review


def test_preview_uses_model_fragments_for_unstructured_selected_person() -> None:
    class Backend:
        def complete(self, **kwargs):
            schema = kwargs["schema"]
            return SimpleNamespace(data=schema.validate_python({"fragments": [
                {"field": "description", "quote": "她在档案馆记录航线。"},
                {"field": "personality", "quote": "她习惯先核对来意。"},
            ]}))

    raw = json.dumps({"spec": "chara_card_v3", "data": {
        "name": "群像卡", "first_mes": "大家在场。",
        "character_book": {"entries": [
            {"comment": "阿月", "keys": ["阿月"], "enabled": True,
             "content": "姓名: 阿月\n她在档案馆记录航线。她习惯先核对来意。阿星也在场。"},
            {"comment": "阿星", "keys": ["阿星"], "enabled": True,
             "content": "姓名: 阿星\n她在码头工作。"},
        ]},
    }}, ensure_ascii=False).encode()
    payload = SillyTavernCardImportRequest(
        file_name="group.json",
        data_url="data:application/json;base64," + base64.b64encode(raw).decode(),
        person_name="阿月",
    )

    card = _parse_uploaded_sillytavern_card(payload, preview=True, backend=Backend())

    assert card["description"] == "她在档案馆记录航线。"
    assert card["personality"] == "她习惯先核对来意。"
    assert "码头" not in json.dumps(card, ensure_ascii=False)


def test_analyze_multi_person_card_and_bind_only_selected_profile(settings) -> None:
    data_url = "data:application/json;base64," + base64.b64encode(_multi_person_card()).decode()
    with TestClient(create_app(settings)) as client:
        world = client.post("/api/worlds", json={"name": "多人卡验证"}).json()
        world_id = world["world"]["id"]
        npc_id = world["characters"][0]["id"]
        base = {"file_name": "multi.json", "data_url": data_url}
        analyzed = client.post(
            f"/api/worlds/{world_id}/characters/sillytavern-analyze", json=base,
        )
        assert analyzed.status_code == 200, analyzed.text
        assert [person["name"] for person in analyzed.json()["people"]] == ["阿月", "阿星"]
        assert analyzed.json()["people"][0]["suggested_entry_indices"] == [2]
        unselected = client.post(
            f"/api/worlds/{world_id}/characters/{npc_id}/sillytavern-card", json=base,
        )
        assert unselected.status_code == 422
        selection = {**base, "person_name": "阿月", "include_entry_indices": [2]}
        preview = client.post(
            f"/api/worlds/{world_id}/characters/sillytavern-preview", json=selection,
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["name"] == "阿月"
        assert "档案馆" in preview.json()["description"]
        assert "档案馆" in preview.json()["source_text"]
        assert "说话简短" in preview.json()["personality"]
        assert "码头" not in json.dumps(preview.json(), ensure_ascii=False)
        assert "不应导入" not in json.dumps(preview.json(), ensure_ascii=False)
        bound = client.post(
            f"/api/worlds/{world_id}/characters/{npc_id}/sillytavern-card", json=selection,
        )
        assert bound.status_code == 200, bound.text
        stored = bound.json()["sillytavern_card"]
        assert stored["name"] == "阿月"
        assert "码头" not in json.dumps(stored, ensure_ascii=False)
        prompt = build_npc_prompt("世界规则", {
            "npc": {"name": "本地人物"}, "player": {"name": "旅人"},
            "npc_card": {"sillytavern_card": stored}, "player_text": "你好",
        }, max_input_tokens=10000)
        prompt_text = " ".join(item["content"] for item in prompt.messages)
        assert "档案馆" in prompt_text
        assert "码头" not in prompt_text
        created_npc = client.post(
            f"/api/worlds/{world_id}/characters/from-sillytavern",
            json={
                **selection, "identity": "档案管理员",
                "location_id": world["locations"][0]["id"],
            },
        )
        assert created_npc.status_code == 201, created_npc.text
        assert created_npc.json()["name"] == "阿月"
        rejected = client.post(
            f"/api/worlds/{world_id}/characters/sillytavern-preview",
            json={**base, "person_name": "阿月", "include_entry_indices": [3]},
        )
        assert rejected.status_code == 422


def test_unstructured_single_person_prose_is_previewed_before_binding(settings) -> None:
    raw = json.dumps({"spec": "chara_card_v3", "data": {
        "name": "群像卡", "first_mes": "大家在场。",
        "character_book": {"entries": [{
            "comment": "阿月", "keys": ["阿月"], "enabled": True,
            "content": "name: 阿月\n她在档案馆工作，尚未分段整理。",
        }]},
    }}, ensure_ascii=False).encode("utf-8")
    base = {"file_name": "unstructured.json",
            "data_url": "data:application/json;base64," + base64.b64encode(raw).decode(),
            "person_name": "阿月"}
    with TestClient(create_app(settings)) as client:
        world = client.post("/api/worlds", json={"name": "人工整理验证"}).json()
        world_id = world["world"]["id"]
        npc_id = world["characters"][0]["id"]
        preview = client.post(
            f"/api/worlds/{world_id}/characters/sillytavern-preview", json=base,
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["name"] == "阿月"
        assert "她在档案馆工作" in preview.json()["description"]
        bound = client.post(
            f"/api/worlds/{world_id}/characters/{npc_id}/sillytavern-card", json=base,
        )
        assert bound.status_code == 200, bound.text
        assert "她在档案馆工作" in bound.json()["sillytavern_card"]["description"]
