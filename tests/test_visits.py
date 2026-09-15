# ruff: noqa: F811
"""门口拜访必须守住真实位置、单次许可、钥匙库存与租客边界。"""

from datetime import timedelta
from uuid import uuid4

import pytest
from test_lived_world import definition, lodging, post, town  # noqa: F401

from scripts.prepare_life_content import export_packet, validate_draft
from world_engine.interiors import InteriorError, InteriorService
from world_engine.life import LifeActivityService
from world_engine.repository import to_iso
from world_engine.society import SocietyService
from world_engine.visits import VisitService


def person(c, cid):
    return c.execute("SELECT * FROM characters WHERE id=?", (cid,)).fetchone()


def make_room(e, *, owner=None, policy="authorized_only", physical=False, inside=True):
    owner = owner or e.npc
    post(e, "contacts", {"recipient_id": e.npc})
    key = (
        definition(
            e,
            {
                "element_type": "commodity",
                "name": "拜访验收钥匙",
                "category": "tool",
                "stack_limit": 1,
            },
        )
        if physical
        else None
    )
    pending = post(
        e,
        "interior-layouts",
        {
            "idempotency_key": str(uuid4()),
            "room": {
                "name": "拜访验收房间",
                "location_id": e.loc["id"],
                "owner_character_id": owner,
                "access_policy": "private",
                "visitor_policy": policy,
                "key_item_type_id": key,
                "fixtures": [{"name": "私人柜子", "kind": "container"}],
            },
        },
        201,
    )
    result = post(e, f"registrations/{pending['registration']['id']}/confirm")
    assert result["status"] == "applied", result
    rid = result["result_entity_id"]
    if inside:
        with e.db.write() as c:
            host = person(c, owner)
            InteriorService.door(c, host, rid, "open", e.at)
            InteriorService.door(c, host, rid, "enter", e.at)
            host = person(c, owner)
            InteriorService.door(c, host, rid, "close", e.at)
            InteriorService.door(c, host, rid, "lock", e.at)
    return rid, key


def knock(e, room, request_id=None):
    return post(e, f"player/life/doors/{room}/knock", {"request_id": request_id or str(uuid4())})


def life(e):
    return e.client.get(f"/api/worlds/{e.wid}/player/life").json()["interior"]


def test_untrusted_knock_does_not_grant_permission_or_reveal_host(town):
    e = town
    room, _ = make_room(e)
    knock(e, room)
    result = life(e)
    assert result["visits"][0]["status"] == "declined"
    assert result["visits"][0]["visitor_label"] is None
    assert "host_id" not in result["visits"][0]
    assert result["doors"][0]["locked"]
    assert not result["doors"][0]["allowed"]


def test_trusted_invitation_is_single_use_and_never_opens_storage(town):
    e = town
    room, _ = make_room(e, policy="trusted_contacts")
    with e.db.write() as c:
        SocietyService._relationship(c, e.wid, e.npc, e.player, 20, 30)
    knock(e, room)
    assert life(e)["visits"][0]["status"] == "invited"
    post(e, f"player/life/doors/{room}", {"operation": "enter"})
    inside = life(e)
    assert inside["visits"][0]["status"] == "entered"
    assert inside["fixtures"][0]["usable"] is False
    post(e, f"player/life/doors/{room}", {"operation": "exit"})
    assert not life(e)["doors"][0]["allowed"]
    result = e.client.post(
        f"/api/worlds/{e.wid}/player/life/doors/{room}", json={"operation": "enter"}
    )
    assert result.status_code == 409
    with e.db.read() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM event_causes WHERE relation='visit_response'"
            ).fetchone()[0]
            == 1
        )


def test_same_knock_key_is_not_replayed_and_invitation_expires_after_restart(town):
    e = town
    room, _ = make_room(e)
    with e.db.write() as c:
        c.execute("INSERT INTO life_room_access VALUES (?,?)", (room, e.player))
    key = str(uuid4())
    first = knock(e, room, key)
    e.db.initialize()
    second = knock(e, room, key)
    assert first["visit_id"] == second["visit_id"]
    with e.db.write() as c:
        assert c.execute("SELECT count(*) FROM life_visits").fetchone()[0] == 1
        c.execute("DELETE FROM life_room_access WHERE room_id=?", (room,))
        c.execute(
            "UPDATE worlds SET current_time=? WHERE id=?",
            (to_iso(e.at + timedelta(minutes=16)), e.wid),
        )
    assert life(e)["visits"][0]["status"] == "expired"
    assert not life(e)["doors"][0]["allowed"]


@pytest.mark.parametrize("reason", ["away", "rest", "manual"])
def test_unanswered_knocks_do_not_reveal_private_reasons(town, reason):
    e = town
    room, _ = make_room(
        e, inside=reason != "away", policy="manual" if reason == "manual" else "authorized_only"
    )
    if reason == "rest":
        with e.db.write() as c:
            LifeActivityService.start(c, person(c, e.npc), e.at, "rest", 30)
    knock(e, room)
    view = life(e)["visits"][0]
    assert view["status"] == "pending"
    assert set(view) == {
        "id",
        "room_name",
        "status",
        "expires_world_time",
        "mine",
        "visitor_label",
        "can_respond",
    }


def test_waiting_visitor_can_be_invited_when_host_finishes_rest(town):
    e = town
    room, _ = make_room(e)
    with e.db.write() as c:
        c.execute("INSERT INTO life_room_access VALUES (?,?)", (room, e.player))
        LifeActivityService.start(c, person(c, e.npc), e.at, "rest", 5)
    knock(e, room)
    with e.db.write() as c:
        LifeActivityService.start(c, person(c, e.player), e.at, "wait", 10)
    e.clock.heartbeat(e.wid, elapsed_seconds=300)
    assert life(e)["visits"][0]["status"] == "invited"


