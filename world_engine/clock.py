from __future__ import annotations

import json
from datetime import datetime, timedelta
from uuid import uuid4

from world_engine.activation import NPCActivationService
from world_engine.config import Settings
from world_engine.contracts import ContractService
from world_engine.database import Database
from world_engine.domain import ClockUpdateResult, HeartbeatResult
from world_engine.life import LifeActivityService
from world_engine.movement import MovementService
from world_engine.overnight import OvernightService
from world_engine.registration import ConstructionProjectService
from world_engine.repository import WorldNotFoundError, from_iso, to_iso, utc_now


class _OvernightSettlementRequired(Exception):
    """哥哥，写事务发现新的住宿边界时，回滚后先逐段结算现实时间。"""


class WorldClockService:
    """负责现实分钟心跳、世界时间比例和连续人物状态。"""

    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings
        self.movement = MovementService()
        self.construction = ConstructionProjectService()
        self.activation = NPCActivationService()

    def reset_offline_baseline(
        self,
        real_now: datetime | None = None,
        *,
        preserve_online_workers: bool = False,
    ) -> int:
        """哥哥，API 重启保留在线世界，worker 重启仍丢弃离线时间。"""

        now = real_now or utc_now()
        with self.database.write() as connection:
            from world_engine.player_steering import PlayerSteeringService

            online_world_ids = set()
            if preserve_online_workers:
                for row in connection.execute(
                    "SELECT r.world_id,r.last_worker_seen_at,c.heartbeat_interval_seconds "
                    "FROM world_runtime r JOIN world_clock c ON c.world_id=r.world_id"
                ):
                    interval = max(
                        row["heartbeat_interval_seconds"], self.settings.worker_interval_seconds
                    )
                    if row["last_worker_seen_at"] and (
                        now - from_iso(row["last_worker_seen_at"])
                    ).total_seconds() <= 3 * interval:
                        online_world_ids.add(row["world_id"])

            for row in connection.execute(
                "SELECT s.world_id,w.current_time FROM player_steering s JOIN worlds w ON w.id=s.world_id "
                "WHERE s.dx!=0 OR s.dy!=0",
            ).fetchall():
                if row["world_id"] in online_world_ids:
                    continue
                PlayerSteeringService.stop(connection, row["world_id"], from_iso(row["current_time"]),
                                           "服务器重启，停止方向移动。")
            world_ids = [
                row["id"] for row in connection.execute(
                    "SELECT id FROM worlds WHERE status='running'"
                ) if row["id"] not in online_world_ids
            ]
            connection.executemany(
                "UPDATE world_clock SET last_heartbeat_real_time=?,updated_at=? WHERE world_id=?",
                [(to_iso(now), to_iso(now), world_id) for world_id in world_ids],
            )
            return len(world_ids)

    def mark_worker_seen(self, real_now: datetime | None = None) -> int:
        now = real_now or utc_now()
        with self.database.write() as connection:
            cursor = connection.execute(
                """
                UPDATE world_runtime
                SET last_worker_seen_at = ?
                WHERE world_id IN (SELECT id FROM worlds WHERE status = 'running')
                """,
                (to_iso(now),),
            )
            return cursor.rowcount

    def clear_worker_seen(self) -> int:
        with self.database.write() as connection:
            cursor = connection.execute(
                "UPDATE world_runtime SET last_worker_seen_at = NULL"
            )
            return cursor.rowcount

    def heartbeat(
        self,
        world_id: str,
        *,
        real_now: datetime | None = None,
        elapsed_seconds: float | None = None,
        activity_skip: dict[str, object] | None = None,
        _single_boundary: bool = False,
        _catchup_progress: dict[str, object] | None = None,
    ) -> HeartbeatResult:
        # 哥哥，手动刷新和后台使用相同的住宿边界，延迟心跳也不整夜越过身体需要。
        if activity_skip:
            return self._heartbeat_once(
                world_id, real_now=real_now, elapsed_seconds=elapsed_seconds,
                activity_skip=activity_skip,
            )
        now = real_now or utc_now()
        if _single_boundary and elapsed_seconds is None:
            return self._heartbeat_once(
                world_id, real_now=now, _overnight_bound=True, _event_bound=True,
                _catchup_progress=_catchup_progress,
            )
        if elapsed_seconds is None:
            results = []
            progress = _catchup_progress if _catchup_progress is not None else {}
            while True:
                result = self._heartbeat_once(
                    world_id, real_now=now, _overnight_bound=True,
                    _catchup_progress=progress,
                )
                results.append(result)
                if progress.get("complete") or result.real_elapsed_seconds == 0:
                    return self._merge_heartbeats(results)
        remaining = elapsed_seconds
        results = []
        while True:
            with self.database.read() as c:
                row = c.execute(
                    "SELECT w.current_time,k.last_heartbeat_real_time,k.time_scale "
                    "FROM worlds w JOIN world_clock k ON k.world_id=w.id WHERE w.id=?",
                    (world_id,),
                ).fetchone()
                if not row:
                    raise WorldNotFoundError(world_id)
                remaining = max(0.0, float(remaining))
                step = remaining
                scale = float(row["time_scale"])
                if scale > 0 and remaining > 0 and OvernightService.has_live(c, world_id):
                    from world_engine.collective_clock import next_boundary

                    at = from_iso(row["current_time"])
                    boundary = next_boundary(
                        c, world_id, at, satiety_rate=self.settings.satiety_loss_per_world_hour,
                    )
                    step = min(step, (boundary - at).total_seconds() / scale)
            remaining = max(0.0, remaining - step)
            results.append(self._heartbeat_once(
                world_id, real_now=now - timedelta(seconds=remaining), elapsed_seconds=step,
            ))
            if remaining == 0:
                break
        return self._merge_heartbeats(results)

    def _merge_heartbeats(self, results):
        if len(results) == 1:
            return results[0]
        result = results[-1]
        fields = (
            "real_elapsed_seconds", "world_delta_seconds", "state_update_count",
            "movements_updated", "construction_updates", "activation_updates",
        )
        totals = {key: sum(getattr(item, key) for item in results) for key in fields}
        ids = [item.heartbeat_id for item in results]
        with self.database.read() as c:
            count = c.execute(
                "SELECT COUNT(DISTINCT character_id) FROM character_state_updates "
                "WHERE heartbeat_id IN (" + ",".join("?" for _ in ids) + ")", ids,
            ).fetchone()[0]
        return result.model_copy(update={
            **totals, "characters_updated": count, "previous_time": results[0].previous_time,
            "adjudication_due": any(item.adjudication_due for item in results),
        })

    def _heartbeat_once(
        self,
        world_id: str,
        *,
        real_now: datetime | None = None,
        elapsed_seconds: float | None = None,
        activity_skip: dict[str, object] | None = None,
        _overnight_bound: bool = False,
        _event_bound: bool = False,
        _catchup_progress: dict[str, object] | None = None,
    ) -> HeartbeatResult:
        now = real_now or utc_now()
        requested_real_end = now
        heartbeat_id = str(uuid4())
        skip_reason = None
        with self.database.write() as connection:
            row = connection.execute(
                """
                SELECT w.current_time, w.version,
                       c.time_scale, c.last_heartbeat_real_time,
                       c.clock_revision, c.next_adjudication_world_time
                FROM worlds w
                JOIN world_clock c ON c.world_id = w.id
                WHERE w.id = ?
                """,
                (world_id,),
            ).fetchone()
            if row is None:
                raise WorldNotFoundError(world_id)

            if activity_skip:
                cached = connection.execute(
                    "SELECT * FROM activity_time_skips WHERE world_id=? AND request_id=?",
                    (world_id, activity_skip["request_id"]),
                ).fetchone()
                if cached:
                    if (cached["activity_id"] != activity_skip["activity_id"]
                            or cached["input_version"] != activity_skip["expected_version"]):
                        raise ValueError("等候请求标识已经用于其他请求")
                    result = HeartbeatResult.model_validate_json(cached["response_json"])
                    result.time_skip_replayed = True
                    return result
            previous_time = from_iso(row["current_time"])
            if elapsed_seconds is None:
                last_real = row["last_heartbeat_real_time"]
                if last_real:
                    now = max(now, from_iso(last_real))
                measured = (
                    (now - from_iso(last_real)).total_seconds() if last_real else 0.0
                )
                real_elapsed_seconds = max(0.0, measured)
            else:
                real_elapsed_seconds = max(0.0, float(elapsed_seconds))

            time_scale = float(row["time_scale"])
            world_delta_seconds = real_elapsed_seconds * time_scale
            world_target = previous_time + timedelta(seconds=world_delta_seconds)
            if elapsed_seconds is None and _catchup_progress is not None:
                # 哥哥，只在本次追赶没有被其他时钟写入打断时复用目标，避免每段现实时间舍入丢掉世界微秒。
                if (
                    _catchup_progress.get("world_id") == world_id
                    and _catchup_progress.get("real_end") == requested_real_end
                    and _catchup_progress.get("clock_revision") == int(row["clock_revision"])
                    and _catchup_progress.get("world_time") == previous_time
                    and _catchup_progress.get("real_time") == row["last_heartbeat_real_time"]
                    and time_scale > 0
                ):
                    world_target = _catchup_progress["world_target"]
                    world_delta_seconds = max(0.0, (world_target - previous_time).total_seconds())
                    real_elapsed_seconds = world_delta_seconds / time_scale
            if _overnight_bound and world_delta_seconds > 0 and (_event_bound or OvernightService.has_live(connection, world_id)):
                from world_engine.collective_clock import next_boundary

                boundary = next_boundary(
                    connection, world_id, previous_time,
                    satiety_rate=self.settings.satiety_loss_per_world_hour,
                )
                if previous_time + timedelta(seconds=world_delta_seconds) > boundary:
                    world_delta_seconds = (boundary - previous_time).total_seconds()
                    real_elapsed_seconds = world_delta_seconds / time_scale
                    now = from_iso(row["last_heartbeat_real_time"]) + timedelta(seconds=real_elapsed_seconds)
            if activity_skip:
                from world_engine.activity_waiting import waiting_boundary
                world_delta_seconds, skip_reason, skip_actor_id = waiting_boundary(
                    connection, world_id, row, activity_skip, self.settings,
                )
                real_elapsed_seconds = 0.0
            current_time = previous_time + timedelta(seconds=world_delta_seconds)
            from world_engine.collective_runner import is_collective_world
            from world_engine.science_runner import is_science_world

            collective = is_collective_world(connection,world_id)
            science = is_science_world(connection,world_id)
            if (collective or science) and world_delta_seconds>0:
                from world_engine.collective_clock import next_boundary

                boundary=next_boundary(connection,world_id,previous_time,satiety_rate=self.settings.satiety_loss_per_world_hour)
                if current_time>boundary+timedelta(microseconds=1):
                    raise ValueError(f'追踪模式心跳不能跳过更早的世界事件边界：目标={to_iso(current_time)}，边界={to_iso(boundary)}，上一时间={to_iso(previous_time)}，比例={time_scale}')
                connection.execute('UPDATE worlds SET current_time=? WHERE id=?',(to_iso(current_time),world_id))
            clock_revision = int(row["clock_revision"]) + 1
            adjudication_due = current_time >= from_iso(
                row["next_adjudication_world_time"]
            )

            connection.execute(
                """
                INSERT INTO world_heartbeats(
                    id, world_id, real_time, real_elapsed_seconds, time_scale,
                    world_delta_seconds, world_time_before, world_time_after,
                    characters_updated, adjudication_due, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    heartbeat_id,
                    world_id,
                    to_iso(now),
                    real_elapsed_seconds,
                    time_scale,
                    world_delta_seconds,
                    to_iso(previous_time),
                    to_iso(current_time),
                    int(adjudication_due),
                    to_iso(now),
                ),
            )
            state_update_count, characters_updated = self._update_character_states(
                connection,
                world_id=world_id,
                heartbeat_id=heartbeat_id,
                previous_time=previous_time,
                current_time=current_time,
                world_delta_seconds=world_delta_seconds,
                created_at=now,
            )
            if (collective or science) and world_delta_seconds>0:
                from world_engine.collective_clock import advance_materials

                advance_materials(connection,world_id,world_delta_seconds/3600)
            movements_updated = self.movement.advance(
                connection,
                world_id=world_id,
                previous_time=previous_time,
                current_time=current_time,
                created_at=now,
            )
            life_updates = LifeActivityService.advance(
                connection, world_id, current_time, heartbeat_id,
            )
            if life_updates:
                state_update_count, characters_updated = connection.execute(
                    "SELECT count(*),count(DISTINCT character_id) FROM character_state_updates "
                    "WHERE heartbeat_id=?", (heartbeat_id,),
                ).fetchone()
            construction_updates = self.construction.advance(
                connection,
                world_id=world_id,
                world_delta_seconds=world_delta_seconds,
                world_time=current_time,
                created_at=now,
            )
            activation_updates = self.activation.refresh(
                connection,
                world_id=world_id,
                world_time=current_time,
            )
            contracts_updated = ContractService.advance(connection, world_id, to_iso(current_time))
            from world_engine.rides import RideService
            RideService.maintain(connection, world_id, current_time)
            if world_delta_seconds > 0:
                from world_engine.principle_cycles import tick as advance_principle_cycles

                advance_principle_cycles(connection, world_id, current_time)
            from world_engine.daily_life import DailyLifeService
            from world_engine.economy import EconomyService
            from world_engine.society import SocietyService
            if science and world_delta_seconds>0:
                from world_engine.collective_clock import basic_life
                from world_engine.science_programs import ScienceService
                from world_engine.visits import VisitService

                ScienceService.tick(connection, world_id, current_time, heartbeat_id=heartbeat_id)
                basic_life(connection, world_id, current_time)
                VisitService.tick(connection, world_id, current_time)
                EconomyService.tick(connection, world_id, current_time)
                SocietyService.tick(connection, world_id, current_time)
                if adjudication_due:
                    connection.execute('UPDATE world_clock SET next_adjudication_world_time=? WHERE world_id=?',
                        (to_iso(current_time+timedelta(hours=12)),world_id))
            elif collective and world_delta_seconds>0:
                from world_engine.collective_clock import basic_life, settle

                settle(connection,world_id,current_time)
                basic_life(connection,world_id,current_time)
                if adjudication_due:
                    connection.execute('UPDATE world_clock SET next_adjudication_world_time=? WHERE world_id=?',(to_iso(current_time+timedelta(hours=12)),world_id))
            elif world_delta_seconds > 0:
                from world_engine.cooperation import CooperationService
                from world_engine.incidents import IncidentService
                from world_engine.visits import VisitService
                IncidentService.tick(connection, world_id, current_time)
                CooperationService.tick(connection, world_id, current_time)
                from world_engine.wonders import WonderService
                WonderService.tick(connection, world_id, current_time)
                from world_engine.autonomous_research import ResearchService

                ResearchService.tick(connection, world_id, current_time, heartbeat_id=heartbeat_id)
                VisitService.tick(connection, world_id, current_time)
                EconomyService.tick(connection, world_id, current_time)
                DailyLifeService.tick(connection, world_id, current_time, adjudication_due=adjudication_due)
                SocietyService.tick(connection, world_id, current_time)
                IncidentService.tick(connection, world_id, current_time)
                CooperationService.tick(connection, world_id, current_time)
            version_increment = int(bool(
                world_delta_seconds > 0 or contracts_updated or life_updates
            ))
            connection.execute(
                """
                UPDATE worlds
                SET current_time = ?, version = version + ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    to_iso(current_time),
                    version_increment,
                    to_iso(now),
                    world_id,
                ),
            )
            connection.execute(
                """
                UPDATE world_clock
                SET last_heartbeat_real_time = ?,
                    clock_revision = ?,
                    updated_at = ?
                WHERE world_id = ?
                """,
                (to_iso(now), clock_revision, to_iso(now), world_id),
            )
            connection.execute(
                """
                UPDATE world_heartbeats
                SET characters_updated = ?
                WHERE id = ?
                """,
                (characters_updated, heartbeat_id),
            )

            result = HeartbeatResult(
                world_id=world_id,
                heartbeat_id=heartbeat_id,
                real_time=now,
                real_elapsed_seconds=real_elapsed_seconds,
                time_scale=time_scale,
                world_delta_seconds=world_delta_seconds,
                previous_time=previous_time,
                current_time=current_time,
                clock_revision=clock_revision,
                characters_updated=characters_updated,
                state_update_count=state_update_count,
                movements_updated=movements_updated,
                construction_updates=construction_updates,
                activation_updates=activation_updates,
                adjudication_due=adjudication_due,
                time_skip_reason=skip_reason,
            )
            if activity_skip:
                from world_engine.actions import ActionService
                ActionService._record_event(
                    connection, world_id=world_id, tick_id=heartbeat_id, occurred_at=current_time,
                    event_type="action.time_waited", actor_id=skip_actor_id, target_id=None,
                    location_id=None,
                    summary=f"你等候了{world_delta_seconds / 60:.1f}个世界分钟：{skip_reason}。",
                    payload={"activity_id": activity_skip["activity_id"], "reason": skip_reason,
                             "world_delta_seconds": world_delta_seconds},
                )
                connection.execute(
                    "INSERT INTO activity_time_skips VALUES (?,?,?,?,?)",
                    (activity_skip["request_id"], world_id, activity_skip["activity_id"],
                     activity_skip["expected_version"], result.model_dump_json()),
                )
        if elapsed_seconds is None and _catchup_progress is not None:
            _catchup_progress.update({
                "world_id": world_id, "real_end": requested_real_end,
                "world_target": world_target, "clock_revision": clock_revision,
                "world_time": current_time, "real_time": to_iso(now),
                "complete": current_time >= world_target,
            })
        return result


    def set_time_scale(
        self,
        world_id: str,
        new_time_scale: float,
        *,
        operator: str = "main_view",
        real_now: datetime | None = None,
    ) -> ClockUpdateResult:
        if not 0 <= new_time_scale <= 10080:
            raise ValueError("时间比例必须在0到10080之间")
        now = real_now or utc_now()
        with self.database.read() as c:
            row = c.execute(
                "SELECT time_scale FROM world_clock WHERE world_id=?", (world_id,),
            ).fetchone()
            overnight = row and abs(float(row[0]) - new_time_scale) >= 0.000001 and OvernightService.has_live(c, world_id)
        results = [self.heartbeat(world_id, real_now=now)] if overnight else []
        while True:
            try:
                result = self._set_time_scale_once(
                    world_id, new_time_scale, operator=operator, real_now=now,
                )
                break
            except _OvernightSettlementRequired:
                results.append(self.heartbeat(world_id, real_now=now))
        settled = self._merge_heartbeats(results) if results else None
        if settled:
            result.previous_world_time = settled.previous_time
            result.settled_world_seconds += settled.world_delta_seconds
            result.state_update_count += settled.state_update_count
            result.movements_updated += settled.movements_updated
            result.construction_updates += settled.construction_updates
            result.activation_updates += settled.activation_updates
            result.adjudication_due = result.adjudication_due or settled.adjudication_due
        return result

    def _set_time_scale_once(
        self,
        world_id: str,
        new_time_scale: float,
        *,
        operator: str = "main_view",
        real_now: datetime | None = None,
    ) -> ClockUpdateResult:
        if not 0 <= new_time_scale <= 10080:
            raise ValueError("时间比例必须在0到10080之间")
        now = real_now or utc_now()
        with self.database.write() as connection:
            row = connection.execute(
                """
                SELECT w.current_time, w.version,
                       c.time_scale, c.clock_revision,
                       c.last_heartbeat_real_time,
                       c.next_adjudication_world_time
                FROM worlds w
                JOIN world_clock c ON c.world_id = w.id
                WHERE w.id = ?
                """,
                (world_id,),
            ).fetchone()
            if row is None:
                raise WorldNotFoundError(world_id)
            old_time_scale = float(row["time_scale"])
            previous_time = from_iso(row["current_time"])
            if abs(old_time_scale - new_time_scale) < 0.000001:
                return ClockUpdateResult(
                    world_id=world_id,
                    old_time_scale=old_time_scale,
                    new_time_scale=new_time_scale,
                    previous_world_time=previous_time,
                    world_time=previous_time,
                    clock_revision=int(row["clock_revision"]),
                    world_version=int(row["version"]),
                    settled_world_seconds=0,
                    state_update_count=0,
                    no_op=True,
                )

            last_real = row["last_heartbeat_real_time"]
            if last_real:
                now = max(now, from_iso(last_real))
            real_elapsed_seconds = max(
                0.0,
                (now - from_iso(last_real)).total_seconds() if last_real else 0.0,
            )
            world_delta_seconds = real_elapsed_seconds * old_time_scale
            current_time = previous_time + timedelta(seconds=world_delta_seconds)
            if world_delta_seconds > 0 and OvernightService.has_live(connection, world_id):
                from world_engine.collective_clock import next_boundary

                boundary = next_boundary(
                    connection, world_id, previous_time,
                    satiety_rate=self.settings.satiety_loss_per_world_hour,
                )
                if current_time > boundary:
                    raise _OvernightSettlementRequired()
            adjudication_due = current_time >= from_iso(
                row["next_adjudication_world_time"]
            )
            clock_revision = int(row["clock_revision"]) + 1
            heartbeat_id: str | None = None
            state_update_count = 0
            characters_updated = 0
            movements_updated = 0
            construction_updates = 0
            activation_updates = 0
            if world_delta_seconds > 0:
                heartbeat_id = str(uuid4())
                connection.execute(
                    """
                    INSERT INTO world_heartbeats(
                        id, world_id, real_time, real_elapsed_seconds, time_scale,
                        world_delta_seconds, world_time_before, world_time_after,
                        characters_updated, adjudication_due, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        heartbeat_id,
                        world_id,
                        to_iso(now),
                        real_elapsed_seconds,
                        old_time_scale,
                        world_delta_seconds,
                        to_iso(previous_time),
                        to_iso(current_time),
                        int(adjudication_due),
                        to_iso(now),
                    ),
                )
                state_update_count, characters_updated = self._update_character_states(
                    connection,
                    world_id=world_id,
                    heartbeat_id=heartbeat_id,
                    previous_time=previous_time,
                    current_time=current_time,
                    world_delta_seconds=world_delta_seconds,
                    created_at=now,
                )
                movements_updated = self.movement.advance(
                    connection,
                    world_id=world_id,
                    previous_time=previous_time,
                    current_time=current_time,
                    created_at=now,
                )
                life_updates = LifeActivityService.advance(
                    connection, world_id, current_time, heartbeat_id,
                )
                if life_updates:
                    state_update_count, characters_updated = connection.execute(
                        "SELECT count(*),count(DISTINCT character_id) FROM character_state_updates "
                        "WHERE heartbeat_id=?", (heartbeat_id,),
                    ).fetchone()
                construction_updates = self.construction.advance(
                    connection,
                    world_id=world_id,
                    world_delta_seconds=world_delta_seconds,
                    world_time=current_time,
                    created_at=now,
                )
                activation_updates = self.activation.refresh(
                    connection,
                    world_id=world_id,
                    world_time=current_time,
                )
                connection.execute(
                    """
                    UPDATE world_heartbeats
                    SET characters_updated = ?
                    WHERE id = ?
                    """,
                    (characters_updated, heartbeat_id),
                )

            ContractService.advance(connection, world_id, to_iso(current_time))
            from world_engine.rides import RideService
            RideService.maintain(connection, world_id, current_time)
            if world_delta_seconds > 0:
                from world_engine.daily_life import DailyLifeService
                from world_engine.economy import EconomyService
                from world_engine.principle_cycles import tick as advance_principle_cycles
                from world_engine.society import SocietyService
                from world_engine.visits import VisitService

                advance_principle_cycles(connection, world_id, current_time)
                VisitService.tick(connection, world_id, current_time)
                EconomyService.tick(connection, world_id, current_time)
                DailyLifeService.tick(connection, world_id, current_time, adjudication_due=adjudication_due)
                SocietyService.tick(connection, world_id, current_time)
            new_world_version = int(row["version"]) + 1
            if world_delta_seconds > 0:
                from world_engine.wonders import WonderService
                WonderService.tick(connection, world_id, current_time)
            from world_engine.player_steering import PlayerSteeringService

            PlayerSteeringService.stop(connection, world_id, current_time, "世界流速改变，请重新输入方向。")
            connection.execute(
                """
                UPDATE world_clock
                SET time_scale = ?,
                    last_heartbeat_real_time = ?,
                    clock_revision = ?,
                    updated_at = ?
                WHERE world_id = ?
                """,
                (
                    new_time_scale,
                    to_iso(now),
                    clock_revision,
                    to_iso(now),
                    world_id,
                ),
            )
            connection.execute(
                """
                UPDATE worlds
                SET current_time = ?, version = ?, updated_at = ?
                WHERE id = ?
                """,
                (to_iso(current_time), new_world_version, to_iso(now), world_id),
            )
            event_id = str(uuid4())
            event_group_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO world_events(
                    id, world_id, tick_id, occurred_at, event_type,
                    summary, payload_json, created_at
                ) VALUES (?, ?, ?, ?, 'world.clock_rate_changed', ?, ?, ?)
                """,
                (
                    event_id,
                    world_id,
                    event_group_id,
                    to_iso(current_time),
                    f"世界时间比例由{old_time_scale}调整为{new_time_scale}。",
                    json.dumps(
                        {
                            "old_time_scale": old_time_scale,
                            "new_time_scale": new_time_scale,
                            "operator": operator,
                            "settled_real_seconds": real_elapsed_seconds,
                            "settled_world_seconds": world_delta_seconds,
                            "state_update_count": state_update_count,
                        },
                        ensure_ascii=False,
                    ),
                    to_iso(now),
                ),
            )
        return ClockUpdateResult(
            world_id=world_id,
            old_time_scale=old_time_scale,
            new_time_scale=new_time_scale,
            previous_world_time=previous_time,
            world_time=current_time,
            clock_revision=clock_revision,
            world_version=new_world_version,
            settled_world_seconds=world_delta_seconds,
            state_update_count=state_update_count,
            movements_updated=movements_updated,
            construction_updates=construction_updates,
            activation_updates=activation_updates,
            adjudication_due=adjudication_due,
            event_id=event_id,
        )

    def record_player_intervention(self, world_id: str, world_time: datetime) -> None:
        now = utc_now()
        with self.database.write() as connection:
            cursor = connection.execute(
                """
                UPDATE world_clock
                SET last_player_intervention_world_time = ?, updated_at = ?
                WHERE world_id = ?
                """,
                (to_iso(world_time), to_iso(now), world_id),
            )
            if cursor.rowcount == 0:
                raise WorldNotFoundError(world_id)

    def _update_character_states(
        self,
        connection,
        *,
        world_id: str,
        heartbeat_id: str,
        previous_time: datetime,
        current_time: datetime,
        world_delta_seconds: float,
        created_at: datetime,
    ) -> tuple[int, int]:
        connection.execute(
            """
            INSERT OR IGNORE INTO character_state_accumulators(
                character_id, world_id, satiety_residual, energy_residual, updated_at
            )
            SELECT id, world_id, 0, 0, updated_at
            FROM characters WHERE world_id = ?
            """,
            (world_id,),
        )
        rows = connection.execute(
            """
            SELECT c.id, c.satiety, c.energy, c.health,
                   a.satiety_residual, a.energy_residual
            FROM characters c
            JOIN character_state_accumulators a ON a.character_id = c.id
            WHERE c.world_id = ?
            ORDER BY c.id
            """,
            (world_id,),
        ).fetchall()
        world_hours = world_delta_seconds / 3600
        state_update_count = 0
        characters_updated = 0
        from world_engine.collective_runner import is_collective_world
        collective=is_collective_world(connection,world_id)
        for row in rows:
            satiety_total = float(row["satiety_residual"]) + (
                self.settings.satiety_loss_per_world_hour * world_hours
            )
            energy_total = float(row["energy_residual"]) + (
                self.settings.energy_recovery_per_world_hour * world_hours
            )
            satiety_step = int(satiety_total)
            energy_step = int(energy_total)
            satiety_before = int(row["satiety"])
            energy_before = int(row["energy"])
            satiety_after = max(0, satiety_before - satiety_step)
            energy_after = min(100, energy_before + energy_step)
            satiety_residual = (
                0.0 if satiety_after <= 0 else satiety_total - satiety_step
            )
            energy_residual = 0.0 if energy_after >= 100 else energy_total - energy_step
            connection.execute(
                """
                UPDATE character_state_accumulators
                SET satiety_residual = ?, energy_residual = ?, updated_at = ?
                WHERE character_id = ?
                """,
                (
                    satiety_residual,
                    energy_residual,
                    to_iso(created_at),
                    row["id"],
                ),
            )
            changes: dict[str, object] = {}
            if collective and world_hours>0:
                from world_engine.collective_regions import RegionService
                rate=self.settings.satiety_loss_per_world_hour
                exhausted=max(0,world_hours-max(0,satiety_before-float(row['satiety_residual']))/rate) if rate>0 else (world_hours if satiety_before==0 else 0)
                damage=RegionService.starve(connection,row['id'],exhausted,current_time)
                if damage:
                    changes['health']={'before':row['health'],'after':row['health']-damage,'delta':-damage,'starvation_hours':exhausted}
            if satiety_after != satiety_before:
                changes["satiety"] = {
                    "before": satiety_before,
                    "after": satiety_after,
                    "delta": satiety_after - satiety_before,
                }
            if energy_after != energy_before:
                changes["energy"] = {
                    "before": energy_before,
                    "after": energy_after,
                    "delta": energy_after - energy_before,
                }
            if not changes:
                continue
            characters_updated += 1
            state_update_count += 1
            connection.execute(
                """
                UPDATE characters
                SET satiety = ?, energy = ?, updated_at = ?
                WHERE id = ?
                """,
                (satiety_after, energy_after, to_iso(created_at), row["id"]),
            )
            connection.execute(
                """
                INSERT INTO character_state_updates(
                    id, world_id, heartbeat_id, character_id,
                    world_time_before, world_time_after,
                    changes_json, cause, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'natural_time_passage', ?)
                """,
                (
                    str(uuid4()),
                    world_id,
                    heartbeat_id,
                    row["id"],
                    to_iso(previous_time),
                    to_iso(current_time),
                    json.dumps(changes, ensure_ascii=False),
                    to_iso(created_at),
                ),
            )
        return state_update_count, characters_updated
