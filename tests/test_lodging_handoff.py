"""哥哥，订房后的分房邀请保留真实来源，但双方同意和门路许可仍独立核验。"""

# ruff: noqa: F811
import json
import sqlite3
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_journey_room_appointments import current_time
from test_lived_world import town  # noqa: F401
from test_lodging_plans import accept as accept_plan
from test_lodging_plans import book, raw, setup, wallets
from test_lodging_plans import choose as plan_choice
from test_separate_stays import choose, get

from world_engine.database import Database
from world_engine.repository import from_iso, to_iso
from world_engine.separate_stays import SeparateStayIntent, SeparateStayService


def prepared(e):
    jid, own, other, fields = setup(e)
    pid = accept_plan(e, fields)
    book(e, pid)
    book(e, pid, npc=True)
    plan_choice(e, "report_ready", pid)
    assert plan_choice(e, "report_ready", pid, npc=True)["status"] == "completed"
    return pid, jid, own, other


def proposal(e, pid, jid, own, other, **changes):
    return {
        "operation": "propose",
        "source_lodging_plan_id": pid,
        "journey_id": jid,
        "own_room_id": own,
        "other_room_id": other,
        "arrival_world_time": to_iso(current_time(e) + timedelta(minutes=30)),
        "minutes": 480,
        "window_minutes": 60,
        "reunion_window_minutes": 60,
        "statement": "订房已共同核对，我另行提议各住已订的本人房间，按这次明确时间休息并会合。",
        **changes,
    }


def leases(e):
    with e.db.read() as c:
        return [
            dict(r) for r in c.execute("SELECT * FROM contract_fulfillments ORDER BY request_id")
        ]


def assert_no_stay(e):
    with e.db.read() as c:
        assert not c.execute("SELECT 1 FROM journey_separate_stays").fetchone()


def test_completed_booking_needs_a_new_explicit_invitation_and_independent_acceptance(town):
    e = town
    pid, jid, own, other = prepared(e)
    assert_no_stay(e)
    before_money, before_leases = wallets(e), leases(e)
    result = choose(e, **proposal(e, pid, jid, own, other))
    sid = result["stay_id"]
    assert get(e, sid)["status"] == "pending"
    assert get(e, sid)["source_lodging_plan_id"] == pid
    with e.db.read() as c:
        event = c.execute(
            "SELECT payload_json FROM world_events WHERE id=?", (result["event_id"],)
        ).fetchone()[0]
        payload = json.loads(event)
        assert payload["source_lodging_plan_id"] == pid
        assert payload["source_lodging_plan_completed_event_id"] == raw(e, pid)["source_event_id"]
        public = SeparateStayService.rows(c, e.wid, e.player, current_time(e), e.npc)[0]
        context = SeparateStayService.context(c, e.wid, e.npc, e.player, current_time(e))[0]
        for row in (public, context):
            assert row["source_lodging_plan_id"] == pid
            assert row["source_lodging_plan"] == {
                "id": pid,
                "required_until_at": raw(e, pid)["required_until_at"],
                "completion_event_id": raw(e, pid)["source_event_id"],
            }
            assert not {"spent", "quote_id", "private_reason"} & row["source_lodging_plan"].keys()
        assert not c.execute(
            "SELECT 1 FROM character_life_activities WHERE status='running' "
            "AND character_id IN (?,?)",
            (e.player, e.npc),
        ).fetchone()
    choose(e, "accept", sid, npc=True)
    assert get(e, sid)["status"] == "accepted"
    assert wallets(e) == before_money and leases(e) == before_leases


def test_current_proposer_owns_own_room_even_when_original_plan_was_reversed(town):
    e = town
    pid, jid, own, other = prepared(e)
    sid = choose(e, npc=True, **proposal(e, pid, jid, other, own))["stay_id"]
    row = get(e, sid)
    assert row["proposer_id"] == e.npc and row["own_room_id"] == other
    choose(e, "accept", sid)
    assert get(e, sid)["status"] == "accepted"


def test_source_cannot_swap_the_original_personal_rooms(town):
    e = town
    pid, jid, own, other = prepared(e)
    with pytest.raises(ValueError, match="原房间"):
        choose(e, **proposal(e, pid, jid, other, own))
    assert_no_stay(e)


@pytest.mark.parametrize("operation", ["accept", "decline", "cancel", "check_in"])
def test_source_is_only_allowed_on_a_fully_explicit_new_proposal(operation):
    with pytest.raises(ValidationError):
        SeparateStayIntent(
            operation=operation,
            stay_id="stay",
            source_lodging_plan_id="source",
            statement="我明确选择本人操作。",
        )


