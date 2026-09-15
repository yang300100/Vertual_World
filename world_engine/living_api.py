"""玩家的生活视角、日常交易、个人目标与编年者经济规则入口。"""

import json
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from world_engine.actions import ActionService
from world_engine.economy import CommoditySpec, EconomyService, WorkplaceBudgetSpec
from world_engine.geo import great_circle_distance_km
from world_engine.inventory import InventoryError, InventoryService
from world_engine.life import LifeActivityService
from world_engine.proximity import same_room
from world_engine.registration import ElementRegistrationSubmit, WorldElementRegistry
from world_engine.repository import from_iso, to_iso, utc_now
from world_engine.society import SocietyService
from world_engine.event_history import EventHistoryService
from world_engine.epistemics import KnowledgeService
from world_engine.character_growth import CharacterGrowthService
from world_engine.routines import RoutinePlanSpec, RoutineService
from world_engine.schedules import ScheduleService

LIVING_SCHEMA = """
CREATE TABLE IF NOT EXISTS player_life_goals (
 id TEXT PRIMARY KEY,world_id TEXT NOT NULL REFERENCES worlds(id),player_id TEXT NOT NULL REFERENCES characters(id),
 kind TEXT NOT NULL,title TEXT NOT NULL,target_id TEXT,quantity INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'active',created_world_time TEXT NOT NULL,completed_world_time TEXT
);
CREATE TABLE IF NOT EXISTS player_trade_requests (
 world_id TEXT NOT NULL,request_id TEXT NOT NULL,payload_json TEXT NOT NULL,response_json TEXT NOT NULL,
 PRIMARY KEY(world_id,request_id)
);
"""


class EconomyDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=8, max_length=100, pattern=r"^[A-Za-z0-9._:-]+$")
    payload: Annotated[CommoditySpec | WorkplaceBudgetSpec, Field(discriminator="element_type")]


class TradeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seller_id: str
    item_id: str
    quantity: int = Field(default=1, ge=1, le=100, strict=True)
    operation: Literal["buy", "sell"] = "buy"
    expected_total: int = Field(ge=0, strict=True)
    request_id: str = Field(min_length=8, max_length=100)


class LifeGoalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: Literal["work", "learn", "friend", "reside"]
    title: str = Field(min_length=1, max_length=100)
    target_id: str | None = None
    quantity: int = Field(default=3, ge=1, le=365, strict=True)


class RoutineDefinition(BaseModel):
    model_config=ConfigDict(extra="forbid")
    idempotency_key:str=Field(min_length=8,max_length=100,pattern=r"^[A-Za-z0-9._:-]+$")
    routine:RoutinePlanSpec


