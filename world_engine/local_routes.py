"""哥哥，已明确核实的局部通路保存几何、来源和开放状态。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from uuid import uuid4

from world_engine.repository import from_iso, to_iso, utc_now


LOCAL_ROUTE_SCHEMA = """
CREATE TABLE IF NOT EXISTS world_local_routes (
 id TEXT PRIMARY KEY,
 world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 dataset_id TEXT NOT NULL REFERENCES navigation_datasets(id),
 location_id TEXT NOT NULL REFERENCES locations(id),
 name TEXT NOT NULL,
 polyline_json TEXT NOT NULL,
 allowed_modes_json TEXT NOT NULL,
 bidirectional INTEGER NOT NULL CHECK(bidirectional IN (0,1)),
 speed_limit_kmh REAL NOT NULL CHECK(speed_limit_kmh>0),
 geometry_sha256 TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision=1),
 is_open INTEGER NOT NULL DEFAULT 1 CHECK(is_open IN (0,1)),
 state_version INTEGER NOT NULL DEFAULT 1 CHECK(state_version>0),
 registration_id TEXT NOT NULL UNIQUE REFERENCES element_registration_requests(id),
 source_event_id TEXT NOT NULL REFERENCES world_events(id),
 registered_event_id TEXT NOT NULL UNIQUE REFERENCES world_events(id),
 registered_at TEXT NOT NULL,
 state_event_id TEXT NOT NULL REFERENCES world_events(id),
 state_source_event_id TEXT NOT NULL REFERENCES world_events(id),
 state_changed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_local_routes_available
 ON world_local_routes(world_id,dataset_id,is_open);