@pytest.mark.parametrize(
    "missing", ["journey_id", "own_room_id", "other_room_id", "arrival_world_time"]
)
def test_source_never_replaces_explicit_terms(missing):
    fields = dict(
        operation="propose",
        source_lodging_plan_id="source",
        journey_id="journey",
        own_room_id="own",
        other_room_id="other",
        arrival_world_time="2026-01-01T00:00:00+00:00",
        statement="我另行提出分房安排。",
    )
    fields.pop(missing)
    with pytest.raises(ValidationError):
        SeparateStayIntent(**fields)


@pytest.mark.parametrize(
    "corruption", ["uncompleted", "wrong_event", "wrong_payload", "wrong_actor", "missing_report"]
)
def test_status_alone_cannot_fabricate_a_completed_shared_source(town, corruption):
    e = town
    pid, jid, own, other = prepared(e)
    source = raw(e, pid)
    with e.db.write() as c:
        if corruption == "uncompleted":
            c.execute("UPDATE journey_lodging_plans SET status='accepted' WHERE id=?", (pid,))
        elif corruption == "wrong_event":
            c.execute(
                "UPDATE journey_lodging_plans SET source_event_id=pause_event_id WHERE id=?", (pid,)
            )
        elif corruption == "wrong_payload":
            c.execute(
                "UPDATE world_events SET payload_json=? WHERE id=?",
                (json.dumps({"plan_id": "another-plan"}), source["source_event_id"]),
            )
        elif corruption == "wrong_actor":
            c.execute(
                "UPDATE world_events SET actor_id=? WHERE id=?",
                (e.owner, source["source_event_id"]),
            )
        else:
            c.execute(
                "UPDATE journey_lodging_roles SET report_event_id=NULL "
                "WHERE plan_id=? AND character_id=?",
                (pid, e.npc),
            )
    with pytest.raises(ValueError, match="真实|回执"):
        choose(e, **proposal(e, pid, jid, own, other))
    assert_no_stay(e)


@pytest.mark.parametrize("field", ["arrival_event_id", "pause_event_id"])
def test_source_requires_the_same_arrival_and_pause_evidence(town, field):
    e = town
    pid, jid, own, other = prepared(e)
    with e.db.write() as c:
        c.execute(f"UPDATE journey_lodging_plans SET {field}=source_event_id WHERE id=?", (pid,))
    with pytest.raises(ValueError, match="暂停记录已变化"):
        choose(e, **proposal(e, pid, jid, own, other))
    assert_no_stay(e)


def test_shared_rooms_remain_known_after_leaving_their_visible_door(town, monkeypatch):
    e = town
    pid, _, own, other = prepared(e)
    from world_engine.interiors import InteriorService

    monkeypatch.setattr(InteriorService, "scene", staticmethod(lambda c, actor: {"doors": []}))
    with e.db.read() as c:
        known = {
            room["id"]
            for room in SeparateStayService.room_options(c, e.wid, e.player, e.npc, current_time(e))
        }
        assert {own, other} <= known
        assert raw(e, pid)["status"] == "completed"


def test_current_shared_rooms_survive_thirty_two_old_known_rooms_without_revealing_unknowns(
    town, monkeypatch
):
    e = town
    pid, jid, own, other = prepared(e)
    from world_engine.interiors import InteriorService
    from world_engine.lodging_plans import LodgingPlanService

    monkeypatch.setattr(InteriorService, "scene", staticmethod(lambda c, actor: {"doors": []}))
    with e.db.write() as c:
        template = dict(c.execute("SELECT * FROM life_rooms WHERE id=?", (own,)).fetchone())
        for index in range(36):
            copied = {
                **template,
                "id": f"-known-{index:03d}",
                "name": f"本人已知的旧客房{index}",
                "owner_character_id": e.player,
            }
            c.execute(
                "INSERT INTO life_rooms("
                + ",".join(copied)
                + ") VALUES ("
                + ",".join("?" for _ in copied)
                + ")",
                tuple(copied.values()),
            )
        hidden = {
            **template,
            "id": "-unknown-private-room",
            "name": "未曾认识的第三方客房",
            "owner_character_id": e.owner,
        }
        c.execute(
            "INSERT INTO life_rooms("
            + ",".join(hidden)
            + ") VALUES ("
            + ",".join("?" for _ in hidden)
            + ")",
            tuple(hidden.values()),
        )
    with e.db.read() as c:
        sources = LodgingPlanService.handoff_sources(c, e.wid, e.player, e.npc, current_time(e))
        assert sources[0]["id"] == pid
        options = SeparateStayService.room_options(c, e.wid, e.player, e.npc, current_time(e))
        assert len(options) == 32
        assert {own, other} <= {room["id"] for room in options[:2]}
        assert hidden["id"] not in {room["id"] for room in options}
    # 哥哥，真正提出新邀请也要过同一候选门禁，不能只让界面看得到两房。
    sid = choose(e, **proposal(e, pid, jid, own, other))["stay_id"]
    assert get(e, sid)["status"] == "pending"


