"""真实相处未完成后，保留本人可自主考虑的再次联系机会。"""

import json

from world_engine.repository import from_iso, to_iso


class AppointmentFollowupService:
    # 哥哥，这只是让本人考虑，不推定对方是否到场或为什么未完成。
    JOB_PREFIX = "本人已确认的相处约定时间已过且未共同完成"
    REASON = (
        JOB_PREFIX + "；可结合本人经历和当前处境，自主决定是否联系、提出需要对方同意的补约，"
        "或暂不联系。不能猜测对方是否到场、其位置或原因。"
    )

    @classmethod
    def _valid(cls, c, world_id, npc_id, player_id, source_event_id, at):
        row = c.execute(
            "SELECT a.*,e.rowid AS missed_order,e.occurred_at AS missed_at,"
            "e.actor_id AS missed_actor,e.target_id AS missed_target,e.payload_json AS missed_payload "
            "FROM social_appointments a JOIN world_events e ON e.id=a.source_event_id AND e.world_id=a.world_id "
            "WHERE a.world_id=? AND a.source_event_id=? AND a.status='missed' AND a.repaired_by IS NULL "
            "AND e.event_type='social.appointment_missed' "
            "AND ((a.proposer_id=? AND a.recipient_id=?) OR (a.proposer_id=? AND a.recipient_id=?))",
            (world_id, source_event_id, npc_id, player_id, player_id, npc_id),
        ).fetchone()
        if row is None:
            return None
        try:
            missed_payload = json.loads(row["missed_payload"] or "{}")
            if (not isinstance(missed_payload, dict) or missed_payload.get("appointment_id") != row["id"]
                    or {row["missed_actor"], row["missed_target"]} != {npc_id, player_id}
                    or not from_iso(row["ends_at"]) <= from_iso(row["missed_at"]) <= at):
                return None
        except (TypeError, ValueError):
            return None
        if c.execute(
            "SELECT 1 FROM social_appointments WHERE world_id=? AND repairs_id=? "
            "AND status IN ('pending','accepted','active') LIMIT 1", (world_id, row["id"]),
        ).fetchone():
            return None
        # 哥哥，错过事件本身不能证明本人知道另一方的境况；只核验本人曾真实获知双方确认。
        confirmations = c.execute(
            "SELECT e.rowid AS event_order,e.occurred_at,e.payload_json,k.learned_world_time "
            "FROM world_events e JOIN character_event_knowledge k ON k.event_id=e.id AND k.world_id=e.world_id "
            "JOIN event_observers o ON o.event_id=e.id AND o.character_id=k.character_id "
            "WHERE e.world_id=? AND e.event_type='social.appointment_accepted' "
            "AND ((e.actor_id=? AND e.target_id=?) OR (e.actor_id=? AND e.target_id=?)) "
            "AND k.character_id=? AND k.source_kind='experienced' AND o.channel='participant'",
            (world_id, npc_id, player_id, player_id, npc_id, npc_id),
        ).fetchall()
        for event in confirmations:
            try:
                payload = json.loads(event["payload_json"] or "{}")
                if (isinstance(payload, dict) and payload.get("appointment_id") == row["id"]
                        and event["event_order"] < row["missed_order"]
                        and from_iso(row["proposed_at"]) <= from_iso(event["occurred_at"]) < from_iso(row["starts_at"])
                        and from_iso(event["occurred_at"]) <= from_iso(event["learned_world_time"]) <= at):
                    return row
            except (TypeError, ValueError):
                continue
        return None

    @classmethod
    def opportunity(cls, c, world_id, npc_id, player_id, at):
        """仅为本人尚未考虑过且仍可商量的真实旧约提供候选。"""
        rows = c.execute(
            "SELECT a.source_event_id FROM social_appointments a "
            "WHERE a.world_id=? AND a.status='missed' AND a.repaired_by IS NULL AND a.ends_at<=? "
            "AND ((a.proposer_id=? AND a.recipient_id=?) OR (a.proposer_id=? AND a.recipient_id=?)) "
            "AND NOT EXISTS (SELECT 1 FROM npc_outreach_jobs j WHERE j.world_id=a.world_id "
            "AND j.npc_id=? AND j.player_id=? AND j.source_event_id=a.source_event_id "
            "AND (j.status IN ('pending','processing','done','declined') "
            "OR (j.status='failed' AND j.attempts>=3))) "
            "ORDER BY a.ends_at DESC,a.rowid DESC LIMIT 12",
            (world_id, to_iso(at), npc_id, player_id, player_id, npc_id, npc_id, player_id),
        ).fetchall()
        for row in rows:
            if cls._valid(c, world_id, npc_id, player_id, row["source_event_id"], at):
                return row["source_event_id"]
        return None

    @classmethod
    def current_job(cls, c, job, at):
        """调用前后重查本人已知的原约和当前状态，其他种类的任务不受影响。"""
        if not str(job["reason"] or "").startswith(cls.JOB_PREFIX):
            return True
        return bool(job["source_event_id"] and cls._valid(
            c, job["world_id"], job["npc_id"], job["player_id"], job["source_event_id"], at,
        ))
