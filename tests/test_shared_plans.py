# ruff: noqa: F811
"""哥哥，共同分工须独立同意，劳动、预算、物品和到场都来自真实结算。"""

import hashlib
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from test_appointment_rooms import room
from test_appointments import people, set_time
from test_lived_world import town  # noqa: F401

from world_engine.actions import ActionService
from world_engine.activity_tasks import TaskService
from world_engine.intimacy import IntimacyService, RelationshipIntent
from world_engine.life import LifeActivityService
from world_engine.repository import to_iso
from world_engine.shared_plans import SharedPlanIntent, SharedPlanService


def choose(e, operation, pid=None, **extra):
    text = extra.pop("statement", "我明确表达自己对这份筹备分工的选择。")
    return e.client.post(
        f"/api/worlds/{e.wid}/player/social-choices",
        json={
            "request_id": str(uuid4()),
            "recipient_id": e.npc,
            "intent": text,
            "evidence": text,
            "shared_plan_choice": {
                "operation": operation,
                "plan_id": pid,
                "statement": text,
                **extra,
            },
        },
    )


def offer(e, own=None, other=None, **changes):
    with e.db.write() as c:
        player, npc = people(e, c)
        return SharedPlanService.apply(
            c,
            npc,
            player,
            e.at,
            SharedPlanIntent(
                operation="propose",
                title="周末出行筹备",
                location_id=e.loc["id"],
                deadline_world_time=to_iso(e.at + timedelta(days=3)),
                own_task=own or {"kind": "earn_money", "target": 12},
                other_task=other or {"kind": "prepare_food", "target": 1},
                statement="我负责劳动筹十二枚，你愿意准备一份食物，之后到这里一起核对吗？",
                **changes,
            ),
        )["plan_id"]


def get(e, pid, viewer=None):
    with e.db.read() as c:
        return next(
            p for p in SharedPlanService.rows(c, e.wid, viewer or e.player, e.at) if p["id"] == pid
        )


def player_food(e, quantity=1):
    with e.db.write() as c:
        iid = str(uuid4())
        c.execute(
            "INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,owner_character_id,quantity,condition) VALUES (?,?,?,'character_inventory',?,?,?,100)",
            (iid, e.wid, e.food, e.player, e.player, quantity),
        )
        return iid


def perform(e, at=None):
    at = at or e.at
    with e.db.write() as c:
        npc = people(e, c)[1]
        places = c.execute(
            "SELECT * FROM locations WHERE world_id=? AND is_active=1", (e.wid,)
        ).fetchall()
        return SharedPlanService.perform(c, npc, at, places, None, None)


def test_proposal_and_acceptance_do_not_fabricate_work_or_other_consent(town):
    e = town
    pid = offer(e)
    with e.db.write() as c:
        player, npc = people(e, c)
        with pytest.raises(ValueError, match="自己的分工"):
            SharedPlanService.apply(
                c,
                npc,
                player,
                e.at,
                SharedPlanIntent(operation="accept", plan_id=pid, statement="我同意。"),
            )
    assert perform(e) is None and get(e, pid)["status"] == "pending"
    assert choose(e, "accept", pid).status_code == 200
    assert get(e, pid)["status"] == "active" and all(
        r["ready_at"] is None for r in get(e, pid)["roles"]
    )
    assert choose(e, "accept", pid).status_code == 409


def test_existing_savings_and_fake_work_event_are_not_new_labor(town):
    e = town
    pid = offer(e)
    choose(e, "accept", pid)
    with e.db.write() as c:
        player, npc = people(e, c)
        ActionService._record_event(
            c,
            world_id=e.wid,
            tick_id=str(uuid4()),
            occurred_at=e.at,
            event_type="action.work",
            actor_id=e.npc,
            target_id=None,
            location_id=e.loc["id"],
            summary="一条没有真实活动结算的记录",
            payload={"work_completed": True, "result": {"completed": True, "payment": 500}},
        )
        with pytest.raises(ValueError, match="真正完成劳动"):
            SharedPlanService.apply(
                c,
                npc,
                player,
                e.at,
                SharedPlanIntent(operation="check_in", plan_id=pid, statement="我已准备好了。"),
            )
    mine = next(r for r in get(e, pid, e.npc)["roles"] if r["character_id"] == e.npc)
    assert mine["progress"] == 0


