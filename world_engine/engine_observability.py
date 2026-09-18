"""`ObservabilityMixin`：Agent 审计落库、运行记录视图与各类“失败不影响主事务”的同步兜底。

从 `WorldEngine` 切出的职责切片。方法体、签名与 `self` 语义一字未改。
"""

from __future__ import annotations

import json
import logging
import sqlite3

from world_engine.domain import AgentRunView
from world_engine.orchestration import AgentProposalRecord, AgentRunRecord
from world_engine.repository import to_iso, utc_now

LOGGER = logging.getLogger("virtual-world.engine")


class ObservabilityMixin:
    """`WorldEngine` 的可观测性职责切片。"""

    def _write_agent_audit(
        self,
        connection: sqlite3.Connection,
        world_id: str,
        run_records: list[AgentRunRecord],
        proposal_records: list[AgentProposalRecord],
    ) -> None:
        for run in run_records:
            connection.execute(
                """
                INSERT INTO agent_runs(
                    id, world_id, trigger, agent_name, input_snapshot_version, status,
                    model_name, input_tokens, output_tokens, latency_ms, error_text,
                    created_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.id,
                    run.world_id,
                    run.trigger,
                    run.agent_name,
                    run.input_snapshot_version,
                    run.status,
                    run.model_name,
                    run.input_tokens,
                    run.output_tokens,
                    run.latency_ms,
                    run.error_text,
                    to_iso(run.created_at),
                    to_iso(run.completed_at) if run.completed_at else None,
                ),
            )
        for record in proposal_records:
            connection.execute(
                """
                INSERT INTO agent_proposals(
                    id, run_id, world_id, actor_id, proposal_type, payload_json,
                    validation_status, rejection_reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.run_id,
                    record.world_id,
                    record.actor_id,
                    record.proposal_type,
                    json.dumps(record.payload, ensure_ascii=False),
                    record.validation_status,
                    record.rejection_reason,
                    to_iso(record.created_at),
                ),
            )


    def _run_records_to_views(self, run_records: list[AgentRunRecord]) -> list[AgentRunView]:
        return [
            AgentRunView(
                id=run.id,
                world_id=run.world_id,
                trigger=run.trigger,
                agent_name=run.agent_name,
                input_snapshot_version=run.input_snapshot_version,
                status=run.status,
                model_name=run.model_name,
                input_tokens=run.input_tokens,
                output_tokens=run.output_tokens,
                latency_ms=run.latency_ms,
                error_text=run.error_text,
                created_at=run.created_at,
                completed_at=run.completed_at,
            )
            for run in run_records
        ]


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


    def _sync_memory_candidates_safely(self, world_id: str) -> None:
        """事件插入触发器已在原事务中入队；这里不再同步调用模型。"""
