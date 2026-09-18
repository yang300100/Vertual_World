"""门口拜访与短时邀请：真实应门动作不冒充 NPC 台词。"""

from datetime import timedelta
from uuid import uuid4

from world_engine.interiors import InteriorError, InteriorService
from world_engine.life import LifeActivityService
from world_engine.repository import from_iso, to_iso


class VisitService:
    @staticmethod
    def controller(c, room, at):
        lease = c.execute(
            "SELECT requester_id FROM contract_fulfillments WHERE asset_id=? AND kind='lodging' "
            "AND status='active' AND due_world_time>?",
            (room["id"], to_iso(at)),
        ).fetchone()
        return lease[0] if lease else room["owner_character_id"]

    @staticmethod
    def available(c, actor):
        return (
            actor["health"] > 0
            and not actor["current_fixture_id"]
            and not (
                LifeActivityService.running(c, actor["id"])
                or c.execute(
                    "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",
                    (actor["id"],),
                ).fetchone()
            )
        )

    @classmethod
    def at_door(cls, c, visitor, room):
        return (
            visitor["current_room_id"] == room["parent_room_id"]
            and visitor["health"] > 0
            and not c.execute(
                "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",
                (visitor["id"],),
            ).fetchone()
            and InteriorService.reachable_door(c, visitor, room)
        )

    @classmethod
    def invitation(cls, c, room, cid, at):
        return c.execute(
            "SELECT id FROM life_visits WHERE room_id=? AND visitor_id=? AND host_id=? "
            "AND status='invited' AND expires_world_time>? ORDER BY created_world_time DESC LIMIT 1",
            (room["id"], cid, cls.controller(c, room, at), to_iso(at)),
        ).fetchone()

    @classmethod
    def knock(cls, c, actor, room_id, request_id, at):
        prior = c.execute(
            "SELECT * FROM life_visits WHERE world_id=? AND visitor_id=? AND request_id=?",
            (actor["world_id"], actor["id"], request_id),
        ).fetchone()
        if prior:
            if prior["room_id"] != room_id:
                raise InteriorError("请求标识已用于另一扇门")
            return {"summary": "这次敲门已记录，请查看拜访状态。", "visit_id": prior["id"]}
        room = InteriorService.room(c, actor["world_id"], room_id)
        if not cls.at_door(c, actor, room) or not cls.available(c, actor):
            raise InteriorError("请先结束活动、起身并走到这扇门的外侧")
        cls.expire(c, actor["world_id"], at)
        prior = c.execute(
            "SELECT 1 FROM life_visits WHERE visitor_id=? AND room_id=? "
            "AND (status IN ('pending','invited') OR created_world_time>?)",
            (actor["id"], room_id, to_iso(at - timedelta(minutes=1))),
        ).fetchone()
        if prior:
            raise InteriorError("刚才的敲门还在等候，请稍后再试")
        vid = str(uuid4())
        event = InteriorService.record(
            c, actor, at, "knock", "门外响起了敲门声。", {"room_id": room_id}
        )
        c.execute(
            "INSERT INTO life_visits VALUES (?,?,?,?,?,?,?,?, 'pending',?,NULL)",
            (
                vid,
                actor["world_id"],
                room_id,
                actor["id"],
                cls.controller(c, room, at),
                request_id,
                to_iso(at),
                to_iso(at + timedelta(minutes=10)),
                event,
            ),
        )
        host = c.execute(
            "SELECT * FROM characters WHERE id=?", (cls.controller(c, room, at),)
        ).fetchone()
        if (
            host
            and host["current_room_id"] == room_id
            and InteriorService.reachable_door(c, host, room)
        ):
            waiting = LifeActivityService.running(c, host["id"])
            if waiting and waiting["kind"] == "wait":
                LifeActivityService.interrupt(c, host["id"], at, "听到门口的敲门声。")
        cls.tick(c, actor["world_id"], at, visit_id=vid)
        return {"summary": "你敲了敲门，正在等候应门。", "visit_id": vid}

    @classmethod
    def respond(cls, c, actor, visit_id, accept, at):
        visit = c.execute(
            "SELECT * FROM life_visits WHERE id=? AND world_id=?", (visit_id, actor["world_id"])
        ).fetchone()
        if not visit:
            raise InteriorError("拜访请求不存在")
        room = InteriorService.room(c, actor["world_id"], visit["room_id"])
        if (
            visit["host_id"] != actor["id"]
            or cls.controller(c, room, at) != actor["id"]
            or actor["current_room_id"] != room["id"]
            or not InteriorService.reachable_door(c, actor, room)
            or not cls.available(c, actor)
        ):
            raise InteriorError("需要当前有接待权的住客在屋内应门，并先结束活动或起身")
        if visit["status"] != "pending":
            raise InteriorError("这次拜访已处理，请刷新查看")
        visitor = c.execute(
            "SELECT * FROM characters WHERE id=?", (visit["visitor_id"],)
        ).fetchone()
        if (
            from_iso(visit["expires_world_time"]) <= at
            or not visitor
            or not cls.at_door(c, visitor, room)
        ):
            raise InteriorError("拜访已经过期，或门外的人已经离开/正在忙碌")
        if accept:
            c.execute("UPDATE life_rooms SET door_open=1,door_locked=0 WHERE id=?", (room["id"],))
        status = "invited" if accept else "declined"
        summary = (
            "屋内住客打开了门，给予来访者一次短时进入许可。" if accept else "这次拜访未获邀请。"
        )
        event = InteriorService.record(
            c,
            actor,
            at,
            "visit_response",
            summary,
            {"room_id": room["id"], "visit_id": visit_id, "accepted": accept},
        )
        from world_engine.event_history import EventHistoryService

        if visit["source_event_id"]:
            EventHistoryService.link(c, event, visit["source_event_id"], "visit_response")
        c.execute(
            "UPDATE life_visits SET status=?,expires_world_time=?,response_event_id=? WHERE id=?",
            (status, to_iso(at + timedelta(minutes=15)), event, visit_id),
        )
        return {"summary": summary, "event_id": event}

    @classmethod
    def withdraw(cls, c, actor, visit_id, at):
        visit = c.execute(
            "SELECT * FROM life_visits WHERE id=? AND world_id=?", (visit_id, actor["world_id"])
        ).fetchone()
        if not visit:
            raise InteriorError("拜访请求不存在")
        room = InteriorService.room(c, actor["world_id"], visit["room_id"])
        if actor["id"] != visit["visitor_id"] and not (
            actor["id"] == visit["host_id"] == cls.controller(c, room, at)
        ):
            raise InteriorError("只有来访者或当前接待者可以撤回这次拜访")
        if visit["status"] not in {"pending", "invited"}:
            return {"summary": "这次拜访已经结束。"}
        c.execute("UPDATE life_visits SET status='cancelled' WHERE id=?", (visit_id,))
        event = InteriorService.record(
            c,
            actor,
            at,
            "visit_cancelled",
            "这次拜访或进入邀请已撤回。",
            {"visit_id": visit_id, "room_id": room["id"]},
        )
        return {"summary": "这次拜访或进入邀请已撤回。", "event_id": event}

    @classmethod
    def expire(cls, c, wid, at):
        c.execute(
            "UPDATE life_visits SET status='expired' WHERE world_id=? "
            "AND status IN ('pending','invited') AND expires_world_time<=?",
            (wid, to_iso(at)),
        )

    @classmethod
    def tick(cls, c, wid, at, visit_id=None):
        cls.expire(c, wid, at)
        for visit in c.execute(
            "SELECT * FROM life_visits WHERE world_id=? AND status='pending' AND (? IS NULL OR id=?)",
            (wid, visit_id, visit_id),
        ).fetchall():
            try:
                room = InteriorService.room(c, wid, visit["room_id"])
            except InteriorError:
                continue
            if cls.controller(c, room, at) != visit["host_id"]:
                c.execute("UPDATE life_visits SET status='cancelled' WHERE id=?", (visit["id"],))
                continue
            visitor = c.execute(
                "SELECT * FROM characters WHERE id=?", (visit["visitor_id"],)
            ).fetchone()
            host = c.execute("SELECT * FROM characters WHERE id=?", (visit["host_id"],)).fetchone()
            if not visitor or not cls.at_door(c, visitor, room):
                c.execute("UPDATE life_visits SET status='cancelled' WHERE id=?", (visit["id"],))
                continue
            if (
                not host
                or host["is_player"]
                or not cls.available(c, host)
                or host["current_room_id"] != room["id"]
                or not InteriorService.reachable_door(c, host, room)
            ):
                continue
            policy = c.execute(
                "SELECT visitor_policy FROM life_door_policies WHERE room_id=?", (room["id"],)
            ).fetchone()
            policy = policy[0] if policy else "authorized_only"
            if policy == "manual":
                continue
            # 先真实开门查看门口，不能隔着关门凭全知身份决定信任。
            original_open, original_locked = room["door_open"], room["door_locked"]
            c.execute("UPDATE life_rooms SET door_open=1,door_locked=0 WHERE id=?", (room["id"],))
            InteriorService.record(
                c,
                host,
                at,
                "door_checked",
                "屋内住客打开门，查看门口的来访者。",
                {"room_id": room["id"], "visit_id": visit["id"]},
            )
            authorized = InteriorService.keyed(c, room, visitor["id"])
            relationship = c.execute(
                "SELECT trust,affinity FROM relationships WHERE world_id=? AND source_character_id=? AND target_character_id=?",
                (wid, host["id"], visitor["id"]),
            ).fetchone()
            known = c.execute(
                "SELECT 1 FROM character_acquaintances WHERE observer_id=? AND subject_id=?",
                (host["id"], visitor["id"]),
            ).fetchone()
            trusted = (
                policy == "trusted_contacts"
                and known
                and relationship
                and (relationship["trust"] >= 20 and relationship["affinity"] >= 10)
            )
            if authorized or trusted:
                cls.respond(c, host, visit["id"], True, at)
            else:
                cls.respond(c, host, visit["id"], False, at)
                c.execute(
                    "UPDATE life_rooms SET door_open=?,door_locked=? WHERE id=?",
                    (original_open, original_locked, room["id"]),
                )

    @classmethod
    def view(cls, c, actor, at):
        from world_engine.society import SocietyService

        output = []
        for row in c.execute(
            "SELECT * FROM life_visits WHERE world_id=? AND (visitor_id=? OR host_id=?) "
            "ORDER BY created_world_time DESC,rowid DESC LIMIT 20",
            (actor["world_id"], actor["id"], actor["id"]),
        ):
            room = c.execute("SELECT * FROM life_rooms WHERE id=?", (row["room_id"],)).fetchone()
            if row["visitor_id"] != actor["id"] and (
                actor["current_room_id"] != room["id"] or cls.controller(c, room, at) != actor["id"]
            ):
                continue
            status = row["status"]
            if status in {"pending", "invited"} and from_iso(row["expires_world_time"]) <= at:
                status = "expired"
            if status in {"pending", "invited"} and cls.controller(c, room, at) != row["host_id"]:
                status = "cancelled"
            mine = row["visitor_id"] == actor["id"]
            output.append(
                {
                    "id": row["id"],
                    "room_name": room["name"],
                    "status": status,
                    "expires_world_time": row["expires_world_time"],
                    "mine": mine,
                    "visitor_label": None
                    if mine
                    else SocietyService.label(c, actor["id"], row["visitor_id"])
                    if room["door_open"]
                    else "门外来访者",
                    "can_respond": not mine and status == "pending" and cls.available(c, actor),
                }
            )
        return output
