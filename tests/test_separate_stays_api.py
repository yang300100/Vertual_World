"""哥哥，分房 API 只记录玩家本人的明确选择，不公开伙伴独处时的活动。"""

# ruff: noqa: F811
import hashlib
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from test_appointments import people, set_time
from test_lived_world import town  # noqa: F401
from test_overnight_options import third_person
from test_separate_stays import (
    choose,
    enter_player,
    get,
    npc_perform,
    settle,
    setup,
)

from world_engine.api_intimacy import NaturalSocialChoice
from world_engine.life import LifeActivityService
from world_engine.repository import from_iso
from world_engine.shared_life import SharedLifeIntent


def body(e, choice, *, request_id=None):
    text = choice["statement"]
    return {
        "request_id": request_id or str(uuid4()),
        "recipient_id": e.npc,
        "intent": "我想和你商量：" + text,
        "evidence": text,
        "separate_stay_choice": choice,
    }


def post(e, payload):
    return e.client.post(f"/api/worlds/{e.wid}/player/social-choices", json=payload)


def rows(e, character_id=None):
    return e.client.get(
        f"/api/worlds/{e.wid}/separate-stays",
        params={"character_id": character_id or e.npc},
    )


def test_natural_choice_routes_to_separate_stay_and_replays_one_receipt(town):
    e = town
    _, _, _, fields = setup(e)
    payload = body(e, fields)
    # 哥哥，结算使用真正的原话证据，不能把候选字段里的另一句话当作授权。
    payload["separate_stay_choice"] = {**fields, "statement": "这段候选文字不是玩家原话。"}
    first = post(e, payload)
    assert first.status_code == 200, first.text
    sid = first.json()["stay_id"]
    assert get(e, sid)["statement"] == payload["evidence"]
    assert post(e, payload).json() == first.json()
    with e.db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM journey_separate_stays").fetchone()[0] == 1
        assert (
            c.execute(
                "SELECT COUNT(*) FROM social_choice_receipts WHERE request_id=?",
                (payload["request_id"],),
            ).fetchone()[0]
            == 1
        )
        event = json.loads(
            c.execute(
                "SELECT payload_json FROM world_events WHERE id=?",
                (first.json()["event_id"],),
            ).fetchone()[0]
        )
        assert event["player_input"] == payload["intent"]
        assert event["choice_evidence"] == payload["evidence"]
        assert not c.execute("SELECT 1 FROM journey_overnight_stays").fetchone()
        assert not c.execute("SELECT 1 FROM family_agreements").fetchone()
    changed = {**payload, "evidence": payload["evidence"] + "我改变了想法。"}
    changed["intent"] = changed["evidence"]
    assert post(e, changed).status_code == 409
    assert get(e, sid)["statement"] == payload["evidence"]


def test_api_acceptance_does_not_enter_rooms_start_rest_or_spend_for_npc(town):
    e = town
    _, own, other, fields = setup(e)
    sid = choose(e, **{**fields, "own_room_id": other, "other_room_id": own}, npc=True)["stay_id"]
    with e.db.read() as c:
        before = {p["id"]: (p["money"], p["current_room_id"]) for p in people(e, c)}
    payload = body(
        e, {"operation": "accept", "stay_id": sid, "statement": "我同意这份指定分房和会合安排。"}
    )
    accepted = post(e, payload)
    assert accepted.status_code == 200, accepted.text
    assert get(e, sid)["status"] == "accepted"
    assert all(p["status"] == "waiting" for p in get(e, sid)["participants"].values())
    with e.db.read() as c:
        assert {p["id"]: (p["money"], p["current_room_id"]) for p in people(e, c)} == before
        assert not LifeActivityService.running(c, e.player)
        assert not LifeActivityService.running(c, e.npc)


def test_check_in_api_only_starts_player_and_is_idempotent(town):
    e = town
    _, own, _, fields = setup(e)
    sid = post(e, body(e, fields)).json()["stay_id"]
    choose(e, "accept", sid, npc=True)
    start = from_iso(fields["arrival_world_time"])
    set_time(e, start)
    payload = body(
        e,
        {
            "operation": "check_in",
            "stay_id": sid,
            "statement": "我选择在自己的指定房间开始本人休息。",
        },
    )
    assert post(e, payload).status_code == 409
    assert get(e, sid)["participants"][e.player]["status"] == "waiting"
    enter_player(e, own)
    first = post(e, payload)
    assert first.status_code == 200, first.text
    assert post(e, payload).json() == first.json()
    part = get(e, sid)["participants"]
    assert part[e.player]["status"] == "active" and part[e.npc]["status"] == "waiting"
    with e.db.read() as c:
        assert not LifeActivityService.running(c, e.npc)
        assert (
            c.execute(
                "SELECT COUNT(*) FROM character_life_activities WHERE character_id=?",
                (e.player,),
            ).fetchone()[0]
            == 1
        )
    assert post(e, {**payload, "actor_id": e.npc}).status_code == 422


