"""哥哥，玩家只表达自己的选择，NPC 通过真实对话独立回应。"""

import hashlib
import json
from datetime import timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from world_engine.api_deps import require_player_and_time
from world_engine.appointments import AppointmentIntent, AppointmentService
from world_engine.companion_proposals import CompanionIntent, CompanionProposalService
from world_engine.demographics import age_years_at
from world_engine.families import FamilyIntent, FamilyService
from world_engine.group_journey_options import group_journey_options
from world_engine.ride_options import ride_options
from world_engine.rides import RideIntent, RideService
from world_engine.group_journeys import GroupJourneyIntent, GroupJourneyService
from world_engine.intimacy import IntimacyService, RelationshipIntent
from world_engine.journeys import JourneyIntent, JourneyService
from world_engine.lodging_plans import LodgingPlanIntent, LodgingPlanService
from world_engine.overnight import OvernightIntent, OvernightService
from world_engine.repository import WorldRepository, to_iso
from world_engine.separate_stays import SeparateStayIntent, SeparateStayService
from world_engine.shared_life import SharedLifeIntent, SharedLifeService
from world_engine.shared_plans import SharedPlanIntent, SharedPlanService


class PlayerRelationshipChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipient_id: str = Field(min_length=1, max_length=100)
    choice: RelationshipIntent


class PlayerSharedChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipient_id: str = Field(min_length=1, max_length=100)
    choice: SharedLifeIntent


class NaturalSocialChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=8, max_length=100)
    recipient_id: str = Field(min_length=1, max_length=100)
    intent: str = Field(min_length=1, max_length=1000)
    evidence: str = Field(min_length=2, max_length=240)
    relationship_choice: RelationshipIntent | None = None
    shared_life_choice: SharedLifeIntent | None = None
    family_choice: FamilyIntent | None = None
    appointment_choice: AppointmentIntent | None = None
    shared_plan_choice: SharedPlanIntent | None = None
    journey_choice: JourneyIntent | None = None
    group_journey_choice: GroupJourneyIntent | None = None
    ride_choice: RideIntent | None = None
    overnight_choice: OvernightIntent | None = None
    separate_stay_choice: SeparateStayIntent | None = None
    lodging_plan_choice: LodgingPlanIntent | None = None
    companion_choice: CompanionIntent | None = None


def compatible_choice_digests(payload):
    """哥哥，只去除历史版本没有的空字段，旧请求仍表示同一份明确选择。"""
    variants=[payload.model_dump(mode="json")]
    if payload.appointment_choice and payload.appointment_choice.meal_supply is None:
        legacy=json.loads(json.dumps(variants[0]));legacy["appointment_choice"].pop("meal_supply",None);variants.append(legacy)
    if payload.appointment_choice and payload.appointment_choice.journey_id is None:
        for variant in list(variants):
            legacy=json.loads(json.dumps(variant));legacy["appointment_choice"].pop("journey_id",None);variants.append(legacy)
    if payload.shared_life_choice:
        for field, default in (("preparation_plan_id", None), ("meal_supply", "each_own"), ("journey_id",None)):
            if getattr(payload.shared_life_choice, field) == default:
                for variant in list(variants):
                    legacy=json.loads(json.dumps(variant));legacy["shared_life_choice"].pop(field,None);variants.append(legacy)
    if payload.separate_stay_choice and payload.separate_stay_choice.source_lodging_plan_id is None:
        for variant in list(variants):
            legacy=json.loads(json.dumps(variant));legacy["separate_stay_choice"].pop("source_lodging_plan_id",None);variants.append(legacy)
    if payload.separate_stay_choice and payload.separate_stay_choice.change_id is None:
        for variant in list(variants):
            legacy=json.loads(json.dumps(variant));legacy["separate_stay_choice"].pop("change_id",None);variants.append(legacy)
    if payload.journey_choice and getattr(payload.journey_choice,"leader_id",None) is None:
        for variant in list(variants):
            legacy=json.loads(json.dumps(variant));legacy["journey_choice"].pop("leader_id",None);variants.append(legacy)
    if payload.ride_choice:
        for field in ("recovery_id", "handoff_id"):
            if getattr(payload.ride_choice,field,None) is None:
                for variant in list(variants):
                    legacy=json.loads(json.dumps(variant));legacy["ride_choice"].pop(field,None);variants.append(legacy)
    for field in ("shared_plan_choice","appointment_choice","family_choice","journey_choice","group_journey_choice","ride_choice","overnight_choice","separate_stay_choice","lodging_plan_choice","companion_choice"):
        if getattr(payload,field) is None:
            for variant in list(variants):
                legacy=json.loads(json.dumps(variant));legacy.pop(field,None);variants.append(legacy)
    if payload.appointment_choice:
        choice=payload.appointment_choice
        if not choice.series_id and not choice.repeat_every_days and choice.repeat_count==1 and not choice.accept_recurrence:
            for variant in list(variants):
                legacy=json.loads(json.dumps(variant))
                for key in ("series_id","repeat_every_days","repeat_count","accept_recurrence"):legacy["appointment_choice"].pop(key,None)
                variants.append(legacy)
        if not choice.room_id and not choice.allow_visit:
            for variant in list(variants):
                legacy=json.loads(json.dumps(variant))
                for key in ("room_id","allow_visit"):legacy["appointment_choice"].pop(key,None)
                variants.append(legacy)
    return {hashlib.sha256(json.dumps(v,ensure_ascii=False,separators=(",",":")).encode("utf-8")).hexdigest() for v in variants}


