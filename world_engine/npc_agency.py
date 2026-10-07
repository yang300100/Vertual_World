"""NPC 的持久目标与可追溯执行记录。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from uuid import uuid4

from world_engine.npc_operators import next_action, predicate_from_json, satisfied
from world_engine.repository import to_iso


class NpcAgencyService:
    @staticmethod
    def adopt(
        connection: sqlite3.Connection,
        world_id: str,
        actor_id: str,
        kind: str,
        predicate: dict[str, object],
        motive: str,
        at: datetime,
        source_event_id: str | None = None,
        priority: int = 5,
    ) -> str:
        """Yuki 为人物登记一个可核验的目标，重复采纳仍沿用原目标。"""
        if source_event_id and not connection.execute(
            "SELECT 1 FROM character_event_knowledge WHERE character_id=? AND world_id=? "
            "AND event_id=? AND learned_world_time<=?",
            (actor_id, world_id, source_event_id, to_iso(at)),
        ).fetchone():
            raise ValueError("人物尚未得知目标所依据的事件")
        encoded = json.dumps(predicate, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if source_event_id:
            earlier = connection.execute(
                "SELECT id FROM npc_agency_goals WHERE world_id=? AND character_id=? "
                "AND kind=? AND source_event_id=? AND predicate_json=? ORDER BY created_world_time LIMIT 1",
                (world_id, actor_id, kind, source_event_id, encoded),
            ).fetchone()
            if earlier:
                return str(earlier["id"])
        existing = connection.execute(
            "SELECT id FROM npc_agency_goals WHERE world_id=? AND character_id=? "
            "AND kind=? AND predicate_json=? "
            "AND status IN ('waiting','active','blocked','paused')",
            (world_id, actor_id, kind, encoded),
        ).fetchone()
        if existing:
            return str(existing["id"])
        goal_id = str(uuid4())
        world_time = to_iso(at)
        connection.execute(
            "INSERT INTO npc_agency_goals(id,world_id,character_id,kind,predicate_json,"
            "motive,source_event_id,priority,created_world_time,updated_world_time) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (goal_id, world_id, actor_id, kind, encoded, motive, source_event_id,
             priority, world_time, world_time),
        )
        from world_engine.actions import ActionService
        from world_engine.event_history import EventHistoryService

        actor = connection.execute(
            "SELECT current_location_id,location_id FROM characters WHERE id=?",
            (actor_id,),
        ).fetchone()
        adoption_event_id = ActionService._record_event(
            connection, world_id=world_id, tick_id=str(uuid4()), occurred_at=at,
            event_type="agency.goal_adopted", actor_id=actor_id, target_id=None,
            location_id=actor["current_location_id"] or actor["location_id"] if actor else None,
            summary=f"人物形成目标：{motive}",
            payload={"goal_id": goal_id, "predicate": predicate,
                     "source_event_id": source_event_id,
                     "trigger_state": ActionService._observed_state(connection, actor_id),
                     "information_scope": "private"},
        )
        if source_event_id:
            EventHistoryService.link(connection, adoption_event_id, source_event_id,
                                     strict=False)
        return goal_id

    @staticmethod
    def view(connection: sqlite3.Connection, actor_id: str) -> list[dict[str, object]]:
        rows = connection.execute(
            "SELECT * FROM npc_agency_goals WHERE character_id=? "
            "ORDER BY created_world_time DESC, id DESC", (actor_id,),
        ).fetchall()
        return [
            {**dict(row), "predicate": json.loads(row["predicate_json"]),
             "next_step": json.loads(row["next_step_json"])}
            for row in rows
        ]

    @staticmethod
    def seed(connection: sqlite3.Connection, world_id: str, actor: sqlite3.Row,
             at: datetime) -> None:
        """从身体、资源和已经传到耳边的情报产生动机。"""
        from world_engine.incidents import IncidentService

        IncidentService.seed_goals(connection, world_id, actor, at)
        from world_engine.npc_cooperation import seed as seed_cooperation

        seed_cooperation(connection, world_id, actor, at)
        if actor["health"] < 80:
            NpcAgencyService.adopt(
                connection, world_id, actor["id"], "recovery",
                {"kind": "health_at_least", "amount": 80}, "想处理伤势，恢复身体", at,
                priority=1,
            )
        if actor["satiety"] < 45:
            NpcAgencyService.adopt(
                connection, world_id, actor["id"], "self_care",
                {"kind": "satiety_at_least", "amount": 65}, "想吃上一顿饭", at,
                priority=1,
            )
        elif actor["money"] < 12:
            NpcAgencyService.adopt(
                connection, world_id, actor["id"], "livelihood",
                {"kind": "money_at_least", "amount": 12}, "想为往后的生活攒钱", at,
            )
        recent_news = connection.execute(
            "SELECT e.id,e.location_id,e.summary,e.actor_id,e.target_id "
            "FROM character_event_knowledge k "
            "JOIN world_events e ON e.id=k.event_id "
            "WHERE k.character_id=? AND k.world_id=? AND k.learned_world_time<=? "
            "AND e.location_id IS NOT NULL AND e.event_type IN "
            "('world.major_death','movement_encounter','action.attack') "
            "ORDER BY k.learned_world_time DESC LIMIT 8",
            (actor["id"], world_id, to_iso(at)),
        ).fetchall()
        investigator = any(word in (actor["identity"] or "") for word in
                           ("守卫", "调查", "侦探", "冒险", "佣兵", "猎人", "游侠"))
        known = None
        for item in recent_news:
            participants = [value for value in (item["actor_id"], item["target_id"])
                            if value and value != actor["id"]]
            related = any(connection.execute(
                "SELECT 1 FROM relationships WHERE world_id=? AND "
                "((source_character_id=? AND target_character_id=?) OR "
                "(target_character_id=? AND source_character_id=?))",
                (world_id, actor["id"], person_id, actor["id"], person_id),
            ).fetchone() for person_id in participants)
            if investigator or related or actor["id"] in (item["actor_id"], item["target_id"]):
                known = item
                break
        if known and known["location_id"]:
            contact_id = next((value for value in (known["actor_id"], known["target_id"])
                               if value and value != actor["id"]), None)
            predicate = ({"kind": "spoke_about_event", "event_id": known["id"],
                          "location_id": known["location_id"], "target_id": contact_id}
                         if contact_id else
                         {"kind": "at_location", "location_id": known["location_id"]})
            NpcAgencyService.adopt(
                connection, world_id, actor["id"], "investigate",
                predicate,
                f"听闻{known['summary'][:80]}，想去现场查看", at, known["id"],
            )
        if any(word in (actor["identity"] or "") for word in ("冒险", "佣兵", "猎人", "游侠")):
            opportunity = connection.execute(
                "SELECT o.id,o.source_event_id,o.reward_money FROM npc_opportunities o "
                "JOIN character_event_knowledge k ON k.event_id=o.source_event_id "
                "AND k.character_id=? AND k.learned_world_time<=? "
                "WHERE o.world_id=? AND (o.status='open' OR "
                "(o.status='accepted' AND o.accepted_by=?)) "
                "ORDER BY o.reward_money DESC,o.created_world_time LIMIT 1",
                (actor["id"], to_iso(at), world_id, actor["id"]),
            ).fetchone()
            if opportunity:
                NpcAgencyService.adopt(
                    connection, world_id, actor["id"], "opportunity",
                    {"kind": "opportunity_rewarded", "opportunity_id": opportunity["id"]},
                    f"想承接公开机会，赢得{opportunity['reward_money']}枚报酬", at,
                    opportunity["source_event_id"], priority=3,
                )

    @staticmethod
    def tick(connection: sqlite3.Connection, world_id: str, actor_id: str,
             at: datetime) -> str | None:
        """每次只执行一步；实际状态而非预期文字决定目标是否完成。"""
        from world_engine.actions import ActionService

        actor = connection.execute(
            "SELECT * FROM characters WHERE id=? AND world_id=? AND is_player=0 AND health>0",
            (actor_id, world_id),
        ).fetchone()
        if not actor:
            return None
        if connection.execute(
            "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",
            (actor_id,),
        ).fetchone() or connection.execute(
            "SELECT 1 FROM character_life_activities WHERE character_id=? AND status='running'",
            (actor_id,),
        ).fetchone():
            return None
        if connection.execute(
            "SELECT 1 FROM npc_agency_steps s JOIN npc_agency_goals g ON g.id=s.goal_id "
            "WHERE g.character_id=? AND s.world_time=? LIMIT 1",
            (actor_id, to_iso(at)),
        ).fetchone():
            return None
        NpcAgencyService.seed(connection, world_id, actor, at)
        goals = connection.execute(
            "SELECT * FROM npc_agency_goals WHERE character_id=? AND status IN ('active','blocked') "
            "AND (retry_world_time IS NULL OR retry_world_time<=?) "
            "ORDER BY priority,created_world_time,rowid", (actor_id, to_iso(at)),
        ).fetchall()
        for goal in goals:
            predicate = predicate_from_json(goal["predicate_json"])
            if actor["health"] < 40 and predicate["kind"] not in {
                "health_at_least", "satiety_at_least", "energy_at_least",
            }:
                continue
            if predicate["kind"] == "opportunity_rewarded":
                from world_engine.opportunities import OpportunityService

                opportunity = OpportunityService._row(
                    connection, str(predicate["opportunity_id"]),
                )
                impossible = opportunity["status"] in {"cancelled", "completed"} and (
                    opportunity["accepted_by"] != actor_id or
                    opportunity["status"] == "cancelled"
                )
                if opportunity["status"] == "accepted" and opportunity["accepted_by"] == actor_id:
                    condition = json.loads(opportunity["condition_json"])
                    if condition["kind"] == "target_health_at_most":
                        target = connection.execute(
                            "SELECT health FROM characters WHERE id=?",
                            (condition["target_id"],),
                        ).fetchone()
                        if (target is None or target["health"] <= 0) and not OpportunityService.accepted_condition(connection, opportunity):
                            OpportunityService.cancel(connection, opportunity["id"], at,
                                                      "目标已失去战力，承接者没有完成证据")
                            impossible = True
                if impossible:
                    connection.execute(
                        "UPDATE npc_agency_goals SET status='failed',updated_world_time=?,"
                        "next_step_json=? WHERE id=?",
                        (to_iso(at), json.dumps({"reason": "机会已无法完成"},
                                                ensure_ascii=False), goal["id"]),
                    )
                    continue
            if satisfied(connection, actor, predicate, goal["id"], at=at):
                completion = ActionService._record_event(
                    connection, world_id=world_id, tick_id=str(uuid4()), occurred_at=at,
                    event_type="agency.goal_completed", actor_id=actor_id, target_id=None,
                    location_id=actor["current_location_id"] or actor["location_id"],
                    summary=f"目标已由实际状态核验达成：{goal['motive']}",
                    payload={"goal_id": goal["id"], "predicate": predicate,
                             "observed": ActionService._observed_state(connection, actor_id),
                             "information_scope": "private"},
                )
                from world_engine.event_history import EventHistoryService

                origins = connection.execute(
                    "SELECT COALESCE(l.result_event_id,s.event_id) AS id FROM npc_agency_steps s "
                    "LEFT JOIN action_execution_links l ON l.source_event_id=s.event_id "
                    "WHERE s.goal_id=? AND s.event_id IS NOT NULL ORDER BY s.world_time DESC LIMIT 1",
                    (goal["id"],),
                ).fetchall()
                for origin in origins:
                    EventHistoryService.link(connection, completion, origin["id"], strict=False)
                connection.execute(
                    "UPDATE npc_agency_goals SET status='completed',updated_world_time=?,"
                    "next_step_json='{}' WHERE id=?", (to_iso(at), goal["id"]),
                )
                continue
            if connection.execute(
                "SELECT 1 FROM npc_agency_steps WHERE goal_id=? AND world_time=?",
                (goal["id"], to_iso(at)),
            ).fetchone():
                return None
            planned = next_action(connection, actor, predicate, goal["motive"], at)
            if planned is None:
                connection.execute(
                    "UPDATE npc_agency_goals SET status='blocked',retry_world_time=?,"
                    "updated_world_time=?,next_step_json=? WHERE id=?",
                    (to_iso(at + timedelta(hours=6)), to_iso(at),
                     json.dumps({"reason": "当前已知资源、地点和身体状态下，未找到可行的有限步计划",
                                 "observed": ActionService._observed_state(connection, actor_id)},
                                ensure_ascii=False), goal["id"]),
                )
                continue
            from world_engine.domain import ActionOutcome, ActionProposal
            from world_engine.event_history import EventHistoryService

            previous_step = connection.execute(
                "SELECT COALESCE(l.result_event_id,s.event_id) AS event_id "
                "FROM npc_agency_steps s LEFT JOIN action_execution_links l "
                "ON l.source_event_id=s.event_id WHERE s.goal_id=? AND s.event_id IS NOT NULL "
                "ORDER BY s.world_time DESC,s.rowid DESC LIMIT 1", (goal["id"],),
            ).fetchone()
            adoption = connection.execute(
                "SELECT id FROM world_events WHERE world_id=? AND actor_id=? "
                "AND event_type='agency.goal_adopted' "
                "AND json_extract(payload_json,'$.goal_id')=? LIMIT 1",
                (world_id, actor_id, goal["id"]),
            ).fetchone()
            cause_ids = [value for value in
                         (goal["source_event_id"], adoption["id"] if adoption else None,
                          previous_step["event_id"] if previous_step else None)
                         if value]
            from world_engine.npc_domain_execution import execute_operation
            from world_engine.npc_planning import DomainOperation

            economic = isinstance(planned, DomainOperation)
            proposal = (ActionProposal(actor_id=actor_id, action=planned.action_type,
                                       reason=goal["motive"])
                        if economic else planned)
            proposal.metadata["goal_id"] = goal["id"]
            proposal.metadata["cause_event_ids"] = cause_ids
            if economic:
                before = ActionService._observed_state(connection, actor_id)
                try:
                    event_id = execute_operation(connection, actor, planned, at)
                    rejection = "现场条件尚不满足，无法执行这项操作"
                except ValueError as exc:
                    event_id, rejection = None, str(exc)
                if event_id:
                    event = connection.execute(
                        "SELECT summary,target_id FROM world_events WHERE id=?", (event_id,),
                    ).fetchone()
                    outcome = ActionOutcome(accepted=True, actor_id=actor_id,
                                            action=proposal.action, summary=event["summary"],
                                            event_id=event_id)
                    for cause_id in cause_ids:
                        EventHistoryService.link(connection, event_id, cause_id, strict=False)
                else:
                    event_id = ActionService._record_event(
                        connection, world_id=world_id, tick_id=str(uuid4()),
                        occurred_at=at, event_type="action.rejected", actor_id=actor_id,
                        target_id=None, location_id=actor["current_location_id"],
                        summary=rejection, payload={"action": planned.name, "goal_id": goal["id"]},
                    )
                    event = None
                    outcome = ActionOutcome(accepted=False, actor_id=actor_id,
                                            action=proposal.action, summary=rejection,
                                            event_id=event_id, rejection_reason=rejection)
                if not connection.execute(
                    "SELECT 1 FROM action_effects WHERE source_event_id=?", (event_id,),
                ).fetchone():
                    ActionService._record_effect(
                        connection, world_id, event_id, proposal, at, before,
                        ActionService._observed_state(connection, actor_id),
                        outcome.rejection_reason, event["target_id"] if event else None,
                        f"action.{planned.name}",
                    )
                else:
                    connection.execute(
                        "UPDATE action_effects SET payload_json=json_set(payload_json,'$.goal_id',?) "
                        "WHERE source_event_id=?", (goal["id"], event_id),
                    )
            else:
                outcome = ActionService().execute(
                    connection, world_id=world_id, tick_id=str(uuid4()),
                    occurred_at=at, proposal=proposal,
                )
            connection.execute(
                "INSERT INTO npc_agency_steps(id,goal_id,world_time,action_type,status,event_id,"
                "cause_event_id,expected_json,observed_json,reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (str(uuid4()), goal["id"], to_iso(at), planned.name if economic else proposal.action.value,
                 "accepted" if outcome.accepted else "rejected", outcome.event_id,
                 goal["source_event_id"], goal["predicate_json"],
                 json.dumps(ActionService._observed_state(connection, actor_id), ensure_ascii=False),
                 outcome.summary),
            )
            rejected_count = connection.execute(
                "SELECT COUNT(*) FROM npc_agency_steps WHERE goal_id=? AND action_type=? "
                "AND status='rejected' AND reason=?",
                (goal["id"], planned.name if economic else proposal.action.value,
                 outcome.summary),
            ).fetchone()[0]
            retry_hours = (1 if outcome.accepted else min(24, 3 * 2 ** max(0, rejected_count - 1)))
            connection.execute(
                "UPDATE npc_agency_goals SET status=?,retry_world_time=?,"
                "updated_world_time=?,next_step_json=? WHERE id=?",
                ("active" if outcome.accepted else "blocked",
                 to_iso(at + timedelta(hours=retry_hours)), to_iso(at),
                 json.dumps({"action": planned.name if economic else proposal.action.value,
                             "event_id": outcome.event_id},
                            ensure_ascii=False), goal["id"]),
            )
            return outcome.summary
        return None