def test_real_work_plus_owned_food_and_both_checkins_complete_only_preparation(town):
    e = town
    pid = offer(e)
    choose(e, "accept", pid)
    iid = player_food(e)
    assert choose(e, "check_in", pid).status_code == 200
    assert "工作" in perform(e)
    with e.db.read() as c:
        activity = LifeActivityService.running(c, e.npc)
        assert activity and activity["kind"] == "work"
        assert c.execute("SELECT money FROM characters WHERE id=?", (e.npc,)).fetchone()[0] == 200
        assert (
            c.execute(
                "SELECT COUNT(*) FROM preparation_work_receipts WHERE plan_id=?", (pid,)
            ).fetchone()[0]
            == 0
        )
    e.clock.heartbeat(e.wid, elapsed_seconds=3600)
    result = get(e, pid)
    assert result["status"] == "completed"
    with e.db.read() as c:
        assert (
            c.execute(
                "SELECT payment FROM preparation_work_receipts WHERE plan_id=?", (pid,)
            ).fetchone()[0]
            == 12
        )
        assert (
            c.execute("SELECT money FROM characters WHERE id=?", (e.player,)).fetchone()[0] == 200
        )
        food = c.execute(
            "SELECT owner_character_id,container_id,quantity FROM item_instances WHERE id=?", (iid,)
        ).fetchone()
        assert tuple(food) == (e.player, e.player, 1)
        assert not c.execute(
            "SELECT 1 FROM shared_life_moments WHERE world_id=?", (e.wid,)
        ).fetchone()


def test_work_started_before_acceptance_at_same_world_time_does_not_count(town):
    e = town
    with e.db.write() as c:
        player, npc = people(e, c)
        TaskService.start(c, npc, e.at, None)
    pid = offer(e)
    choose(e, "accept", pid)
    e.clock.heartbeat(e.wid, elapsed_seconds=3600)
    with e.db.read() as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM preparation_work_receipts WHERE plan_id=?", (pid,)
            ).fetchone()[0]
            == 0
        )


def test_interrupted_work_cannot_satisfy_earnings(town):
    e = town
    pid = offer(e)
    choose(e, "accept", pid)
    perform(e)
    with e.db.write() as c:
        row = LifeActivityService.running(c, e.npc)
        LifeActivityService.finish(
            c, row, e.at + timedelta(minutes=15), "interrupted", "我决定先休息。"
        )
        SharedPlanService.maintain(c, e.wid, e.at + timedelta(minutes=15))
        assert (
            c.execute(
                "SELECT COUNT(*) FROM preparation_work_receipts WHERE plan_id=?", (pid,)
            ).fetchone()[0]
            == 0
        )
    assert get(e, pid)["status"] == "active"


def test_food_spoilage_or_other_owner_cannot_satisfy_player_role(town):
    e = town
    pid = offer(e)
    choose(e, "accept", pid)
    iid = player_food(e)
    with e.db.write() as c:
        c.execute(
            "UPDATE item_instances SET spoils_world_time=? WHERE id=?",
            (to_iso(e.at - timedelta(minutes=1)), iid),
        )
    assert choose(e, "check_in", pid).status_code == 409
    with e.db.write() as c:
        c.execute(
            "UPDATE item_instances SET spoils_world_time=NULL,owner_character_id=? WHERE id=?",
            (e.npc, iid),
        )
    assert choose(e, "check_in", pid).status_code == 409


def test_peer_private_progress_and_spending_are_not_exposed(town):
    e = town
    pid = offer(e)
    choose(e, "accept", pid)
    with e.db.write() as c:
        c.execute(
            "UPDATE shared_preparation_roles SET progress=8,spent=0,private_reason='我暂时不想说的个人顾虑' WHERE plan_id=? AND character_id=?",
            (pid, e.npc),
        )
    peer = next(r for r in get(e, pid)["roles"] if r["character_id"] == e.npc)
    own = next(r for r in get(e, pid, e.npc)["roles"] if r["character_id"] == e.npc)
    assert peer["progress"] is None and peer["spent"] is None and peer["reason"] == ""
    assert own["progress"] == 8 and own["reason"] == "我暂时不想说的个人顾虑"


