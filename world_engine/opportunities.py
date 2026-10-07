"""世界公开机会：条件、承接与托管报酬由真实存档结算。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from uuid import uuid4

from world_engine.actions import ActionService
from world_engine.domain import ActionProposal, ActionType
from world_engine.event_history import EventHistoryService
from world_engine.geo import great_circle_distance_km
from world_engine.npc_operators import near_location
from world_engine.repository import to_iso


class OpportunityService:
    @staticmethod
    def publish(c: sqlite3.Connection, world_id: str, issuer_id: str,
                issuer_location_id: str, objective_location_id: str,
                condition: dict[str, object], reward_money: int,
                at: datetime) -> str:
        """先托管报酬，公告才可能成为人物已知的公开机会。"""
        issuer = c.execute(
            "SELECT * FROM characters WHERE id=? AND world_id=? AND health>0",
            (issuer_id, world_id),
        ).fetchone()
        if not issuer or reward_money < 0 or issuer["money"] < reward_money:
            raise ValueError("发布者不存在或报酬不足")
        if condition.get("kind") not in {"target_health_at_most", "at_location"}:
            raise ValueError("机会条件不受支持")
        if condition["kind"] == "target_health_at_most" and not c.execute(
            "SELECT 1 FROM characters WHERE id=? AND world_id=? AND health>0",
            (condition.get("target_id"), world_id),
        ).fetchone():
            raise ValueError("目标人物不存在或已经失去战力")
        condition_locations = (condition.get("location_id"),) if condition["kind"] == "at_location" else ()
        for location_id in (issuer_location_id, objective_location_id, *condition_locations):
            if not c.execute(
                "SELECT 1 FROM locations WHERE id=? AND world_id=? AND is_active=1",
                (location_id, world_id),
            ).fetchone():
                raise ValueError("机会地点不存在")
        before = ActionService._observed_state(c, issuer_id)
        c.execute("UPDATE characters SET money=money-? WHERE id=?",
                  (reward_money, issuer_id))
        opportunity_id = str(uuid4())
        event_id = ActionService._record_event(
            c, world_id=world_id, tick_id=str(uuid4()), occurred_at=at,
            event_type="opportunity.posted", actor_id=issuer_id, target_id=None,
            location_id=issuer_location_id,
            summary=f"公开机会已发布，完成条件后可领取{reward_money}枚货币。",
            payload={"opportunity_id": opportunity_id, "condition": condition,
                     "information_scope": "public"}, importance="major",
        )
        c.execute(
            "INSERT INTO npc_opportunities(id,world_id,issuer_id,issuer_location_id,"
            "objective_location_id,condition_json,reward_money,source_event_id,created_world_time) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (opportunity_id, world_id, issuer_id, issuer_location_id,
             objective_location_id, json.dumps(condition, ensure_ascii=False, sort_keys=True),
             reward_money, event_id, to_iso(at)),
        )
        c.execute(
            "INSERT OR IGNORE INTO character_event_knowledge(character_id,event_id,world_id,"
            "source_kind,confidence,learned_world_time) VALUES (?,?,?,'experienced',1,?)",
            (issuer_id, event_id, world_id, to_iso(at)),
        )
        for witness in c.execute(
            "SELECT * FROM characters WHERE world_id=? AND is_player=0 AND health>0 "
            "AND current_location_id=?", (world_id, issuer_location_id),
        ).fetchall():
            if great_circle_distance_km(
                issuer["longitude"], issuer["latitude"],
                witness["longitude"], witness["latitude"],
            ) <= .1:
                c.execute(
                    "INSERT OR IGNORE INTO character_event_knowledge(character_id,event_id,world_id,"
                    "source_kind,confidence,learned_world_time) VALUES (?,?,?,'observed',1,?)",
                    (witness["id"], event_id, world_id, to_iso(at)),
                )
        OpportunityService._audit(c, world_id, event_id, issuer_id, None, at,
                                  "opportunity.posted", before,
                                  ActionService._observed_state(c, issuer_id))
        return opportunity_id

    @staticmethod
    def _audit(c: sqlite3.Connection, world_id: str, event_id: str,
               actor_id: str, target_id: str | None, at: datetime,
               effect_type: str, before: dict[str, object],
               after: dict[str, object]) -> None:
        proposal = ActionProposal(actor_id=actor_id, action=ActionType.USE,
                                  reason="公开机会协议结算")
        ActionService._record_effect(c, world_id, event_id, proposal, at, before,
                                     after, None, target_id, effect_type)

    @staticmethod
    def _row(c: sqlite3.Connection, opportunity_id: str) -> sqlite3.Row:
        row = c.execute("SELECT * FROM npc_opportunities WHERE id=?",
                        (opportunity_id,)).fetchone()
        if row is None:
            raise ValueError("机会不存在")
        return row

    @staticmethod
    def accepted_condition(c: sqlite3.Connection, row: sqlite3.Row) -> str | None:
        condition = json.loads(row["condition_json"])
        if condition["kind"] == "target_health_at_most":
            target = c.execute("SELECT health FROM characters WHERE id=? AND world_id=?",
                               (condition["target_id"], row["world_id"])).fetchone()
            if not target or target["health"] > int(condition.get("amount", 0)):
                return None
            evidence = c.execute(
                "SELECT id FROM world_events WHERE world_id=? AND actor_id=? AND target_id=? "
                "AND event_type IN ('action.target_down','world.major_death') "
                "AND occurred_at>=? "
                "ORDER BY occurred_at DESC LIMIT 1",
                (row["world_id"], row["accepted_by"], condition["target_id"],
                 row["accepted_world_time"]),
            ).fetchone()
            return evidence["id"] if evidence else None
        if condition["kind"] == "at_location":
            place = c.execute("SELECT longitude,latitude FROM locations WHERE id=? AND world_id=?",
                              (condition["location_id"], row["world_id"])).fetchone()
            if not place or not row["accept_event_id"]:
                return None
            events = c.execute(
                "SELECT id,payload_json FROM world_events WHERE world_id=? AND actor_id=? "
                "AND event_type='action.movement_arrived' AND occurred_at>=? "
                "AND rowid>(SELECT rowid FROM world_events WHERE id=?) ORDER BY occurred_at DESC",
                (row["world_id"], row["accepted_by"], row["accepted_world_time"], row["accept_event_id"]),
            )
            for event in events:
                payload = json.loads(event["payload_json"])
                longitude, latitude = payload.get("destination_longitude"), payload.get("destination_latitude")
                if longitude is not None and latitude is not None and great_circle_distance_km(
                    longitude, latitude, place["longitude"], place["latitude"],
                ) <= .1:
                    return event["id"]
            # Yuki 也保留接取时已经位于巡查点的真实快照证明。
            if condition["location_id"] == row["issuer_location_id"]:
                return row["accept_event_id"]
            return None
        return None

    @staticmethod
    def next_step(c: sqlite3.Connection, actor: sqlite3.Row,
                  opportunity_id: str, motive: str) -> ActionProposal | str | None:
        row = OpportunityService._row(c, opportunity_id)
        if row["world_id"] != actor["world_id"]:
            return None
        if row["status"] == "open":
            destination = row["issuer_location_id"]
            operation = "accept_opportunity"
        elif row["status"] == "accepted" and row["accepted_by"] == actor["id"]:
            evidence = OpportunityService.accepted_condition(c, row)
            if evidence:
                destination = row["issuer_location_id"]
                operation = "claim_opportunity"
            else:
                destination = row["objective_location_id"]
                operation = "pursue"
        else:
            return None
        if not near_location(c, actor, destination):
            return ActionProposal(actor_id=actor["id"], action=ActionType.TRAVEL,
                                  destination_id=destination, reason=motive)
        if operation == "pursue":
            condition = json.loads(row["condition_json"])
            if condition["kind"] == "target_health_at_most":
                return ActionProposal(actor_id=actor["id"], action=ActionType.ATTACK,
                                      target_id=condition["target_id"], reason=motive)
            return None
        return operation

    @staticmethod
    def accept(c: sqlite3.Connection, opportunity_id: str, actor_id: str,
               at: datetime) -> str:
        row = OpportunityService._row(c, opportunity_id)
        before = {**ActionService._observed_state(c, actor_id),
                  "opportunity_status": row["status"]}
        actor = c.execute("SELECT * FROM characters WHERE id=? AND world_id=? AND health>0",
                          (actor_id, row["world_id"])).fetchone()
        place = c.execute("SELECT longitude,latitude FROM locations WHERE id=?",
                          (row["issuer_location_id"],)).fetchone()
        if row["status"] != "open" or not actor or not near_location(c, actor, row["issuer_location_id"]):
            raise ValueError("机会已被接取或人物尚未抵达发布地点")
        if great_circle_distance_km(actor["longitude"], actor["latitude"],
                                    place["longitude"], place["latitude"]) > .1:
            raise ValueError("人物距离发布地点过远")
        if not c.execute(
            "SELECT 1 FROM character_event_knowledge WHERE character_id=? AND event_id=? "
            "AND learned_world_time<=?", (actor_id, row["source_event_id"], to_iso(at)),
        ).fetchone():
            raise ValueError("人物尚未获知机会")
        c.execute(
            "UPDATE npc_opportunities SET status='accepted',accepted_by=?,accepted_world_time=? "
            "WHERE id=?", (actor_id, to_iso(at), opportunity_id),
        )
        event_id = ActionService._record_event(
            c, world_id=row["world_id"], tick_id=str(uuid4()), occurred_at=at,
            event_type="opportunity.accepted", actor_id=actor_id, target_id=row["issuer_id"],
            location_id=row["issuer_location_id"], summary="人物承接了一项公开机会。",
            payload={"opportunity_id": opportunity_id},
        )
        EventHistoryService.link(c, event_id, row["source_event_id"], strict=False)
        c.execute("UPDATE npc_opportunities SET accept_event_id=? WHERE id=?",
                  (event_id, opportunity_id))
        OpportunityService._audit(
            c, row["world_id"], event_id, actor_id, row["issuer_id"], at,
            "opportunity.accepted", before,
            {**ActionService._observed_state(c, actor_id),
             "opportunity_status": "accepted"},
        )
        return event_id

    @staticmethod
    def claim(c: sqlite3.Connection, opportunity_id: str, actor_id: str,
              at: datetime) -> str:
        row = OpportunityService._row(c, opportunity_id)
        if row["status"] == "completed" and row["accepted_by"] == actor_id:
            return row["payout_event_id"]
        actor = c.execute("SELECT * FROM characters WHERE id=? AND world_id=?",
                          (actor_id, row["world_id"])).fetchone()
        if row["status"] != "accepted" or row["accepted_by"] != actor_id or not actor:
            raise ValueError("人物没有承接这项机会")
        before = {**ActionService._observed_state(c, actor_id),
                  "opportunity_status": row["status"]}
        if not near_location(c, actor, row["issuer_location_id"]):
            raise ValueError("需要回到发布地点核验")
        evidence = OpportunityService.accepted_condition(c, row)
        if not evidence:
            raise ValueError("完成条件尚无可核验的行动证据")
        c.execute("UPDATE characters SET money=money+? WHERE id=?",
                  (row["reward_money"], actor_id))
        event_id = ActionService._record_event(
            c, world_id=row["world_id"], tick_id=str(uuid4()), occurred_at=at,
            event_type="opportunity.paid", actor_id=actor_id, target_id=row["issuer_id"],
            location_id=row["issuer_location_id"],
            summary=f"条件核验成立，领取托管报酬{row['reward_money']}枚货币。",
            payload={"opportunity_id": opportunity_id, "amount": row["reward_money"]},
        )
        c.execute("UPDATE npc_opportunities SET status='completed',payout_event_id=? WHERE id=?",
                  (event_id, opportunity_id))
        EventHistoryService.link(c, event_id, evidence, strict=False)
        OpportunityService._audit(
            c, row["world_id"], event_id, actor_id, row["issuer_id"], at,
            "opportunity.paid", before,
            {**ActionService._observed_state(c, actor_id),
             "opportunity_status": "completed"},
        )
        return event_id

    @staticmethod
    def cancel(c: sqlite3.Connection, opportunity_id: str, at: datetime,
               reason: str) -> str:
        row = OpportunityService._row(c, opportunity_id)
        if row["status"] not in {"open", "accepted"}:
            raise ValueError("机会已经结算或取消")
        before = {**ActionService._observed_state(c, row["issuer_id"]),
                  "opportunity_status": row["status"]}
        c.execute("UPDATE characters SET money=money+? WHERE id=?",
                  (row["reward_money"], row["issuer_id"]))
        event_id = ActionService._record_event(
            c, world_id=row["world_id"], tick_id=str(uuid4()), occurred_at=at,
            event_type="opportunity.cancelled", actor_id=row["issuer_id"],
            target_id=row["accepted_by"], location_id=row["issuer_location_id"],
            summary=f"公开机会取消：{reason}；托管报酬退还发布者。",
            payload={"opportunity_id": opportunity_id, "reason": reason},
        )
        c.execute("UPDATE npc_opportunities SET status='cancelled' WHERE id=?",
                  (opportunity_id,))
        EventHistoryService.link(c, event_id, row["source_event_id"], strict=False)
        OpportunityService._audit(
            c, row["world_id"], event_id, row["issuer_id"], row["accepted_by"], at,
            "opportunity.cancelled", before,
            {**ActionService._observed_state(c, row["issuer_id"]),
             "opportunity_status": "cancelled"},
        )
        return event_id