def test_player_host_hears_knock_and_can_revoke_single_entry(town):
    e = town
    room, _ = make_room(e, owner=e.player, policy="manual")
    with e.db.write() as c:
        LifeActivityService.start(c, person(c, e.player), e.at, "wait", 30)
        result = VisitService.knock(c, person(c, e.npc), room, str(uuid4()), e.at)
        assert not LifeActivityService.running(c, e.player)
    visit = life(e)["visits"][0]
    assert visit["visitor_label"] == "门外来访者"
    assert visit["can_respond"]
    post(e, f"player/life/visits/{result['visit_id']}/respond", {"accept": True})
    post(e, f"player/life/visits/{result['visit_id']}/withdraw")
    with e.db.read() as c:
        assert not InteriorService.allowed(c, InteriorService.room(c, e.wid, room), e.npc)
    assert life(e)["visits"][0]["status"] == "cancelled"


def test_tenant_handles_visits_and_lease_change_invalidates_invitation(town):
    e = town
    room = lodging(e)
    with e.db.write() as c:
        c.execute("UPDATE life_rooms SET access_policy='private' WHERE id=?", (room,))
        host = person(c, e.player)
        InteriorService.door(c, host, room, "open", e.at)
        InteriorService.door(c, host, room, "enter", e.at)
        guest = c.execute(
            "SELECT * FROM characters WHERE world_id=? AND id NOT IN (?,?) LIMIT 1",
            (e.wid, e.npc, e.player),
        ).fetchone()
        c.execute(
            "UPDATE characters SET longitude=?,latitude=?,current_location_id=? WHERE id=?",
            (e.loc["longitude"], e.loc["latitude"], e.loc["id"], guest["id"]),
        )
        result = VisitService.knock(c, person(c, guest["id"]), room, str(uuid4()), e.at)
        with pytest.raises(InteriorError):
            VisitService.respond(c, person(c, e.npc), result["visit_id"], True, e.at)
        VisitService.respond(c, person(c, e.player), result["visit_id"], True, e.at)
        assert InteriorService.allowed(c, InteriorService.room(c, e.wid, room), guest["id"])
        c.execute(
            "UPDATE contract_fulfillments SET due_world_time=? WHERE asset_id=?",
            (to_iso(e.at), room),
        )
        assert not InteriorService.allowed(c, InteriorService.room(c, e.wid, room), guest["id"])


@pytest.mark.parametrize("state", ["missing", "broken", "stored", "other_owner", "valid"])
def test_physical_key_requires_usable_owned_inventory(town, state):
    e = town
    room, key = make_room(e, owner=e.player, physical=True, inside=False)
    with e.db.write() as c:
        c.execute("UPDATE life_rooms SET door_locked=1 WHERE id=?", (room,))
        if state != "missing":
            c.execute(
                "INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,quantity,condition,owner_character_id) VALUES (?,?,?,?,?,1,?,?)",
                (
                    str(uuid4()),
                    e.wid,
                    key,
                    "fixture_storage" if state == "stored" else "character_inventory",
                    e.player,
                    0 if state == "broken" else 100,
                    e.npc if state == "other_owner" else e.player,
                ),
            )
    response = e.client.post(
        f"/api/worlds/{e.wid}/player/life/doors/{room}", json={"operation": "unlock"}
    )
    assert response.status_code == (200 if state == "valid" else 409)


def test_key_without_permission_is_not_permission_and_inside_escape_still_works(town):
    e = town
    room, key = make_room(e, physical=True)
    with e.db.write() as c:
        c.execute(
            "INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,quantity,condition,owner_character_id) VALUES (?,?,?,'character_inventory',?,1,100,?)",
            (str(uuid4()), e.wid, key, e.player, e.player),
        )
        assert not InteriorService.can_unlock(c, InteriorService.room(c, e.wid, room), e.player)
        host = person(c, e.npc)
        InteriorService.door(c, host, room, "unlock", e.at)
        InteriorService.door(c, host, room, "open", e.at)
        InteriorService.door(c, host, room, "exit", e.at)


def test_distant_knock_and_cross_room_idempotency_reuse_are_rejected(town):
    e = town
    room, _ = make_room(e, policy="manual")
    key = str(uuid4())
    knock(e, room, key)
    response = e.client.post(
        f"/api/worlds/{e.wid}/player/life/doors/another-room/knock", json={"request_id": key}
    )
    assert response.status_code == 409
    with e.db.write() as c:
        c.execute("UPDATE characters SET longitude=longitude+1 WHERE id=?", (e.player,))
    response = e.client.post(
        f"/api/worlds/{e.wid}/player/life/doors/{room}/knock", json={"request_id": str(uuid4())}
    )
    assert response.status_code == 409


def test_content_packet_preserves_door_policy_and_rejects_non_key(town, tmp_path):
    e = town
    rid, key = make_room(e, physical=True, policy="manual")
    packet = export_packet(e.settings.database_path, e.wid, [e.loc["id"]], tmp_path / "packet")
    room = next(r for r in packet["rooms"] if r["id"] == rid)
    assert room["key_item_type_id"] == key
    assert room["visitor_policy"] == "manual"
    draft = {
        "format_version": 1,
        "world_id": e.wid,
        "entries": [
            {
                "key": "bad-key",
                "source_note": "隔离结构检查",
                "payload": {
                    "element_type": "interior_room",
                    "name": "钥匙错误的房间",
                    "location_id": e.loc["id"],
                    "owner_character_id": e.npc,
                    "key_item_type_id": e.food,
                },
            }
        ],
    }
    with pytest.raises(ValueError, match="实体钥匙"):
        validate_draft(draft, packet)
