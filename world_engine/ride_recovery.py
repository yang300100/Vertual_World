"""哥哥，途中恢复只续用原真实路线，每位在车者另作有限授权。"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import timedelta
from uuid import uuid4

from world_engine.geo import great_circle_distance_km
from world_engine.repository import from_iso, to_iso, utc_now


RECOVERY_OPERATIONS = {"propose_recovery", "accept_recovery", "decline_recovery", "withdraw_recovery", "resume"}
RECOVERY_SCHEMA = """
CREATE TABLE IF NOT EXISTS vehicle_ride_recoveries (
 id TEXT PRIMARY KEY,ride_id TEXT NOT NULL REFERENCES vehicle_rides(id) ON DELETE CASCADE,
 world_id TEXT NOT NULL REFERENCES worlds(id),epoch INTEGER NOT NULL CHECK(epoch>0),
 proposer_id TEXT NOT NULL REFERENCES characters(id),controller_id TEXT NOT NULL REFERENCES characters(id),
 movement_id TEXT NOT NULL REFERENCES character_movements(id),
 block_event_id TEXT NOT NULL REFERENCES world_events(id),
 status TEXT NOT NULL CHECK(status IN ('pending','accepted','declined','withdrawn','executed','expired','invalidated')),
 statement TEXT NOT NULL,proposed_at TEXT NOT NULL,deadline_at TEXT NOT NULL,
 terms_json TEXT NOT NULL,snapshot_json TEXT NOT NULL,proposal_event_id TEXT NOT NULL REFERENCES world_events(id),
 source_event_id TEXT NOT NULL REFERENCES world_events(id),executed_at TEXT,execution_event_id TEXT REFERENCES world_events(id),
 reason TEXT NOT NULL DEFAULT '',UNIQUE(ride_id,epoch)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_vehicle_ride_recovery_pending ON vehicle_ride_recoveries(ride_id)
 WHERE status IN ('pending','accepted');
CREATE INDEX IF NOT EXISTS idx_vehicle_ride_recovery_history ON vehicle_ride_recoveries(ride_id,epoch);
CREATE TABLE IF NOT EXISTS vehicle_ride_recovery_members (
 recovery_id TEXT NOT NULL REFERENCES vehicle_ride_recoveries(id) ON DELETE CASCADE,
 character_id TEXT NOT NULL REFERENCES characters(id),ordinal INTEGER NOT NULL,
 decision TEXT NOT NULL DEFAULT 'pending' CHECK(decision IN ('pending','accepted','declined')),
 decision_at TEXT,decision_event_id TEXT REFERENCES world_events(id),
 PRIMARY KEY(recovery_id,character_id),UNIQUE(recovery_id,ordinal)
);
"""


class RideRecoveryService:
    @staticmethod
    def install(c):
        for statement in RECOVERY_SCHEMA.split(";"):
            if statement.strip():
                c.execute(statement)

    @staticmethod
    def _rides():
        from world_engine.rides import RideService
        return RideService

    @staticmethod
    def _json(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _members(c, row):
        return c.execute("SELECT * FROM vehicle_ride_recovery_members WHERE recovery_id=? ORDER BY ordinal", (row["id"],)).fetchall()

    @classmethod
    def _terms(cls, row):
        return json.loads(row["terms_json"])

    @staticmethod
    def _route(movement):
        return {key: movement[key] for key in (
            "id", "world_id", "character_id", "vehicle_id", "movement_type", "speed_kmh",
            "origin_longitude", "origin_latitude", "destination_longitude", "destination_latitude",
            "destination_location_id", "total_distance_km", "route_json", "navigation_dataset_id", "started_at_world",
        )}

    @classmethod
    def remaining(cls, movement):
        """哥哥，按真实各段速度扣去已走距离，暂停多久都不吃掉余程。"""
        distance = float(movement["distance_travelled_km"])
        total = float(movement["total_distance_km"])
        if not math.isfinite(distance) or not math.isfinite(total) or distance < 0 or total <= 0:
            raise ValueError("原路线进度不构成可恢复的途中余程")
        route = json.loads(movement["route_json"] or "[]")
        if isinstance(route, dict) and route.get("edges"):
            edges = route["edges"]
            if not route.get("polyline") or len(route["polyline"]) != len(edges) + 1:
                raise ValueError("原路线折线与分段不一致，不能重新猜测路线")
            passed = 0.0
            hours = 0.0
            for edge in edges:
                length, speed = float(edge["length_km"]), float(edge["speed_kmh"])
                if not math.isfinite(length) or length < 0 or not math.isfinite(speed) or speed <= 0:
                    raise ValueError("原路线分段长度或速度无效")
                hours += max(0.0, passed + length - max(distance, passed)) / speed
                passed += length
            # 哥哥，序列化路线总长允许原有千米舍入误差，不允许另造一段余程。
            if abs(passed - total) > max(0.002, 0.0006 + len(edges) * 0.000051):
                raise ValueError("原路线总长度与冻结行程不一致")
            index = int(movement.get('route_index', 0)) if isinstance(movement, dict) else int(movement['route_index'])
            if not 0 <= index < len(edges) or distance >= passed:
                raise ValueError("原路线分段进度已不构成途中余程")
            start = sum(float(edge['length_km']) for edge in edges[:index])
            end = start + float(edges[index]['length_km'])
            if distance < start - 1e-8 or distance > end + 1e-8:
                raise ValueError("原路线所在分段与已走距离不一致")
            total = passed
        elif route == []:
            speed = float(movement["speed_kmh"])
            if not math.isfinite(speed) or speed <= 0:
                raise ValueError("原路线速度无效")
            hours = (total - distance) / speed
        else:
            raise ValueError("原路线无法核验，不能用直线替代恢复")
        if not math.isfinite(hours) or hours <= 0:
            raise ValueError("原路线没有可实际续行的余程")
        return total - distance, hours * 3600

    @classmethod
    def _block_event(cls, c, ride, at):
        event = c.execute(
            "SELECT * FROM world_events WHERE world_id=? AND event_type='social.ride_blocked' "
            "AND json_extract(payload_json,'$.ride_id')=? AND occurred_at<=? ORDER BY occurred_at DESC,rowid DESC LIMIT 1",
            (ride["world_id"], ride["id"], to_iso(at)),
        ).fetchone()
        if event is None:
            raise ValueError("缺少本轮真实挂起来源")
        return event

    @classmethod
    def _snapshot(cls, c, ride, at):
        rides = cls._rides()
        if ride["status"] != "blocked" or not ride["movement_id"]:
            raise ValueError("只有原路线已实际出发并挂起的途中共乘才能恢复")
        movement = c.execute("SELECT * FROM character_movements WHERE id=? AND world_id=?", (ride["movement_id"], ride["world_id"])).fetchone()
        if movement is None or movement["status"] != "suspended" or movement["character_id"] != ride["controller_id"] or movement["vehicle_id"] != ride["vehicle_id"]:
            raise ValueError("原真实载具路线并未处于可恢复的挂起状态")
        if from_iso(movement["updated_at_world"]) > at:
            raise ValueError("未来路线进度不能用于当前恢复")
        if not rides._accepted(c, ride, at):
            raise ValueError("原逐人同意来源已失效，不能用恢复覆盖旧约定")
        state = rides.state(c, ride["world_id"], ride["vehicle_id"])
        if state["status"] != "blocked" or state["capacity"] != ride["capacity"]:
            raise ValueError("原车辆物理状态或真实容量已经改变")
        occupants = rides._members(c, ride)
        for member in occupants:
            if not member["live"] or not member["boarded_at"] or member["disembarked_at"] or not rides._source(c, ride, member["board_event_id"], member["character_id"], "boarded", at, terms=True):
                raise ValueError("原完整名单必须仍各自有效登乘且没有离车")
            person = rides._person(c, ride["world_id"], member["character_id"])
            if person["space_layer"] != state["space_layer"] or person["current_room_id"] or person["current_fixture_id"] or great_circle_distance_km(
                person["longitude"], person["latitude"], state["longitude"], state["latitude"],
            ) > 0.00001:
                raise ValueError("在车成员与车辆的真实位置或空间层不一致")
            rides._conflicts(c, person, at, ride)
        from world_engine.movement import MovementService
        _, longitude, latitude, arrived, _ = MovementService()._advance_one(movement, 0)
        if arrived or great_circle_distance_km(longitude, latitude, state["longitude"], state["latitude"]) > 0.00001:
            raise ValueError("车辆实际位置不在原挂起路线进度上")
        block = cls._block_event(c, ride, at)
        stop_events = [row[0] for row in c.execute(
            "SELECT id FROM world_events WHERE world_id=? AND event_type='social.ride_stop_requested' "
            "AND json_extract(payload_json,'$.ride_id')=? AND occurred_at<=? ORDER BY occurred_at,rowid",
            (ride["world_id"], ride["id"], to_iso(at)),
        )]
        snapshot = {
            "block_event_id": block["id"], "route": cls._route(movement),
            "distance_travelled_km": movement["distance_travelled_km"], "route_index": movement["route_index"],
            "vehicle": {key: state[key] for key in ("vehicle_id", "world_id", "capacity", "longitude", "latitude", "space_layer", "current_location_id", "distance_travelled_km", "registration_id")},
            "members": [{key: m[key] for key in ("character_id", "ordinal", "decision_event_id", "boarded_at", "board_event_id")} for m in occupants],
            "stop_requested": bool(ride["stop_requested"]), "stop_event_ids": stop_events,
        }
        # 哥哥，老存档没有交接来源时保持原快照；换过驾驶者才冻结真实接管回执。
        control = rides.control_source(c, ride, at)
        if control is not None:
            snapshot['control_source'] = control
        return snapshot, movement

    @classmethod
    def _ready(cls, c, ride, recovery, at):
        snapshot, movement = cls._snapshot(c, ride, at)
        if snapshot != json.loads(recovery["snapshot_json"]):
            raise ValueError("原路线、位置、挂起轮次、乘员或停止请求已变化，请重新商量恢复")
        if not cls._coherent(ride, recovery):
            raise ValueError("恢复冻结条款与真实来源快照不一致")
        rides = cls._rides()
        vehicle, _ = rides._controller(c, ride, at)
        if recovery['controller_id'] != rides.effective_controller(c, ride, at):
            raise ValueError('当前真实驾驶者已变化，旧恢复不能替另一人续程')
        state = rides.state(c, ride["world_id"], ride["vehicle_id"])
        final = rides._place(c, ride["world_id"], ride["destination_location_id"], state["space_layer"], vehicle["movement_type"])
        _, seconds = cls.remaining(movement)
        if at + timedelta(seconds=seconds) > from_iso(recovery["deadline_at"]):
            raise ValueError("剩余真实路线无法在新期限内完成")
        lease = c.execute("SELECT due_world_time FROM contract_fulfillments WHERE asset_id=? AND status='active'", (ride["vehicle_id"],)).fetchone()
        if lease and at + timedelta(seconds=seconds) > from_iso(lease[0]):
            raise ValueError("实际租用期限不足以完成剩余路线，不能自动延租")
        from world_engine.ride_handoff import RideHandoffService
        control = RideHandoffService.control_row(c, ride, at)
        if control and from_iso(recovery['deadline_at']) > from_iso(control['deadline_at']):
            raise ValueError('恢复期限不能越过原持有人授予的有限驾驶期限')
        from world_engine.group_journeys import GroupJourneyService
        for member in rides._members(c, ride):
            person = rides._person(c, ride["world_id"], member["character_id"])
            GroupJourneyService._knows(c, person, [final["id"]], at)
            rides._commitments(c, person, at, seconds, final)
        return movement, seconds

    @classmethod
    def _record(cls, c, ride, row, actor, at, operation, statement):
        return cls._rides()._event(c, ride, actor, at, "recovery_" + operation, reason=statement,
            extra={"recovery_id": row["id"], "recovery_epoch": row["epoch"], "recovery_terms": cls._terms(row)})

    @classmethod
    def _source(cls, c, ride, row, eid, cid, operation, at):
        event = c.execute("SELECT * FROM world_events WHERE id=? AND world_id=?", (eid, row["world_id"])).fetchone()
        if event is None:
            return False
        try:
            payload = json.loads(event["payload_json"])
            return bool(cls._coherent(ride, row) and event["actor_id"] == cid and event["target_id"] is None and event["event_type"] == "social.ride_recovery_" + operation
                and from_iso(row["proposed_at"]) <= from_iso(event["occurred_at"]) <= at
                and from_iso(event["occurred_at"]) < from_iso(row["deadline_at"])
                and payload.get("ride_id") == ride["id"] and payload.get("vehicle_id") == ride["vehicle_id"]
                and payload.get("recovery_id") == row["id"] and payload.get("recovery_epoch") == row["epoch"]
                and payload.get("recovery_terms") == cls._terms(row)
                and c.execute("SELECT 1 FROM character_event_knowledge WHERE character_id=? AND event_id=? AND world_id=? AND source_kind='experienced' AND learned_world_time<=?", (cid, eid, row["world_id"], to_iso(at))).fetchone())
        except (ValueError, TypeError, KeyError):
            return False

    @classmethod
    def _coherent(cls, ride, row):
        terms = cls._terms(row)
        snapshot = json.loads(row['snapshot_json'])
        expected = {'recovery_id': row['id'], 'ride_id': ride['id'], 'world_id': ride['world_id'],
            'epoch': row['epoch'], 'proposer_id': row['proposer_id'], 'controller_id': row['controller_id'],
            'vehicle_id': ride['vehicle_id'], 'movement_id': ride['movement_id'], 'block_event_id': row['block_event_id'],
            'destination_location_id': ride['destination_location_id'], 'deadline_at': row['deadline_at'],
            'snapshot_sha256': hashlib.sha256(cls._json(snapshot).encode()).hexdigest()}
        return all(terms.get(key) == value for key, value in expected.items()) and terms['member_ids'] == [m['character_id'] for m in snapshot['members']]

    @classmethod
    def _accepted(cls, c, ride, row, at):
        try:
            terms = cls._terms(row)
            members = cls._members(c, row)
            return terms["member_ids"] == [m["character_id"] for m in members] and all(
                m["decision"] == "accepted" and m["decision_at"] and cls._source(c, ride, row, m["decision_event_id"], m["character_id"],
                    "proposed" if m["character_id"] == row["proposer_id"] else "accepted", at)
                and c.execute("SELECT occurred_at FROM world_events WHERE id=?", (m["decision_event_id"],)).fetchone()[0] == m["decision_at"]
                for m in members)
        except (ValueError, TypeError, KeyError):
            return False

    @classmethod
    def invalidate(cls, c, ride, at, reason):
        for row in c.execute("SELECT * FROM vehicle_ride_recoveries WHERE ride_id=? AND status IN ('pending','accepted')", (ride["id"],)).fetchall():
            actor = cls._rides()._person(c, ride['world_id'], cls._rides().effective_controller(c, ride, at))
            event = cls._record(c, ride, row, actor, at, 'invalidated', reason)
            c.execute("UPDATE vehicle_ride_recoveries SET status='invalidated',reason=?,source_event_id=? WHERE id=?", (reason, event, row['id']))

    @classmethod
    def maintain(cls, c, wid, at):
        for row in c.execute("SELECT * FROM vehicle_ride_recoveries WHERE world_id=? AND status IN ('pending','accepted')", (wid,)).fetchall():
            ride = c.execute("SELECT * FROM vehicle_rides WHERE id=?", (row["ride_id"],)).fetchone()
            if at >= from_iso(row["deadline_at"]):
                c.execute("UPDATE vehicle_ride_recoveries SET status='expired',reason='新的有限恢复期限已过' WHERE id=?", (row["id"],))
            else:
                try:
                    snapshot, _ = cls._snapshot(c, ride, at)
                    if snapshot != json.loads(row["snapshot_json"]):
                        raise ValueError("原挂起路线或成员状态已经变化")
                except (ValueError, LookupError, TypeError):
                    cls.invalidate(c, ride, at, "原挂起路线或成员状态已经变化，需要各人重新决定")

    @classmethod
    def executed(cls, c, ride, at, observer=None):
        rows = c.execute("SELECT * FROM vehicle_ride_recoveries WHERE ride_id=? AND status='executed' AND executed_at<=? ORDER BY epoch DESC", (ride["id"], to_iso(at))).fetchall()
        for row in rows:
            if row["movement_id"] != ride["movement_id"] or not cls._accepted(c, ride, row, at) or not cls._source(c, ride, row, row["execution_event_id"], row["controller_id"], "resumed", at):
                return None
            if observer and not c.execute("SELECT 1 FROM character_event_knowledge WHERE character_id=? AND event_id=? AND world_id=? AND learned_world_time<=?", (observer, row["execution_event_id"], row["world_id"], to_iso(at))).fetchone():
                continue
            return row
        return None

    @classmethod
    def effective_deadline(cls, c, ride, at, observer=None):
        for pending in c.execute("SELECT * FROM vehicle_ride_recoveries WHERE ride_id=? AND status='accepted' AND proposed_at<=? ORDER BY epoch DESC", (ride['id'], to_iso(at))):
            try:
                snapshot, _ = cls._snapshot(c, ride, at)
                if snapshot == json.loads(pending['snapshot_json']) and cls._accepted(c, ride, pending, at):
                    if not observer or c.execute("SELECT 1 FROM character_event_knowledge WHERE character_id=? AND event_id=? AND world_id=? AND learned_world_time<=?", (observer, pending['source_event_id'], ride['world_id'], to_iso(at))).fetchone():
                        return from_iso(pending['deadline_at'])
            except (ValueError, LookupError):
                continue
        from world_engine.ride_handoff import RideHandoffService
        governing = RideHandoffService.governing(c, ride, at, observer)
        if governing is not None:
            return from_iso(governing[1]['deadline_at'])
        row = cls.executed(c, ride, at, observer)
        return from_iso(row["deadline_at"] if row else ride["deadline_at"])

    @classmethod
    def assert_execution(cls, c, ride, at):
        # 哥哥，曾恢复过的行程不能在新授权来源失效后退回旧期限偷偷继续。
        from world_engine.ride_handoff import RideHandoffService
        governing = RideHandoffService.governing(c, ride, at)
        if governing is not None and governing[0] == 'handoff':
            return
        latest = c.execute("SELECT * FROM vehicle_ride_recoveries WHERE ride_id=? AND status='executed' ORDER BY epoch DESC LIMIT 1", (ride["id"],)).fetchone()
        if latest is None:
            return
        valid = cls.executed(c, ride, at)
        if valid is None or valid["id"] != latest["id"]:
            raise ValueError("本轮恢复的逐人授权或执行来源已失效")
        if valid['controller_id'] != cls._rides().effective_controller(c, ride, at):
            raise ValueError('本轮恢复与当前实际驾驶者不一致')
        if json.loads(valid['snapshot_json']).get('control_source') != cls._rides().control_source(c, ride, at):
            raise ValueError('本轮恢复的有限驾驶来源已经变化')
        movement = c.execute("SELECT * FROM character_movements WHERE id=?", (ride["movement_id"],)).fetchone()
        snapshot = json.loads(valid["snapshot_json"])
        if movement is None or cls._route(movement) != snapshot["route"] or movement["distance_travelled_km"] < snapshot["distance_travelled_km"]:
            raise ValueError("恢复后的原路线或真实进度已失效")
        cls.remaining(movement)
        if at > from_iso(valid["deadline_at"]):
            raise ValueError("本轮恢复的有限期限已过，保留乘员等待处理")

    @classmethod
    def apply(cls, c, actor, at, choice):
        rides = cls._rides()
        ride = c.execute("SELECT * FROM vehicle_rides WHERE id=? AND world_id=?", (choice.ride_id, actor["world_id"])).fetchone()
        member = c.execute("SELECT * FROM vehicle_ride_members WHERE ride_id=? AND character_id=?", (choice.ride_id, actor["id"])).fetchone()
        if ride is None or member is None or not member["boarded_at"] or member["disembarked_at"]:
            raise ValueError("只有原名单中仍实际在车的本人能够选择恢复")
        cls.maintain(c, ride["world_id"], at)
        if choice.operation == "propose_recovery":
            if c.execute("SELECT 1 FROM vehicle_ride_handoffs WHERE ride_id=? AND status IN ('pending','accepted')", (ride['id'],)).fetchone():
                raise ValueError('已有待处理的驾驶交接方案，请先明确处理原方案')
            snapshot, movement = cls._snapshot(c, ride, at)
            if not c.execute("SELECT 1 FROM character_event_knowledge WHERE character_id=? AND event_id=? AND world_id=? AND learned_world_time<=?", (actor['id'], snapshot['block_event_id'], ride['world_id'], to_iso(at))).fetchone():
                raise ValueError("本人尚不知本轮真实挂起来源")
            if not at < choice.deadline_world_time <= at + timedelta(hours=24):
                raise ValueError("新的恢复期限须在现在之后且不超过24个世界小时")
            if c.execute("SELECT 1 FROM vehicle_ride_recoveries WHERE ride_id=? AND status IN ('pending','accepted')", (ride["id"],)).fetchone():
                raise ValueError("本轮已有待处理恢复方案，不能替换别人的原回应")
            distance, seconds = cls.remaining(movement)
            epoch = c.execute("SELECT COALESCE(MAX(epoch),0)+1 FROM vehicle_ride_recoveries WHERE ride_id=?", (ride["id"],)).fetchone()[0]
            rid = str(uuid4())
            terms = {"recovery_id": rid, "ride_id": ride["id"], "world_id": ride['world_id'], 'proposer_id': actor['id'], "epoch": epoch, "vehicle_id": ride["vehicle_id"],
                "controller_id": rides.effective_controller(c, ride, at), "movement_id": movement["id"], "block_event_id": snapshot["block_event_id"],
                "destination_location_id": ride["destination_location_id"], "deadline_at": to_iso(choice.deadline_world_time),
                "member_ids": [m["character_id"] for m in snapshot["members"]], "remaining_distance_km": distance,
                "remaining_seconds": seconds, "route_sha256": hashlib.sha256(movement["route_json"].encode()).hexdigest(),
                "distance_travelled_km": movement["distance_travelled_km"], "route_index": movement["route_index"],
                "stop_requested": snapshot["stop_requested"], "stop_policy": rides.terms(c, ride)["stop_policy"], "statement": choice.statement,
                'snapshot_sha256': hashlib.sha256(cls._json(snapshot).encode()).hexdigest()}
            row = {"id": rid, "ride_id": ride["id"], "world_id": ride["world_id"], "epoch": epoch,
                "proposer_id": actor["id"], "controller_id": terms['controller_id'], "movement_id": movement["id"],
                "block_event_id": snapshot["block_event_id"], "deadline_at": terms["deadline_at"], "proposed_at": to_iso(at),
                "terms_json": cls._json(terms), "snapshot_json": cls._json(snapshot)}
            cls._ready(c, ride, row, at)
            event = cls._record(c, ride, row, actor, at, "proposed", choice.statement)
            c.execute("INSERT INTO vehicle_ride_recoveries(id,ride_id,world_id,epoch,proposer_id,controller_id,movement_id,block_event_id,status,statement,proposed_at,deadline_at,terms_json,snapshot_json,proposal_event_id,source_event_id) VALUES (?,?,?,?,?,?,?,?,'pending',?,?,?,?,?,?,?)",
                (rid, ride["id"], ride["world_id"], epoch, actor["id"], terms['controller_id'], movement["id"], snapshot["block_event_id"], choice.statement, to_iso(at), terms["deadline_at"], row["terms_json"], row["snapshot_json"], event, event))
            for ordinal, cid in enumerate(terms["member_ids"]):
                own = cid == actor["id"]
                c.execute("INSERT INTO vehicle_ride_recovery_members VALUES (?,?,?,?,?,?)", (rid, cid, ordinal, "accepted" if own else "pending", to_iso(at) if own else None, event if own else None))
            if len(terms["member_ids"]) == 1:
                c.execute("UPDATE vehicle_ride_recoveries SET status='accepted' WHERE id=?", (rid,))
        else:
            row = c.execute("SELECT * FROM vehicle_ride_recoveries WHERE id=? AND ride_id=? AND world_id=?", (choice.recovery_id, ride["id"], actor["world_id"])).fetchone()
            own = c.execute("SELECT * FROM vehicle_ride_recovery_members WHERE recovery_id=? AND character_id=?", (choice.recovery_id, actor["id"])).fetchone()
            if row is None or own is None:
                raise ValueError("本人不属于该恢复轮次，不能借旧编号恢复")
            if not cls._source(c, ride, row, row['proposal_event_id'], row['proposer_id'], 'proposed', at) or not c.execute("SELECT 1 FROM character_event_knowledge WHERE character_id=? AND event_id=? AND world_id=? AND learned_world_time<=?", (actor['id'], row['proposal_event_id'], ride['world_id'], to_iso(at))).fetchone():
                raise ValueError("本人尚无当前可核验的恢复提议来源")
            operation = choice.operation
            if operation in {"accept_recovery", "decline_recovery"}:
                decision = "accepted" if operation == "accept_recovery" else "declined"
                if own["decision"] == decision:
                    own_operation = 'proposed' if actor['id'] == row['proposer_id'] else decision
                    if not own['decision_at'] or from_iso(own['decision_at']) > at or not cls._source(c, ride, row, own['decision_event_id'], actor['id'], own_operation, at):
                        raise ValueError("本人原回应来源尚未在当前时刻有效")
                    return rides._result(c, ride, actor, at, own["decision_event_id"])
                if own["decision"] != "pending" or row["status"] != "pending" or at >= from_iso(row["deadline_at"]):
                    raise ValueError("原恢复方案已不在本人可回应状态，不能覆盖旧拒绝")
                if decision == "accepted":
                    cls._ready(c, ride, row, at)
                event = cls._record(c, ride, row, actor, at, decision, choice.statement)
                c.execute("UPDATE vehicle_ride_recovery_members SET decision=?,decision_at=?,decision_event_id=? WHERE recovery_id=? AND character_id=?", (decision, to_iso(at), event, row["id"], actor["id"]))
                status = "declined" if decision == "declined" else "accepted" if cls._accepted(c, ride, row, at) else "pending"
                c.execute("UPDATE vehicle_ride_recoveries SET status=?,source_event_id=? WHERE id=?", (status, event, row["id"]))
            elif operation == "withdraw_recovery":
                if actor["id"] != row["proposer_id"] and own['decision'] != 'accepted':
                    raise ValueError("只有提议者或本人已经接受的成员能够撤回本人的恢复授权")
                if row["status"] == "withdrawn":
                    if not cls._source(c, ride, row, row['source_event_id'], actor['id'], 'withdrawn', at):
                        raise ValueError("本轮已由另一名成员撤回，不能借用他人的回执")
                    return rides._result(c, ride, actor, at, row["source_event_id"])
                if row["status"] not in {"pending", "accepted"}:
                    raise ValueError("本轮恢复已不能撤回")
                event = cls._record(c, ride, row, actor, at, "withdrawn", choice.statement)
                c.execute("UPDATE vehicle_ride_recoveries SET status='withdrawn',source_event_id=? WHERE id=?", (event, row["id"]))
            elif operation == "resume":
                if actor["id"] != row["controller_id"]:
                    raise ValueError("只有原控制者能在全员新授权后另行恢复驾驶")
                if row["status"] == "executed":
                    if not cls._source(c, ride, row, row['execution_event_id'], actor['id'], 'resumed', at):
                        raise ValueError("本轮原恢复执行来源尚未有效")
                    return rides._result(c, ride, actor, at, row["execution_event_id"])
                if row["status"] != "accepted" or not cls._accepted(c, ride, row, at):
                    raise ValueError("须原在车名单全员分别有效接受本轮恢复条款")
                movement, seconds = cls._ready(c, ride, row, at)
                event = cls._record(c, ride, row, actor, at, "resumed", choice.statement)
                c.execute("UPDATE character_movements SET status='moving',estimated_arrival_world=?,updated_at_world=?,updated_at=? WHERE id=? AND status='suspended'", (to_iso(at + timedelta(seconds=seconds)), to_iso(at), to_iso(utc_now()), movement["id"]))
                c.execute("UPDATE vehicle_ride_recoveries SET status='executed',executed_at=?,execution_event_id=?,source_event_id=? WHERE id=?", (to_iso(at), event, event, row["id"]))
                c.execute("UPDATE vehicle_rides SET status='moving',reason='',source_event_id=? WHERE id=?", (event, ride["id"]))
                c.execute("UPDATE vehicle_states SET status='moving',updated_at=? WHERE vehicle_id=?", (to_iso(at), ride["vehicle_id"]))
            else:
                raise ValueError("未知恢复操作")
        c.execute("UPDATE vehicle_rides SET source_event_id=? WHERE id=?", (event, ride["id"]))
        return rides._result(c, c.execute("SELECT * FROM vehicle_rides WHERE id=?", (ride["id"],)).fetchone(), actor, at, event)

    @classmethod
    def view(cls, c, ride, at, observer=None):
        from world_engine.society import SocietyService
        known = c.execute(
            "SELECT r.* FROM vehicle_ride_recoveries r JOIN world_events e ON e.id=r.proposal_event_id AND e.world_id=r.world_id "
            + ("JOIN character_event_knowledge k ON k.event_id=e.id AND k.world_id=e.world_id AND k.character_id=? " if observer else "")
            + "WHERE r.ride_id=? AND r.proposed_at<=? AND e.occurred_at<=? "
            + ("AND k.learned_world_time<=? " if observer else "") + "ORDER BY r.epoch DESC LIMIT 1",
            ([observer] if observer else []) + [ride["id"], to_iso(at), to_iso(at)] + ([to_iso(at)] if observer else []),
        ).fetchone()
        if known is None:
            return None
        if not cls._source(c, ride, known, known['proposal_event_id'], known['proposer_id'], 'proposed', at):
            return None
        events = c.execute(
            "SELECT e.* FROM world_events e "
            + ("JOIN character_event_knowledge k ON k.event_id=e.id AND k.world_id=e.world_id AND k.character_id=? " if observer else "")
            + "WHERE e.world_id=? AND json_extract(e.payload_json,'$.recovery_id')=? AND e.event_type LIKE 'social.ride_recovery_%' AND e.occurred_at<=? "
            + ("AND k.learned_world_time<=? " if observer else "") + "ORDER BY e.occurred_at,e.rowid",
            ([observer] if observer else []) + [ride["world_id"], known["id"], to_iso(at)] + ([to_iso(at)] if observer else []),
        ).fetchall()
        ids = {event["id"] for event in events}
        members = [dict(m) for m in cls._members(c, known)]
        for member in members:
            member["name"] = SocietyService.label(c, observer, member["character_id"]) if observer else "在车成员"
            operation = 'proposed' if member['character_id'] == known['proposer_id'] else member['decision']
            if not member["decision_at"] or from_iso(member["decision_at"]) > at or member["decision_event_id"] not in ids or not cls._source(c, ride, known, member['decision_event_id'], member['character_id'], operation, at):
                member.update(decision="pending", decision_at=None, decision_event_id=None)
        operations = [event["event_type"].removeprefix("social.ride_recovery_") for event in events]
        status = "accepted" if all(m["decision"] == "accepted" for m in members) else "pending"
        for operation in operations:
            if operation in {"declined", "withdrawn", "invalidated"}:
                status = operation
            elif operation == "resumed":
                status = "executed"
        if status in {"pending", "accepted"} and at >= from_iso(known["deadline_at"]):
            status = "expired"
        result = {key: known[key] for key in ("id", "ride_id", "epoch", "proposer_id", "controller_id", "movement_id", "deadline_at", "proposed_at")}
        result.update(status=status, terms=cls._terms(known), members=members, source_event_id=events[-1]["id"] if events else known["proposal_event_id"],
            can_accept=False, can_decline=False, can_withdraw=False, can_resume=False)
        if observer:
            own = next((m for m in members if m["character_id"] == observer), None)
            actual_now = from_iso(c.execute("SELECT w.current_time FROM worlds w WHERE id=?", (ride["world_id"],)).fetchone()[0])
            if own and at >= actual_now and self_aboard(c, ride["id"], observer):
                result["can_decline"] = status == "pending" and own["decision"] == "pending"
                result["can_withdraw"] = status in {"pending", "accepted"} and (observer == known["proposer_id"] or own['decision'] == 'accepted')
                try:
                    cls._ready(c, ride, known, at)
                    result["can_accept"] = status == "pending" and own["decision"] == "pending"
                    result["can_resume"] = status == "accepted" and observer == known["controller_id"] and cls._accepted(c, ride, known, at)
                except (ValueError, LookupError):
                    pass
        return result

    @classmethod
    def can_propose(cls, c, ride, at, observer):
        if not observer or not self_aboard(c, ride["id"], observer) or c.execute("SELECT 1 FROM vehicle_ride_recoveries WHERE ride_id=? AND status IN ('pending','accepted')", (ride["id"],)).fetchone() or c.execute("SELECT 1 FROM vehicle_ride_handoffs WHERE ride_id=? AND status IN ('pending','accepted')", (ride['id'],)).fetchone():
            return False
        try:
            cls._snapshot(c, ride, at)
            cls._rides()._controller(c, ride, at)
            return True
        except (ValueError, LookupError):
            return False


def self_aboard(c, ride_id, character_id):
    return bool(c.execute("SELECT 1 FROM vehicle_ride_members WHERE ride_id=? AND character_id=? AND live=1 AND boarded_at IS NOT NULL AND disembarked_at IS NULL", (ride_id, character_id)).fetchone())
