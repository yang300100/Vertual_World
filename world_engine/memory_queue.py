"""可恢复的记忆任务；按批整理事件，限制后台模型请求。"""

import json
import logging
from datetime import timedelta

from world_engine.bounded_calls import submit_call
from world_engine.orchestration import AgentContext, AgentName, MemoryCuratorAgent
from world_engine.repository import to_iso, utc_now

LOGGER = logging.getLogger("virtual-world.memory-queue")


def process_memory_batch(engine, world_id: str, *, limit: int = 10) -> int:
    """一批事件最多调用一次记忆模型，并记录用量与任务结果。"""
    batch_size = min(max(1, limit), engine.settings.memory_jobs_per_batch, 4)
    now = utc_now()
    with engine.database.write() as connection:
        connection.execute(
            """UPDATE memory_jobs SET status='failed',last_error='lease_expired'
               WHERE world_id=? AND status='processing'
               AND attempt_count>=3 AND updated_at<?""",
            (world_id, to_iso(now - timedelta(minutes=5))),
        )
        rows = connection.execute(
            """SELECT * FROM memory_jobs WHERE world_id=? AND attempt_count<3 AND (
                (status IN ('pending','failed') AND (retry_at IS NULL OR retry_at<=?))
                OR (status='processing' AND updated_at<?))
                ORDER BY created_at,id LIMIT ?""",
            (world_id, to_iso(now), to_iso(now - timedelta(minutes=5)), batch_size),
        ).fetchall()
        if not rows:
            return 0
        jobs = [dict(row) for row in rows]
        claimed: list[tuple[dict[str, object], int]] = []
        for job in jobs:
            attempt = int(job["attempt_count"]) + 1
            connection.execute(
                """UPDATE memory_jobs SET status='processing',attempt_count=?,updated_at=?
                   WHERE id=?""",
                (attempt, to_iso(now), job["id"]),
            )
            claimed.append((job, attempt))

    events_by_id: dict[str, dict[str, object]] = {}
    try:
        with engine.database.read() as connection:
            snapshot = engine.repository.get_snapshot(connection, world_id)
            ids = [str(job["event_id"]) for job, _ in claimed]
            placeholders = ",".join("?" for _ in ids)
            event_rows = connection.execute(
                f"SELECT * FROM world_events WHERE world_id=? AND id IN ({placeholders})",
                (world_id, *ids),
            ).fetchall()
        events_by_id = {str(row["id"]): dict(row) for row in event_rows}
        valid_events = [
            events_by_id[str(job["event_id"])]
            for job, _ in claimed
            if str(job["event_id"]) in events_by_id
            and events_by_id[str(job["event_id"])].get("event_type") != "action.rejected"
        ]
        if not valid_events:
            candidates = []
            model_metrics = None
        else:
            ctx = AgentContext(
                name=AgentName.MEMORY_CURATOR,
                scene=engine.assembler.assemble(snapshot, trigger="event_followup"),
                snapshot=snapshot,
                model_backend=engine.agent_model_backend,
            )
            curator = MemoryCuratorAgent()
            candidates = submit_call(
                lambda: curator.run(
                    ctx,
                    events=valid_events,
                    max_output_tokens=engine.settings.memory_batch_max_output_tokens,
                )
            ).result(timeout=engine.settings.world_agent_timeout_seconds)
            model_metrics = ctx.model_metrics

        with engine.database.write() as connection:
            for job, attempt in claimed:
                current = connection.execute(
                    "SELECT status,attempt_count FROM memory_jobs WHERE id=?", (job["id"],)
                ).fetchone()
                if (
                    current is None
                    or current["status"] != "processing"
                    or current["attempt_count"] != attempt
                ):
                    continue
                event = events_by_id.get(str(job["event_id"]))
                if event is None or event.get("event_type") == "action.rejected":
                    connection.execute(
                        "UPDATE memory_jobs SET status='done',last_error=NULL,retry_at=NULL,updated_at=? WHERE id=?",
                        (to_iso(utc_now()), job["id"]),
                    )
                    continue
                for candidate in candidates:
                    if candidate.event_id != event["id"] or not curator._visible_to(
                        ctx, candidate.character_id, event
                    ):
                        continue
                    connection.execute(
                        """INSERT OR IGNORE INTO character_memories(
                            id,world_id,character_id,event_id,memory_type,summary,
                            importance,confidence,created_at)
                            SELECT ?,?,?,?,?,?,?,?,? WHERE NOT EXISTS(
                                SELECT 1 FROM character_memories
                                WHERE character_id=? AND event_id=?)""",
                        (
                            f"curated:{candidate.character_id}:{event['id']}",
                            world_id,
                            candidate.character_id,
                            event["id"],
                            candidate.memory_type,
                            candidate.summary,
                            candidate.importance,
                            candidate.confidence,
                            to_iso(utc_now()),
                            candidate.character_id,
                            event["id"],
                        ),
                    )
                connection.execute(
                    """UPDATE memory_jobs SET status='done',last_error=NULL,retry_at=NULL,
                       updated_at=? WHERE id=?""",
                    (to_iso(utc_now()), job["id"]),
                )
            if model_metrics is not None:
                LOGGER.info(
                    "记忆批次完成 world=%s events=%d model=%s input_tokens=%s output_tokens=%s latency_ms=%s",
                    world_id,
                    len(valid_events),
                    model_metrics.model_name,
                    model_metrics.input_tokens,
                    model_metrics.output_tokens,
                    model_metrics.latency_ms,
                )
        return len(claimed)
    except Exception as exc:
        with engine.database.write() as connection:
            for job, attempt in claimed:
                connection.execute(
                    """UPDATE memory_jobs SET status='failed',last_error=?,retry_at=?,updated_at=?
                       WHERE id=? AND status='processing' AND attempt_count=?""",
                    (
                        json.dumps({"type": type(exc).__name__}),
                        to_iso(utc_now() + timedelta(seconds=30 * attempt)),
                        to_iso(utc_now()),
                        job["id"],
                        attempt,
                    ),
                )
        LOGGER.warning(
            "记忆批次失败 world=%s events=%d attempt_max=%d error=%s",
            world_id,
            len(claimed),
            max(attempt for _, attempt in claimed),
            type(exc).__name__,
        )
        return 0