def build_living_router(database, engine):
    router = APIRouter(prefix="/api/worlds/{world_id}", tags=["生活与社会"])
    from world_engine.sequences import SequenceRequest, execute_sequence

    @router.post("/player/sequence")
    def sequence(world_id: str, payload: SequenceRequest):
        try:
            return execute_sequence(engine, world_id, payload)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get("/player/sequences")
    def sequences(world_id: str):
        with database.read() as c:
            player, _ = actor(c, world_id)
            return [
                {
                    "id": row["id"],
                    "request_id": row["request_key"],
                    "status": row["status"],
                    "next_index": row["next_index"],
                    "results": json.loads(row["results_json"]),
                    "error": row["error"],
                    **json.loads(row["payload_json"]),
                }
                for row in c.execute(
                    "SELECT * FROM player_action_sequences WHERE world_id=? AND player_id=? AND status!='completed' ORDER BY rowid DESC LIMIT 10",
                    (world_id, player["id"]),
                )
            ]

    def actor(c, wid):
        world = c.execute("SELECT w.current_time FROM worlds w WHERE id=?", (wid,)).fetchone()
        if world is None:
            raise HTTPException(404, "世界不存在")
        player = c.execute(
            "SELECT * FROM characters WHERE world_id=? AND is_player=1", (wid,)
        ).fetchone()
        if player is None:
            raise HTTPException(404, "当前世界尚无玩家")
        return player, from_iso(world[0])

    def visible(c, player):
        return [
            row
            for row in c.execute(
                "SELECT * FROM characters WHERE world_id=? AND is_player=0 AND health>0",
                (player["world_id"],),
            )
            if same_room(player, row)
            and great_circle_distance_km(
                player["longitude"], player["latitude"], row["longitude"], row["latitude"]
            )
            <= 0.1
        ]

    def knowledge_name(c, pid, cid):
        return SocietyService.label(c, pid, cid)

    @router.get("/player/experience")
    def experience(world_id: str):
        with database.read() as c:
            player, at = actor(c, world_id)
            people = []
            for index, npc in enumerate(visible(c, player), 1):
                name = knowledge_name(c, player["id"], npc["id"])
                if name == "尚未认识的人":
                    name = f"陌生人{index}"
                activity = LifeActivityService.running(c, npc["id"])
                people.append(
                    {
                        "id": npc["id"],
                        "name": name,
                        "known_name": name if not name.startswith("陌生人") else None,
                        "distance_m": round(
                            great_circle_distance_km(
                                player["longitude"],
                                player["latitude"],
                                npc["longitude"],
                                npc["latitude"],
                            )
                            * 1000
                        ),
                        "activity": activity["kind"] if activity else "idle",
                    }
                )
            known = []
            for row in c.execute(
                "SELECT * FROM character_acquaintances WHERE observer_id=? ORDER BY last_seen DESC",
                (player["id"],),
            ):
                rel = c.execute(
                    "SELECT affinity,trust FROM relationships WHERE world_id=? AND source_character_id=? AND target_character_id=?",
                    (world_id, player["id"], row["subject_id"]),
                ).fetchone()
                stage = (
                    "熟悉"
                    if row["shared_experiences"] >= 3
                    else "相识"
                    if row["known_name"]
                    else "见过"
                )
                if rel and rel["trust"] >= 50 and row["shared_experiences"] >= 5:
                    stage = "信赖"
                known.append(
                    {
                        "id": row["subject_id"],
                        "name": row["known_name"] or "见过但未得知姓名的人",
                        "stage": stage,
                        "last_seen": row["last_seen"],
                        "shared_experiences": row["shared_experiences"],
                        "conflict": row["conflict"],
                    }
                )
            known_ids={item["id"] for item in known}
            for node in c.execute("SELECT subject_id FROM observer_knowledge_nodes WHERE observer_id=?",(player["id"],)):
                if node[0] not in known_ids and node[0]!=player["id"]:
                    known.append({"id":node[0],"name":KnowledgeService.known_label(c,player["id"],node[0]),"stage":"听闻","last_seen":None,"shared_experiences":0,"conflict":0})
            events = []
            for row in c.execute(
                "SELECT e.*,k.source_kind,k.source_character_id,k.confidence,k.learned_world_time FROM character_event_knowledge k JOIN world_events e ON e.id=k.event_id WHERE k.world_id=? AND k.character_id=? ORDER BY k.learned_world_time DESC,e.created_at DESC LIMIT 80",
                (world_id, player["id"]),
            ):
                entry = dict(row)
                entry["payload"] = json.loads(entry.pop("payload_json"))
                entry["history"] = EventHistoryService.view(c,entry["id"],player["id"])
                entry["summary"] = SocietyService.mask_text(
                    c, world_id, player["id"], entry["summary"]
                )
                if entry["source_kind"] == "heard" or player["id"] not in {
                    entry["actor_id"],
                    entry["target_id"],
                }:
                    entry["payload"] = {
                        key: value
                        for key, value in entry["payload"].items()
                        if key in {"dialogue", "reply", "delivery"}
                        and entry["source_kind"] != "heard"
                    }
                elif entry["actor_id"] != player["id"]:
                    entry["payload"].pop("reason", None)
                    entry["payload"].pop("metadata", None)
                events.append(entry)
            notifications = [
                dict(row)
                for row in c.execute(
                    "SELECT * FROM player_notifications WHERE world_id=? AND recipient_id=? ORDER BY created_at DESC LIMIT 40",
                    (world_id, player["id"]),
                )
            ]
            for entry in notifications:
                entry["title"] = SocietyService.mask_text(c, world_id, player["id"], entry["title"])
            residences = [
                dict(row)
                for row in c.execute(
                    "SELECT r.name,r.location_id,f.due_world_time,f.started_world_time,f.status,r.id AS room_id FROM contract_fulfillments f JOIN life_rooms r ON r.id=f.asset_id WHERE f.world_id=? AND f.requester_id=? AND f.kind='lodging' ORDER BY f.started_world_time DESC",
                    (world_id, player["id"]),
                )
            ]
            work = [
                dict(row)
                for row in c.execute(
                    "SELECT e.location_id,l.name,count(*) AS completed_units FROM world_events e JOIN locations l ON l.id=e.location_id WHERE e.world_id=? AND e.actor_id=? AND e.event_type='action.work' AND json_extract(e.payload_json,'$.work_completed')=1 GROUP BY e.location_id",
                    (world_id, player["id"]),
                )
            ]
            goals = []
            for row in c.execute(
                "SELECT * FROM player_life_goals WHERE world_id=? AND player_id=? ORDER BY created_world_time DESC",
                (world_id, player["id"]),
            ):
                progress = 0
                if row["kind"] == "work":
                    progress = sum(
                        item["completed_units"]
                        for item in work
                        if not row["target_id"] or item["location_id"] == row["target_id"]
                    )
                if row["kind"] == "learn":
                    skill = c.execute(
                        "SELECT proficiency FROM character_skill_proficiencies WHERE character_id=? AND skill_name=?",
                        (player["id"], row["target_id"]),
                    ).fetchone()
                    progress = skill[0] if skill else 0
                if row["kind"] == "friend":
                    progress = next(
                        (
                            item["shared_experiences"]
                            for item in known
                            if item["id"] == row["target_id"]
                        ),
                        0,
                    )
                if row["kind"] == "reside":
                    progress = max(
                        [
                            max(
                                0,
                                (
                                    min(at, from_iso(item["due_world_time"]))
                                    - from_iso(item["started_world_time"])
                                ).days,
                            )
                            for item in residences
                            if not row["target_id"] or item["room_id"] == row["target_id"]
                        ]
                        or [0]
                    )
                goals.append(
                    {
                        **dict(row),
                        "progress": progress,
                        "achieved": row["status"] == "completed" or progress >= row["quantity"],
                    }
                )
            return {
                "world_time": to_iso(at),
                "public_routines":[{**schedule,"name":SocietyService.label(c,player["id"],person["id"])} for person in known if (schedule:=RoutineService.view(c,world_id,person["id"],public_only=True))],
                "appointments": ScheduleService.list_for(c,world_id,player["id"]),
                "beliefs": KnowledgeService.view(c,player["id"],at),
                "dispositions": CharacterGrowthService.view(c,player["id"]),
                "people": people,
                "known_people": known,
                "events": events,
                "notifications": notifications,
                "personal_state": SocietyService.personal_context(c, world_id, player["id"], at)[
                    "emotion"
                ],
                "residences": residences,
                "work_history": work,
                "goals": goals,
            }

    @router.post("/player/notifications/{notification_id}/read")
    def read_notification(world_id: str, notification_id: str):
        with database.write() as c:
            player, _ = actor(c, world_id)
            changed = c.execute(
                "UPDATE player_notifications SET read_at=COALESCE(read_at,?) WHERE id=? AND world_id=? AND recipient_id=?",
                (to_iso(utc_now()), notification_id, world_id, player["id"]),
            ).rowcount
            if not changed:
                raise HTTPException(404, "通知不存在")
        return {"ok": True}

    @router.post("/player/knowledge/observe/{subject_id}")
    def observe_knowledge(world_id:str,subject_id:str):
        with database.write() as c:
            player,at=actor(c,world_id)
            LifeActivityService.assert_available(c,player["id"],talking=True)
            subject=next((row for row in visible(c,player) if row["id"]==subject_id),None)
            if subject is None:
                raise HTTPException(409,"只能核对当前确实看见的人物，不能远程读取对方位置")
            event_id=ActionService._record_event(c,world_id=world_id,tick_id=str(uuid4()),occurred_at=at,event_type="knowledge.observed",actor_id=player["id"],target_id=subject_id,location_id=player["current_location_id"] or player["location_id"],summary="你核对了眼前人物所在的位置。",payload={})
            c.execute("UPDATE worlds SET version=version+1 WHERE id=?",(world_id,))
            return {"summary":"已用当前亲眼所见更新位置记录；旧说法保留供对照。","event_id":event_id}

    @router.get("/player/events/{event_id}/causes")
    def event_causes(world_id:str,event_id:str):
        with database.read() as c:
            player,_=actor(c,world_id)
            result=EventHistoryService.view(c,event_id,player["id"])
            if result is None:
                raise HTTPException(404,"你尚未获知这件事")
            return result

    @router.post("/player/life-goals", status_code=201)
    def goal(world_id: str, payload: LifeGoalRequest):
        with database.write() as c:
            player, at = actor(c, world_id)
            gid = str(uuid4())
            if payload.kind in {"learn", "friend"} and not payload.target_id:
                raise HTTPException(400, "请指定技能或人物")
            c.execute(
                "INSERT INTO player_life_goals(id,world_id,player_id,kind,title,target_id,quantity,created_world_time) VALUES (?,?,?,?,?,?,?,?)",
                (
                    gid,
                    world_id,
                    player["id"],
                    payload.kind,
                    payload.title,
                    payload.target_id,
                    payload.quantity,
                    to_iso(at),
                ),
            )
            return {"id": gid, "summary": "你的个人目标已记录；它不会替NPC安排计划或授予能力。"}

    @router.get("/player/market")
    def market(world_id: str):
        from world_engine.food import FoodService

        with database.read() as c:
            player, at = actor(c, world_id)
            offers = []
            for npc in visible(c, player):
                if not any(
                    word in (npc["identity"] or "") for word in ("商", "店", "贩", "医", "匠", "厨")
                ):
                    continue
                for item in c.execute(
                    "SELECT i.*,t.name,p.price FROM item_instances i JOIN item_types t ON t.id=i.item_type_id JOIN world_item_profiles p ON p.item_type_id=i.item_type_id WHERE i.world_id=? AND i.container_type='character_inventory' AND i.container_id=? AND i.owner_character_id=? AND i.quantity>0",
                    (world_id, npc["id"], npc["id"]),
                ):
                    if FoodService.spoiled(item, at):
                        continue
                    offers.append(
                        {
                            "item_id": item["id"],
                            "seller_id": npc["id"],
                            "seller_name": knowledge_name(c, player["id"], npc["id"]),
                            "name": item["name"],
                            "quantity": item["quantity"],
                            "price": item["price"],
                            "freshness": FoodService.view(item, at),
                        }
                    )
            rooms = [
                dict(row)
                for row in c.execute(
                    "SELECT r.id,r.name,r.location_id,r.owner_character_id,p.nightly_rate,l.longitude,l.latitude FROM life_rooms r JOIN room_rental_rates p ON p.room_id=r.id JOIN locations l ON l.id=r.location_id WHERE r.world_id=? AND l.is_active=1 AND r.id NOT IN (SELECT asset_id FROM contract_fulfillments WHERE kind='lodging' AND status='active')",
                    (world_id,),
                )
                if great_circle_distance_km(
                    player["longitude"], player["latitude"], row["longitude"], row["latitude"]
                )
                <= 0.1
            ]
            merchants = [
                {"id": row["id"], "name": knowledge_name(c, player["id"], row["id"])}
                for row in visible(c, player)
                if any(
                    word in (row["identity"] or "") for word in ("商", "店", "贩", "医", "匠", "厨")
                )
            ]
            sell_items = [
                dict(row)
                for row in c.execute(
                    "SELECT i.id,i.quantity,t.name,MAX(1,p.price/2) AS price FROM item_instances i JOIN item_types t ON t.id=i.item_type_id JOIN world_item_profiles p ON p.item_type_id=i.item_type_id WHERE i.world_id=? AND i.container_type='character_inventory' AND i.container_id=? AND i.owner_character_id=? AND (i.spoils_world_time IS NULL OR i.spoils_world_time>?)",
                    (world_id, player["id"], player["id"], to_iso(at)),
                )
            ]
            return {
                "offers": offers,
                "rooms": rooms,
                "merchants": merchants,
                "sell_items": sell_items,
            }

    @router.post("/player/trade")
    def trade(world_id: str, payload: TradeRequest):
        try:
            with database.write() as c:
                player, at = actor(c, world_id)
                canonical = payload.model_dump_json(exclude={"request_id"})
                prior = c.execute(
                    "SELECT * FROM player_trade_requests WHERE world_id=? AND request_id=?",
                    (world_id, payload.request_id),
                ).fetchone()
                if prior:
                    if prior["payload_json"] != canonical:
                        raise HTTPException(409, "请求标识已用于其他交易")
                    return json.loads(prior["response_json"])
                LifeActivityService.assert_available(c, player["id"])
                seller = c.execute(
                    "SELECT * FROM characters WHERE id=? AND world_id=? AND is_player=0",
                    (payload.seller_id, world_id),
                ).fetchone()
                if (
                    seller is None
                    or not same_room(player, seller)
                    or great_circle_distance_km(
                        player["longitude"],
                        player["latitude"],
                        seller["longitude"],
                        seller["latitude"],
                    )
                    > 0.1
                ):
                    raise HTTPException(409, "交易对象不在当前场景")
                if not any(
                    word in (seller["identity"] or "")
                    for word in ("商", "店", "贩", "医", "匠", "厨")
                ):
                    raise HTTPException(409, "对方没有经营这类交易")
                owner = seller if payload.operation == "buy" else player
                buyer = player if payload.operation == "buy" else seller
                item = c.execute(
                    "SELECT i.*,t.name,t.stack_limit,t.slot_size,p.price FROM item_instances i JOIN item_types t ON t.id=i.item_type_id JOIN world_item_profiles p ON p.item_type_id=i.item_type_id WHERE i.id=? AND i.world_id=? AND i.container_type='character_inventory' AND i.container_id=? AND i.owner_character_id=?",
                    (payload.item_id, world_id, owner["id"], owner["id"]),
                ).fetchone()
                if item is None:
                    raise HTTPException(409, "物品已不在交易库存中")
                from world_engine.food import FoodService

                if FoodService.spoiled(item, at):
                    raise HTTPException(409, "食物已经变质，本次买卖取消")
                unit = item["price"] if payload.operation == "buy" else max(1, item["price"] // 2)
                total = unit * payload.quantity
                if total != payload.expected_total:
                    raise HTTPException(409, "价格已变化，请重新核对")
                if buyer["money"] < total:
                    raise HTTPException(409, "付款方资金不足")
                before = InventoryService.snapshot(c, world_id)
                InventoryService.transfer(c, item, buyer["id"], payload.quantity)
                c.execute("UPDATE characters SET money=money-? WHERE id=?", (total, buyer["id"]))
                c.execute("UPDATE characters SET money=money+? WHERE id=?", (total, owner["id"]))
                summary = f"成交：{item['name']} × {payload.quantity}，共{total}枚货币。"
                eid = ActionService._record_event(
                    c,
                    world_id=world_id,
                    tick_id=payload.request_id,
                    occurred_at=at,
                    event_type="action.trade",
                    actor_id=player["id"],
                    target_id=seller["id"],
                    location_id=player["current_location_id"] or player["location_id"],
                    summary=summary,
                    payload={"item_id": item["id"], "quantity": payload.quantity, "total": total},
                )
                InventoryService.audit(c, world_id, eid, before)
                c.execute("UPDATE worlds SET version=version+1 WHERE id=?", (world_id,))
                response = {"summary": summary, "event_id": eid}
                c.execute(
                    "INSERT INTO player_trade_requests VALUES (?,?,?,?)",
                    (
                        world_id,
                        payload.request_id,
                        canonical,
                        json.dumps(response, ensure_ascii=False),
                    ),
                )
                return response
        except (ValueError, InventoryError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/player/harvest")
    def harvest(world_id: str):
        with database.write() as c:
            player, at = actor(c, world_id)
            LifeActivityService.assert_available(c, player["id"])
            if not EconomyService.harvest(c, player, at):
                raise HTTPException(409, "当前没有可合法收取的已登记资源，或背包/精力不足")
            c.execute("UPDATE worlds SET version=version+1 WHERE id=?", (world_id,))
            return {"summary": "已收取一份真实资源。"}

    @router.post("/economy-definitions", status_code=201)
    def definition(world_id: str, payload: EconomyDefinition):
        with database.write() as c:
            _, at = actor(c, world_id)
            key = "economy:" + payload.idempotency_key
            existing = c.execute(
                "SELECT source_event_id FROM element_registration_requests WHERE world_id=? AND idempotency_key=?",
                (world_id, key),
            ).fetchone()
            source = (
                existing[0]
                if existing
                else ActionService._record_event(
                    c,
                    world_id=world_id,
                    tick_id=key,
                    occurred_at=at,
                    event_type="world.economy_proposed",
                    actor_id=None,
                    target_id=None,
                    location_id=None,
                    summary="编年者提交了经济规则待审核。",
                    payload={},
                )
            )
            result = WorldElementRegistry().submit(
                c,
                world_id=world_id,
                request=ElementRegistrationSubmit(
                    source_event_id=source, idempotency_key=key, payload=payload.payload
                ),
                auto_apply=False,
            )
            return {
                "summary": "已提交经济规则，请核对后确认登记。",
                "registration": result.model_dump(mode="json"),
            }

    @router.get("/npc-routines")
    def routine_catalogue(world_id:str):
        with database.read() as c:
            actor(c,world_id)
            return [RoutineService.view(c,world_id,row[0]) for row in c.execute("SELECT character_id FROM npc_routine_plans WHERE world_id=?",(world_id,))]

    @router.post("/npc-routines",status_code=201)
    def routine_definition(world_id:str,payload:RoutineDefinition):
        with database.write() as c:
            _,at=actor(c,world_id)
            key="routine:"+payload.idempotency_key
            old=c.execute("SELECT source_event_id FROM element_registration_requests WHERE world_id=? AND idempotency_key=?",(world_id,key)).fetchone()
            source=old[0] if old else ActionService._record_event(c,world_id=world_id,tick_id=key,occurred_at=at,event_type="world.routine_proposed",actor_id=None,target_id=None,location_id=None,summary="编年者提交了周期作息草案，等待核对。",payload={})
            try:
                result=WorldElementRegistry().submit(c,world_id=world_id,request=ElementRegistrationSubmit(source_event_id=source,idempotency_key=key,payload=payload.routine),auto_apply=False)
            except ValueError as exc:
                raise HTTPException(409,str(exc)) from exc
            return {"summary":"周期作息已提交，核对并确认登记后才会用于日常安排。","registration":result.model_dump(mode="json")}

    return router