class PlayerFamilyChoice(BaseModel):
    model_config=ConfigDict(extra="forbid")
    recipient_id:str=Field(min_length=1,max_length=100)
    choice:FamilyIntent


class PlayerLifeDetails(BaseModel):
    model_config=ConfigDict(extra="forbid")
    age_years:int=Field(ge=0,le=10000,strict=True)


def build_intimacy_router(database):
    router = APIRouter(prefix="/api/worlds/{world_id}", tags=["亲密关系"])

    @router.get("/relationships")
    def relationships(world_id: str):
        with database.read() as c:
            player, at = require_player_and_time(c, world_id)
            return IntimacyService.rows(c, world_id, player["id"], at)

    @router.get("/relationships/{character_id}/memories")
    def relationship_memories(world_id: str, character_id: str):
        with database.read() as c:
            player, _ = require_player_and_time(c, world_id)
            if not c.execute("SELECT 1 FROM characters WHERE id=? AND world_id=?", (character_id, world_id)).fetchone():
                raise HTTPException(404, "人物不属于当前世界")
            return IntimacyService.landmarks(c, world_id, player["id"], character_id)

    @router.post("/relationships")
    def choose_relationship(world_id: str, payload: PlayerRelationshipChoice):
        with database.write() as c:
            player, at = require_player_and_time(c, world_id)
            npc = c.execute("SELECT * FROM characters WHERE id=? AND world_id=? AND is_player=0",
                            (payload.recipient_id, world_id)).fetchone()
            if npc is None:
                raise HTTPException(404, "NPC 不属于当前世界")
            try:
                result = IntimacyService.apply(c, player, npc, at, payload.choice)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            WorldRepository().bump_version(c, world_id)
            return result

    @router.post("/shared-life")
    def choose_shared_life(world_id: str, payload: PlayerSharedChoice):
        with database.write() as c:
            player, at = require_player_and_time(c, world_id)
            npc = c.execute("SELECT * FROM characters WHERE id=? AND world_id=? AND is_player=0", (payload.recipient_id, world_id)).fetchone()
            try:
                result = SharedLifeService.apply(c, player, npc, at, payload.choice)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            WorldRepository().bump_version(c, world_id)
            return result

    @router.post("/player/social-choices")
    def natural_choice(world_id: str, payload: NaturalSocialChoice):
        if payload.evidence not in payload.intent or sum(bool(x) for x in (payload.relationship_choice,payload.shared_life_choice,payload.family_choice,payload.appointment_choice,payload.shared_plan_choice,payload.journey_choice,payload.group_journey_choice,payload.ride_choice,payload.overnight_choice,payload.separate_stay_choice,payload.lodging_plan_choice,payload.companion_choice))!=1:
            raise HTTPException(422, "需要一份有玩家原话证据的明确选择")
        digest = hashlib.sha256(payload.model_dump_json().encode("utf-8")).hexdigest()
        compatible_digests=compatible_choice_digests(payload)|{digest}
        with database.write() as c:
            player, at = require_player_and_time(c, world_id)
            receipt = c.execute("SELECT * FROM social_choice_receipts WHERE world_id=? AND player_id=? AND request_id=?", (world_id, player["id"], payload.request_id)).fetchone()
            if receipt:
                if receipt["payload_hash"] not in compatible_digests:
                    raise HTTPException(409, "同一请求编号不能表示另一份选择")
                return json.loads(receipt["result_json"])
            npc = c.execute("SELECT * FROM characters WHERE id=? AND world_id=? AND is_player=0", (payload.recipient_id, world_id)).fetchone()
            choice = payload.relationship_choice or payload.shared_life_choice or payload.family_choice or payload.appointment_choice or payload.shared_plan_choice or payload.journey_choice or payload.group_journey_choice or payload.ride_choice or payload.overnight_choice or payload.separate_stay_choice or payload.lodging_plan_choice or payload.companion_choice
            choice = choice.model_copy(update={"statement": payload.evidence})
            before_event_rowid = c.execute("SELECT COALESCE(MAX(rowid),0) FROM world_events").fetchone()[0]
            try:
                service = IntimacyService if payload.relationship_choice else SharedLifeService if payload.shared_life_choice else AppointmentService if payload.appointment_choice else SharedPlanService if payload.shared_plan_choice else JourneyService if payload.journey_choice else OvernightService if payload.overnight_choice else SeparateStayService if payload.separate_stay_choice else LodgingPlanService if payload.lodging_plan_choice else CompanionProposalService if payload.companion_choice else FamilyService
                if payload.ride_choice:
                    if npc is None:
                        raise ValueError("人物不属于当前世界")
                    if choice.operation == "propose":
                        members = set(choice.member_ids)
                        visible = {person["id"] for person in ride_options(c,world_id,player["id"],at)["members"]}
                        if not members.issubset(visible):
                            raise ValueError("共乘名单只能包含当前眼前的人物")
                    else:
                        shared = next((row for row in RideService.rows(c,player,at) if row["id"] == choice.ride_id),None)
                        if shared is None:
                            raise ValueError("只能选择本人参加的真实共乘")
                        members = {member["character_id"] for member in shared["members"]}
                    if npc["id"] not in members:
                        raise ValueError("目标人物必须属于这份共乘的原名单")
                    result = RideService.apply(c,player,at,choice)
                elif payload.group_journey_choice:
                    if npc is None:
                        raise ValueError("人物不属于当前世界")
                    if choice.operation == "propose":
                        members = set(choice.member_ids)
                    else:
                        shared = next((row for row in GroupJourneyService.rows(c, world_id, player["id"], at)
                                       if row["id"] == choice.journey_id), None)
                        if shared is None:
                            raise ValueError("只能选择本人参加的真实行程")
                        members = {member["character_id"] for member in shared["members"]}
                    if npc["id"] not in members:
                        raise ValueError("目标索引必须属于原完整名单，不能替换成员")
                    if choice.operation not in {"leave", "pause", "end", "withdraw_follow"}:
                        visible = {person["id"] for person in group_journey_options(c, world_id, player["id"], at)["members"]}
                        if npc["id"] not in visible:
                            raise ValueError("远处只能操作本人已有行程的暂停或退出")
                        if choice.operation == "propose" and not members.issubset(visible | {player["id"]}):
                            raise ValueError("完整名单只能选择当前可见的成员")
                    result = GroupJourneyService.apply(c, player, at, choice)
                else:
                    result = service.apply(c, player, npc, at, choice)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            row = c.execute("SELECT payload_json,actor_id,rowid AS event_rowid FROM world_events WHERE id=?", (result["event_id"],)).fetchone()
            source = json.loads(row[0])
            # 哥哥，只给本次新建的本人事件附原话；幂等回执不能改写旧历史或别人的同意。
            if row["actor_id"] == player["id"] and row["event_rowid"] > before_event_rowid:
                for key, value in {"player_input": payload.intent, "input_request_id": payload.request_id, "choice_evidence": payload.evidence}.items():
                    source.setdefault(key, value)
                c.execute("UPDATE world_events SET payload_json=? WHERE id=?", (json.dumps(source, ensure_ascii=False), result["event_id"]))
            c.execute("INSERT INTO social_choice_receipts VALUES (?,?,?,?,?)", (world_id, payload.request_id, player["id"], digest, json.dumps(result, ensure_ascii=False)))
            WorldRepository().bump_version(c, world_id)
            return result

    @router.get("/rides/options")
    def ride_candidates(world_id: str):
        with database.read() as c:
            player,at=require_player_and_time(c,world_id)
            return ride_options(c,world_id,player["id"],at)

    @router.get("/rides")
    def rides(world_id: str):
        with database.read() as c:
            player,at=require_player_and_time(c,world_id)
            return {"rides":RideService.rows(c,player,at)}

    @router.get("/group-journeys/options")
    def group_options(world_id: str):
        with database.read() as c:
            player, at = require_player_and_time(c, world_id)
            return group_journey_options(c, world_id, player["id"], at)

    @router.get("/group-journeys")
    def group_journeys(world_id: str, character_id: str | None = None):
        with database.read() as c:
            player, at = require_player_and_time(c, world_id)
            if character_id and not c.execute("SELECT 1 FROM characters WHERE id=? AND world_id=? AND is_player=0", (character_id, world_id)).fetchone():
                raise HTTPException(404, "人物不属于当前世界")
            return GroupJourneyService.rows(c, world_id, player["id"], at, character_id)

    @router.get("/journeys")
    def journeys(world_id:str,character_id:str|None=None):
        with database.read() as c:
            player,at=require_player_and_time(c,world_id)
            if character_id and not c.execute("SELECT 1 FROM characters WHERE id=? AND world_id=? AND is_player=0",(character_id,world_id)).fetchone():raise HTTPException(404,"人物不属于当前世界")
            return JourneyService.rows(c,world_id,player["id"],at,character_id)

    @router.get("/overnights")
    def overnights(world_id:str,character_id:str|None=None):
        with database.read() as c:
            player,at=require_player_and_time(c,world_id)
            if character_id and not c.execute("SELECT 1 FROM characters WHERE id=? AND world_id=? AND is_player=0",(character_id,world_id)).fetchone():raise HTTPException(404,"人物不属于当前世界")
            return OvernightService.rows(c,world_id,player["id"],at,character_id)

    @router.get("/overnights/options/{character_id}")
    def overnight_options(world_id: str, character_id: str):
        with database.read() as c:
            player, at = require_player_and_time(c, world_id)
            npc = c.execute(
                "SELECT * FROM characters WHERE id=? AND world_id=? AND is_player=0",
                (character_id, world_id),
            ).fetchone()
            if not npc:
                raise HTTPException(404, "人物不属于当前世界")
            if not c.execute(
                "SELECT 1 FROM character_acquaintances WHERE world_id=? AND observer_id=? AND subject_id=?",
                (world_id, player["id"], character_id),
            ).fetchone():
                try:
                    IntimacyService._reachable(c, player, npc)
                except ValueError as exc:
                    raise HTTPException(404, "目前不认识或无法接触这位人物") from exc
            return {
                "room_options": OvernightService.room_options(
                    c, world_id, player["id"], character_id, at
                )
            }

    @router.get("/separate-stays")
    def separate_stays(world_id: str, character_id: str | None = None):
        with database.read() as c:
            player, at = require_player_and_time(c, world_id)
            if character_id and not c.execute(
                "SELECT 1 FROM characters WHERE id=? AND world_id=? AND is_player=0",
                (character_id, world_id),
            ).fetchone():
                raise HTTPException(404, "人物不属于当前世界")
            return SeparateStayService.rows(c, world_id, player["id"], at, character_id)

    @router.get("/separate-stays/options/{character_id}")
    def separate_stay_options(world_id: str, character_id: str):
        # 哥哥，认识对方也不公开其所有住处，房间仍须来自本人已知或眼前所见。
        with database.read() as c:
            player, at = require_player_and_time(c, world_id)
            npc = c.execute(
                "SELECT * FROM characters WHERE id=? AND world_id=? AND is_player=0",
                (character_id, world_id),
            ).fetchone()
            if not npc:
                raise HTTPException(404, "人物不属于当前世界")
            if not c.execute(
                "SELECT 1 FROM character_acquaintances WHERE world_id=? AND observer_id=? AND subject_id=?",
                (world_id, player["id"], character_id),
            ).fetchone():
                try:
                    IntimacyService._reachable(c, player, npc)
                except ValueError as exc:
                    raise HTTPException(404, "目前不认识或无法接触这位人物") from exc
            return {"room_options": SeparateStayService.room_options(c, world_id, player["id"], character_id, at)}

    @router.get("/lodging-plans")
    def lodging_plans(world_id: str, character_id: str | None = None):
        with database.read() as c:
            player, at = require_player_and_time(c, world_id)
            if character_id and not c.execute(
                "SELECT 1 FROM characters WHERE id=? AND world_id=? AND is_player=0",
                (character_id, world_id),
            ).fetchone():
                raise HTTPException(404, "人物不属于当前世界")
            return LodgingPlanService.rows(c, world_id, player["id"], at, character_id)

    @router.get("/lodging-plans/options/{character_id}")
    def lodging_plan_options(world_id: str, character_id: str):
        with database.read() as c:
            player, at = require_player_and_time(c, world_id)
            npc = c.execute(
                "SELECT * FROM characters WHERE id=? AND world_id=? AND is_player=0",
                (character_id, world_id),
            ).fetchone()
            if not npc:
                raise HTTPException(404, "人物不属于当前世界")
            if not c.execute(
                "SELECT 1 FROM character_acquaintances WHERE world_id=? AND observer_id=? AND subject_id=?",
                (world_id, player["id"], character_id),
            ).fetchone():
                try:
                    IntimacyService._reachable(c, player, npc)
                except ValueError as exc:
                    raise HTTPException(404, "目前不认识或无法接触这位人物") from exc
            return {"room_options": LodgingPlanService.room_options(c, world_id, player["id"], character_id, at)}

    @router.get("/journeys/options")
    def journey_options(world_id:str):
        with database.read() as c:
            player,_=require_player_and_time(c,world_id)
            return JourneyService.places(c,world_id,player["id"])

    @router.get("/appointments")
    def appointments(world_id:str,character_id:str|None=None):
        with database.read() as c:
            player,at=require_player_and_time(c,world_id)
            if character_id and not c.execute("SELECT 1 FROM characters WHERE id=? AND world_id=? AND is_player=0",(character_id,world_id)).fetchone():
                raise HTTPException(404,"人物不属于当前世界")
            return AppointmentService.rows(c,world_id,player["id"],at,character_id)

    @router.get("/shared-plans")
    def shared_plans(world_id:str,character_id:str|None=None):
        with database.read() as c:
            player,at=require_player_and_time(c,world_id)
            if character_id and not c.execute("SELECT 1 FROM characters WHERE id=? AND world_id=? AND is_player=0",(character_id,world_id)).fetchone():
                raise HTTPException(404,"人物不属于当前世界")
            return SharedPlanService.rows(c,world_id,player["id"],at,character_id)

    @router.get("/appointments/series")
    def appointment_series(world_id:str,character_id:str|None=None):
        from world_engine.appointment_series import AppointmentSeriesService
        with database.read() as c:
            player,_=require_player_and_time(c,world_id)
            if character_id and not c.execute("SELECT 1 FROM characters WHERE id=? AND world_id=? AND is_player=0",(character_id,world_id)).fetchone():
                raise HTTPException(404,"人物不属于当前世界")
            return AppointmentSeriesService.rows(c,world_id,player["id"],character_id)

    @router.get("/appointments/options/{character_id}")
    def appointment_options(world_id:str,character_id:str):
        with database.read() as c:
            player,at=require_player_and_time(c,world_id)
            if not c.execute("SELECT 1 FROM characters WHERE id=? AND world_id=? AND is_player=0",(character_id,world_id)).fetchone():
                raise HTTPException(404,"人物不属于当前世界")
            result=AppointmentService.context(c,world_id,player["id"],character_id,at)
            result["journey_options"]=[{"id":j["id"],"title":j["title"],"location_id":j["stops"][j["next_index"]-1]["location_id"],"stop_index":j["next_index"]-1} for j in JourneyService.rows(c,world_id,player["id"],at,character_id) if j["status"] in {"active","paused"} and j["phase"]=="stopped" and j["next_index"]>0]
            return result

    @router.get("/families")
    def families(world_id:str):
        with database.read() as c:
            player,at=require_player_and_time(c,world_id)
            return FamilyService.rows(c,world_id,player["id"],at)

    @router.post("/player/life-details")
    def player_life_details(world_id:str,payload:PlayerLifeDetails):
        from uuid import uuid4

        from world_engine.actions import ActionService
        from world_engine.repository import from_iso

        with database.write() as c:
            player,at=require_player_and_time(c,world_id)
            if player["birth_world_time"]:
                if age_years_at(from_iso(player["birth_world_time"]),at)!=payload.age_years:
                    raise HTTPException(409,"已确定的人物出生时间不能通过这个入口改写")
                return {"status":"already","age_years":payload.age_years}
            try:birth=to_iso(at-timedelta(days=365*payload.age_years))
            except OverflowError as exc:raise HTTPException(422,"当前世界历法无法表示这个出生年份") from exc
            c.execute("UPDATE characters SET birth_world_time=? WHERE id=?",(birth,player["id"]))
            event=ActionService._record_event(c,world_id=world_id,tick_id=str(uuid4()),occurred_at=at,event_type="social.player_age_declared",
                actor_id=player["id"],target_id=None,location_id=player["current_location_id"] or player["location_id"],summary=f"你确认了自己的世界人物年龄：{payload.age_years}岁。",payload={"age_years":payload.age_years,"birth_world_time":birth})
            WorldRepository().bump_version(c,world_id)
            return {"status":"applied","age_years":payload.age_years,"event_id":event}

    @router.get("/families/options/{character_id}")
    def family_options(world_id:str,character_id:str):
        with database.read() as c:
            player,at=require_player_and_time(c,world_id)
            if not c.execute("SELECT 1 FROM characters WHERE id=? AND world_id=?",(character_id,world_id)).fetchone():raise HTTPException(404,"人物不属于当前世界")
            return FamilyService.context(c,world_id,player["id"],character_id,at)

    @router.post("/families")
    def choose_family(world_id:str,payload:PlayerFamilyChoice):
        with database.write() as c:
            player,at=require_player_and_time(c,world_id)
            npc=c.execute("SELECT * FROM characters WHERE id=? AND world_id=? AND is_player=0",(payload.recipient_id,world_id)).fetchone()
            try:result=FamilyService.apply(c,player,npc,at,payload.choice)
            except ValueError as exc:raise HTTPException(409,str(exc)) from exc
            WorldRepository().bump_version(c,world_id)
            return result

    return router
