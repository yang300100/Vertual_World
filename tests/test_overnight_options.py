"""哥哥，住宿选项只补入眼前真实房门，询问不产生接待或进入许可。"""

# ruff: noqa: F811
from test_appointment_rooms import room
from test_lived_world import town  # noqa: F401

from world_engine.interiors import InteriorService


def options(e, character_id=None):
    return e.client.get(
        f"/api/worlds/{e.wid}/overnights/options/{character_id or e.npc}"
    )


def third_person(e):
    with e.db.read() as c:
        return c.execute(
            "SELECT id FROM characters WHERE world_id=? AND id NOT IN (?,?) LIMIT 1",
            (e.wid, e.player, e.npc),
        ).fetchone()[0]


def snapshot(e):
    with e.db.read() as c:
        return {
            "people": [
                tuple(person)
                for person in c.execute(
                    "SELECT id,money,longitude,latitude,current_room_id FROM characters "
                    "WHERE world_id=? ORDER BY id",
                    (e.wid,),
                )
            ],
            "facts": {
                table: c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "life_room_access",
                    "journey_overnight_stays",
                    "character_life_activities",
                    "world_events",
                    "contract_fulfillments",
                )
            },
        }


def test_visible_private_door_can_be_asked_without_granting_access(town):
    e = town
    rid = room(e, name="眼前未获准的客房")
    old = e.client.get(f"/api/worlds/{e.wid}/appointments/options/{e.npc}")
    assert rid not in {item["id"] for item in old.json()["room_options"]}
    before = snapshot(e)
    response = options(e)
    assert response.status_code == 200, response.text
    target = next(item for item in response.json()["room_options"] if item["id"] == rid)
    assert target == {
        "id": rid,
        "name": "眼前未获准的客房",
        "location_id": e.loc["id"],
        "location_name": e.loc["name"],
        "may_host": False,
    }
    assert options(e).json() == response.json()
    assert snapshot(e) == before
    with e.db.read() as c:
        target = InteriorService.room(c, e.wid, rid)
        assert not InteriorService.allowed(c, target, e.player)
        assert not InteriorService.storage_allowed(c, target, e.player)
        assert not InteriorService.can_unlock(c, target, e.player)


def test_options_do_not_reveal_unseen_inner_rooms_or_unpermitted_third_party_rooms(town):
    e = town
    outer = room(e, name="眼前会客门")
    inner = room(e, parent=outer, name="未见的私人内室")
    third = room(e, owner=third_person(e), name="第三人的私人房间")
    response = options(e)
    assert response.status_code == 200, response.text
    found = {item["id"] for item in response.json()["room_options"]}
    assert outer in found and inner not in found and third not in found
    assert "未见的私人内室" not in response.text
    assert "第三人的私人房间" not in response.text


def test_visible_inner_door_still_requires_permission_for_outer_route(town):
    e = town
    outer = room(e, owner=third_person(e), name="外层已撤销许可的前厅")
    inner = room(e, parent=outer, name="眼前仍需外层通路的客房")
    # 哥哥，隔离前置模拟已在屋内但外层许可撤销，看到门不能恢复整条通路。
    with e.db.write() as c:
        c.execute(
            "UPDATE characters SET current_room_id=? WHERE id IN (?,?)",
            (outer, e.player, e.npc),
        )
    with e.db.read() as c:
        player = c.execute("SELECT * FROM characters WHERE id=?", (e.player,)).fetchone()
        assert inner in {door["id"] for door in InteriorService.scene(c, player)["doors"]}
    response = options(e)
    assert response.status_code == 200, response.text
    assert inner not in {item["id"] for item in response.json()["room_options"]}


def test_existing_known_player_room_keeps_separate_host_option(town):
    e = town
    rid = room(e, owner=e.player, name="本人已有接待权的房间")
    response = options(e)
    assert response.status_code == 200, response.text
    matches = [item for item in response.json()["room_options"] if item["id"] == rid]
    assert len(matches) == 1 and matches[0]["may_host"] is True


def test_unknown_unreachable_person_cannot_be_used_to_enumerate_room_options(town):
    e = town
    stranger = third_person(e)
    with e.db.write() as c:
        c.execute(
            "DELETE FROM character_acquaintances WHERE observer_id=? AND subject_id=?",
            (e.player, stranger),
        )
        c.execute(
            "DELETE FROM character_contacts WHERE "
            "(requester_id=? AND recipient_id=?) OR (requester_id=? AND recipient_id=?)",
            (e.player, stranger, stranger, e.player),
        )
        c.execute(
            "UPDATE characters SET longitude=?,latitude=?,current_room_id=NULL WHERE id=?",
            (e.loc["longitude"] + 1, e.loc["latitude"], stranger),
        )
    response = options(e, stranger)
    assert response.status_code == 404
    assert "room_options" not in response.text
    assert options(e, "不存在的人物").status_code == 404

