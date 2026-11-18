"""哥哥，亲密关系由双方的明确选择维持，经历和界限都留下来源。"""

from datetime import timedelta
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from world_engine.actions import ActionService
from world_engine.demographics import age_years_at
from world_engine.geo import great_circle_distance_km
from world_engine.proximity import same_room
from world_engine.relationship_continuity import ContinuityUpdate, RelationshipContinuityService
from world_engine.repository import from_iso, to_iso

INTIMACY_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_world_events_pair_time
ON world_events(world_id,actor_id,target_id,occurred_at DESC);
CREATE TABLE IF NOT EXISTS intimate_bonds (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    first_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    second_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    proposer_id TEXT NOT NULL REFERENCES characters(id),
    kind TEXT NOT NULL CHECK(kind IN ('close_friend','romantic')),
    status TEXT NOT NULL CHECK(status IN ('pending','active','declined','ended','withdrawn','expired')),
    statement TEXT NOT NULL,
    response TEXT,
    proposed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    resolved_at TEXT,
    proposal_event_id TEXT NOT NULL REFERENCES world_events(id),
    source_event_id TEXT NOT NULL REFERENCES world_events(id),
    CHECK(first_id < second_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_intimate_bonds_open
ON intimate_bonds(world_id,first_id,second_id,kind) WHERE status IN ('pending','active');
CREATE INDEX IF NOT EXISTS idx_intimate_bonds_first ON intimate_bonds(world_id,first_id,status);
CREATE INDEX IF NOT EXISTS idx_intimate_bonds_second ON intimate_bonds(world_id,second_id,status);
CREATE TABLE IF NOT EXISTS intimate_boundaries (
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    other_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    statement TEXT NOT NULL,
    pause_contact INTEGER NOT NULL CHECK(pause_contact IN (0,1)),
    updated_at TEXT NOT NULL,
    source_event_id TEXT NOT NULL REFERENCES world_events(id),
    PRIMARY KEY(world_id,character_id,other_id)
);
CREATE TABLE IF NOT EXISTS intimate_perspectives (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    other_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    feeling TEXT NOT NULL,
    need TEXT NOT NULL,
    concern TEXT NOT NULL,
    world_time TEXT NOT NULL,
    source_event_id TEXT NOT NULL REFERENCES world_events(id),
    UNIQUE(character_id,other_id,source_event_id)
);
CREATE INDEX IF NOT EXISTS idx_intimate_perspectives_owner ON intimate_perspectives(character_id,other_id,world_time DESC);
CREATE TABLE IF NOT EXISTS relationship_landmarks (
 id TEXT PRIMARY KEY,world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 owner_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
 other_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
 source_event_id TEXT NOT NULL REFERENCES world_events(id) ON DELETE CASCADE,
 category TEXT NOT NULL,title TEXT NOT NULL,meaning TEXT NOT NULL,
 importance INTEGER NOT NULL,world_time TEXT NOT NULL,
 UNIQUE(owner_id,other_id,source_event_id)
);
CREATE INDEX IF NOT EXISTS idx_relationship_landmarks_owner ON relationship_landmarks(owner_id,other_id,importance DESC,world_time DESC);
"""

LABELS = {"close_friend": "挚友", "romantic": "恋人"}


class RelationshipReflection(BaseModel):
    """NPC 对这次真实相处的主观看法，仅供本人以后回忆。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    feeling: str = Field(min_length=1, max_length=160)
    need: str = Field(default="", max_length=160)
    concern: str = Field(default="", max_length=160)
    significance: Literal["everyday", "milestone", "conflict", "repair"] = "everyday"
    meaning: str = Field(default="", max_length=200)
    anchor_event_id: str | None = Field(default=None, max_length=100)
    continuity: list[ContinuityUpdate] = Field(default_factory=list, max_length=3)

    @model_validator(mode="before")
    @classmethod
    def continuity_default(cls, value):
        if isinstance(value, dict) and value.get("continuity") is None:
            value = {k: v for k, v in value.items() if k != "continuity"}
        return value


class RelationshipIntent(BaseModel):
    """只提交本人愿意表达的选择，不替另一方表示同意。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    operation: Literal["propose", "accept", "decline", "end", "boundary"]
    kind: Literal["close_friend", "romantic"] | None = None
    bond_id: str | None = Field(default=None, min_length=1, max_length=100)
    statement: str = Field(min_length=1, max_length=240)
    pause_contact: bool = False

    @model_validator(mode="after")
    def complete_choice(self):
        if self.operation == "propose" and (not self.kind or self.bond_id):
            raise ValueError("提出关系须指定类型，不得指定旧约定")
        if self.operation in {"accept", "decline", "end"} and not self.bond_id:
            raise ValueError("回应关系须引用真实约定")
        if self.operation != "boundary" and self.pause_contact:
            raise ValueError("暂停主动联系须作为界限单独表达")
        return self


class IntimacyService:
    @staticmethod
    def rows(c, wid, cid, at, other=None):
        query = "SELECT * FROM intimate_bonds WHERE world_id=? AND (first_id=? OR second_id=?)"
        args = [wid, cid, cid]
        if other:
            query += " AND (first_id=? OR second_id=?)"
            args += [other, other]
        query += " ORDER BY CASE WHEN status='active' THEN 0 WHEN status='pending' THEN 1 ELSE 2 END,proposed_at DESC,id DESC LIMIT 24"
        result = []
        for row in c.execute(query, args):
            value = dict(row)
            if value["status"] == "pending" and from_iso(value["expires_at"]) <= at:
                value["status"] = "expired"
            value["label"] = LABELS[value["kind"]]
            value["other_id"] = value["second_id"] if value["first_id"] == cid else value["first_id"]
            result.append(value)
        return result

    @classmethod
    def context(cls, c, wid, npc, player, at, query=""):
        bonds = cls.rows(c, wid, npc, at, player)
        boundaries = [dict(row) for row in c.execute(
            "SELECT character_id,statement,pause_contact,updated_at,source_event_id "
            "FROM intimate_boundaries WHERE world_id=? AND "
            "((character_id=? AND other_id=?) OR (character_id=? AND other_id=?))",
            (wid, npc, player, player, npc),
        )]
        # 哥哥，只读取双方确实共同知道的事件，不能借亲密关系窥见私人经历。
        experiences = [dict(row) for row in c.execute(
            "SELECT e.id AS source_event_id,e.occurred_at,e.event_type,e.summary "
            "FROM world_events e JOIN worlds w ON w.id=e.world_id "
            "JOIN character_event_knowledge a ON a.event_id=e.id AND a.world_id=e.world_id AND a.character_id=? "
            "JOIN character_event_knowledge b ON b.event_id=e.id AND b.world_id=e.world_id AND b.character_id=? "
            "WHERE e.world_id=? AND ((e.actor_id=? AND e.target_id=?) OR "
            "(e.actor_id=? AND e.target_id=?)) AND e.occurred_at<=w.current_time "
            "AND a.learned_world_time<=w.current_time AND b.learned_world_time<=w.current_time "
            "ORDER BY e.occurred_at DESC,e.created_at DESC LIMIT 4",
            (npc, player, wid, npc, player, player, npc),
        )]
        from world_engine.society import SocietyService

        for experience in experiences:
            experience["summary"] = SocietyService.event_summary(
                c, wid, npc, experience["source_event_id"], experience["summary"]
            )
        perspective = [dict(row) for row in c.execute(
            "SELECT p.feeling,p.need,p.concern,p.world_time,p.source_event_id FROM intimate_perspectives p "
            "JOIN worlds w ON w.id=p.world_id "
            "JOIN world_events e ON e.id=p.source_event_id AND e.world_id=p.world_id "
            "JOIN character_event_knowledge k ON k.event_id=e.id AND k.world_id=e.world_id AND k.character_id=p.character_id "
            "WHERE p.world_id=? AND p.character_id=? AND p.other_id=? "
            "AND p.world_time<=w.current_time AND e.occurred_at<=w.current_time "
            "AND k.learned_world_time<=w.current_time ORDER BY p.world_time DESC,p.rowid DESC LIMIT 1",
            (wid, npc, player),
        )]
        # 哥哥，稳定约定占据上下文核心，只保留本轮选择所需的字段。
        core_keys = ("id", "proposer_id", "kind", "status", "statement", "response", "expires_at", "source_event_id")
        compact_bonds = [{key: bond[key] for key in core_keys} for bond in bonds[:6]]
        from world_engine.appointments import AppointmentService
        from world_engine.companion_proposals import CompanionProposalService
        from world_engine.families import FamilyService
        from world_engine.group_journeys import GroupJourneyService
        from world_engine.rides import RideService
        from world_engine.journeys import JourneyService
        from world_engine.lodging_plans import LodgingPlanService
        from world_engine.overnight import OvernightService
        from world_engine.separate_stays import SeparateStayService
        from world_engine.shared_life import SharedLifeService
        from world_engine.shared_plans import SharedPlanService
        moments = SharedLifeService.rows(c, wid, npc, at, player)
        separate_stays = SeparateStayService.context(c,wid,npc,player,at)
        pending_stay_change = any(
            change.get("status") == "pending"
            for stay in separate_stays for change in stay.get("reschedule_requests", [])
        )
        moment_keys = ("id", "proposer_id", "recipient_id", "kind", "status", "minutes", "statement", "reason", "source_event_id", "preparation_plan_id", "meal_supply", "meal_provider_id", "journey_id", "journey_stop_index")
        return {"bonds": compact_bonds, "expressed_boundaries": boundaries, "own_perspective": perspective,
                "ongoing_considerations": RelationshipContinuityService.context(c,wid,npc,player,at,query),
                "family": FamilyService.context(c,wid,npc,player,at),
                "appointments": AppointmentService.context(c,wid,npc,player,at),
                "shared_plans": SharedPlanService.context(c,wid,npc,player,at),
                "journeys": JourneyService.context(c,wid,npc,player,at),
                "group_journeys": GroupJourneyService.context(c,wid,npc,player,at),
                "rides": RideService.context(c,c.execute("SELECT * FROM characters WHERE id=? AND world_id=?", (npc,wid)).fetchone(),at),
                "overnights": OvernightService.context(c,wid,npc,player,at),
                "separate_stays": separate_stays,
                "separate_stay_rooms": SeparateStayService.room_options(c,wid,npc,player,at),
                "lodging_plans": LodgingPlanService.context(c,wid,npc,player,at),
                "companion_proposals": CompanionProposalService.context(c,wid,npc,player,at),
                "lodging_handoff_sources": LodgingPlanService.handoff_sources(c,wid,npc,player,at),
                "lodging_room_options": LodgingPlanService.room_options(c,wid,npc,player,at),
                "shared_moments": [{key: m[key] for key in moment_keys} for m in moments[:4]],
                "lasting_memories": cls.landmarks(c, wid, npc, player, query),
                "shared_experiences": experiences,
                "consent_rule": "只有pending约定的另一方能accept；propose不代表对方同意。关系不授予物品、房间、身体接触或独占权限。" + (
                    "当前分房改期在reschedule_requests中pending，原住宿仍accepted。明确接受或拒绝改期须用accept_change或decline_change并引用其id为change_id和原stay_id；口头愿意而意图null不改时间。"
                    if pending_stay_change else "")}

    @staticmethod
    def reflect(c, npc, other_id, at, source_event_id, reflection):
        event = c.execute(
            "SELECT 1 FROM world_events WHERE id=? AND world_id=? AND "
            "((actor_id=? AND target_id=?) OR (actor_id=? AND target_id=?))",
            (source_event_id, npc["world_id"], npc["id"], other_id, other_id, npc["id"]),
        ).fetchone()
        if not event or npc["is_player"]:
            return
        RelationshipContinuityService.apply(c, npc, other_id, at, source_event_id, reflection.continuity)
        previous = c.execute(
            "SELECT world_time FROM intimate_perspectives WHERE character_id=? AND other_id=? "
            "ORDER BY world_time DESC LIMIT 1", (npc["id"], other_id),
        ).fetchone()
        # 哥哥，重大经历可以及时留下影响；同样的感受和普通寒暄仍不反复积累。
        duplicate = c.execute("SELECT 1 FROM intimate_perspectives WHERE character_id=? AND other_id=? AND feeling=? AND need=? AND concern=? AND world_time>?",
                              (npc["id"], other_id, reflection.feeling, reflection.need, reflection.concern, to_iso(at-timedelta(days=1)))).fetchone()
        meaningful = reflection.significance != "everyday" and bool(reflection.meaning)
        if duplicate and not meaningful:
            return
        if previous and at - from_iso(previous[0]) < timedelta(hours=6) and not meaningful:
            return
        anchor = reflection.anchor_event_id or source_event_id
        known = c.execute("SELECT 1 FROM character_event_knowledge WHERE character_id=? AND event_id=? AND world_id=?", (npc["id"], anchor, npc["world_id"])).fetchone()
        if not known:
            anchor = source_event_id
        if not duplicate:
            c.execute("INSERT OR IGNORE INTO intimate_perspectives VALUES (?,?,?,?,?,?,?,?,?)",
                      (str(uuid4()), npc["world_id"], npc["id"], other_id, reflection.feeling,
                       reflection.need, reflection.concern, to_iso(at), source_event_id))
        if meaningful:
            # 主观解释只归本人，不写进另一方的看法或公开日志。
            c.execute("INSERT OR IGNORE INTO relationship_landmarks VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (str(uuid4()), npc["world_id"], npc["id"], other_id, anchor, reflection.significance,
                       reflection.feeling, reflection.meaning, 8, to_iso(at)))

    @staticmethod
    def remember_event(c, wid, first, second, event, at, category, summary):
        for owner, other in ((first, second), (second, first)):
            c.execute("INSERT OR IGNORE INTO relationship_landmarks VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (str(uuid4()), wid, owner, other, event, category, summary[:180], summary[:240], 9 if category in {"milestone", "conflict"} else 7, to_iso(at)))

    @staticmethod
    def landmarks(c, wid, owner, other=None, query=""):
        args = [wid, owner]
        condition = " AND l.other_id=?" if other else ""
        if other:
            args.append(other)
        source_query = (
            "SELECT l.* FROM relationship_landmarks l JOIN worlds w ON w.id=l.world_id "
            "JOIN world_events e ON e.id=l.source_event_id AND e.world_id=l.world_id "
            "JOIN character_event_knowledge k ON k.event_id=e.id AND k.world_id=e.world_id AND k.character_id=l.owner_id "
            "WHERE l.world_id=? AND l.owner_id=? AND l.world_time<=w.current_time "
            "AND e.occurred_at<=w.current_time AND k.learned_world_time<=w.current_time" + condition
        )
        rows = [dict(row) for row in c.execute(
            source_query + " ORDER BY l.importance DESC,l.world_time DESC LIMIT 40", args)]
        if not rows:
            return []
        from world_engine.dialogue_relevance import DialogueRelevance
        relevance = DialogueRelevance(query)
        # 哥哥，既保留与本轮相关的往事，也留住早期转折，避免只记得最近几句。
        selected = sorted(rows, key=lambda row: (relevance.matches(row), row["importance"], row["world_time"]), reverse=True)[:3]
        first = c.execute(
            source_query + " ORDER BY l.world_time,l.rowid LIMIT 1", args,
        ).fetchone()
        earliest = dict(first)
        if earliest not in selected:
            selected.append(earliest)
        IntimacyService.mask_records(c, wid, owner, selected, ("title", "meaning"))
        return selected

    @staticmethod
    def mask_records(c, wid, owner, rows, fields):
        import re

        from world_engine.society import SocietyService

        text = " ".join(str(row.get(key, "")) for row in rows for key in fields)
        if not text:
            return
        # 哥哥，只为这些记忆中实际出现的姓名查询称呼，避免每条记忆遍历全城认知。
        labels = {person["name"]: SocietyService.label(c, owner, person["id"]) for person in c.execute(
            "SELECT id,name FROM characters WHERE world_id=? AND id!=?", (wid, owner),
        ) if person["name"] and person["name"] in text}
        if labels:
            pattern = "|".join(re.escape(name) for name in sorted(labels, key=len, reverse=True))
            for row in rows:
                for key in fields:
                    row[key] = re.sub(pattern, lambda match: labels[match.group()], row[key])

    @staticmethod
    def paused(c, wid, first, second):
        return bool(c.execute(
            "SELECT 1 FROM intimate_boundaries WHERE world_id=? AND pause_contact=1 AND "
            "((character_id=? AND other_id=?) OR (character_id=? AND other_id=?))",
            (wid, first, second, second, first),
        ).fetchone())

    @staticmethod
    def connected(c, wid, first, second):
        return bool(c.execute(
            "SELECT 1 FROM intimate_bonds WHERE world_id=? AND first_id=? AND second_id=? AND status='active'",
            (wid, *sorted((first, second))),
        ).fetchone())

    @staticmethod
    def _adult(c, person, at):
        profile = c.execute("SELECT adult_age_world_years FROM species_profiles WHERE id=?", (person["species"],)).fetchone()
        age = age_years_at(from_iso(person["birth_world_time"]) if person["birth_world_time"] else None, at)
        if profile is None or age is None or age < profile[0]:
            raise ValueError("这项关系安排需要双方已确认达到各自种族的成年年龄")

    @staticmethod
    def _reachable(c, actor, other):
        if same_room(actor, other) and great_circle_distance_km(
            actor["longitude"], actor["latitude"], other["longitude"], other["latitude"]
        ) <= 0.02:
            return
        if c.execute(
            "SELECT 1 FROM character_contacts WHERE world_id=? AND status='accepted' AND "
            "((requester_id=? AND recipient_id=?) OR (requester_id=? AND recipient_id=?))",
            (actor["world_id"], actor["id"], other["id"], other["id"], actor["id"]),
        ).fetchone():
            return
        raise ValueError("请在能正常交谈的距离内表达，或先建立双方同意的信笺联络")

    @staticmethod
    def _event(c, actor, other, at, operation, summary, payload):
        event = ActionService._record_event(
            c, world_id=actor["world_id"], tick_id=str(uuid4()), occurred_at=at,
            event_type=f"social.intimacy_{operation}", actor_id=actor["id"], target_id=other["id"],
            location_id=None, summary=summary, payload=payload,
        )
        for cid in (actor["id"], other["id"]):
            ActionService._record_memory(c, world_id=actor["world_id"], character_id=cid,
                                        event_id=event, memory_type="experienced", summary=summary, importance=8)
        return event

    @classmethod
    def apply(cls, c, actor, other, at, choice):
        wid = actor["world_id"]
        if other is None or other["world_id"] != wid or actor["id"] == other["id"]:
            raise ValueError("关系必须由同一世界中的两个人分别选择")
        op = choice.operation
        if op != "end" and (actor["health"] <= 0 or other["health"] <= 0):
            raise ValueError("当前人物无法表达关系选择")
        cls._reachable(c, actor, other)
        pair = sorted((actor["id"], other["id"]))
        if op == "boundary":
            event = cls._event(c, actor, other, at, op, f"一方表达了相处界限：{choice.statement}",
                               {"statement": choice.statement, "pause_contact": choice.pause_contact})
            c.execute(
                "INSERT INTO intimate_boundaries VALUES (?,?,?,?,?,?,?) ON CONFLICT(world_id,character_id,other_id) "
                "DO UPDATE SET statement=excluded.statement,pause_contact=excluded.pause_contact,"
                "updated_at=excluded.updated_at,source_event_id=excluded.source_event_id",
                (wid, actor["id"], other["id"], choice.statement, int(choice.pause_contact), to_iso(at), event),
            )
            return {"status": "applied", "event_id": event, "summary": "相处界限已记录。"}
        if op == "propose":
            if cls.paused(c, wid, *pair):
                raise ValueError("一方已要求留出空间，请先尊重相处界限")
            c.execute("UPDATE intimate_bonds SET status='expired',resolved_at=? WHERE world_id=? AND first_id=? AND second_id=? AND status='pending' AND expires_at<=?",
                      (to_iso(at), wid, *pair, to_iso(at)))
            existing = c.execute("SELECT * FROM intimate_bonds WHERE world_id=? AND first_id=? AND second_id=? AND kind=? AND status IN ('pending','active')",
                                 (wid, *pair, choice.kind)).fetchone()
            if existing:
                raise ValueError("已有相同关系的约定，请回应现有约定")
            previous = c.execute("SELECT resolved_at FROM intimate_bonds WHERE world_id=? AND first_id=? AND second_id=? AND kind=? AND status IN ('declined','ended') ORDER BY resolved_at DESC LIMIT 1",
                                 (wid, *pair, choice.kind)).fetchone()
            if previous and at - from_iso(previous[0]) < timedelta(days=1):
                raise ValueError("刚拒绝或结束这段关系，请至少留出一天世界时间，不反复施压")
            if choice.kind == "romantic":
                for person in (actor, other):
                    cls._adult(c, person, at)
            bid = str(uuid4())
            summary = f"一方提出成为{LABELS[choice.kind]}，等待另一方明确回应：{choice.statement}"
            event = cls._event(c, actor, other, at, op, summary, {"bond_id": bid, "kind": choice.kind, "statement": choice.statement})
            c.execute("INSERT INTO intimate_bonds VALUES (?,?,?,?,?,?,'pending',?,NULL,?,?,NULL,?,?)",
                      (bid, wid, *pair, actor["id"], choice.kind, choice.statement, to_iso(at), to_iso(at + timedelta(days=7)), event, event))
            return {"status": "pending", "bond_id": bid, "event_id": event, "summary": summary}
        bond = c.execute("SELECT * FROM intimate_bonds WHERE id=? AND world_id=? AND first_id=? AND second_id=?",
                         (choice.bond_id, wid, *pair)).fetchone()
        if bond is None:
            raise ValueError("这份关系约定不属于双方")
        if op in {"accept", "decline"}:
            if bond["status"] != "pending" or from_iso(bond["expires_at"]) <= at:
                raise ValueError("这份提议已得到回应或已过期")
            if bond["proposer_id"] == actor["id"]:
                raise ValueError("不能替另一方同意或拒绝自己的提议")
            if op == "accept":
                if cls.paused(c, wid, *pair):
                    raise ValueError("一方仍要求留出空间，不能确认亲密关系")
                if bond["kind"] == "romantic":
                    for person in (actor, other):
                        cls._adult(c, person, at)
            status = "active" if op == "accept" else "declined"
            summary = (f"双方明确同意成为{LABELS[bond['kind']]}" if op == "accept" else "另一方拒绝了关系提议") + f"：{choice.statement}"
        else:
            if bond["status"] not in {"pending", "active"}:
                raise ValueError("这份关系约定已经结束")
            if bond["status"] == "pending" and bond["proposer_id"] != actor["id"]:
                raise ValueError("请拒绝对方的提议，而非撤回别人的提议")
            status = "ended" if bond["status"] == "active" else "withdrawn"
            summary = f"一方结束了关系约定，另一方无需批准：{choice.statement}"
        event = cls._event(c, actor, other, at, op, summary, {"bond_id": bond["id"], "statement": choice.statement, "status": status})
        c.execute("UPDATE intimate_bonds SET status=?,response=?,resolved_at=?,source_event_id=? WHERE id=?",
                  (status, choice.statement, to_iso(at), event, bond["id"]))
        cls.remember_event(c, wid, actor["id"], other["id"], event, at, "milestone", summary)
        return {"status": status, "bond_id": bond["id"], "event_id": event, "summary": summary}
