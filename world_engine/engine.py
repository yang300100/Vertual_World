from __future__ import annotations

import json
import logging
import sqlite3
from datetime import timedelta
from uuid import uuid4

from world_engine.actions import ActionService
from world_engine.config import Settings
from world_engine.database import Database
from world_engine.decisions import DecisionProvider, RuleDecisionProvider, build_decision_provider
from world_engine.domain import ActionProposal, CharacterState, TickResult, WorldSnapshot
from world_engine.history import HistoryExportResult, WorldHistoryLogger
from world_engine.repository import WorldRepository, to_iso, utc_now


class ConcurrentWorldUpdateError(RuntimeError):
    pass


LOGGER = logging.getLogger("virtual-world.engine")


class WorldEngine:
    """协调感知、决策、校验、结算、事件和记忆的世界推进器。"""

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
        self.history_logger = (
            WorldHistoryLogger(database, settings.history_directory)
            if settings.history_logging_enabled and settings.history_directory is not None
            else None
        )

    def tick(self, world_id: str) -> TickResult:
        started_at = utc_now()
        tick_id = str(uuid4())
        with self.database.read() as connection:
            snapshot = self.repository.get_snapshot(connection, world_id)

        active_characters = self._select_active_characters(snapshot)
        proposals, provider_name = self._propose(snapshot, active_characters)
        proposal_by_actor = self._normalize_proposals(snapshot, active_characters, proposals)

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
                self._apply_background_needs(connection, world_id)

                outcomes = []
                for character in active_characters:
                    proposal = proposal_by_actor[character.id]
                    outcomes.append(
                        self.actions.execute(
                            connection,
                            world_id=world_id,
                            tick_id=tick_id,
                            occurred_at=snapshot.world.current_time,
                            proposal=proposal,
                        )
                    )

                new_time = snapshot.world.current_time + timedelta(
                    minutes=snapshot.world.minutes_per_tick
                )
                new_version = snapshot.world.version + 1
                completed_at = utc_now()
                connection.execute(
                    """
                    UPDATE worlds
                    SET current_time = ?, version = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (to_iso(new_time), new_version, to_iso(completed_at), world_id),
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
                connection.execute(
                    """
                    INSERT INTO world_events(
                        id, world_id, tick_id, occurred_at, event_type,
                        summary, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, 'world.tick', ?, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        world_id,
                        tick_id,
                        to_iso(new_time),
                        f"世界时间推进了{snapshot.world.minutes_per_tick}分钟。",
                        json.dumps(
                            {
                                "provider": provider_name,
                                "active_character_count": len(active_characters),
                                "accepted_action_count": sum(
                                    1 for item in outcomes if item.accepted
                                ),
                                "previous_version": snapshot.world.version,
                                "current_version": new_version,
                                "previous_time": to_iso(snapshot.world.current_time),
                                "current_time": to_iso(new_time),
                            },
                            ensure_ascii=False,
                        ),
                        to_iso(completed_at),
                    ),
                )

            result = TickResult(
                world_id=world_id,
                tick_id=tick_id,
                started_at=started_at,
                completed_at=completed_at,
                previous_time=snapshot.world.current_time,
                current_time=new_time,
                previous_version=snapshot.world.version,
                current_version=new_version,
                outcomes=outcomes,
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

    def _select_active_characters(self, snapshot: WorldSnapshot) -> list[CharacterState]:
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
    ) -> tuple[list[ActionProposal], str]:
        try:
            return self.decision_provider.propose(snapshot, characters), self.decision_provider.name
        except Exception as exc:
            LOGGER.warning(
                "决策器%s调用失败，当前轮次降级为规则引擎：%s",
                self.decision_provider.name,
                exc,
            )
            return self.fallback_provider.propose(snapshot, characters), self.fallback_provider.name

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

    @staticmethod
    def _apply_background_needs(connection, world_id: str) -> None:
        now = to_iso(utc_now())
        connection.execute(
            """
            UPDATE characters
            SET hunger = MIN(100, hunger + 3),
                energy = MAX(0, energy - 2),
                updated_at = ?
            WHERE world_id = ?
            """,
            (now, world_id),
        )

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
            # 日志是客观事件表的派生视图，导出失败不能回滚已经完成的世界轮次。
            LOGGER.exception("世界%s已完成推进，但历史日志同步失败", world_id)
