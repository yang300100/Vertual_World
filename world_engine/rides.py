"""哥哥，共乘绑定真实载具的一条路线，每个人的同意和登离都单独留证。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from world_engine.actions import ActionService
from world_engine.contracts import ContractService
from world_engine.geo import great_circle_distance_km
from world_engine.repository import from_iso, to_iso, utc_now


RIDE_SCHEMA = """
CREATE TABLE IF NOT EXISTS vehicle_states (
 vehicle_id TEXT PRIMARY KEY REFERENCES vehicles(id) ON DELETE CASCADE,
 world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 capacity INTEGER NOT NULL CHECK(capacity BETWEEN 1 AND 64),
 longitude REAL NOT NULL CHECK(longitude BETWEEN -180 AND 180),
 latitude REAL NOT NULL CHECK(latitude BETWEEN -90 AND 90),
 space_layer TEXT NOT NULL,current_location_id TEXT REFERENCES locations(id),
 status TEXT NOT NULL CHECK(status IN ('parked','moving','blocked')),
 distance_travelled_km REAL NOT NULL DEFAULT 0 CHECK(distance_travelled_km>=0),
 registration_id TEXT NOT NULL REFERENCES element_registration_requests(id),updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vehicle_rides (
 id TEXT PRIMARY KEY,world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 vehicle_id TEXT NOT NULL REFERENCES vehicle_states(vehicle_id),
 proposer_id TEXT NOT NULL REFERENCES characters(id),controller_id TEXT NOT NULL REFERENCES characters(id),
 origin_location_id TEXT NOT NULL REFERENCES locations(id),destination_location_id TEXT NOT NULL REFERENCES locations(id),
 status TEXT NOT NULL CHECK(status IN ('pending','accepted','boarding','moving','arrived','blocked','ended','expired','declined')),
 statement TEXT NOT NULL,proposed_at TEXT NOT NULL,deadline_at TEXT NOT NULL,
 capacity INTEGER NOT NULL CHECK(capacity BETWEEN 1 AND 64),
 terms_json TEXT NOT NULL,proposal_event_id TEXT NOT NULL REFERENCES world_events(id),
 movement_id TEXT UNIQUE REFERENCES character_movements(id),started_at TEXT,arrived_at TEXT,ended_at TEXT,
 stop_requested INTEGER NOT NULL DEFAULT 0 CHECK(stop_requested IN (0,1)),
 reason TEXT NOT NULL DEFAULT '',source_event_id TEXT NOT NULL REFERENCES world_events(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_vehicle_rides_one_live ON vehicle_rides(vehicle_id)
 WHERE status IN ('pending','accepted','boarding','moving','arrived','blocked');
CREATE INDEX IF NOT EXISTS idx_vehicle_rides_world ON vehicle_rides(world_id,status,deadline_at);
CREATE TABLE IF NOT EXISTS vehicle_ride_members (
 ride_id TEXT NOT NULL REFERENCES vehicle_rides(id) ON DELETE CASCADE,
 character_id TEXT NOT NULL REFERENCES characters(id),ordinal INTEGER NOT NULL,
 live INTEGER NOT NULL DEFAULT 1 CHECK(live IN (0,1)),
 decision TEXT NOT NULL DEFAULT 'pending' CHECK(decision IN ('pending','accepted','declined')),
 decision_at TEXT,decision_event_id TEXT REFERENCES world_events(id),
 boarded_at TEXT,board_event_id TEXT REFERENCES world_events(id),
 disembarked_at TEXT,disembark_event_id TEXT REFERENCES world_events(id),
 PRIMARY KEY(ride_id,character_id),UNIQUE(ride_id,ordinal)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_vehicle_ride_member_one_live ON vehicle_ride_members(character_id) WHERE live=1 AND decision='accepted';
"""


class RideIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["propose", "accept", "decline", "board", "depart", "request_stop", "disembark", "end", "propose_recovery", "accept_recovery", "decline_recovery", "withdraw_recovery", "resume", "propose_handoff", "authorize_handoff", "accept_handoff", "decline_handoff", "withdraw_handoff", "take_control", "revoke_handoff"]
    ride_id: str | None = Field(default=None, min_length=1, max_length=100)
    recovery_id: str | None = Field(default=None, min_length=1, max_length=100)
    handoff_id: str | None = Field(default=None, min_length=1, max_length=100)
    vehicle_id: str | None = Field(default=None, min_length=1, max_length=100)
    member_ids: list[str] | None = Field(default=None, min_length=1, max_length=64)
    controller_id: str | None = Field(default=None, min_length=1, max_length=100)
    destination_location_id: str | None = Field(default=None, min_length=1, max_length=100)
    deadline_world_time: datetime | None = None
    statement: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def shape(self):
        fields = (self.vehicle_id, self.member_ids, self.controller_id, self.destination_location_id, self.deadline_world_time)
        from world_engine.ride_handoff import HANDOFF_OPERATIONS
        if self.operation in HANDOFF_OPERATIONS:
            if not self.ride_id or self.recovery_id or self.vehicle_id or self.member_ids or self.destination_location_id:
                raise ValueError("交接只引用原共乘，不更换载具、名单或目的地")
            if self.operation == "propose_handoff":
                if self.handoff_id or not self.controller_id or self.deadline_world_time is None or self.deadline_world_time.tzinfo is None:
                    raise ValueError("交接提议须明确原在车新驾驶者与带时区的新期限")
            elif not self.handoff_id or self.controller_id or self.deadline_world_time is not None:
                raise ValueError("交接回应只引用原共乘及本轮交接编号")
            return self
        if self.handoff_id is not None:
            raise ValueError("其他操作不能夹带驾驶交接编号")
        from world_engine.ride_recovery import RECOVERY_OPERATIONS
        if self.operation in RECOVERY_OPERATIONS:
            if not self.ride_id or any(value is not None for value in fields[:4]):
                raise ValueError("恢复只引用原共乘，不替换载具、名单、控制者或目的地")
            if self.operation == "propose_recovery":
                if self.recovery_id or self.deadline_world_time is None or self.deadline_world_time.tzinfo is None:
                    raise ValueError("恢复提议需要带时区的新期限，不复用旧恢复编号")
            elif not self.recovery_id or self.deadline_world_time is not None:
                raise ValueError("恢复回应仅引用原共乘及明确的恢复轮次")
            return self
        if self.recovery_id is not None:
            raise ValueError("普通共乘操作不能夹带恢复编号")
        if self.operation == "propose":
            if self.ride_id is not None or any(x is None for x in fields):
                raise ValueError("共乘提议须明确载具、全部乘员、控制者、目的地和期限")
            if len(set(self.member_ids)) != len(self.member_ids):
                raise ValueError("乘员名单不能重复")
            if self.deadline_world_time.tzinfo is None:
                raise ValueError("共乘期限需要时区")
        elif not self.ride_id or any(x is not None for x in fields):
            raise ValueError("共乘回应只引用原约定，不改写载具、名单或路线")
        return self


class RideService:
    @staticmethod
    def install(c):
        # 哥哥，逐条执行保留调用方事务，不能让 DDL 偷偷提交已有业务。
        for statement in RIDE_SCHEMA.split(";"):
            if statement.strip():
                c.execute(statement)
        from world_engine.ride_recovery import RideRecoveryService
        RideRecoveryService.install(c)
        from world_engine.ride_handoff import RideHandoffService
        RideHandoffService.install(c)

    @staticmethod
    def _json(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _person(c, wid, cid):
        person = c.execute("SELECT * FROM characters WHERE world_id=? AND id=?", (wid, cid)).fetchone()
        if person is None:
            raise ValueError("共乘人物不存在或不属于当前世界")
        return person

    @staticmethod
    def active(c, wid, cid):
        return c.execute(
            "SELECT r.* FROM vehicle_rides r JOIN vehicle_ride_members m ON m.ride_id=r.id "
            "WHERE r.world_id=? AND m.character_id=? AND m.live=1 AND m.decision='accepted' LIMIT 1", (wid, cid),
        ).fetchone()

    @staticmethod
    def aboard(c, cid):
        return c.execute(
            "SELECT r.* FROM vehicle_rides r JOIN vehicle_ride_members m ON m.ride_id=r.id "
            "WHERE m.character_id=? AND m.boarded_at IS NOT NULL AND m.disembarked_at IS NULL LIMIT 1", (cid,),
        ).fetchone()

    @classmethod
    def assert_not_aboard(cls, c, cid):
        if cls.aboard(c, cid):
            raise ValueError("本人仍在真实载具上，请安全靠站并由本人下车后再行动")

    @classmethod
    def assert_available(cls, c, actor):
        if cls.active(c, actor["world_id"], actor["id"]):
            raise ValueError("本人已有未结束的共乘约定，请先处理原约定")

    @staticmethod
    def state(c, wid, vid):
        state = c.execute("SELECT * FROM vehicle_states WHERE vehicle_id=? AND world_id=?", (vid, wid)).fetchone()
        if state is None:
            raise ValueError("这辆载具尚无已确认的真实位置和容量，不能推测座位或传送到车主身边")
        return state

    @staticmethod
    def _place(c, wid, lid, layer, kind):
        place = c.execute("SELECT * FROM locations WHERE id=? AND world_id=? AND is_active=1", (lid, wid)).fetchone()
        if place is None or place["longitude"] is None or place["latitude"] is None:
            raise ValueError("载具站点不存在或没有真实坐标")
        if place["space_layer"] != layer:
            raise ValueError("本次共乘仅支持同层站点，跨层需要真实转换节点")
        safe = {
            "ship": {"port", "dock", "harbor"}, "water": {"port", "dock", "harbor"},
            "flight": {"airfield", "landing_site"}, "underground": {"underground_station"},
        }
        if kind in safe and place["kind"] not in safe[kind]:
            raise ValueError("该地点不是此类载具已确认的安全登离站点")
        return place

    @classmethod
    def _vehicle(cls, c, row):
        vehicle = c.execute("SELECT * FROM vehicles WHERE id=? AND world_id=?", (row["vehicle_id"], row["world_id"])).fetchone()
        if vehicle is None:
            raise ValueError("原载具不存在")
        return vehicle

    @staticmethod
    def _members(c, row):
        return c.execute("SELECT * FROM vehicle_ride_members WHERE ride_id=? ORDER BY ordinal", (row["id"],)).fetchall()

    @classmethod
    def terms(cls, c, row):
        return {
            "ride_id": row["id"], "world_id": row["world_id"], "vehicle_id": row["vehicle_id"],
            "proposer_id": row["proposer_id"], "controller_id": row["controller_id"],
            "origin_location_id": row["origin_location_id"], "destination_location_id": row["destination_location_id"],
            "capacity": row["capacity"], "deadline_world_time": row["deadline_at"],
            "statement": row["statement"], "member_ids": [m["character_id"] for m in cls._members(c, row)],
            "stop_policy": "途中请求停止将保留乘员，按原路线抵达原安全目的地后各自下车；无权继续时原位挂起，不抛下乘员",
        }

    @classmethod
    def _event(cls, c, row, actor, at, operation, *, terms=False, reason="", audience=None, extra=None, location_id=None):
        data = {"ride_id": row["id"], "vehicle_id": row["vehicle_id"], **(extra or {})}
        if terms:
            data["terms"] = cls.terms(c, row)
        summary = f"{actor['name']}：{reason or operation}"
        event = ActionService._record_event(
            c, world_id=row["world_id"], tick_id=str(uuid4()), occurred_at=at,
            event_type="social.ride_" + operation, actor_id=actor["id"], target_id=None,
            location_id=location_id, summary=summary, payload=data,
        )
        if audience is not None:
            ids = audience
        else:
            ids = [actor['id']]
            for member in cls._members(c, row):
                if not member['live'] or member['character_id'] == actor['id']:
                    continue
                person = cls._person(c, row['world_id'], member['character_id'])
                if person['space_layer'] == actor['space_layer'] and person['current_room_id'] == actor['current_room_id'] and great_circle_distance_km(
                    person['longitude'], person['latitude'], actor['longitude'], actor['latitude'],
                ) <= 0.02:
                    ids.append(person['id'])
        for cid in dict.fromkeys(ids):
            c.execute("INSERT OR IGNORE INTO event_observers(event_id,character_id,channel) VALUES (?,?,'participant')", (event, cid))
            c.execute(
                "INSERT OR IGNORE INTO character_event_knowledge(character_id,event_id,world_id,source_kind,confidence,hops,learned_world_time) "
                "VALUES (?,?,?,'experienced',1,0,?)", (cid, event, row["world_id"], to_iso(at)),
            )
            ActionService._record_memory(c, world_id=row["world_id"], character_id=cid, event_id=event,
                                         memory_type="experienced", summary=summary, importance=6)
        return event

    @classmethod
    def _source(cls, c, row, eid, cid, operation, at, *, terms=False):
        event = c.execute("SELECT * FROM world_events WHERE id=? AND world_id=?", (eid, row["world_id"])).fetchone()
        if not event:
            return False
        try:
            payload = json.loads(event["payload_json"])
            return bool(
                event["actor_id"] == cid and event["target_id"] is None
                and event["event_type"] == "social.ride_" + operation
                and from_iso(row["proposed_at"]) <= from_iso(event["occurred_at"]) <= at
                and from_iso(event["occurred_at"]) < from_iso(row["deadline_at"])
                and payload.get("ride_id") == row["id"] and payload.get("vehicle_id") == row["vehicle_id"]
                and (not terms or payload.get("terms") == cls.terms(c, row))
                and c.execute(
                    "SELECT 1 FROM character_event_knowledge WHERE character_id=? AND event_id=? AND world_id=? "
                    "AND source_kind='experienced' AND learned_world_time<=?", (cid, eid, row["world_id"], to_iso(at)),
                ).fetchone()
            )
        except (TypeError, ValueError, KeyError):
            return False

    @classmethod
    def _accepted(cls, c, row, at):
        try:
            if json.loads(row["terms_json"]) != cls.terms(c, row):
                return False
            return all(
                m["decision"] == "accepted" and m["decision_at"] and cls._source(
                    c, row, m["decision_event_id"], m["character_id"],
                    "proposed" if m["character_id"] == row["proposer_id"] else "accepted", at, terms=True,
                ) and c.execute("SELECT occurred_at FROM world_events WHERE id=?", (m["decision_event_id"],)).fetchone()[0] == m["decision_at"]
                for m in cls._members(c, row)
            )
        except (TypeError, ValueError):
            return False

    @classmethod
    def _safe_parked(cls, c, row):
        state = cls.state(c, row["world_id"], row["vehicle_id"])
        if state["status"] == "moving":
            raise ValueError("载具仍在行驶中，请先请求安全停靠")
        place = cls._place(c, row["world_id"], state["current_location_id"], state["space_layer"], cls._vehicle(c, row)["movement_type"])
        if great_circle_distance_km(state["longitude"], state["latitude"], place["longitude"], place["latitude"]) > 0.02:
            raise ValueError("载具尚未实际靠到安全站点，不能在途中下车")
        return state, place

    @classmethod
    def _near_vehicle(cls, c, row, actor):
        state, place = cls._safe_parked(c, row)
        if actor["space_layer"] != state["space_layer"] or actor["current_room_id"] or actor["current_fixture_id"]:
            raise ValueError("请本人先起身并到载具所在的同层室外站点")
        if actor["current_location_id"] != place["id"] or great_circle_distance_km(
            actor["longitude"], actor["latitude"], state["longitude"], state["latitude"],
        ) > 0.02:
            raise ValueError("请本人先实际来到载具20米内，再选择登乘")
        return state

    @classmethod
    def _conflicts(cls, c, actor, at, row=None):
        from world_engine.group_journeys import GroupJourneyService
        from world_engine.journeys import JourneyService
        from world_engine.life import LifeActivityService
        from world_engine.player_steering import PlayerSteeringService

        ride = cls.active(c, actor['world_id'], actor['id'])
        if ride and (row is None or ride['id'] != row['id']):
            raise ValueError('本人已接受另一份共乘约定，请先处理原安排')
        LifeActivityService.assert_available(c, actor["id"])
        if PlayerSteeringService.active(c, actor["id"]):
            raise ValueError("请先停止本人方向移动")
        if c.execute("SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'", (actor["id"],)).fetchone():
            raise ValueError("请先结束本人已有移动")
        if actor["health"] < 1:
            raise ValueError("当前身体状态无法登乘")
        group = GroupJourneyService.active(c, actor["world_id"], actor["id"])
        if group and group["status"] != "paused":
            raise ValueError("请先暂停或结束本人原多人徒步安排")
        pair = JourneyService.active(c, actor["world_id"], actor["id"])
        if pair and pair["status"] != "paused":
            raise ValueError("请先暂停或结束本人原双人徒步安排")
        if c.execute("SELECT 1 FROM companion_sessions WHERE status='active' AND (leader_id=? OR companion_id=?)", (actor["id"], actor["id"])).fetchone():
            raise ValueError("请先处理原短时徒步同行")
        for table in ("shared_life_moments", "shared_preparation_plans"):
            if c.execute(f"SELECT 1 FROM {table} WHERE status='active' AND (proposer_id=? OR recipient_id=?)", (actor["id"], actor["id"])).fetchone():
                raise ValueError("本人已有正在执行的共同活动或筹备")

    @classmethod
    def _controller(cls, c, row, at):
        from world_engine.ride_handoff import RideHandoffService
        handed = RideHandoffService.controller(c, row, at)
        vehicle = handed[0] if handed else ContractService.vehicle_for(c, row["world_id"], row["controller_id"], row["vehicle_id"], at=at)
        if vehicle is None:
            raise ValueError("原控制者当前没有车主或有效租客的驾驶使用权")
        return cls._driver_state(c,row,at,handed[1]['id'] if handed else row['controller_id'],vehicle)

    @classmethod
    def _driver_state(cls,c,row,at,driver_id,vehicle):
        if not vehicle['is_available']:
            raise ValueError('原载具当前不可用')
        metadata = json.loads(vehicle["metadata_json"] or "{}")
        if not isinstance(metadata, dict):
            raise ValueError("载具资源定义无效")
        # 哥哥，无论旧定义还是新规格，都不能在消耗规则完成前免费启用。
        from world_engine.movement import assert_vehicle_fuel_accounted
        assert_vehicle_fuel_accounted(c, vehicle)
        driver = cls._person(c, row["world_id"], driver_id)
        if driver["health"] < 40 or driver["energy"] <= 20 or driver["satiety"] <= 30:
            raise ValueError("控制者需要先处理身体、用饭或休息")
        return vehicle, driver

    @classmethod
    def _commitments(cls, c, person, at, seconds, final):
        from world_engine.group_journeys import GroupJourneyService

        # 哥哥，复用逐人既有赴约、住宿和照护时限核验，不让乘车吞掉原承诺。
        GroupJourneyService._commitments(c, person, at, seconds, final)

    @classmethod
    def apply(cls, c, actor, at, choice):
        c.execute("SAVEPOINT ride_choice")
        try:
            result = cls._apply(c, actor, at, choice)
            c.execute("RELEASE ride_choice")
            return result
        except Exception:
            c.execute("ROLLBACK TO ride_choice")
            c.execute("RELEASE ride_choice")
            raise

    @classmethod
    def _result(cls, c, row, actor, at, event_id=None):
        result = cls.view(c, row, at, actor['id'])
        if event_id:
            # 哥哥，状态可以来自后来共同事件，但本人重试必须返回本人的原回执。
            result['event_id'] = event_id
        return result

    @classmethod
    def _apply(cls, c, actor, at, choice):
        from world_engine.intimacy import IntimacyService

        # 哥哥，入口重新读取真实人物，不能信任前端或模型递交的身份属性。
        actor = cls._person(c, actor["world_id"], actor["id"])
        cls.maintain(c, actor["world_id"], at)
        from world_engine.ride_handoff import HANDOFF_OPERATIONS, RideHandoffService
        if choice.operation in HANDOFF_OPERATIONS:
            result = RideHandoffService.apply(c, actor, at, choice)
            result['handoff_id'] = choice.handoff_id or (result.get('handoff') or {}).get('id')
            return result
        from world_engine.ride_recovery import RECOVERY_OPERATIONS, RideRecoveryService
        if choice.operation in RECOVERY_OPERATIONS:
            result = RideRecoveryService.apply(c, actor, at, choice)
            result['recovery_id'] = choice.recovery_id or (result.get('recovery') or {}).get('id')
            return result
        if choice.operation == "propose":
            cls.assert_available(c, actor)
            if actor["id"] not in choice.member_ids or choice.controller_id not in choice.member_ids:
                raise ValueError("提议者和控制者均须在原乘员名单内")
            if not at < choice.deadline_world_time <= at + timedelta(hours=24):
                raise ValueError("共乘期限须在现在之后且不超过24个世界小时")
            state = cls.state(c, actor["world_id"], choice.vehicle_id)
            if state["status"] != "parked":
                raise ValueError("该载具当前没有可新建共乘的安全停放状态")
            if len(choice.member_ids) > state["capacity"]:
                raise ValueError("乘员人数超过真实容量，容量包含控制者")
            if c.execute("SELECT 1 FROM vehicle_rides WHERE vehicle_id=? AND status IN ('pending','accepted','boarding','moving','arrived','blocked')", (choice.vehicle_id,)).fetchone():
                raise ValueError("该载具已有未结束的共乘安排")
            people = [cls._person(c, actor["world_id"], cid) for cid in choice.member_ids]
            for person in people:
                cls.assert_available(c, person)
                if person["id"] != actor["id"]:
                    IntimacyService._reachable(c, actor, person)
                    if person['space_layer'] != actor['space_layer'] or person['current_room_id'] != actor['current_room_id'] or great_circle_distance_km(person['longitude'], person['latitude'], actor['longitude'], actor['latitude']) > 0.02:
                        raise ValueError('新共乘提议需全员在同层真实20米交谈范围内，信笺权限不代替集合')
            row = dict(id=str(uuid4()), world_id=actor["world_id"], vehicle_id=choice.vehicle_id,
                       proposer_id=actor["id"], controller_id=choice.controller_id,
                       origin_location_id=state["current_location_id"], destination_location_id=choice.destination_location_id,
                       capacity=state["capacity"], statement=choice.statement, proposed_at=to_iso(at), deadline_at=to_iso(choice.deadline_world_time))
            vehicle, driver = cls._controller(c, row, at)
            cls._near_vehicle(c, row, actor)
            final = cls._place(c, row["world_id"], row["destination_location_id"], state["space_layer"], vehicle["movement_type"])
            if final["id"] == row["origin_location_id"]:
                raise ValueError("本程需要不同的真实目的地")
            for person in people:
                from world_engine.group_journeys import GroupJourneyService
                GroupJourneyService._knows(c, person, [row["origin_location_id"], final["id"]], at)
            cls._conflicts(c, actor, at)
            event = cls._event(c, row, actor, at, "proposed", reason=choice.statement, audience=choice.member_ids)
            c.execute(
                "INSERT INTO vehicle_rides(id,world_id,vehicle_id,proposer_id,controller_id,origin_location_id,destination_location_id,status,statement,proposed_at,deadline_at,capacity,terms_json,proposal_event_id,source_event_id) "
                "VALUES (?,?,?,?,?,?,?,'pending',?,?,?,?,?,?,?)",
                (row["id"], row["world_id"], row["vehicle_id"], row["proposer_id"], row["controller_id"], row["origin_location_id"], row["destination_location_id"], row["statement"], row["proposed_at"], row["deadline_at"], row["capacity"], "{}", event, event),
            )
            for index, person in enumerate(people):
                c.execute("INSERT INTO vehicle_ride_members(ride_id,character_id,ordinal,decision,decision_at,decision_event_id) VALUES (?,?,?,?,?,?)",
                          (row["id"], person["id"], index, "accepted" if person["id"] == actor["id"] else "pending", to_iso(at) if person["id"] == actor["id"] else None, event if person["id"] == actor["id"] else None))
            row = c.execute("SELECT * FROM vehicle_rides WHERE id=?", (row["id"],)).fetchone()
            terms = cls.terms(c, row)
            c.execute("UPDATE vehicle_rides SET terms_json=?,status=? WHERE id=?", (cls._json(terms), "accepted" if len(people) == 1 else "pending", row["id"]))
            # 哥哥，来源事件在同一事务内补齐刚冻结的全员条款，交付后不再改写。
            payload = json.loads(c.execute("SELECT payload_json FROM world_events WHERE id=?", (event,)).fetchone()[0])
            c.execute("UPDATE world_events SET payload_json=? WHERE id=?", (cls._json({**payload, "terms": terms}), event))
            return cls._result(c, c.execute("SELECT * FROM vehicle_rides WHERE id=?", (row["id"],)).fetchone(), actor, at, event)

        row = c.execute("SELECT * FROM vehicle_rides WHERE id=? AND world_id=?", (choice.ride_id, actor["world_id"])).fetchone()
        member = c.execute("SELECT * FROM vehicle_ride_members WHERE ride_id=? AND character_id=?", (choice.ride_id, actor["id"])).fetchone()
        if row is None or member is None:
            raise ValueError("本人不是这份真实共乘约定的成员")
        if from_iso(row['proposed_at']) > at or not c.execute(
            'SELECT 1 FROM world_events e JOIN character_event_knowledge k ON k.event_id=e.id AND k.world_id=e.world_id '
            'WHERE e.id=? AND e.world_id=? AND e.occurred_at<=? AND k.character_id=? AND k.learned_world_time<=?',
            (row['proposal_event_id'], row['world_id'], to_iso(at), actor['id'], to_iso(at)),
        ).fetchone():
            raise ValueError('本人目前尚未实际得知这份共乘提议，未来来源不能提前回应')
        op = choice.operation
        if op in {"accept", "decline"}:
            if row["status"] != "pending" or at >= from_iso(row["deadline_at"]):
                if op == "accept" and member["decision"] == "accepted" and row["status"] in {"accepted", "boarding"}:
                    return cls._result(c, row, actor, at, member['decision_event_id'])
                raise ValueError("原提议已不在可回应状态")
            if member["decision"] != "pending":
                if member["decision"] == ("accepted" if op == "accept" else "declined"):
                    return cls._result(c, row, actor, at, member['decision_event_id'])
                raise ValueError("已有本人原回应，不能覆盖拒绝或原同意")
            if op == "accept":
                cls._conflicts(c, actor, at, row)
                cls._near_vehicle(c, row, actor)
                from world_engine.group_journeys import GroupJourneyService
                GroupJourneyService._knows(c, actor, [row["origin_location_id"], row["destination_location_id"]], at)
                cls._controller(c, row, at)
            event = cls._event(c, row, actor, at, "accepted" if op == "accept" else "declined", terms=True, reason=choice.statement)
            c.execute("UPDATE vehicle_ride_members SET decision=?,decision_at=?,decision_event_id=? WHERE ride_id=? AND character_id=?", ("accepted" if op == "accept" else "declined", to_iso(at), event, row["id"], actor["id"]))
            if op == "decline":
                c.execute("UPDATE vehicle_rides SET status='declined',ended_at=?,source_event_id=?,reason=? WHERE id=?", (to_iso(at), event, choice.statement, row["id"]))
                c.execute("UPDATE vehicle_ride_members SET live=0 WHERE ride_id=?", (row["id"],))
            elif cls._accepted(c, row, at):
                c.execute("UPDATE vehicle_rides SET status='accepted',source_event_id=? WHERE id=?", (event, row["id"]))
        elif op == "board":
            if member["boarded_at"] and not member["disembarked_at"]:
                return cls._result(c, row, actor, at, member['board_event_id'])
            if row["status"] not in {"accepted", "boarding"} or not cls._accepted(c, row, at) or at >= from_iso(row["deadline_at"]):
                raise ValueError("需要原名单所有成员分别有效接受后才能登乘")
            if member["disembarked_at"]:
                raise ValueError("本人已离开本次共乘，不能复用旧条款重新登乘")
            cls._conflicts(c, actor, at, row)
            state = cls._near_vehicle(c, row, actor)
            if state["capacity"] != row["capacity"]:
                raise ValueError("载具容量与原条款不符，请结束旧约重新确认")
            count = c.execute("SELECT COUNT(*) FROM vehicle_ride_members WHERE ride_id=? AND boarded_at IS NOT NULL AND disembarked_at IS NULL", (row["id"],)).fetchone()[0]
            if count >= state["capacity"]:
                raise ValueError("载具已无空座")
            cls._controller(c, row, at)
            event = cls._event(c, row, actor, at, "boarded", terms=True, reason=choice.statement)
            c.execute("UPDATE vehicle_ride_members SET boarded_at=?,board_event_id=? WHERE ride_id=? AND character_id=?", (to_iso(at), event, row["id"], actor["id"]))
            c.execute("UPDATE vehicle_rides SET status='boarding',source_event_id=? WHERE id=?", (event, row["id"]))
            # 哥哥，近距登乘后位置属于真实载具；并非跟随者复制领队坐标。
            c.execute("UPDATE characters SET longitude=?,latitude=?,current_location_id=?,location_id=?,updated_at=? WHERE id=?", (state["longitude"], state["latitude"], state["current_location_id"], state["current_location_id"], to_iso(utc_now()), actor["id"]))
        elif op == "depart":
            if actor["id"] != row["controller_id"]:
                raise ValueError("只有原约定的控制者能驾驶出发")
            if row["status"] == "moving":
                event = c.execute("SELECT id FROM world_events WHERE world_id=? AND actor_id=? AND event_type='social.ride_departed' AND json_extract(payload_json,'$.ride_id')=? ORDER BY rowid LIMIT 1", (row['world_id'], actor['id'], row['id'])).fetchone()
                return cls._result(c, row, actor, at, event['id'] if event else row['source_event_id'])
            if row["status"] != "boarding" or not cls._accepted(c, row, at):
                raise ValueError("原共乘还没有取得所有成员的有效同意和登乘")
            if at >= from_iso(row["deadline_at"]):
                raise ValueError("原共乘期限已到")
            members = cls._members(c, row)
            for passenger in members:
                if not passenger["boarded_at"] or passenger["disembarked_at"] or not cls._source(c, row, passenger["board_event_id"], passenger["character_id"], "boarded", at, terms=True):
                    raise ValueError("原名单每个人均须亲自登乘且仍在车内")
                person = cls._person(c, row["world_id"], passenger["character_id"])
                cls._conflicts(c, person, at, row)
                cls._near_vehicle(c, row, person)
            vehicle, driver = cls._controller(c, row, at)
            state, _ = cls._safe_parked(c, row)
            if state['capacity'] != row['capacity'] or len(members) > state['capacity']:
                raise ValueError('当前真实容量与原冻结名单不符，不能超员出发')
            destination = cls._place(c, row["world_id"], row["destination_location_id"], state["space_layer"], vehicle["movement_type"])
            from world_engine.movement import MovementService
            movement = MovementService()._start_solo(
                c, world_id=row["world_id"], character_id=driver["id"],
                destination_longitude=destination["longitude"], destination_latitude=destination["latitude"],
                vehicle_id=row["vehicle_id"], world_time=at, record_log=False, ride_id=row["id"],
            )
            if movement.estimated_arrival_world > from_iso(row["deadline_at"]):
                raise ValueError("真实路程无法在原共乘期限内抵达")
            lease = c.execute("SELECT due_world_time FROM contract_fulfillments WHERE asset_id=? AND status='active'", (row["vehicle_id"],)).fetchone()
            if lease and movement.estimated_arrival_world > from_iso(lease["due_world_time"]):
                raise ValueError("租用期限不足以完成本程，不能带乘员驶入途中")
            seconds = (movement.estimated_arrival_world - at).total_seconds()
            for passenger in members:
                cls._commitments(c, cls._person(c, row["world_id"], passenger["character_id"]), at, seconds, destination)
            event = cls._event(c, row, actor, at, "departed", terms=True, reason=choice.statement, extra={"movement_id": movement.id})
            c.execute("UPDATE vehicle_rides SET status='moving',movement_id=?,started_at=?,source_event_id=? WHERE id=?", (movement.id, to_iso(at), event, row["id"]))
            c.execute("UPDATE vehicle_states SET status='moving',updated_at=? WHERE vehicle_id=?", (to_iso(at), row["vehicle_id"]))
        elif op == "request_stop":
            if row["status"] not in {"moving", "blocked"} or not member["boarded_at"] or member["disembarked_at"]:
                raise ValueError("本人当前没有需要安全停靠的在车行程")
            prior = c.execute("SELECT id FROM world_events WHERE world_id=? AND actor_id=? AND event_type='social.ride_stop_requested' AND json_extract(payload_json,'$.ride_id')=? ORDER BY rowid LIMIT 1", (row['world_id'], actor['id'], row['id'])).fetchone()
            if prior:
                return cls._result(c, row, actor, at, prior['id'])
            event = cls._event(c, row, actor, at, "stop_requested", reason=choice.statement)
            RideRecoveryService.invalidate(c, row, at, "本人新增安全停靠请求，恢复授权需要重新商量")
            c.execute("UPDATE vehicle_rides SET stop_requested=1,reason=?,source_event_id=? WHERE id=?", ("已请求在原安全目的地停靠，不在途中抛下乘员。", event, row["id"]))
        elif op == "disembark":
            if member["disembarked_at"]:
                return cls._result(c, row, actor, at, member['disembark_event_id'])
            if not member["boarded_at"]:
                raise ValueError("本人尚未登乘")
            cls._safe_parked(c, row)
            from world_engine.life import LifeActivityService
            if c.execute("SELECT 1 FROM aboard_life_activities b JOIN character_life_activities a ON a.id=b.activity_id "
                         "WHERE b.ride_id=? AND b.character_id=? AND a.status='running'", (row['id'],actor['id'])).fetchone():
                LifeActivityService.interrupt(c,actor['id'],at,"本人选择安全下车，结束车内休息或等待。")
            event = cls._event(c, row, actor, at, "disembarked", reason=choice.statement)
            RideRecoveryService.invalidate(c, row, at, "原名单有人实际下车，不能继续复用原完整恢复授权")
            c.execute("UPDATE vehicle_ride_members SET disembarked_at=?,disembark_event_id=?,live=0 WHERE ride_id=? AND character_id=?", (to_iso(at), event, row["id"], actor["id"]))
            occupied = c.execute("SELECT 1 FROM vehicle_ride_members WHERE ride_id=? AND boarded_at IS NOT NULL AND disembarked_at IS NULL", (row["id"],)).fetchone()
            if row["status"] in {"accepted", "boarding"}:
                cls._block(c, row, at, "原名单有成员下车，旧登乘不能用于新的出发")
            if not occupied:
                c.execute("UPDATE vehicle_rides SET status='ended',ended_at=?,source_event_id=? WHERE id=?", (to_iso(at), event, row["id"]))
                c.execute("UPDATE vehicle_ride_members SET live=0 WHERE ride_id=?", (row["id"],))
                c.execute("UPDATE vehicle_states SET status='parked',updated_at=? WHERE vehicle_id=?", (to_iso(at), row["vehicle_id"]))
        elif op == "end":
            if row["status"] in {"ended", "expired", "declined"}:
                prior = c.execute("SELECT id FROM world_events WHERE world_id=? AND actor_id=? AND event_type='social.ride_ended' AND json_extract(payload_json,'$.ride_id')=? ORDER BY rowid LIMIT 1", (row['world_id'], actor['id'], row['id'])).fetchone()
                return cls._result(c, row, actor, at, prior['id'] if prior else member['disembark_event_id'] or member['decision_event_id'])
            if row["status"] == "moving" or cls.aboard(c, actor["id"]):
                raise ValueError("请先请求安全停靠并各自下车，结束约定不能丢弃乘员")
            if actor["id"] != row["proposer_id"]:
                raise ValueError("仅提议者能结束未出发的整份共乘；本人可婉拒原邀请")
            if c.execute("SELECT 1 FROM vehicle_ride_members WHERE ride_id=? AND boarded_at IS NOT NULL AND disembarked_at IS NULL", (row["id"],)).fetchone():
                raise ValueError("仍有乘员在车内，请先各自安全下车")
            event = cls._event(c, row, actor, at, "ended", reason=choice.statement)
            c.execute("UPDATE vehicle_rides SET status='ended',ended_at=?,source_event_id=? WHERE id=?", (to_iso(at), event, row["id"]))
            c.execute("UPDATE vehicle_ride_members SET live=0 WHERE ride_id=?", (row["id"],))
            c.execute("UPDATE vehicle_states SET status='parked',updated_at=? WHERE vehicle_id=?", (to_iso(at), row["vehicle_id"]))
        if 'event' in locals():
            c.execute('UPDATE vehicle_rides SET source_event_id=? WHERE id=?', (event, row['id']))
        return cls._result(c, c.execute("SELECT * FROM vehicle_rides WHERE id=?", (row["id"],)).fetchone(), actor, at, event if 'event' in locals() else None)

    @classmethod
    def _block(cls, c, row, at, reason):
        from world_engine.ride_recovery import RideRecoveryService
        RideRecoveryService.invalidate(c, row, at, reason)
        from world_engine.ride_handoff import RideHandoffService
        RideHandoffService.invalidate(c,row,at,reason)
        source = row['source_event_id']
        if row['status'] != 'blocked' or row['reason'] != reason:
            actor = cls._person(c, row['world_id'], cls.effective_controller(c,row,at))
            source = cls._event(c, row, actor, at, 'blocked', reason=reason, extra={'reason': reason})
        c.execute("UPDATE vehicle_rides SET status='blocked',reason=?,source_event_id=? WHERE id=?", (reason, source, row["id"]))
        c.execute("UPDATE vehicle_states SET status='blocked',updated_at=? WHERE vehicle_id=?", (to_iso(at), row["vehicle_id"]))
        if row["movement_id"]:
            c.execute("UPDATE character_movements SET status='suspended',updated_at_world=? WHERE id=? AND status='moving'", (to_iso(at), row["movement_id"]))

    @classmethod
    def suspend_vehicle(cls, c, vehicle_id, at=None, reason="载具使用权结束，原位挂起并保留全部乘员"):
        row = c.execute("SELECT * FROM vehicle_rides WHERE vehicle_id=? AND status IN ('pending','accepted','boarding','moving','arrived','blocked')", (vehicle_id,)).fetchone()
        if row is None:
            return False
        at = at or from_iso(c.execute("SELECT w.current_time FROM worlds w WHERE id=?", (row["world_id"],)).fetchone()[0])
        cls._block(c, row, at, reason)
        return True

    @classmethod
    def maintain(cls, c, wid, at):
        from world_engine.ride_recovery import RideRecoveryService
        RideRecoveryService.maintain(c, wid, at)
        from world_engine.ride_handoff import RideHandoffService
        RideHandoffService.maintain(c,wid,at)
        rows = c.execute("SELECT * FROM vehicle_rides WHERE world_id=? AND status IN ('pending','accepted','boarding','moving')", (wid,)).fetchall()
        for row in rows:
            if row["status"] == "moving":
                movement = c.execute("SELECT * FROM character_movements WHERE id=?", (row["movement_id"],)).fetchone()
                if movement is None or movement["status"] != "moving":
                    cls._block(c, row, at, "原绑定路线失效，保留车辆和全部乘员的实际位置")
                elif not cls._accepted(c, row, at):
                    cls._block(c, row, at, "原逐人同意来源失效，保留载具和乘员位置等待处理")
                else:
                    try:
                        cls._controller(c, row, at)
                        RideHandoffService.assert_execution(c,row,at)
                        RideRecoveryService.assert_execution(c, row, at)
                    except (ValueError, LookupError) as exc:
                        cls._block(c, row, at, str(exc))
                continue
            if at >= from_iso(row["deadline_at"]):
                occupied = c.execute("SELECT 1 FROM vehicle_ride_members WHERE ride_id=? AND boarded_at IS NOT NULL AND disembarked_at IS NULL", (row["id"],)).fetchone()
                if occupied:
                    cls._block(c, row, at, "期限已到，保留登乘者等待各自安全下车")
                else:
                    c.execute("UPDATE vehicle_rides SET status='expired',ended_at=? WHERE id=?", (to_iso(at), row["id"]))
                    c.execute("UPDATE vehicle_ride_members SET live=0 WHERE ride_id=?", (row["id"],))

    @classmethod
    def project_movement(cls, c, movement, longitude, latitude, location_id, arrived, at, created_at):
        row = c.execute("SELECT * FROM vehicle_rides WHERE movement_id=? AND status='moving'", (movement["id"],)).fetchone()
        if row is None:
            return []
        distance = c.execute("SELECT distance_travelled_km FROM character_movements WHERE id=?", (movement["id"],)).fetchone()[0]
        delta = max(0.0, float(distance) - float(movement["distance_travelled_km"]))
        state = cls.state(c, row["world_id"], row["vehicle_id"])
        c.execute(
            "UPDATE vehicle_states SET longitude=?,latitude=?,current_location_id=?,status=?,distance_travelled_km=distance_travelled_km+?,updated_at=? WHERE vehicle_id=?",
            (longitude, latitude, location_id, "parked" if arrived else "moving", delta, to_iso(at), row["vehicle_id"]),
        )
        projected = []
        for member in cls._members(c, row):
            if not member["boarded_at"] or member["disembarked_at"]:
                continue
            c.execute(
                "UPDATE characters SET longitude=?,latitude=?,current_location_id=?,location_id=CASE WHEN ? THEN COALESCE(?,location_id) ELSE location_id END,updated_at=? WHERE id=? AND world_id=?",
                (longitude, latitude, location_id, int(arrived), row["destination_location_id"], to_iso(created_at), member["character_id"], row["world_id"]),
            )
            projected.append(member["character_id"])
        if arrived:
            # 哥哥，安全目的地取原冻结地点，重叠场景不应把靠站变成别处。
            c.execute("UPDATE vehicle_states SET current_location_id=? WHERE vehicle_id=?", (row["destination_location_id"], row["vehicle_id"]))
            c.execute("UPDATE characters SET current_location_id=?,location_id=? WHERE id IN (SELECT character_id FROM vehicle_ride_members WHERE ride_id=? AND boarded_at IS NOT NULL AND disembarked_at IS NULL)", (row["destination_location_id"], row["destination_location_id"], row["id"]))
            c.execute("UPDATE vehicle_rides SET status='arrived',arrived_at=? WHERE id=?", (to_iso(at), row["id"]))
        return projected

    @classmethod
    def record_arrival(cls, c, movement, at):
        row = c.execute("SELECT * FROM vehicle_rides WHERE movement_id=? AND status='arrived'", (movement["id"],)).fetchone()
        if row:
            actor = cls._person(c, row["world_id"], cls.effective_controller(c,row,at))
            event = cls._event(c, row, actor, at, "arrived", reason="载具按原路线实际抵达安全目的地，乘员仍需各自下车", extra={"movement_id": movement["id"]}, location_id=row['destination_location_id'])
            c.execute("UPDATE vehicle_rides SET source_event_id=? WHERE id=?", (event, row["id"]))

    @classmethod
    def view(cls, c, row, at, observer=None):
        from world_engine.society import SocietyService

        result = dict(row)
        result['ride_id'] = row['id']
        result.pop("terms_json", None)
        try:
            result['terms'] = json.loads(row['terms_json'])
        except (TypeError, ValueError):
            result['terms'] = cls.terms(c, row)
        result["members"] = [dict(m) for m in cls._members(c, row)]
        known_events = c.execute(
            "SELECT e.* FROM world_events e "
            + ("JOIN character_event_knowledge k ON k.event_id=e.id AND k.world_id=e.world_id AND k.character_id=? " if observer else "")
            + "WHERE e.world_id=? AND e.event_type LIKE 'social.ride_%' AND json_extract(e.payload_json,'$.ride_id')=? "
            "AND e.occurred_at<=? "
            + ("AND k.learned_world_time<=? " if observer else "")
            + "ORDER BY e.occurred_at,e.rowid",
            ([observer] if observer else []) + [row['world_id'], row['id'], to_iso(at)] + ([to_iso(at)] if observer else []),
        ).fetchall()
        known_ids = {event['id'] for event in known_events}
        operations = [event['event_type'].removeprefix('social.ride_') for event in known_events]
        for member in result['members']:
            # 哥哥，按本人当时已知来源重放，未来的接受、登乘、离开不能倒灌旧时刻。
            if not member['decision_at'] or from_iso(member['decision_at']) > at or member['decision_event_id'] not in known_ids:
                member.update(decision='pending', decision_at=None, decision_event_id=None)
            if not member['boarded_at'] or from_iso(member['boarded_at']) > at or member['board_event_id'] not in known_ids:
                member.update(boarded_at=None, board_event_id=None)
            if not member['disembarked_at'] or from_iso(member['disembarked_at']) > at or member['disembark_event_id'] not in known_ids:
                member.update(disembarked_at=None, disembark_event_id=None)
            member['live'] = int(not member['disembarked_at'])
        for member in result['members']:
            member['name'] = SocietyService.label(c, observer, member['character_id']) if observer else '同行成员'
        all_accepted = all(m['decision'] == 'accepted' for m in result['members'])
        status = 'accepted' if all_accepted else 'pending'
        if any(m['boarded_at'] for m in result['members']):
            status = 'boarding'
        if 'departed' in operations:
            status = 'arrived' if 'arrived' in operations else 'moving'
        for operation in operations:
            if operation in {'departed', 'recovery_resumed', 'handoff_taken'}:
                status = 'moving'
            elif operation == 'arrived':
                status = 'arrived'
            elif operation == 'blocked':
                status = 'blocked'
        if 'declined' in operations:
            status = 'declined'
        elif 'ended' in operations:
            status = 'ended'
        elif any(m['disembarked_at'] for m in result['members']):
            status = 'ended' if all(not m['boarded_at'] or m['disembarked_at'] for m in result['members']) else 'arrived' if 'arrived' in operations else 'blocked'
        elif at >= from_iso(row['deadline_at']) and status in {'pending', 'accepted', 'boarding'}:
            status = 'blocked' if any(m['boarded_at'] for m in result['members']) else 'expired'
        result['status'] = status
        result['source_event_id'] = known_events[-1]['id'] if known_events else row['proposal_event_id']
        result['event_id'] = result['source_event_id']
        if result['source_event_id'] != row['source_event_id']:
            result['reason'] = ''
        result['summary'] = f"共乘约定当前状态：{status}。{result['reason']}"
        if 'departed' not in operations:
            result['started_at'] = result['movement_id'] = None
        if 'arrived' not in operations:
            result['arrived_at'] = None
        if 'stop_requested' not in operations:
            result['stop_requested'] = 0
        if status not in {'ended', 'expired', 'declined'}:
            result['ended_at'] = None
        else:
            for member in result['members']:
                member['live'] = 0
        is_current = status not in {'ended', 'expired', 'declined'} and (observer is None or any(m['character_id'] == observer and m['live'] for m in result['members']))
        state = cls.state(c, row['world_id'], row['vehicle_id'])
        can_observe = observer is None
        if observer:
            person = cls._person(c, row['world_id'], observer)
            can_observe = bool(cls.aboard(c, observer)) or bool(
                person['space_layer'] == state['space_layer'] and not person['current_room_id']
                and great_circle_distance_km(person['longitude'], person['latitude'], state['longitude'], state['latitude']) <= 0.1
            )
        world_time = from_iso(c.execute('SELECT w.current_time FROM worlds w WHERE id=?', (row['world_id'],)).fetchone()[0])
        can_observe = can_observe and at >= world_time
        result["vehicle"] = dict(state) if is_current and can_observe else {'vehicle_id': row['vehicle_id'], 'capacity': row['capacity']}
        result['vehicle_name'] = cls._vehicle(c, row)['name']
        result["vehicle"]["name"] = result['vehicle_name']
        try:
            cls._safe_parked(c, row)
            result['safe_parked'] = bool(is_current and can_observe)
        except (ValueError, LookupError):
            result['safe_parked'] = False
        result['can_disembark'] = result['safe_parked'] and bool(observer and cls.aboard(c, observer))
        from world_engine.ride_recovery import RideRecoveryService
        result['recovery'] = RideRecoveryService.view(c, row, at, observer)
        result['effective_deadline_at'] = to_iso(cls.effective_deadline(c, row, at, observer))
        result['can_propose_recovery'] = bool(can_observe and is_current and RideRecoveryService.can_propose(c, row, at, observer))
        from world_engine.ride_handoff import RideHandoffService
        result['handoff'] = RideHandoffService.view(c,row,at,observer)
        result['effective_controller_id'] = cls.effective_controller(c,row,at,observer)
        result['can_propose_handoff'] = bool(can_observe and is_current and RideHandoffService.can_propose(c,row,at,observer))
        return result

    @classmethod
    def rows(cls, c, actor, at):
        return [cls.view(c, row, at, actor['id']) for row in c.execute(
            "SELECT r.* FROM vehicle_rides r JOIN vehicle_ride_members m ON m.ride_id=r.id "
            "JOIN world_events e ON e.id=r.proposal_event_id AND e.world_id=r.world_id "
            "JOIN character_event_knowledge k ON k.event_id=e.id AND k.world_id=r.world_id AND k.character_id=m.character_id "
            "WHERE r.world_id=? AND m.character_id=? AND r.proposed_at<=? AND e.occurred_at<=? AND k.learned_world_time<=? "
            "ORDER BY r.proposed_at DESC,r.id DESC LIMIT 16",
            (actor["world_id"], actor["id"], to_iso(at), to_iso(at), to_iso(at)),
        )]

    @classmethod
    def context(cls, c, actor, at):
        result = []
        for row in cls.rows(c, actor, at):
            if row['status'] in {'ended', 'expired', 'declined'}:
                continue
            own = next((member for member in row['members'] if member['character_id'] == actor['id']), None)
            if own and own.get('disembarked_at'):
                # 哥哥，本人已下车的经历留在历史；其他人未离车不再给本人续程权限或义务。
                continue
            # 哥哥，上下文保留一份完整冻结条款及逐人来源，界面别名和重复条款不再占提示预算。
            value = {key: row[key] for key in ('terms', 'status', 'proposal_event_id', 'source_event_id', 'safe_parked', 'can_disembark')}
            value['members'] = [
                {key: member[key] for key in ('character_id', 'name', 'decision', 'decision_at', 'decision_event_id', 'boarded_at', 'board_event_id', 'disembarked_at', 'disembark_event_id') if member[key] is not None}
                for member in row['members']
            ]
            value['vehicle'] = {key: row['vehicle'][key] for key in ('name', 'status', 'longitude', 'latitude', 'space_layer', 'current_location_id', 'registration_id', 'distance_travelled_km') if row['vehicle'].get(key) is not None}
            for key in ('movement_id', 'started_at', 'arrived_at', 'stop_requested', 'reason'):
                if row.get(key):
                    value[key] = row[key]
            if row.get('recovery'):
                recovery = row['recovery']
                # 哥哥，恢复条款只保留一份；界面别名不重复挤占本人选择的预算。
                compact = {key: recovery[key] for key in ('terms', 'status', 'source_event_id', 'proposed_at', 'can_accept', 'can_decline', 'can_withdraw', 'can_resume')}
                compact['members'] = [{key: member[key] for key in ('character_id', 'name', 'decision', 'decision_at', 'decision_event_id') if member.get(key) is not None} for member in recovery['members']]
                value['recovery'] = compact
            if row.get('can_propose_recovery') or row.get('recovery'):
                value['can_propose_recovery'] = row.get('can_propose_recovery', False)
                value['effective_deadline_at'] = row['effective_deadline_at']
            if row.get('handoff'):
                handoff=row['handoff']
                compact={key:handoff[key] for key in ('terms','status','source_event_id','authority','can_accept','can_decline','can_withdraw','can_authorize','can_take_control','can_revoke')}
                compact['members']=[{key:member[key] for key in ('character_id','name','decision','decision_at','decision_event_id') if member.get(key) is not None} for member in handoff['members']]
                value['handoff']=compact
            if row.get('can_propose_handoff') or row.get('handoff'):
                value['can_propose_handoff']=row['can_propose_handoff']
                value['effective_controller_id']=row['effective_controller_id']
                value['effective_deadline_at']=row['effective_deadline_at']
            result.append(value)
        return result

    @classmethod
    def boundary(cls, c, wid, at, result, satiety_rate=3):
        from world_engine.ride_recovery import RideRecoveryService
        for handoff in c.execute("SELECT deadline_at FROM vehicle_ride_handoffs WHERE world_id=? AND status IN ('pending','accepted','executed')",(wid,)):
            if from_iso(handoff[0])>at:result=min(result,from_iso(handoff[0]))
        for recovery in c.execute("SELECT deadline_at FROM vehicle_ride_recoveries WHERE world_id=? AND status IN ('pending','accepted')", (wid,)):
            if from_iso(recovery[0]) > at:
                result = min(result, from_iso(recovery[0]))
        for row in c.execute("SELECT * FROM vehicle_rides WHERE world_id=? AND status IN ('pending','accepted','boarding','moving')", (wid,)):
            deadline = cls.effective_deadline(c, row, at)
            if deadline > at:
                result = min(result, deadline)
            if row['status'] == 'moving':
                movement = c.execute('SELECT estimated_arrival_world FROM character_movements WHERE id=? AND status=\'moving\'', (row['movement_id'],)).fetchone()
                if movement and from_iso(movement[0]) > at:
                    result = min(result, from_iso(movement[0]))
                driver = cls._person(c, wid, cls.effective_controller(c,row,at))
                residual = c.execute('SELECT satiety_residual FROM character_state_accumulators WHERE character_id=?', (driver['id'],)).fetchone()
                if satiety_rate > 0 and driver['satiety'] > 30:
                    hours = (driver['satiety'] - 30 - (residual[0] if residual else 0)) / satiety_rate
                    if hours > 0:
                        result = min(result, at + timedelta(hours=hours))
                lease = c.execute("SELECT due_world_time FROM contract_fulfillments WHERE asset_id=? AND status='active'", (row['vehicle_id'],)).fetchone()
                if lease and from_iso(lease[0]) > at:
                    result = min(result, from_iso(lease[0]))
        return result

    @classmethod
    def effective_deadline(cls, c, row, at, observer=None):
        from world_engine.ride_handoff import RideHandoffService
        pending=RideHandoffService.effective_deadline(c,row,at,observer)
        if pending is not None:return pending
        from world_engine.ride_recovery import RideRecoveryService
        return RideRecoveryService.effective_deadline(c, row, at,observer)

    @classmethod
    def effective_controller(cls,c,row,at,observer=None):
        from world_engine.ride_handoff import RideHandoffService
        return RideHandoffService.driver_id(c,row,at,observer)

    @classmethod
    def control_source(cls,c,row,at):
        from world_engine.ride_handoff import RideHandoffService
        return RideHandoffService.control_source(c,row,at)
