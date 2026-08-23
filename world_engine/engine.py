from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from uuid import uuid4

from world_engine.actions import ActionService
from world_engine.clock import WorldClockService
from world_engine.config import Settings
from world_engine.database import Database
from world_engine.decisions import DecisionProvider, RuleDecisionProvider, build_decision_provider
from world_engine.domain import (
    ActionProposal,
    CharacterState,
    ClockUpdateResult,
    HeartbeatResult,
    TickResult,
    WorldSnapshot,
)
from world_engine.history import HistoryExportResult, WorldHistoryLogger
from world_engine.repository import WorldRepository, to_iso, utc_now
from world_engine.time_utils import next_adjudication_boundary


class ConcurrentWorldUpdateError(RuntimeError):
    pass


LOGGER = logging.getLogger("virtual-world.engine")


class WorldEngine:
    """协调状态心跳、人物决策、本地裁判、事件、记忆和历史日志。"""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        decision_provider: DecisionProvider | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.repository = WorldRepository()
        self.decision_provider = decision_provider or build_decision_provider(settings)
        self.fallback_provider = RuleDecisionProvider()
        self.actions = ActionService()
        self.clock = WorldClockService(database, settings)
        self.history_logger = (
            WorldHistoryLogger(database, settings.history_directory)
            if settings.history_logging_enabled and settings.history_directory is not None
            else None
        )

    def reset_offline_baseline(self, real_now: datetime | None = None) -> int:
        return self.clock.reset_offline_baseline(real_now)

    def heartbeat(
        self,
        world_id: str,
        *,
        real_now: datetime | None = None,
        elapsed_seconds: float | None = None,
    ) -> HeartbeatResult:
        result = self.clock.heartbeat(
            world_id,
            real_now=real_now,
            elapsed_seconds=elapsed_seconds,
        )
        if result.adjudication_due:
            try:
                with self.database.read() as connection:
                    snapshot = self.repository.get_snapshot(connection, world_id)
                result.adjudication = self.adjudicate(
                    world_id,
                    trigger="scheduled_12h",
                    window_start=snapshot.world.last_adjudication_time,
                    window_end=result.current_time,
                )
            except Exception as exc:
                result.adjudication_error = str(exc)
                LOGGER.exception("世界%s到达裁判点，但自主裁判未完成", world_id)
        self._sync_state_logs_safely(world_id)
        return result

    def set_time_scale(
        self,
        world_id: str,
        new_time_scale: float,
        *,
        operator: str = "main_view",
    ) -> ClockUpdateResult:
        # 先按旧比例结算到此刻，避免调速把此前的现实时间误按新比例计算。
        self.heartbeat(world_id)
        result = self.clock.set_time_scale(
            world_id,
            new_time_scale,
            operator=operator,
        )
        self._sync_history_safely(world_id)
        return result

    def tick(self, world_id: str) -> TickResult:
        """兼容旧入口：现在只强制裁判，不再推进世界时间。"""

        return self.adjudicate(world_id, trigger="manual")

    def adjudicate(
        self,
        world_id: str,
        *,
        trigger: str,
        character_ids: list[str] | None = None,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> TickResult:
        started_at = utc_now()
        adjudication_id = str(uuid4())
        with self.database.read() as connection:
            snapshot = self.repository.get_snapshot(connection, world_id)

        active_characters = self._select_active_characters(snapshot, character_ids)
        proposals, provider_name, fallback_used, provider_error = self._propose(
            snapshot, active_characters
        )
        proposal_by_actor = self._normalize_proposals(
            snapshot, active_characters, proposals
        )
        resolved_window_start = window_start or snapshot.world.current_time
        resolved_window_end = window_end or snapshot.world.current_time

        try:
            with self.database.write() as connection:
                current_snapshot = self.repository.get_snapshot(connection, world_id)
                if current_snapshot.world.version != snapshot.world.version:
                    raise ConcurrentWorldUpdateError(
                        f"世界版本已经从{snapshot.world.version}变化为"
                        f"{current_snapshot.world.version}，本轮必须重新决策"
                    )

                connection.execute(
                    """
                    UPDATE world_runtime
                    SET last_tick_started_at = ?, last_tick_status = 'running'
                    WHERE world_id = ?
                    """,
                    (to_iso(started_at), world_id),
                )
                outcomes = []
                for character in active_characters:
                    proposal = proposal_by_actor[character.id]
                    outcomes.append(
                        self.actions.execute(
                            connection,
                            world_id=world_id,
                            tick_id=adjudication_id,
                            occurred_at=snapshot.world.current_time,
                            proposal=proposal,
                        )
                    )

                new_version = snapshot.world.version + 1
                completed_at = utc_now()
                connection.execute(
                    """
                    UPDATE worlds
                    SET version = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (new_version, to_iso(completed_at), world_id),
                )
                connection.execute(
                    """
                    UPDATE world_runtime
                    SET tick_count = tick_count + 1,
                        last_tick_finished_at = ?,
                        last_tick_status = 'completed'
                    WHERE world_id = ?
                    """,
                    (to_iso(completed_at), world_id),
                )
                if trigger == "scheduled_12h":
                    next_boundary = next_adjudication_boundary(
                        resolved_window_end,
                        snapshot.world.adjudication_interval_minutes,
                    )
                    connection.execute(
                        """
                        UPDATE world_clock
                        SET last_adjudication_world_time = ?,
                            next_adjudication_world_time = ?,
                            clock_revision = clock_revision + 1,
                            updated_at = ?
                        WHERE world_id = ?
                        """,
                        (
                            to_iso(resolved_window_end),
                            to_iso(next_boundary),
                            to_iso(completed_at),
                            world_id,
                        ),
                    )
                elif trigger == "player_intervention":
                    connection.execute(
                        """
                        UPDATE world_clock
                        SET last_player_intervention_world_time = ?, updated_at = ?
                        WHERE world_id = ?
                        """,
                        (
                            to_iso(snapshot.world.current_time),
                            to_iso(completed_at),
                            world_id,
                        ),
                    )

                adjudication_event_id = str(uuid4())
                connection.execute(
                    """
                    INSERT INTO world_events(
                        id, world_id, tick_id, occurred_at, event_type,
                        summary, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, 'world.adjudication', ?, ?, ?)
                    """,
                    (
                        adjudication_event_id,
                        world_id,
                        adjudication_id,
                        to_iso(snapshot.world.current_time),
                        "世界在当前时间完成了一次人物与事件裁判。",
                        json.dumps(
                            {
                                "trigger": trigger,
                                "provider": provider_name,
                                "fallback_used": fallback_used,
                                "provider_error": provider_error,
                                "active_character_count": len(active_characters),
                                "accepted_action_count": sum(
                                    1 for item in outcomes if item.accepted
                                ),
                                "previous_version": snapshot.world.version,
                                "current_version": new_version,
                                "window_start": to_iso(resolved_window_start),
                                "window_end": to_iso(resolved_window_end),
                            },
                            ensure_ascii=False,
                        ),
                        to_iso(completed_at),
                    ),
                )
                proposal_records = [
                    proposal_by_actor[item.id].model_dump(mode="json")
                    for item in active_characters
                ]
                rejections = [
                    item.model_dump(mode="json")
                    for item in outcomes
                    if not item.accepted
                ]
                final_event_ids = [
                    item.event_id for item in outcomes if item.event_id is not None
                ] + [adjudication_event_id]
                connection.execute(
                    """
                    INSERT INTO adjudication_runs(
                        id, world_id, trigger_type, window_start, window_end,
                        provider, selected_character_ids_json, proposals_json,
                        rule_rejections_json, final_event_ids_json,
                        fallback_used, status, started_at, completed_at, error_text
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?)
                    """,
                    (
                        adjudication_id,
                        world_id,
                        trigger,
                        to_iso(resolved_window_start),
                        to_iso(resolved_window_end),
                        provider_name,
                        json.dumps(
                            [item.id for item in active_characters], ensure_ascii=False
                        ),
                        json.dumps(proposal_records, ensure_ascii=False),
                        json.dumps(rejections, ensure_ascii=False),
                        json.dumps(final_event_ids, ensure_ascii=False),
                        int(fallback_used),
                        to_iso(started_at),
                        to_iso(completed_at),
                        provider_error,
                    ),
                )

            result = TickResult(
                world_id=world_id,
                tick_id=adjudication_id,
                started_at=started_at,
                completed_at=completed_at,
                previous_time=snapshot.world.current_time,
                current_time=snapshot.world.current_time,
                previous_version=snapshot.world.version,
                current_version=new_version,
                outcomes=outcomes,
                trigger=trigger,
                provider=provider_name,
                fallback_used=fallback_used,
            )
            self._sync_history_safely(world_id)
            return result
        except ConcurrentWorldUpdateError:
            raise
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower():
                raise
            self._mark_failed(world_id)
            raise
        except Exception:
            self._mark_failed(world_id)
            raise

    def close(self) -> None:
        close = getattr(self.decision_provider, "close", None)
        if close is not None:
            close()

    def sync_history(self, world_id: str) -> HistoryExportResult:
        if self.history_logger is None:
            raise RuntimeError("世界历史日志未启用")
        return self.history_logger.sync_world(world_id)

    def _select_active_characters(
        self,
        snapshot: WorldSnapshot,
        character_ids: list[str] | None = None,
    ) -> list[CharacterState]:
        if character_ids is not None:
            allowed = set(character_ids)
            selected = [item for item in snapshot.characters if item.id in allowed]
            return selected[: self.settings.active_character_limit]

        def priority(character: CharacterState) -> tuple[int, str]:
            urgency = character.hunger + (100 - character.energy)
            if character.money < 10:
                urgency += 15
            if character.is_core:
                urgency += 40
            return (-urgency, character.name)

        return sorted(snapshot.characters, key=priority)[: self.settings.active_character_limit]

    def _propose(
        self, snapshot: WorldSnapshot, characters: list[CharacterState]
    ) -> tuple[list[ActionProposal], str, bool, str | None]:
        try:
            return (
                self.decision_provider.propose(snapshot, characters),
                self.decision_provider.name,
                False,
                None,
            )
        except Exception as exc:
            LOGGER.warning(
                "决策器%s调用失败，当前裁判降级为规则引擎：%s",
                self.decision_provider.name,
                exc,
            )
            return (
                self.fallback_provider.propose(snapshot, characters),
                self.fallback_provider.name,
                True,
                str(exc),
            )

    def _normalize_proposals(
        self,
        snapshot: WorldSnapshot,
        characters: list[CharacterState],
        proposals: list[ActionProposal],
    ) -> dict[str, ActionProposal]:
        allowed_ids = {item.id for item in characters}
        normalized: dict[str, ActionProposal] = {}
        for proposal in proposals:
            if proposal.actor_id in allowed_ids and proposal.actor_id not in normalized:
                normalized[proposal.actor_id] = proposal

        missing = [item for item in characters if item.id not in normalized]
        for fallback in self.fallback_provider.propose(snapshot, missing):
            normalized[fallback.actor_id] = fallback
        return normalized

    def _mark_failed(self, world_id: str) -> None:
        try:
            with self.database.write() as connection:
                connection.execute(
                    """
                    UPDATE world_runtime
                    SET last_tick_finished_at = ?, last_tick_status = 'failed'
                    WHERE world_id = ?
                    """,
                    (to_iso(utc_now()), world_id),
                )
        except Exception:
            # 原始异常必须优先返回，状态记录失败不能掩盖真正原因。
            return

    def _sync_history_safely(self, world_id: str) -> None:
        if self.history_logger is None:
            return
        try:
            self.history_logger.sync_world(world_id)
        except Exception:
            # 日志是客观事件表的派生视图，导出失败不能回滚已完成事务。
            LOGGER.exception("世界%s事务已完成，但历史日志同步失败", world_id)

    def _sync_state_logs_safely(self, world_id: str) -> None:
        if self.history_logger is None:
            return
        try:
            self.history_logger.sync_state_logs(world_id)
        except Exception:
            # 状态日志同样可以从SQLite心跳与状态差值表重新生成。
            LOGGER.exception("世界%s心跳已完成，但人物状态日志同步失败", world_id)