def test_food_purchase_uses_only_own_agreed_budget(town):
    e = town
    pid = offer(
        e,
        own={"kind": "prepare_food", "target": 2, "spending_limit": 3},
        other={"kind": "prepare_food", "target": 1},
    )
    choose(e, "accept", pid)
    with e.db.write() as c:
        seller = c.execute(
            "SELECT id FROM characters WHERE world_id=? AND id NOT IN (?,?) LIMIT 1",
            (e.wid, e.player, e.npc),
        ).fetchone()[0]
        c.execute(
            "UPDATE item_instances SET owner_character_id=?,container_id=? WHERE world_id=? AND item_type_id=? AND container_id=?",
            (seller, seller, e.wid, e.food, e.npc),
        )
        c.execute(
            "UPDATE characters SET identity='商人',longitude=?,latitude=?,current_location_id=? WHERE id=?",
            (e.loc["longitude"], e.loc["latitude"], e.loc["id"], seller),
        )
        c.execute(
            "UPDATE world_item_profiles SET resource_owner_id=? WHERE item_type_id=?",
            (seller, e.food),
        )
    assert "买到了" in perform(e)
    perform(e, e.at + timedelta(minutes=15))
    with e.db.read() as c:
        role = c.execute(
            "SELECT spent FROM shared_preparation_roles WHERE plan_id=? AND character_id=?",
            (pid, e.npc),
        ).fetchone()[0]
        assert role == 3
        assert c.execute("SELECT money FROM characters WHERE id=?", (e.npc,)).fetchone()[0] == 197
        assert (
            c.execute("SELECT money FROM characters WHERE id=?", (e.player,)).fetchone()[0] == 200
        )


def test_both_must_actually_meet_in_target_scene_to_finish(town):
    e = town
    pid = offer(
        e, own={"kind": "prepare_food", "target": 1}, other={"kind": "prepare_food", "target": 1}
    )
    choose(e, "accept", pid)
    player_food(e)
    with e.db.write() as c:
        c.execute("UPDATE characters SET longitude=longitude+0.0005 WHERE id=?", (e.npc,))
    assert choose(e, "check_in", pid).status_code == 200
    with e.db.write() as c:
        player, npc = people(e, c)
        SharedPlanService.apply(
            c,
            npc,
            player,
            e.at,
            SharedPlanIntent(operation="check_in", plan_id=pid, statement="我的食物已核对。"),
        )
    assert get(e, pid)["status"] == "active"
    with e.db.write() as c:
        c.execute("UPDATE characters SET longitude=? WHERE id=?", (e.loc["longitude"], e.npc))
        SharedPlanService.maintain(c, e.wid, e.at)
    assert get(e, pid)["status"] == "completed"


def test_cancellation_retains_real_wages_and_items_without_forcing_player_work(town):
    e = town
    pid = offer(e)
    choose(e, "accept", pid)
    perform(e)
    assert choose(e, "end", pid).status_code == 200
    e.clock.heartbeat(e.wid, elapsed_seconds=3600)
    with e.db.read() as c:
        assert c.execute("SELECT money FROM characters WHERE id=?", (e.npc,)).fetchone()[0] >= 212
        assert not c.execute(
            "SELECT 1 FROM preparation_work_receipts WHERE plan_id=?", (pid,)
        ).fetchone()
        assert not c.execute(
            "SELECT 1 FROM character_life_activities WHERE character_id=?", (e.player,)
        ).fetchone()
    assert get(e, pid)["status"] == "ended"


def test_boundary_prevents_autonomous_shared_preparation(town):
    e = town
    pid = offer(e)
    choose(e, "accept", pid)
    with e.db.write() as c:
        player, npc = people(e, c)
        IntimacyService.apply(
            c,
            player,
            npc,
            e.at,
            RelationshipIntent(
                operation="boundary", pause_contact=True, statement="我需要先独处。"
            ),
        )
    assert perform(e) is None
    assert choose(e, "check_in", pid).status_code == 409
    assert choose(e, "end", pid).status_code == 200


