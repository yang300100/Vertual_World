"""哥哥，NPC 持续在意的相处事项保留来源，改变看法也留下经过。"""

import json
from datetime import timedelta
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from world_engine.repository import from_iso, to_iso

CONTINUITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS relationship_considerations (
 id TEXT PRIMARY KEY,world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 owner_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
 other_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
 kind TEXT NOT NULL CHECK(kind IN ('preference','concern','hope')),
 topic TEXT NOT NULL,meaning TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('active','resolved','set_aside')),
 version INTEGER NOT NULL,opened_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 opening_event_id TEXT NOT NULL REFERENCES world_events(id),source_event_id TEXT NOT NULL REFERENCES world_events(id),
 anchor_event_id TEXT NOT NULL REFERENCES world_events(id),CHECK(owner_id<>other_id)
);
CREATE INDEX IF NOT EXISTS idx_relationship_considerations_owner ON relationship_considerations(world_id,owner_id,other_id,status);
CREATE INDEX IF NOT EXISTS idx_relationship_considerations_recent ON relationship_considerations(world_id,owner_id,other_id,updated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_relationship_considerations_topic ON relationship_considerations(owner_id,other_id,kind,topic) WHERE status='active';
CREATE TABLE IF NOT EXISTS relationship_consideration_updates (
 id TEXT PRIMARY KEY,world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 owner_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,other_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
 thread_id TEXT REFERENCES relationship_considerations(id) ON DELETE CASCADE,
 source_event_id TEXT NOT NULL REFERENCES world_events(id),anchor_event_id TEXT,
 operation TEXT NOT NULL,proposal_json TEXT NOT NULL,applied INTEGER NOT NULL CHECK(applied IN (0,1)),
 reason TEXT NOT NULL,world_time TEXT NOT NULL,
 UNIQUE(owner_id,other_id,source_event_id,proposal_json)
);
CREATE TABLE IF NOT EXISTS relationship_continuity_followups (
 job_id TEXT PRIMARY KEY REFERENCES npc_outreach_jobs(id) ON DELETE CASCADE,
 thread_id TEXT NOT NULL REFERENCES relationship_considerations(id) ON DELETE CASCADE,
 version INTEGER NOT NULL,considered_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_continuity_followups_version ON relationship_continuity_followups(thread_id,version);
"""

# 哥哥，普通台词、道歉和提出邀请不能充当已经兑现的行为证据。
FULFILLED_EVENTS = {
    "social.appointment_overnight_completed",
    "social.appointment_journey_completed",
    "social.shared_completed", "social.appointment_completed", "social.appointment_repaired",
    "social.appointment_preparation_completed",
}


class ContinuityUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    operation: Literal["remember", "revise", "resolve", "set_aside"] = "remember"
    thread_id: str | None = Field(default=None, min_length=1, max_length=100)
    kind: Literal["preference", "concern", "hope"] | None = None
    topic: str = Field(default="", max_length=40)
    meaning: str = Field(min_length=1, max_length=160)
    anchor_event_id: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="before")
    @classmethod
    def defaults(cls, value):
        if isinstance(value, dict):
            value = {k: v for k, v in value.items() if not (k in {"operation", "topic"} and v is None)}
        return value

    @model_validator(mode="after")
    def coherent(self):
        if self.operation == "remember" and (self.thread_id or not self.kind or not self.topic):
            raise ValueError("新的相处事项需要本人的类别和主题，不能指定旧事项")
        if self.operation != "remember" and (not self.thread_id or self.kind or self.topic):
            raise ValueError("修改或结束只引用本人真实的事项，不能改换主题与类别")
        return self


class RelationshipContinuityService:
    @staticmethod
    def _fulfilled(c, event):
        payload = json.loads(event["payload_json"] or "{}")
        if event["event_type"]=="social.appointment_overnight_completed":
            return c.execute("SELECT 1 FROM journey_overnight_stays WHERE id=? AND world_id=? AND status='completed' AND source_event_id=?",(payload.get("stay_id"),event["world_id"],event["id"])).fetchone() is not None
        if event["event_type"]=="social.appointment_journey_completed":
            return c.execute("SELECT 1 FROM joint_journeys WHERE id=? AND world_id=? AND status='completed' AND source_event_id=?",(payload.get("journey_id"),event["world_id"],event["id"])).fetchone() is not None
        if event["event_type"] == "social.shared_completed":
            return c.execute("SELECT 1 FROM shared_life_moments WHERE id=? AND world_id=? AND status='completed' AND source_event_id=?", (payload.get("moment_id"), event["world_id"], event["id"])).fetchone() is not None
        if event["event_type"] == "social.appointment_preparation_completed":
            return c.execute("SELECT 1 FROM shared_preparation_plans WHERE id=? AND world_id=? AND status='completed' AND source_event_id=?", (payload.get("plan_id"), event["world_id"], event["id"])).fetchone() is not None
        if event["event_type"] == "social.appointment_completed":
            return c.execute("SELECT 1 FROM social_appointments WHERE id=? AND world_id=? AND status='completed' AND source_event_id=?", (payload.get("appointment_id"), event["world_id"], event["id"])).fetchone() is not None
        if event["event_type"] == "social.appointment_repaired":
            return c.execute("SELECT 1 FROM social_appointments a JOIN social_appointments old ON old.id=a.repairs_id WHERE a.id=? AND a.world_id=? AND a.status='completed' AND old.id=? AND old.repaired_by=a.id", (payload.get("appointment_id"), event["world_id"], payload.get("repairs_id"))).fetchone() is not None
        return False

    @staticmethod
    def _known(c, wid, owner, other, event_id, at):
        row = c.execute(
            "SELECT e.*,k.learned_world_time FROM world_events e JOIN character_event_knowledge k ON k.event_id=e.id AND k.character_id=? AND k.world_id=e.world_id "
            "WHERE e.id=? AND e.world_id=? AND ((e.actor_id=? AND e.target_id=?) OR (e.actor_id=? AND e.target_id=?))",
            (owner, event_id, wid, owner, other, other, owner),
        ).fetchone()
        if row and from_iso(row["occurred_at"]) <= at and from_iso(row["learned_world_time"]) <= at and json.loads(row["payload_json"] or "{}").get("information_scope") != "author_hidden":
            return row
        return None

    @classmethod
    def apply(cls, c, npc, other, at, source_event_id, proposals):
        if npc["is_player"] or not cls._known(c, npc["world_id"], npc["id"], other, source_event_id, at):
            return
        for proposal in proposals:
            encoded = proposal.model_dump_json()
            if c.execute("SELECT 1 FROM relationship_consideration_updates WHERE owner_id=? AND other_id=? AND source_event_id=? AND proposal_json=?", (npc["id"], other, source_event_id, encoded)).fetchone():
                continue
            anchor = proposal.anchor_event_id or source_event_id
            evidence = cls._known(c, npc["world_id"], npc["id"], other, anchor, at)
            thread = c.execute("SELECT * FROM relationship_considerations WHERE id=? AND world_id=? AND owner_id=? AND other_id=?", (proposal.thread_id, npc["world_id"], npc["id"], other)).fetchone() if proposal.thread_id else None
            applied, reason, tid = False, "", thread["id"] if thread else None
            if evidence is None:
                reason = "来源并非本人已知的双方真实相处，不能引用隐藏或未来事件。"
            elif proposal.operation == "remember":
                same = c.execute("SELECT id FROM relationship_considerations WHERE owner_id=? AND other_id=? AND kind=? AND topic=? AND status='active'", (npc["id"], other, proposal.kind, proposal.topic)).fetchone()
                if same:
                    tid, reason = same[0], "同一主题已保留；重复记忆不重启联系机会。"
                elif c.execute("SELECT COUNT(*) FROM relationship_considerations WHERE owner_id=? AND other_id=? AND status='active'", (npc["id"], other)).fetchone()[0] >= 8:
                    reason = "本人已保留八项持续事项，请先整理已有事项。"
                else:
                    tid = str(uuid4())
                    c.execute("INSERT INTO relationship_considerations VALUES (?,?,?,?,?,?,?,'active',1,?,?,?,?,?)", (
                        tid, npc["world_id"], npc["id"], other, proposal.kind, proposal.topic, proposal.meaning,
                        to_iso(at), to_iso(at), source_event_id, source_event_id, anchor))
                    applied = True
            elif thread is None or thread["status"] != "active":
                reason = "这不是本人对该人物仍在保留的事项。"
            elif proposal.operation == "set_aside":
                c.execute("UPDATE relationship_considerations SET status='set_aside',meaning=?,version=version+1,updated_at=?,source_event_id=?,anchor_event_id=? WHERE id=?", (proposal.meaning, to_iso(at), source_event_id, anchor, tid))
                applied = True
            elif proposal.operation == "resolve":
                opening = c.execute("SELECT created_at FROM world_events WHERE id=?", (thread["opening_event_id"],)).fetchone()
                if evidence["event_type"] not in FULFILLED_EVENTS or not cls._fulfilled(c, evidence) or from_iso(evidence["occurred_at"]) < from_iso(thread["opened_at"]) or from_iso(evidence["created_at"]) <= from_iso(opening[0]):
                    reason = "结束事项需要形成后真正兑现的相处或筹备证据；道歉、答应或旧回执不能代替。"
                else:
                    c.execute("UPDATE relationship_considerations SET status='resolved',meaning=?,version=version+1,updated_at=?,source_event_id=?,anchor_event_id=? WHERE id=?", (proposal.meaning, to_iso(at), source_event_id, anchor, tid))
                    applied = True
            elif proposal.meaning == thread["meaning"]:
                reason = "内容未变化，保留形成时间和联系节奏。"
            else:
                c.execute("UPDATE relationship_considerations SET meaning=?,version=version+1,updated_at=?,source_event_id=?,anchor_event_id=? WHERE id=?", (proposal.meaning, to_iso(at), source_event_id, anchor, tid))
                applied = True
            c.execute("INSERT INTO relationship_consideration_updates VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                str(uuid4()), npc["world_id"], npc["id"], other, tid, source_event_id, anchor,
                proposal.operation, encoded, int(applied), reason, to_iso(at)))

    @classmethod
    def context(cls, c, wid, owner, other, at, query="", *, focus_id=None):
        from world_engine.dialogue_relevance import DialogueRelevance
        from world_engine.intimacy import IntimacyService

        person = c.execute("SELECT is_player FROM characters WHERE id=? AND world_id=?", (owner, wid)).fetchone()
        if not person or person[0]:
            return []
        rows = [dict(r) for r in c.execute("SELECT * FROM relationship_considerations WHERE world_id=? AND owner_id=? AND other_id=? AND status='active' ORDER BY updated_at DESC,rowid DESC", (wid, owner, other))
            if cls._known(c, wid, owner, other, r["anchor_event_id"], at)]
        relevance = DialogueRelevance(query)
        rows.sort(key=lambda r: (r["id"] == focus_id, relevance.matches(r), r["kind"] == "concern", r["updated_at"]), reverse=True)
        selected_rows = rows[:1]
        preference = next((r for r in rows[1:] if r["kind"] == "preference"), None)
        if preference:
            selected_rows.append(preference)
        closed = c.execute("SELECT * FROM relationship_considerations WHERE world_id=? AND owner_id=? AND other_id=? AND status<>'active' ORDER BY updated_at DESC,rowid DESC LIMIT 1", (wid,owner,other)).fetchone()
        if closed and cls._known(c,wid,owner,other,closed["anchor_event_id"],at):
            selected_rows.append(dict(closed))
        for row in rows:
            if len(selected_rows) >= 3:
                break
            if not any(r["id"] == row["id"] for r in selected_rows):
                selected_rows.append(row)
        selected = [{k: r[k] for k in ("id", "kind", "topic", "status", "meaning", "anchor_event_id")} for r in selected_rows[:3]]
        IntimacyService.mask_records(c, wid, owner, selected, ("topic", "meaning"))
        return selected

    @classmethod
    def opportunity(cls, c, wid, owner, other, at):
        for row in c.execute(
            "SELECT r.* FROM relationship_considerations r WHERE r.world_id=? AND r.owner_id=? AND r.other_id=? AND r.status='active' AND r.kind IN ('concern','hope') AND r.updated_at<=? "
            "AND NOT EXISTS (SELECT 1 FROM relationship_continuity_followups f JOIN npc_outreach_jobs j ON j.id=f.job_id WHERE f.thread_id=r.id AND f.version=r.version "
            "AND (f.considered_at IS NOT NULL OR (j.world_day>=? AND j.status IN ('pending','processing','failed')))) ORDER BY r.updated_at,r.rowid LIMIT 8",
            (wid, owner, other, to_iso(at - timedelta(days=1)), at.date().isoformat()),
        ):
            if cls._known(c, wid, owner, other, row["anchor_event_id"], at):
                return row
        return None

    @staticmethod
    def attach(c, job_id, thread):
        c.execute("INSERT OR IGNORE INTO relationship_continuity_followups(job_id,thread_id,version) VALUES (?,?,?)", (job_id, thread["id"], thread["version"]))

    @staticmethod
    def considered(c, job_id, at):
        c.execute("UPDATE relationship_continuity_followups SET considered_at=? WHERE job_id=?", (to_iso(at), job_id))

    @staticmethod
    def current_job(c, job_id):
        row = c.execute("SELECT r.status,r.version,f.version expected,f.considered_at FROM relationship_continuity_followups f JOIN relationship_considerations r ON r.id=f.thread_id WHERE f.job_id=?", (job_id,)).fetchone()
        return row is None or row["status"] == "active" and row["version"] == row["expected"] and row["considered_at"] is None
