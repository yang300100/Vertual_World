"""哥哥，室内赴约沿真实门路前进，短时接待只授予本次访问权。"""

from datetime import timedelta

from world_engine.interiors import InteriorError, InteriorService
from world_engine.repository import from_iso, to_iso


class AppointmentRoomService:
    @staticmethod
    def path(c, wid, room_id):
        rooms = []
        seen = set()
        while room_id:
            if room_id in seen or len(rooms) >= 5:
                raise InteriorError("房间通路存在循环或层级过深")
            seen.add(room_id)
            room = InteriorService.room(c, wid, room_id)
            if rooms and room["location_id"] != rooms[0]["location_id"]:
                raise InteriorError("房间通路不在同一地点")
            rooms.append(room)
            room_id = room["parent_room_id"]
        return rooms

    @classmethod
    def transition_minutes(cls, c, wid, current_room_id, target_room_id):
        current = [r["id"] for r in cls.path(c, wid, current_room_id)] + [None]
        target = [r["id"] for r in cls.path(c, wid, target_room_id)] + [None]
        common = next(rid for rid in current if rid in target)
        return 5 * (current.index(common) + target.index(common))

    @staticmethod
    def lease(c, room, start, end):
        return c.execute(
            "SELECT * FROM contract_fulfillments WHERE world_id=? AND asset_id=? "
            "AND kind='lodging' AND status='active' AND started_world_time<? AND due_world_time>?",
            (room["world_id"], room["id"], to_iso(end), to_iso(start)),
        ).fetchone()

    @classmethod
    def controller(cls, c, room, start, end):
        lease = cls.lease(c, room, start, end)
        if lease:
            if (
                from_iso(lease["started_world_time"]) > start
                or from_iso(lease["due_world_time"]) < end
            ):
                raise InteriorError("这段相处时间跨越房间租约变更，请另选时间或地点")
            return lease["requester_id"]
        return room["owner_character_id"]

    @classmethod
    def base_access(cls, c, room, cid, start, end):
        from world_engine.families import FamilyService

        lease = cls.lease(c, room, start, end)
        if lease:
            return cls.controller(c, room, start, end) == cid
        return bool(
            room["access_policy"] == "public"
            or c.execute(
                "SELECT 1 FROM life_room_access WHERE room_id=? AND character_id=?",
                (room["id"], cid),
            ).fetchone()
            or FamilyService.has_residency(c, room, cid, at=start)
        )

    @classmethod
    def validate(
        cls,
        c,
        wid,
        room_id,
        location_id,
        first,
        second,
        start,
        end,
        *,
        visit_host_id=None,
        awaiting_host_id=None,
    ):
        if not room_id:
            if visit_host_id:
                raise InteriorError("室外安排不需要房间接待许可")
            return
        rooms = cls.path(c, wid, room_id)
        if rooms[0]["location_id"] != location_id:
            raise InteriorError("约会房间与指定地点不一致")
        host = cls.controller(c, rooms[0], start, end)
        if visit_host_id and (host != visit_host_id or host not in {first, second}):
            raise InteriorError("只有当前有接待权的所有者或住客能允许本次室内访问")
        for room in rooms:
            controller = cls.controller(c, room, start, end)
            for cid in (first, second):
                if cls.base_access(c, room, cid, start, end):
                    continue
                if controller in {first, second} and controller in {
                    visit_host_id,
                    awaiting_host_id,
                }:
                    continue
                raise InteriorError(
                    "双方缺少房间或外层通路的进入许可；请由有接待权的人明确允许本次访问"
                )

    @classmethod
    def visit_allowed(cls, c, room, cid, at):
        for date in c.execute(
            "SELECT * FROM social_appointments WHERE world_id=? AND room_id IS NOT NULL "
            "AND visit_host_id IS NOT NULL AND visit_host_id<>? AND (proposer_id=? OR recipient_id=?) "
            "AND status IN ('accepted','active') AND starts_at<=? AND ends_at>?",
            (room["world_id"], cid, cid, cid, to_iso(at), to_iso(at)),
        ):
            host = c.execute(
                "SELECT * FROM characters WHERE id=?", (date["visit_host_id"],)
            ).fetchone()
            if not host or host["health"] <= 0 or host["current_room_id"] != date["room_id"]:
                continue
            try:
                chain = cls.path(c, room["world_id"], date["room_id"])
                if room["id"] not in {r["id"] for r in chain}:
                    continue
                # 接待权改变、改期或取消会立即停止本次访问权，不延长成居住或储物权。
                cls.validate(
                    c,
                    date["world_id"],
                    date["room_id"],
                    date["location_id"],
                    date["proposer_id"],
                    date["recipient_id"],
                    at,
                    from_iso(date["ends_at"]),
                    visit_host_id=date["visit_host_id"],
                )
                if cls.controller(c, room, at, from_iso(date["ends_at"])) == date["visit_host_id"]:
                    return True
            except InteriorError:
                continue
        return False

    @classmethod
    def room_options(cls, c, wid, cid, other, at):
        actor = c.execute("SELECT * FROM characters WHERE id=?", (cid,)).fetchone()
        result = []
        for room in c.execute(
            "SELECT r.*,l.name location_name FROM life_rooms r JOIN locations l ON l.id=r.location_id "
            "WHERE r.world_id=? AND l.is_active=1 AND (r.owner_character_id=? OR r.id=? OR "
            "r.id IN (SELECT room_id FROM life_room_access WHERE character_id=?) OR "
            "r.id IN (SELECT room_id FROM home_residency_permissions WHERE resident_id=?) OR "
            "r.id IN (SELECT room_id FROM family_agreements WHERE world_id=? AND kind='cohabit' AND status='active' AND (proposer_id=? OR recipient_id=?)) OR "
            "r.id IN (SELECT asset_id FROM contract_fulfillments WHERE world_id=? AND kind='lodging' AND status='active' AND requester_id=? AND due_world_time>?) OR "
            "r.id IN (SELECT room_id FROM social_appointments WHERE world_id=? AND ((proposer_id=? AND recipient_id=?) OR (proposer_id=? AND recipient_id=?)) AND status IN ('pending','accepted','active') AND ends_at>?) OR "
            "(r.access_policy='public' AND r.location_id=?)) ORDER BY (r.id=?) DESC,r.name LIMIT 32",
            (
                wid,
                cid,
                actor["current_room_id"],
                cid,
                cid,
                wid,
                cid,
                cid,
                wid,
                cid,
                to_iso(at),
                wid,
                cid,
                other,
                other,
                cid,
                to_iso(at),
                actor["current_location_id"] or actor["location_id"],
                actor["current_room_id"],
            ),
        ):
            try:
                host = cls.controller(c, room, at, at + timedelta(seconds=1))
                cls.validate(
                    c,
                    wid,
                    room["id"],
                    room["location_id"],
                    cid,
                    other,
                    at,
                    at + timedelta(seconds=1),
                    awaiting_host_id=host if host in {cid, other} else None,
                )
                result.append(
                    {
                        "id": room["id"],
                        "name": room["name"],
                        "location_id": room["location_id"],
                        "location_name": room["location_name"],
                        "may_host": host == cid,
                    }
                )
            except InteriorError:
                continue
        return result[:12]

    @classmethod
    def risk(cls, c, date, at):
        if not date or not date["room_id"]:
            return ""
        try:
            cls.validate(
                c,
                date["world_id"],
                date["room_id"],
                date["location_id"],
                date["proposer_id"],
                date["recipient_id"],
                max(at, from_iso(date["starts_at"])),
                from_iso(date["ends_at"]),
                visit_host_id=date["visit_host_id"],
            )
        except InteriorError as exc:
            return str(exc)
        return ""

    @classmethod
    def approach(cls, c, actor, date, at, destination):
        from world_engine.daily_life import DailyLifeService

        if risk:=cls.risk(c,date,at):
            return "房间接待条件已变化，等待重新商量："+risk
        target = date["room_id"]
        if actor["current_room_id"] == target:
            return "已到约定房间，等候对方"
        rooms = cls.path(c, actor["world_id"], target)
        ids = [r["id"] for r in rooms]
        if actor["current_room_id"] and actor["current_room_id"] not in ids:
            return DailyLifeService._travel(c, actor, at, destination)
        if not actor["current_room_id"] and not InteriorService.near_location(
            c, actor, date["location_id"]
        ):
            return DailyLifeService._travel(c, actor, at, destination)
        next_room = next(r for r in rooms if r["parent_room_id"] == actor["current_room_id"])
        if not InteriorService.allowed(c, next_room, actor["id"],at=at):
            return "等候约会接待者开门与接待"
        if actor["current_fixture_id"]:
            InteriorService.operate_fixture(c, actor, actor["current_fixture_id"], "stand", at)
            actor = c.execute("SELECT * FROM characters WHERE id=?", (actor["id"],)).fetchone()
        if next_room["door_locked"]:
            if not InteriorService.can_unlock(c, next_room, actor["id"],at=at):
                return "等候约会接待者开门与接待"
            InteriorService.door(c, actor, next_room["id"], "unlock", at)
        if not next_room["door_open"]:
            InteriorService.door(c, actor, next_room["id"], "open", at)
        InteriorService.door(c, actor, next_room["id"], "enter", at)
        return "经过约会房间入口"