def test_get_rows_keeps_partner_independent_activity_private_until_real_reunion(town):
    e = town
    _, own, _, fields = setup(e)
    sid = post(e, body(e, fields)).json()["stay_id"]
    choose(e, "accept", sid, npc=True)
    start = from_iso(fields["arrival_world_time"])
    assert "入口" in npc_perform(e, start - timedelta(minutes=5))
    assert "完整分房休息" in npc_perform(e, start)
    private = get(e, sid)["participants"][e.npc]
    response = rows(e)
    assert response.status_code == 200, response.text
    row = next(item for item in response.json() if item["id"] == sid)
    by_person = {p["character_id"]: p for p in row["participants"]}
    assert row["status"] == "accepted"
    assert by_person[e.player]["status"] == "waiting"
    assert by_person[e.npc]["status"] == "unknown"
    hidden = {"activity_id", "checked_in_at", "ends_at", "finished_at", "reason"}
    assert not hidden.intersection(by_person[e.npc])
    assert private["activity_id"] not in response.text
    enter_player(e, own)
    started = post(
        e,
        body(
            e,
            {
                "operation": "check_in",
                "stay_id": sid,
                "statement": "我在自己的指定房间开始自己的休息。",
            },
        ),
    )
    assert started.status_code == 200, started.text
    response = rows(e)
    row = next(item for item in response.json() if item["id"] == sid)
    by_person = {p["character_id"]: p for p in row["participants"]}
    assert row["status"] == "active" and by_person[e.player]["activity_id"]
    assert by_person[e.player]["checked_in_at"] and by_person[e.player]["ends_at"]
    assert by_person[e.npc]["status"] == "unknown"
    settle(e, start + timedelta(hours=8))
    row = next(item for item in rows(e).json() if item["id"] == sid)
    assert get(e, sid)["status"] == "completed"
    assert row["status"] == "accepted" and row["reunion_status"] == "waiting"
    assert (
        next(p for p in row["participants"] if p["character_id"] == e.player)["status"]
        == "completed"
    )
    assert next(p for p in row["participants"] if p["character_id"] == e.npc)["status"] == "unknown"


def test_legacy_receipt_without_new_null_field_accepts_explicit_null(town):
    e = town
    text = "我邀请你在这里短休五分钟。"
    payload = NaturalSocialChoice(
        request_id=str(uuid4()),
        recipient_id=e.npc,
        intent=text,
        evidence=text,
        shared_life_choice=SharedLifeIntent(
            operation="invite", kind="rest", minutes=5, statement=text
        ),
    ).model_dump(mode="json")
    assert payload.pop("separate_stay_choice") is None
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    saved = {"status": "pending", "event_id": "old-receipt", "summary": "此前选择已结算。"}
    with e.db.write() as c:
        c.execute(
            "INSERT INTO social_choice_receipts VALUES (?,?,?,?,?)",
            (e.wid, payload["request_id"], e.player, digest, json.dumps(saved)),
        )
    assert post(e, {**payload, "separate_stay_choice": None}).json() == saved
    assert post(e, payload).json() == saved
    with e.db.read() as c:
        assert not c.execute("SELECT 1 FROM shared_life_moments").fetchone()
        assert not c.execute("SELECT 1 FROM journey_separate_stays").fetchone()


def test_unknown_or_foreign_npc_cannot_enumerate_options(town):
    e = town
    stranger = third_person(e)
    with e.db.write() as c:
        c.execute(
            "DELETE FROM character_acquaintances WHERE observer_id=? AND subject_id=?",
            (e.player, stranger),
        )
        c.execute(
            "DELETE FROM character_contacts WHERE (requester_id=? AND recipient_id=?) "
            "OR (requester_id=? AND recipient_id=?)",
            (e.player, stranger, stranger, e.player),
        )
        c.execute(
            "UPDATE characters SET longitude=?,current_room_id=NULL WHERE id=?",
            (e.loc["longitude"] + 5, stranger),
        )
    foreign = e.client.post("/api/worlds", json={"name": "别的隔离世界"}).json()["characters"][0][
        "id"
    ]
    for cid in (stranger, foreign, str(uuid4()), e.player):
        response = e.client.get(f"/api/worlds/{e.wid}/separate-stays/options/{cid}")
        assert response.status_code == 404, response.text
        assert "room_options" not in response.text
    for cid in (foreign, str(uuid4()), e.player):
        assert rows(e, cid).status_code == 404


def test_room_options_only_expose_player_access_and_do_not_disclose_partner_permissions(town):
    e = town
    _, own, other, _ = setup(e)
    with e.db.read() as c:
        before = c.execute("SELECT COUNT(*) FROM life_room_access").fetchone()[0]
    response = e.client.get(f"/api/worlds/{e.wid}/separate-stays/options/{e.npc}")
    assert response.status_code == 200, response.text
    found = {item["id"]: item for item in response.json()["room_options"]}
    assert own in found and other in found
    assert found[own]["own_access"] is True
    assert found[other]["own_access"] is False
    for option in found.values():
        assert set(option) == {"id", "name", "location_id", "location_name", "own_access"}
        assert "other_access" not in option
    with e.db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM life_room_access").fetchone()[0] == before
        assert not c.execute("SELECT 1 FROM journey_separate_stays").fetchone()


@pytest.mark.parametrize(
    "extra", [{"allow_visit": True}, {"minutes": 240}, {"own_room_id": "changed"}]
)
def test_api_accept_cannot_add_reception_or_rewrite_terms(town, extra):
    e = town
    _, own, other, fields = setup(e)
    sid = choose(e, **{**fields, "own_room_id": other, "other_room_id": own}, npc=True)["stay_id"]
    response = post(
        e,
        body(
            e,
            {
                "operation": "accept",
                "stay_id": sid,
                "statement": "我只回应本人收到的这份分房邀请。",
                **extra,
            },
        ),
    )
    assert response.status_code == 422
    assert get(e, sid)["status"] == "pending"