"""


class LocalRouteNotFound(LookupError):
    pass


class LocalRouteConflict(ValueError):
    pass


def geometry_sha256(polyline):
    encoded = json.dumps(polyline, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class LocalRouteService:
    @staticmethod
    def _world(c, wid):
        world = c.execute("SELECT w.current_time FROM worlds w WHERE w.id=?", (wid,)).fetchone()
        if world is None:
            raise LocalRouteNotFound("世界不存在")
        return from_iso(world["current_time"])

    @staticmethod
    def source_time(value, at):
        try:
            occurred_at = datetime.fromisoformat(value)
            if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
                raise ValueError("来源事件缺少完整时区时间")
            is_future = occurred_at > at
        except (TypeError, ValueError, OverflowError) as exc:
            raise LocalRouteConflict("通路来源事件的发生时间无效") from exc
        if is_future:
            raise LocalRouteConflict("未来事件不能核实当前通路")
        return occurred_at

    @staticmethod
    def _source(c, wid, event_id, location_id, at):
        event = c.execute("SELECT * FROM world_events WHERE id=? AND world_id=?", (event_id, wid)).fetchone()
        if event is None or event["location_id"] != location_id:
            raise LocalRouteConflict("通路来源事件必须属于当前世界并发生在核实地点")
        LocalRouteService.source_time(event["occurred_at"], at)
        return event

    @staticmethod
    def _event(c, wid, location_id, at, event_type, summary, payload):
        event_id = str(uuid4())
        c.execute(
            "INSERT INTO world_events(id,world_id,tick_id,occurred_at,event_type,location_id,"
            "summary,importance,payload_json,created_at) VALUES (?,?,?,?,?,?,?,'routine',?,?)",
            (event_id, wid, str(uuid4()), to_iso(at), event_type, location_id, summary,
             json.dumps(payload, ensure_ascii=False), to_iso(utc_now())),
        )
        return event_id

    @classmethod
    def register(cls, context, payload):
        """明确确认只建立核实通路，不生成食物或改动人物位置。"""
        c, wid, at = context.connection, context.world_id, context.world_time
        if context.requested_by_character_id is not None:
            raise LocalRouteConflict("局部通路须由编年者核实，人物不能凭描述确认道路")
        location = c.execute(
            "SELECT * FROM locations WHERE id=? AND world_id=? AND is_active=1",
            (payload.location_id, wid),
        ).fetchone()
        if location is None or location["space_layer"] != "ground":
            raise LocalRouteConflict("通路核实地点必须是本世界有效的地面地点")
        dataset = c.execute(
            "SELECT id FROM navigation_datasets WHERE id=? AND world_id=? AND review_status='approved'",
            (payload.dataset_id, wid),
        ).fetchone()
        if dataset is None:
            raise LocalRouteConflict("局部通路必须绑定本世界已审核的导航数据集")
        cls._source(c, wid, context.source_event["id"], payload.location_id, at)
        route_id = str(uuid4())
        polyline = [list(point) for point in payload.polyline]
        geometry = geometry_sha256(polyline)
        registered_event_id = cls._event(
            c, wid, payload.location_id, at, "world.local_route_registered",
            f"编年者核实了既存局部通路“{payload.name}”的实际走向。",
            {"local_route_id": route_id, "registration_id": context.registration_id,
             "source_event_id": context.source_event["id"], "dataset_id": payload.dataset_id,
             "geometry_sha256": geometry, "revision": 1},
        )
        c.execute(
            "INSERT INTO world_local_routes(id,world_id,dataset_id,location_id,name,polyline_json,"
            "allowed_modes_json,bidirectional,speed_limit_kmh,geometry_sha256,registration_id,"
            "source_event_id,registered_event_id,registered_at,state_event_id,state_source_event_id,state_changed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (route_id, wid, payload.dataset_id, payload.location_id, payload.name,
             json.dumps(polyline, ensure_ascii=False), json.dumps(payload.allowed_modes),
             int(payload.bidirectional), payload.speed_limit_kmh, geometry, context.registration_id,
             context.source_event["id"], registered_event_id, to_iso(at), registered_event_id,
             context.source_event["id"], to_iso(at)),
        )
        return route_id

    @classmethod
    def get(cls, c, wid, route_id):
        row = c.execute("SELECT * FROM world_local_routes WHERE id=? AND world_id=?", (route_id, wid)).fetchone()
        if row is None:
            raise LocalRouteNotFound("局部通路不存在或不属于当前世界")
        try:
            return cls._validated(c, dict(row), cls._world(c, wid))
        except (ValueError, TypeError, KeyError, IndexError, OverflowError) as exc:
            raise LocalRouteConflict("局部通路的登记、来源或几何记录不一致") from exc

    @classmethod
    def _validated(cls, c, route, at):
        from world_engine.registration.models import LocalRouteSpec

        wid = route["world_id"]
        registration = c.execute(
            "SELECT * FROM element_registration_requests WHERE id=? AND world_id=? "
            "AND element_type='local_route' AND status='applied' AND requested_by_character_id IS NULL",
            (route["registration_id"], wid),
        ).fetchone()
        if registration is None or registration["result_entity_id"] != route["id"]:
            raise ValueError
        spec = LocalRouteSpec.model_validate(json.loads(registration["payload_json"]))
        polyline = json.loads(route["polyline_json"])
        modes = json.loads(route["allowed_modes_json"])
        expected = [list(point) for point in spec.polyline]
        if (route["revision"] != 1 or route["source_event_id"] != registration["source_event_id"]
                or spec.dataset_id != route["dataset_id"] or spec.location_id != route["location_id"]
                or spec.name != route["name"] or expected != polyline or spec.allowed_modes != modes
                or spec.bidirectional != bool(route["bidirectional"])
                or spec.speed_limit_kmh != route["speed_limit_kmh"]
                or geometry_sha256(polyline) != route["geometry_sha256"]):
            raise ValueError
        dataset = c.execute(
            "SELECT id FROM navigation_datasets WHERE id=? AND world_id=? AND review_status='approved'",
            (route["dataset_id"], wid),
        ).fetchone()
        location = c.execute(
            "SELECT id FROM locations WHERE id=? AND world_id=? AND is_active=1 AND space_layer='ground'",
            (route["location_id"], wid),
        ).fetchone()
        registered_at = from_iso(route["registered_at"])
        changed_at = from_iso(route["state_changed_at"])
        if (dataset is None or location is None or type(route["state_version"]) is not int
                or route["state_version"] < 1 or registered_at > at or not registered_at <= changed_at <= at):
            raise ValueError
        cls._source(c, wid, route["source_event_id"], route["location_id"], registered_at)
        registered = cls._source(c, wid, route["registered_event_id"], route["location_id"], registered_at)
        details = json.loads(registered["payload_json"])
        if (registered["event_type"] != "world.local_route_registered"
                or from_iso(registered["occurred_at"]) != registered_at
                or details.get("local_route_id") != route["id"]
                or details.get("registration_id") != route["registration_id"]
                or details.get("source_event_id") != route["source_event_id"]
                or details.get("dataset_id") != route["dataset_id"]
                or details.get("geometry_sha256") != route["geometry_sha256"]
                or details.get("revision") != route["revision"]):
            raise ValueError
        state_source = cls._source(c, wid, route["state_source_event_id"], route["location_id"], changed_at)
        if route["state_version"] == 1:
            if (not route["is_open"] or route["state_event_id"] != route["registered_event_id"]
                    or route["state_source_event_id"] != route["source_event_id"] or changed_at != registered_at):
                raise ValueError
        else:
            if from_iso(state_source["occurred_at"]) < registered_at:
                raise ValueError
            state_event = cls._source(c, wid, route["state_event_id"], route["location_id"], changed_at)
            state = json.loads(state_event["payload_json"])
            expected_type = "world.local_route_reopened" if route["is_open"] else "world.local_route_closed"
            if (state_event["event_type"] != expected_type or from_iso(state_event["occurred_at"]) != changed_at
                    or state.get("local_route_id") != route["id"]
                    or state.get("state_version") != route["state_version"]
                    or state.get("is_open") != bool(route["is_open"])
                    or state.get("source_event_id") != route["state_source_event_id"]):
                raise ValueError
        route.update(polyline=polyline, allowed_modes=modes,
                     bidirectional=bool(route["bidirectional"]), is_open=bool(route["is_open"]))
        return route

    @classmethod
    def available(cls, c, wid, dataset_id, movement_type):
        rows = c.execute(
            "SELECT id FROM world_local_routes WHERE world_id=? AND dataset_id=? AND is_open=1 ORDER BY id",
            (wid, dataset_id),
        ).fetchall()
        result = []
        for row in rows:
            try:
                route = cls.get(c, wid, row["id"])
            except (LocalRouteConflict, LocalRouteNotFound):
                continue
            if movement_type in route["allowed_modes"]:
                result.append(route)
        return result

    @classmethod
    def rows(cls, c, wid):
        cls._world(c, wid)
        result = []
        for row in c.execute("SELECT id FROM world_local_routes WHERE world_id=? ORDER BY name,id", (wid,)).fetchall():
            try:
                result.append(cls.get(c, wid, row["id"]))
            except (LocalRouteConflict, LocalRouteNotFound):
                continue
        return result

    @classmethod
    def set_open(cls, c, wid, route_id, *, is_open, expected_state_version,
                 source_event_id, requested_by_character_id=None):
        if type(is_open) is not bool or type(expected_state_version) is not int or expected_state_version < 1:
            raise LocalRouteConflict("通路状态和版本必须是明确的布尔值与正整数")
        if requested_by_character_id is not None:
            raise LocalRouteConflict("人物不能自行确认局部通路的开放或关闭事实")
        route = cls.get(c, wid, route_id)
        if (route["is_open"] == is_open and route["state_source_event_id"] == source_event_id
                and expected_state_version in {route["state_version"], route["state_version"] - 1}):
            return route
        if expected_state_version != route["state_version"]:
            raise LocalRouteConflict("通路状态已变化，请重新核对版本")
        at = cls._world(c, wid)
        source = cls._source(c, wid, source_event_id, route["location_id"], at)
        if from_iso(source["occurred_at"]) < from_iso(route["state_changed_at"]):
            raise LocalRouteConflict("通路状态来源早于已经确认的状态")
        if route["is_open"] == is_open:
            return route
        version = route["state_version"] + 1
        c.execute("SAVEPOINT local_route_state")
        try:
            event = cls._event(
                c, wid, route["location_id"], at,
                "world.local_route_reopened" if is_open else "world.local_route_closed",
                f"编年者确认局部通路“{route['name']}”{'重新开放' if is_open else '关闭'}。",
                {"local_route_id": route_id, "state_version": version,
                 "is_open": is_open, "source_event_id": source_event_id},
            )
            updated = c.execute(
                "UPDATE world_local_routes SET is_open=?,state_version=?,state_event_id=?,"
                "state_source_event_id=?,state_changed_at=? WHERE id=? AND world_id=? AND state_version=?",
                (int(is_open), version, event, source_event_id, to_iso(at), route_id, wid, expected_state_version),
            )
            if updated.rowcount != 1:
                raise LocalRouteConflict("通路状态已由另一操作更新")
            c.execute("UPDATE worlds SET version=version+1,updated_at=? WHERE id=?", (to_iso(utc_now()), wid))
            result = cls.get(c, wid, route_id)
            c.execute("RELEASE SAVEPOINT local_route_state")
            return result
        except BaseException:
            c.execute("ROLLBACK TO SAVEPOINT local_route_state")
            c.execute("RELEASE SAVEPOINT local_route_state")
            raise
