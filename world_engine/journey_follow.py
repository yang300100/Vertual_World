"""哥哥，跟随只授权已经共同同意的有限路线，暂停后要由本人重新选择。"""

import json
from datetime import timedelta

from world_engine.repository import from_iso, to_iso


class JourneyFollowService:
    @staticmethod
    def has_live(c, wid):
        return bool(
            c.execute(
                "SELECT 1 FROM joint_journeys WHERE world_id=? "
                "AND status IN ('accepted','active') AND follow_authorized_by IS NOT NULL LIMIT 1",
                (wid,),
            ).fetchone()
        )

    @classmethod
    def boundary(cls, c, wid, at, result):
        rows = c.execute(
            "SELECT j.* FROM joint_journeys j WHERE j.world_id=? "
            "AND j.status IN ('accepted','active') AND j.phase<>'moving' "
            "AND j.follow_authorized_by IS NOT NULL "
            "AND (j.next_depart_at IS NULL OR j.next_depart_at<=?) "
            "AND EXISTS (SELECT 1 FROM journey_stops s "
            "WHERE s.journey_id=j.id AND s.stop_index=j.next_index)",
            (wid, to_iso(at)),
        ).fetchall()
        # 哥哥，只唤醒当前可开始的一程；在途、停留和结束后沿用已有时间边界。
        return (
            min(result, at + timedelta(seconds=1))
            if any(cls.valid(c, row, at) for row in rows)
            else result
        )

    @staticmethod
    def leader_id(c, row):
        value = dict(row)
        if value.get("leader_id"):
            return value["leader_id"]
        player = c.execute(
            "SELECT id FROM characters WHERE id IN (?,?) AND is_player=1",
            (row["proposer_id"], row["recipient_id"]),
        ).fetchone()
        return player[0] if player else None

    @classmethod
    def assert_leader_knows(cls, c, row, at):
        leader = c.execute(
            "SELECT * FROM characters WHERE id=?", (cls.leader_id(c, row),)
        ).fetchone()
        if leader is None or leader["is_player"]:
            return
        locations = [row["origin_id"]] + [
            s[0]
            for s in c.execute(
                "SELECT location_id FROM journey_stops WHERE journey_id=? ORDER BY stop_index",
                (row["id"],),
            )
        ]
        for lid in locations:
            place = c.execute("SELECT space_layer FROM locations WHERE id=?", (lid,)).fetchone()
            if (
                place is None
                or place[0] != leader["space_layer"]
                or (
                    lid != (leader["current_location_id"] or leader["location_id"])
                    and not c.execute(
                        "SELECT 1 FROM world_events e JOIN event_observers o ON o.event_id=e.id "
                        "WHERE e.world_id=? AND e.location_id=? AND o.character_id=? "
                        "AND o.channel='participant' AND e.occurred_at<=? LIMIT 1",
                        (row["world_id"], lid, leader["id"], to_iso(at)),
                    ).fetchone()
                )
            ):
                raise ValueError("领队本人尚不能确认整份路线，需要本人拒绝或另提可带路的行程")

    @classmethod
    def terms(cls, c, row):
        return {
            "journey_id": row["id"],
            "world_id": row["world_id"],
            "proposer_id": row["proposer_id"],
            "recipient_id": row["recipient_id"],
            "leader_id": cls.leader_id(c, row),
            "origin_id": row["origin_id"],
            "deadline_at": row["deadline_at"],
            "stops": [
                dict(s)
                for s in c.execute(
                    "SELECT stop_index,location_id,minutes,is_return FROM journey_stops "
                    "WHERE journey_id=? ORDER BY stop_index",
                    (row["id"],),
                )
            ],
        }

    @classmethod
    def valid(cls, c, row, at):
        value = dict(row)
        if row["status"] not in {"accepted", "active"} or at >= from_iso(row["deadline_at"]):
            return False
        player = c.execute(
            "SELECT id FROM characters WHERE id IN (?,?) AND is_player=1",
            (row["proposer_id"], row["recipient_id"]),
        ).fetchone()
        if not player or cls.leader_id(c, row) == player[0]:
            return False
        if value.get("follow_authorized_by") != player[0] or not value.get(
            "follow_source_event_id"
        ):
            return False
        event = c.execute(
            "SELECT * FROM world_events WHERE id=? AND world_id=?",
            (value["follow_source_event_id"], row["world_id"]),
        ).fetchone()
        try:
            current = cls.terms(c, row)
            saved = json.loads(value["follow_terms_json"])
            payload = json.loads(event["payload_json"]) if event else {}
            return bool(
                event
                and event["event_type"] == "social.appointment_journey_follow"
                and event["actor_id"] == player[0]
                and event["target_id"] == cls.leader_id(c, row)
                and event["occurred_at"] == value["follow_authorized_at"]
                and from_iso(event["occurred_at"]) <= at
                and payload.get("journey_id") == row["id"]
                and payload.get("follow_terms") == saved == current
            )
        except (KeyError, TypeError, ValueError):
            return False

    @staticmethod
    def clear(c, jid):
        c.execute(
            "UPDATE joint_journeys SET follow_authorized_by=NULL,follow_authorized_at=NULL,"
            "follow_source_event_id=NULL,follow_terms_json=NULL WHERE id=?",
            (jid,),
        )

    @classmethod
    def shared(cls, c, row, cid, at):
        from world_engine.society import SocietyService

        leader = cls.leader_id(c, row)
        player = c.execute("SELECT is_player FROM characters WHERE id=?", (cid,)).fetchone()
        following = cls.valid(c, row, at)
        remains = c.execute(
            "SELECT 1 FROM journey_stops WHERE journey_id=? AND stop_index=?",
            (row["id"], row["next_index"]),
        ).fetchone()
        return {
            "leader_id": leader,
            "leader_name": "你" if leader == cid else SocietyService.label(c, cid, leader),
            "following": following,
            # 哥哥，可操作提示只用共同确认的行程和期限，不窥探伙伴的私下身体细节。
            "can_follow": bool(
                player
                and player[0]
                and leader != cid
                and not following
                and remains
                and row["status"] in {"accepted", "active"}
                and row["phase"] != "moving"
                and at < from_iso(row["deadline_at"])
            ),
        }

    @classmethod
    def apply(cls, c, row, actor, other, at, choice):
        from world_engine.intimacy import IntimacyService
        from world_engine.journeys import JourneyService

        if not actor["is_player"] or cls.leader_id(c, row) != other["id"]:
            raise ValueError("只有玩家本人能选择跟随本次已约定的 NPC 领队，或撤回本人跟随")
        if choice.operation == "withdraw_follow":
            if not dict(row).get("follow_authorized_by") or row["status"] not in {
                "accepted",
                "active",
            }:
                raise ValueError("这份行程目前没有本人的有效跟随安排")
            return JourneyService.pause(c, row, at, "玩家本人撤回跟随，后续出发须重新商量。")
        if cls.valid(c, row, at):
            return {
                "status": row["status"],
                "journey_id": row["id"],
                "event_id": row["follow_source_event_id"],
                "summary": "本人此前已明确选择跟随这份有限路线，未重复移动。",
            }
        if row["status"] not in {"accepted", "active"} or row["phase"] == "moving":
            raise ValueError("请先共同确认或恢复有限行程，并实际会合后选择跟随")
        if IntimacyService.paused(c, row["world_id"], actor["id"], other["id"]):
            raise ValueError("一方仍要求留出空间，暂不授予新的跟随安排")
        JourneyService._ready(c, row, at)
        terms = cls.terms(c, row)
        event = JourneyService._record(
            c,
            actor,
            other,
            at,
            "follow",
            "玩家本人明确选择跟随这份已经共同确认的有限路线，"
            "由约定的 NPC 领队实际带路；暂停后不自动重授。" + choice.statement,
            {"journey_id": row["id"], "follow_terms": terms},
        )
        c.execute(
            "UPDATE joint_journeys SET follow_authorized_by=?,follow_authorized_at=?,"
            "follow_source_event_id=?,follow_terms_json=?,source_event_id=? WHERE id=?",
            (
                actor["id"],
                to_iso(at),
                event,
                json.dumps(terms, ensure_ascii=False),
                event,
                row["id"],
            ),
        )
        c.execute(
            "UPDATE npc_daily_states SET next_world_time=? WHERE character_id=?",
            (to_iso(at), other["id"]),
        )
        return {
            "status": row["status"],
            "journey_id": row["id"],
            "event_id": event,
            "summary": "本人已选择跟随已约定的 NPC 领队；尚未移动，领队仍按真实条件自行出发。",
        }

    @classmethod
    def require_departure(cls, c, row, actor, at):
        leader = cls.leader_id(c, row)
        if actor["id"] != leader:
            raise ValueError("本次须由已经约定的领队本人选择出发")
        if not actor["is_player"] and not cls.valid(c, row, at):
            raise ValueError("NPC 领队尚未取得玩家本人对这份有限路线的有效跟随选择")
        cls.assert_leader_knows(c, row, at)