def test_original_permission_endpoint_covers_rest_but_not_outdoor_reunion(town):
    e = town
    pid, jid, own, other = prepared(e)
    end = from_iso(raw(e, pid)["required_until_at"])
    fields = proposal(e, pid, jid, own, other, arrival_world_time=to_iso(end - timedelta(hours=9)))
    sid = choose(e, **fields)["stay_id"]
    row = get(e, sid)
    assert from_iso(row["latest_end_at"]) == end
    assert from_iso(row["reunion_ends_at"]) > end
    choose(e, "accept", sid, npc=True)


def test_rest_cannot_extend_the_original_preparation_permission_even_if_lease_is_longer(town):
    e = town
    pid, jid, own, other = prepared(e)
    before = leases(e)
    end = from_iso(raw(e, pid)["required_until_at"])
    fields = proposal(
        e,
        pid,
        jid,
        own,
        other,
        arrival_world_time=to_iso(end - timedelta(hours=9) + timedelta(minutes=1)),
    )
    with pytest.raises(ValueError, match="许可终点"):
        choose(e, **fields)
    assert_no_stay(e)
    assert leases(e) == before


@pytest.mark.parametrize("change", ["lease", "key", "pause"])
def test_acceptance_rechecks_current_permissions_keys_and_original_pause(town, change):
    e = town
    pid, jid, own, other = prepared(e)
    sid = choose(e, **proposal(e, pid, jid, own, other))["stay_id"]
    before_money = wallets(e)
    with e.db.write() as c:
        if change == "lease":
            c.execute(
                "UPDATE contract_fulfillments SET due_world_time=? WHERE asset_id=?",
                (to_iso(current_time(e) + timedelta(hours=1)), other),
            )
        elif change == "key":
            key = str(uuid4())
            c.execute(
                "INSERT INTO item_types(id,name,category,stack_limit) VALUES (?,?,'tool',1)",
                (key, "隔离测试的新门钥匙"),
            )
            c.execute(
                "UPDATE life_door_policies SET key_item_type_id=? WHERE room_id=?", (key, other)
            )
            c.execute("UPDATE life_rooms SET door_locked=1 WHERE id=?", (other,))
        else:
            c.execute(
                "UPDATE joint_journeys SET source_event_id=? WHERE id=?",
                (raw(e, pid)["source_event_id"], jid),
            )
    before_leases = leases(e)
    with pytest.raises(ValueError, match="租约|许可|钥匙|暂停记录"):
        choose(e, "accept", sid, npc=True)
    assert get(e, sid)["status"] == "pending"
    assert wallets(e) == before_money and leases(e) == before_leases
    with e.db.read() as c:
        assert not c.execute(
            "SELECT 1 FROM world_events "
            "WHERE event_type='social.appointment_separate_stay_accepted'"
        ).fetchone()


def test_cancelled_source_handoff_can_be_explicitly_proposed_again(town):
    e = town
    pid, jid, own, other = prepared(e)
    first = choose(e, **proposal(e, pid, jid, own, other))["stay_id"]
    choose(e, "cancel", first)
    second = choose(e, **proposal(e, pid, jid, own, other))["stay_id"]
    assert second != first and get(e, second)["source_lodging_plan_id"] == pid


def test_live_source_handoff_does_not_create_a_duplicate(town):
    e = town
    pid, jid, own, other = prepared(e)
    first = choose(e, **proposal(e, pid, jid, own, other))["stay_id"]
    with pytest.raises(ValueError, match="邀请|冲突|处理|完成分房"):
        choose(e, **proposal(e, pid, jid, own, other))
    with e.db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM journey_separate_stays").fetchone()[0] == 1
    choose(e, "accept", first, npc=True)
    with pytest.raises(ValueError, match="冲突|处理|完成分房"):
        choose(e, **proposal(e, pid, jid, own, other))


