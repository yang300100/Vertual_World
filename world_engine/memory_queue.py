"""可恢复的记忆任务；外部推理始终发生在数据库事务外。"""

import json
from datetime import timedelta

from world_engine.bounded_calls import submit_call
from world_engine.orchestration import AgentContext, AgentName, MemoryCuratorAgent
from world_engine.repository import to_iso, utc_now


def process_memory_batch(engine, world_id: str, *, limit: int = 10) -> int:
    completed = 0
    for _ in range(limit):
        now = utc_now()
        with engine.database.write() as connection:
            connection.execute(
                """UPDATE memory_jobs SET status='failed',last_error='lease_expired'
                   WHERE world_id=? AND status='processing'
                   AND attempt_count>=3 AND updated_at<?""",
                (world_id, to_iso(now - timedelta(minutes=5))),
            )
            # 崩溃后的领取租约会过期；旧执行者须凭 attempt_count 才能提交。
            job = connection.execute(
                """SELECT * FROM memory_jobs WHERE world_id=? AND attempt_count<3 AND (
                    (status IN ('pending','failed') AND (retry_at IS NULL OR retry_at<=?))
                    OR (status='processing' AND updated_at<?))
                    ORDER BY created_at,id LIMIT 1""",
                (world_id, to_iso(now), to_iso(now - timedelta(minutes=5))),
            ).fetchone()
            if job is None:
                break
            attempt = int(job["attempt_count"]) + 1
            connection.execute(
                """UPDATE memory_jobs SET status='processing', attempt_count=?, updated_at=?
                   WHERE id=?""",
                (attempt, to_iso(now), job["id"]),
            )
        try:
            with engine.database.read() as connection:
                snapshot = engine.repository.get_snapshot(connection, world_id)
                row = connection.execute(
                    "SELECT * FROM world_events WHERE id=? AND world_id=?",
                    (job["event_id"], world_id),
                ).fetchone()
            if row is None:
                continue
            event = dict(row)
            ctx = AgentContext(
                name=AgentName.MEMORY_CURATOR,
                scene=engine.assembler.assemble(snapshot, trigger="event_followup"),
                snapshot=snapshot,
                model_backend=engine.agent_model_backend,
            )
            curator = MemoryCuratorAgent()
            candidates = submit_call(curator.run, ctx, events=[event]).result(
                timeout=engine.settings.world_agent_timeout_seconds
            )
            with engine.database.write() as connection:
                current = connection.execute(
                    "SELECT status,attempt_count FROM memory_jobs WHERE id=?", (job["id"],)
                ).fetchone()
                if (
                    current is None
                    or current["status"] != "processing"
                    or current["attempt_count"] != attempt
                ):
                    continue
                for candidate in candidates:
                    if candidate.event_id != event["id"] or not curator._visible_to(
                        ctx, candidate.character_id, event
                    ):
                        continue
                    # 行动本身已经保存亲历记忆时，不再生成同一人物的听闻副本。
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
            completed += 1
        except Exception as exc:
            with engine.database.write() as connection:
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
    return completed