def test_private_room_requires_existing_permissions_and_real_entry(town):
    e = town
    rid = room(e)
    with pytest.raises(ValueError, match="进入许可"):
        offer(e, room_id=rid)
    assert e.client.get(f"/api/worlds/{e.wid}/shared-plans").json() == []


def test_shared_choices_are_idempotent_and_survive_old_payload_hashes(town):
    from world_engine.api_intimacy import NaturalSocialChoice

    e = town
    pid = offer(e)
    text = "我同意自己负责这份筹备。"
    body = {
        "request_id": str(uuid4()),
        "recipient_id": e.npc,
        "intent": text,
        "evidence": text,
        "shared_plan_choice": {"operation": "accept", "plan_id": pid, "statement": text},
    }
    url = f"/api/worlds/{e.wid}/player/social-choices"
    first = e.client.post(url, json=body)
    assert first.status_code == 200, first.text
    assert e.client.post(url, json=body).json() == first.json()
    # 旧约会回执在新增共同筹备字段之后仍表示同一份本人选择。
    old_text = "我想三小时后在这里聊十五分钟。"
    old = {
        "request_id": str(uuid4()),
        "recipient_id": e.npc,
        "intent": old_text,
        "evidence": old_text,
        "appointment_choice": {
            "operation": "propose",
            "kind": "talk",
            "location_id": e.loc["id"],
            "meeting_world_time": to_iso(e.at + timedelta(hours=3)),
            "statement": old_text,
        },
    }
    saved = e.client.post(url, json=old)
    assert saved.status_code == 200, saved.text
    legacy = NaturalSocialChoice.model_validate(old).model_dump(mode="json")
    legacy.pop("shared_plan_choice")
    digest = hashlib.sha256(
        json.dumps(legacy, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    with e.db.write() as c:
        c.execute(
            "UPDATE social_choice_receipts SET payload_hash=? WHERE request_id=?",
            (digest, old["request_id"]),
        )
    assert e.client.post(url, json=old).json() == saved.json()


def test_spent_labor_income_does_not_count_as_currently_prepared_budget(town):
    e = town
    pid = offer(e)
    choose(e, "accept", pid)
    perform(e)
    e.clock.heartbeat(e.wid, elapsed_seconds=3600)
    with e.db.write() as c:
        c.execute("UPDATE characters SET money=0 WHERE id=?", (e.npc,))
    player_food(e)
    assert choose(e, "check_in", pid).status_code == 200
    assert get(e, pid)["status"] == "active"
    mine = next(r for r in get(e, pid, e.npc)["roles"] if r["character_id"] == e.npc)
    assert mine["progress"] == 0


def test_old_checkin_is_not_sufficient_after_a_world_day(town):
    e = town
    pid = offer(
        e, own={"kind": "prepare_food", "target": 1}, other={"kind": "prepare_food", "target": 1}
    )
    choose(e, "accept", pid)
    player_food(e)
    with e.db.write() as c:
        c.execute("UPDATE characters SET longitude=longitude+0.0005 WHERE id=?", (e.player,))
        player, npc = people(e, c)
        SharedPlanService.apply(
            c,
            npc,
            player,
            e.at,
            SharedPlanIntent(operation="check_in", plan_id=pid, statement="我已准备好食物。"),
        )
    now = e.at + timedelta(days=2)
    set_time(e, now)
    with e.db.write() as c:
        c.execute("UPDATE characters SET longitude=? WHERE id=?", (e.loc["longitude"], e.player))
    assert choose(e, "check_in", pid).status_code == 200 and get(e, pid)["status"] == "active"
    with e.db.write() as c:
        player, npc = people(e, c)
        SharedPlanService.apply(
            c,
            npc,
            player,
            now,
            SharedPlanIntent(
                operation="check_in", plan_id=pid, statement="我重新核对了自己携带的食物。"
            ),
        )
    assert get(e, pid)["status"] == "completed"


def test_acceptance_rechecks_remaining_time_and_overlapping_commitments(town):
    e = town
    pid = offer(e)
    late = e.at + timedelta(days=2, hours=23, minutes=30)
    set_time(e, late)
    result = choose(e, "accept", pid)
    assert result.status_code == 409 and "劳动时间" in result.text
    set_time(e, e.at)
    assert choose(e, "accept", pid).status_code == 200
    with e.db.write() as c:
        third = c.execute(
            "SELECT * FROM characters WHERE world_id=? AND id NOT IN (?,?) LIMIT 1",
            (e.wid, e.player, e.npc),
        ).fetchone()
        c.execute(
            "UPDATE characters SET longitude=?,latitude=?,current_location_id=? WHERE id=?",
            (e.loc["longitude"], e.loc["latitude"], e.loc["id"], third["id"]),
        )
        third = c.execute("SELECT * FROM characters WHERE id=?", (third["id"],)).fetchone()
        player = people(e, c)[0]
        second = SharedPlanService.apply(
            c,
            third,
            player,
            e.at,
            SharedPlanIntent(
                operation="propose",
                title="另一份筹备",
                location_id=e.loc["id"],
                deadline_world_time=to_iso(e.at + timedelta(days=2)),
                own_task={"kind": "prepare_food", "target": 1},
                other_task={"kind": "prepare_food", "target": 1},
                statement="我们也一起准备食物吧。",
            ),
        )
        with pytest.raises(ValueError, match="重复承诺"):
            SharedPlanService.apply(
                c,
                player,
                third,
                e.at,
                SharedPlanIntent(
                    operation="accept", plan_id=second["plan_id"], statement="我接受。"
                ),
            )


def test_ending_or_expiring_does_not_create_missing_completion(town):
    e = town
    pid = offer(e)
    choose(e, "accept", pid)
    with e.db.write() as c:
        SharedPlanService.maintain(c, e.wid, e.at + timedelta(days=4))
    assert get(e, pid)["status"] == "expired"
    with e.db.read() as c:
        assert not c.execute(
            "SELECT 1 FROM world_events WHERE world_id=? AND event_type='social.appointment_preparation_completed'",
            (e.wid,),
        ).fetchone()


def test_profile_query_returns_only_the_selected_persons_shared_plans(town):
    e = town
    pid = offer(e)
    with e.db.read() as c:
        third = c.execute(
            "SELECT id FROM characters WHERE world_id=? AND id NOT IN (?,?) LIMIT 1",
            (e.wid, e.player, e.npc),
        ).fetchone()[0]
    prefix = f"/api/worlds/{e.wid}/shared-plans?character_id="
    assert [p["id"] for p in e.client.get(prefix + e.npc).json()] == [pid]
    assert e.client.get(prefix + third).json() == []
    assert e.client.get(prefix + "missing").status_code == 404


def test_actual_preparation_difficulty_creates_a_discretionary_outreach_opportunity(town):
    from world_engine.society import SocietyService

    e = town
    pid = offer(e)
    choose(e, "accept", pid)
    with e.db.write() as c:
        c.execute(
            "UPDATE shared_preparation_roles SET private_reason='本人暂时没有找到可用工作' WHERE plan_id=? AND character_id=?",
            (pid, e.npc),
        )
        SocietyService.tick(c, e.wid, e.at)
        job = c.execute(
            "SELECT reason FROM npc_outreach_jobs WHERE npc_id=? AND player_id=?", (e.npc, e.player)
        ).fetchone()
        assert job and "共同筹备" in job[0] and "自主决定" in job[0]


@pytest.mark.parametrize(
    "patch",
    [
        {"deadline_world_time": "2030-01-01T10:00:00"},
        {"deadline_world_time": "2000-01-01T10:00:00Z"},
        {"location_id": "missing"},
        {"own_task": {"kind": "prepare_food", "target": 13}},
        {"other_task": {"kind": "earn_money", "target": True}},
    ],
)
def test_invalid_plans_leave_no_partial_proposal(town, patch):
    e = town
    fields = {
        "title": "共同筹备",
        "location_id": e.loc["id"],
        "deadline_world_time": to_iso(e.at + timedelta(days=2)),
        "own_task": {"kind": "prepare_food", "target": 2},
        "other_task": {"kind": "earn_money", "target": 12},
        **patch,
    }
    result = choose(e, "propose", **fields)
    assert result.status_code in {409, 422}, result.text
    assert e.client.get(f"/api/worlds/{e.wid}/shared-plans").json() == []