@pytest.mark.parametrize("change", ["missing", "participants", "location"])
def test_source_cannot_reference_an_unrelated_plan_or_location(town, change):
    e = town
    pid, jid, own, other = prepared(e)
    with e.db.write() as c:
        if change == "participants":
            c.execute("UPDATE journey_lodging_plans SET proposer_id=? WHERE id=?", (e.owner, pid))
        elif change == "location":
            c.execute(
                "UPDATE journey_lodging_plans SET location_id=? WHERE id=?", (e.loc["id"], pid)
            )
    with pytest.raises(ValueError, match="真实完成|暂停记录"):
        choose(e, **proposal(e, "missing" if change == "missing" else pid, jid, own, other))
    assert_no_stay(e)


def test_proposal_requires_real_key_even_with_a_valid_lease(town):
    e = town
    pid, jid, own, other = prepared(e)
    key = str(uuid4())
    with e.db.write() as c:
        c.execute(
            "INSERT INTO item_types(id,name,category,stack_limit) VALUES (?,?,'tool',1)",
            (key, "隔离测试的真实客房钥匙"),
        )
        c.execute("UPDATE life_door_policies SET key_item_type_id=? WHERE room_id=?", (key, own))
        c.execute("UPDATE life_rooms SET door_locked=1 WHERE id=?", (own,))
    before = leases(e)
    with pytest.raises(ValueError, match="真实钥匙"):
        choose(e, **proposal(e, pid, jid, own, other))
    with e.db.write() as c:
        c.execute(
            "INSERT INTO item_instances(id,world_id,item_type_id,container_id,"
            "container_type,quantity,condition,owner_character_id) "
            "VALUES (?,?,?,?,'character_inventory',1,100,?)",
            (str(uuid4()), e.wid, key, e.player, e.player),
        )
    sid = choose(e, **proposal(e, pid, jid, own, other))["stay_id"]
    choose(e, "accept", sid, npc=True)
    assert leases(e) == before


def test_failed_proposal_rolls_back_source_audit_even_if_outer_transaction_continues(
    town, monkeypatch
):
    e = town
    pid, jid, own, other = prepared(e)
    from test_appointments import people

    original = SeparateStayService._record

    def fail(c, row, at, state, summary, cid=None):
        original(c, row, at, state, summary, cid)
        raise RuntimeError("隔离测试：写回执后模拟故障")

    monkeypatch.setattr(SeparateStayService, "_record", staticmethod(fail))
    with e.db.write() as c:
        player, npc = people(e, c)
        before = c.execute("SELECT COUNT(*) FROM world_events").fetchone()[0]
        with pytest.raises(RuntimeError):
            SeparateStayService.apply(
                c,
                player,
                npc,
                current_time(e),
                SeparateStayIntent(**proposal(e, pid, jid, own, other)),
            )
        assert c.execute("SELECT COUNT(*) FROM world_events").fetchone()[0] == before
        assert not c.execute("SELECT 1 FROM journey_separate_stays").fetchone()


def test_legacy_initialization_adds_nullable_source_once_and_preserves_history(town):
    e = town
    pid, jid, own, other = prepared(e)
    fields = proposal(e, pid, jid, own, other)
    fields.pop("source_lodging_plan_id")
    sid = choose(e, **fields)["stay_id"]
    before = get(e, sid)
    with e.db.write() as c:
        c.execute("DROP INDEX idx_separate_stay_lodging_source")
        c.execute("ALTER TABLE journey_separate_stays DROP COLUMN source_lodging_plan_id")
    restarted = Database(e.settings.database_path)
    restarted.initialize()
    restarted.initialize()
    after = get(e, sid)
    assert after == before and after["source_lodging_plan_id"] is None
    with restarted.read() as c:
        foreign = [dict(r) for r in c.execute("PRAGMA foreign_key_list(journey_separate_stays)")]
        assert any(
            r["from"] == "source_lodging_plan_id" and r["table"] == "journey_lodging_plans"
            for r in foreign
        )
        assert (
            len(
                [
                    r
                    for r in c.execute("PRAGMA table_info(journey_separate_stays)")
                    if r["name"] == "source_lodging_plan_id"
                ]
            )
            == 1
        )
    with pytest.raises(sqlite3.IntegrityError):
        with restarted.write() as c:
            c.execute(
                "UPDATE journey_separate_stays SET source_lodging_plan_id='missing' WHERE id=?",
                (sid,),
            )
