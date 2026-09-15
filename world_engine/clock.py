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
from world_engine.registration import ConstructionProjectService
from world_engine.repository import WorldNotFoundError, from_iso, to_iso, utc_now


class WorldClockService:
    """负责现实分钟心跳、世界时间比例和连续人物状态。"""

    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings
        self.movement = MovementService()
        self.construction = ConstructionProjectService()
        self.activation = NPCActivationService()

    def reset_offline_baseline(self, real_now: datetime | None = None) -> int:
        """服务器启动时重置现实时间基准，明确不补算离线时间。"""

        now = real_now or utc_now()
        with self.database.write() as connection:
            cursor = connection.execute(
                """
                UPDATE world_clock
                SET last_heartbeat_real_time = ?, updated_at = ?
                WHERE world_id IN (SELECT id FROM worlds WHERE status = 'running')
                """,
                (to_iso(now), to_iso(now)),
            )
            return cursor.rowcount

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
    ) -> HeartbeatResult:
        now = real_now or utc_now()
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
                measured = (
                    (now - from_iso(last_real)).total_seconds() if last_real else 0.0
                )
                real_elapsed_seconds = max(0.0, measured)
            else:
                real_elapsed_seconds = max(0.0, float(elapsed_seconds))

            time_scale = float(row["time_scale"])
            world_delta_seconds = real_elapsed_seconds * time_scale
            if activity_skip:
                from world_engine.activity_waiting import waiting_boundary
                world_delta_seconds, skip_reason, skip_actor_id = waiting_boundary(
                    connection, world_id, row, activity_skip, self.settings,
                )
                real_elapsed_seconds = 0.0
            current_time = previous_time + timedelta(seconds=world_delta_seconds)
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
            from world_engine.daily_life import DailyLifeService
            from world_engine.economy import EconomyService
            from world_engine.society import SocietyService
            if world_delta_seconds > 0:
                from world_engine.visits import VisitService
                VisitService.tick(connection, world_id, current_time)
                EconomyService.tick(connection, world_id, current_time)
                DailyLifeService.tick(connection, world_id, current_time, adjudication_due=adjudication_due)
                SocietyService.tick(connection, world_id, current_time)
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
            real_elapsed_seconds = max(
                0.0,
                (now - from_iso(last_real)).total_seconds() if last_real else 0.0,
            )
            world_delta_seconds = real_elapsed_seconds * old_time_scale
            current_time = previous_time + timedelta(seconds=world_delta_seconds)
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
            if world_delta_seconds > 0:
                from world_engine.daily_life import DailyLifeService
                from world_engine.economy import EconomyService
                from world_engine.society import SocietyService
                from world_engine.visits import VisitService
                VisitService.tick(connection, world_id, current_time)
                EconomyService.tick(connection, world_id, current_time)
                DailyLifeService.tick(connection, world_id, current_time, adjudication_due=adjudication_due)
                SocietyService.tick(connection, world_id, current_time)
            new_world_version = int(row["version"]) + 1
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
            SELECT c.id, c.satiety, c.energy,
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
