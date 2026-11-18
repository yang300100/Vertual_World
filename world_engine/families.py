"""哥哥，婚姻、住所和家庭各自取得同意，财物与亲子关系都有真实来源。"""

import json
from datetime import timedelta
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from world_engine.actions import ActionService
from world_engine.intimacy import IntimacyService
from world_engine.repository import from_iso, to_iso

FAMILY_SCHEMA = """
CREATE TABLE IF NOT EXISTS family_agreements (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 proposer_id TEXT NOT NULL REFERENCES characters(id), recipient_id TEXT NOT NULL REFERENCES characters(id),
 kind TEXT NOT NULL CHECK(kind IN ('marriage','cohabit','family','parenthood','allowance')),
 status TEXT NOT NULL CHECK(status IN ('pending','active','declined','ended','suspended','completed','expired')),
 title TEXT NOT NULL, statement TEXT NOT NULL, response TEXT, room_id TEXT REFERENCES life_rooms(id),
 family_id TEXT REFERENCES family_agreements(id), amount INTEGER NOT NULL DEFAULT 0 CHECK(amount>=0),
 exclusive INTEGER NOT NULL DEFAULT 1 CHECK(exclusive IN (0,1)),
 child_name TEXT, child_species TEXT, registration_id TEXT REFERENCES element_registration_requests(id),
 proposed_at TEXT NOT NULL, expires_at TEXT NOT NULL, resolved_at TEXT,
 source_event_id TEXT NOT NULL REFERENCES world_events(id), rules_json TEXT NOT NULL DEFAULT '{}',
 CHECK(proposer_id<>recipient_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_family_open_pair ON family_agreements(
 world_id,kind,MIN(proposer_id,recipient_id),MAX(proposer_id,recipient_id)) WHERE status IN ('pending','active');
CREATE INDEX IF NOT EXISTS idx_family_world_status ON family_agreements(world_id,status,kind);
CREATE TABLE IF NOT EXISTS home_residency_permissions (
 room_id TEXT NOT NULL REFERENCES life_rooms(id) ON DELETE CASCADE,
 resident_id TEXT NOT NULL REFERENCES characters(id), owner_id TEXT NOT NULL REFERENCES characters(id),
 source_event_id TEXT NOT NULL REFERENCES world_events(id), PRIMARY KEY(room_id,resident_id)
);
CREATE TABLE IF NOT EXISTS household_accounts (
 family_id TEXT NOT NULL REFERENCES family_agreements(id), contributor_id TEXT NOT NULL REFERENCES characters(id),
 balance INTEGER NOT NULL DEFAULT 0 CHECK(balance>=0), PRIMARY KEY(family_id,contributor_id)
);
CREATE TABLE IF NOT EXISTS household_ledger (
 id TEXT PRIMARY KEY, family_id TEXT NOT NULL REFERENCES family_agreements(id),
 character_id TEXT NOT NULL REFERENCES characters(id), operation TEXT NOT NULL,
 amount INTEGER NOT NULL CHECK(amount>0), source_event_id TEXT NOT NULL REFERENCES world_events(id),world_time TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS family_dependents (
 family_id TEXT NOT NULL REFERENCES family_agreements(id), child_id TEXT NOT NULL REFERENCES characters(id),
 parenthood_id TEXT NOT NULL REFERENCES family_agreements(id), source_event_id TEXT NOT NULL REFERENCES world_events(id),
 PRIMARY KEY(family_id,child_id)
);
"""

FAMILY_LABELS = {"marriage":"婚姻", "cohabit":"共同居住", "family":"共同家庭", "parenthood":"共同养育计划", "allowance":"家庭资金拨付"}


class FamilyIntent(BaseModel):
    model_config = ConfigDict(extra="forbid",str_strip_whitespace=True)
    operation: Literal["propose","accept","decline","end","contribute","withdraw","register_birth","feed_child","authorize_residence","revoke_residence"]
    kind: Literal["marriage","cohabit","family","parenthood","allowance"] | None = None
    agreement_id: str | None = Field(default=None,min_length=1,max_length=100)
    room_id: str | None = Field(default=None,min_length=1,max_length=100)
    title: str = Field(default="",max_length=80)
    statement: str = Field(min_length=1,max_length=240)
    amount: int = Field(default=0,ge=0,le=1_000_000,strict=True)
    exclusive: bool = True
    child_name: str | None = Field(default=None,min_length=1,max_length=60)
    child_species: str | None = Field(default=None,min_length=1,max_length=40)
    child_id: str | None = Field(default=None,min_length=1,max_length=100)

    @model_validator(mode="before")
    @classmethod
    def unused_optional_fields(cls,value):
        # 哥哥，模型常把无关的可选字段写成 null；需要的金额仍由下面的规则严格核验。
        if isinstance(value,dict):
            value=dict(value)
            for key in ("title","amount","exclusive"):
                if value.get(key,"missing") is None:value.pop(key)
        return value

    @model_validator(mode="after")
    def complete_choice(self):
        if self.operation=="propose" and not self.kind:
            raise ValueError("家庭提议需要明确约定类型")
        if self.operation in {"accept","decline","end","contribute","withdraw","register_birth"} and not self.agreement_id:
            raise ValueError("需要引用双方真实的家庭约定")
        if self.operation in {"contribute","withdraw"} and self.amount<=0:
            raise ValueError("家庭资金操作需要明确正整数金额")
        if self.operation=="feed_child" and not self.child_id:
            raise ValueError("需要明确本人的孩子")
        if self.operation in {"authorize_residence","revoke_residence"} and not self.room_id:
            raise ValueError("住房许可需要明确真实房间")
        return self


class FamilyService:
    @staticmethod
    def rows(c,wid,cid,at,other=None):
        args=[wid,cid,cid]
        extra=""
        if other:
            extra=" AND (proposer_id=? OR recipient_id=?)";args.extend((other,other))
        rows=[]
        for row in c.execute("SELECT * FROM family_agreements WHERE world_id=? AND (proposer_id=? OR recipient_id=?)"+extra+
                             " ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,proposed_at DESC LIMIT 32",args):
            value=dict(row);value.pop("rules_json",None)
            value["other_id"]=row["recipient_id"] if row["proposer_id"]==cid else row["proposer_id"]
            value["label"]=FAMILY_LABELS[row["kind"]]
            if row["status"]=="pending" and from_iso(row["expires_at"])<=at:value["status"]="expired"
            if row["kind"]=="family":
                value["accounts"]=[dict(a) for a in c.execute("SELECT contributor_id,balance FROM household_accounts WHERE family_id=?",(row["id"],))]
                value["balance"]=sum(a["balance"] for a in value["accounts"])
            if row["room_id"]:
                room=c.execute("SELECT name,location_id FROM life_rooms WHERE id=?",(row["room_id"],)).fetchone()
                if room:value["room_name"],value["location_id"]=room
            if row["registration_id"]:
                plan=c.execute("SELECT status,due_at_world,completed_registration_id FROM family_plans WHERE registration_id=?",(row["registration_id"],)).fetchone()
                if plan:value["child_plan"]=dict(plan)
            rows.append(value)
        return rows

    @staticmethod
    def pair(c,wid,first,second,kind):
        return c.execute("SELECT * FROM family_agreements WHERE world_id=? AND kind=? AND status='active' AND "
                         "((proposer_id=? AND recipient_id=?) OR (proposer_id=? AND recipient_id=?))",
                         (wid,kind,first,second,second,first)).fetchone()

    @staticmethod
    def _personal_residency(c,room,cid,at):
        return room["owner_character_id"]==cid or bool(c.execute(
            "SELECT 1 FROM home_residency_permissions WHERE room_id=? AND resident_id=? AND owner_id=?",
            (room["id"],cid,room["owner_character_id"])).fetchone()) or bool(c.execute(
            "SELECT 1 FROM contract_fulfillments WHERE asset_id=? AND requester_id=? AND kind='lodging' AND status='active' AND due_world_time>?",
            (room["id"],cid,to_iso(at))).fetchone())

    @classmethod
    def room_valid(cls,c,room,first,second,at):
        occupants={first,second}
        lease=c.execute("SELECT requester_id FROM contract_fulfillments WHERE asset_id=? AND kind='lodging' AND status='active' AND due_world_time>?",(room["id"],to_iso(at))).fetchone()
        if lease and lease[0] not in occupants:return False
        if room["owner_character_id"] in occupants:return True
        return all(cls._personal_residency(c,room,cid,at) for cid in occupants)

    @classmethod
    def has_residency(cls,c,room,cid,*,at=None):
        if at is None:
            at=from_iso(c.execute("SELECT w.current_time FROM worlds w WHERE w.id=?",(room["world_id"],)).fetchone()[0])
        if cls._personal_residency(c,room,cid,at):return True
        for home in c.execute("SELECT proposer_id,recipient_id FROM family_agreements WHERE world_id=? AND room_id=? AND kind='cohabit' AND status='active' AND (proposer_id=? OR recipient_id=?)",(room["world_id"],room["id"],cid,cid)):
            if cls.room_valid(c,room,*home,at):return True
        return False

    @classmethod
    def home(cls,c,wid,cid,at):
        for row in c.execute("SELECT a.* FROM family_agreements a WHERE a.world_id=? AND a.kind='cohabit' AND a.status='active' AND (a.proposer_id=? OR a.recipient_id=?) ORDER BY a.resolved_at DESC",(wid,cid,cid)):
            room=c.execute("SELECT r.* FROM life_rooms r JOIN locations l ON l.id=r.location_id WHERE r.id=? AND l.is_active=1",(row["room_id"],)).fetchone()
            if room and cls.room_valid(c,room,row["proposer_id"],row["recipient_id"],at):return room
        return None

    @classmethod
    def room_options(cls,c,wid,cid,other,at):
        rooms=[]
        for room in c.execute("SELECT r.* FROM life_rooms r JOIN locations l ON l.id=r.location_id WHERE r.world_id=? AND l.is_active=1",(wid,)):
            if cls._personal_residency(c,room,cid,at) and cls.room_valid(c,room,cid,other,at):
                rooms.append({"id":room["id"],"name":room["name"],"location_id":room["location_id"],"owned":room["owner_character_id"]==cid})
        return rooms[:12]

    @classmethod
    def context(cls,c,wid,cid,other,at):
        agreements=cls.rows(c,wid,cid,at,other)
        # 哥哥，保留当前全部承诺及最近一次结束记录，旧账不会无限挤占对话。
        current=[a for a in agreements if a["status"] in {"active","pending"}]
        previous=[a for a in agreements if a["status"] not in {"active","pending"}]
        agreements=current+previous[:1]
        keep=("id","kind","status","label","proposer_id","recipient_id","statement","response","room_id","room_name","title","family_id","balance","accounts","amount","exclusive","child_name","child_species","child_plan")
        children=[dict(row) for row in c.execute("SELECT d.child_id,d.family_id,d.parenthood_id FROM family_dependents d JOIN character_lineages p ON p.child_character_id=d.child_id WHERE p.world_id=? AND p.parent_character_id=?",(wid,cid))]
        species=[dict(row) for row in c.execute("SELECT id,display_name FROM species_profiles WHERE id IN (SELECT species FROM characters WHERE id IN (?,?) AND world_id=?)",(cid,other,wid))]
        return {"agreements":[{k:a[k] for k in keep if k in a} for a in agreements[:10]],"home_options":cls.room_options(c,wid,cid,other,at),"children":children,"species_options":species,
                "rules":"婚姻、同居、家庭分别确认。资金贡献只扣本人财物，拨付需双方同意。计划获准不等于孩子已经出生。"}

    @staticmethod
    def record(c,actor,other,at,operation,summary,payload):
        event=ActionService._record_event(c,world_id=actor["world_id"],tick_id=str(uuid4()),occurred_at=at,event_type="social.family_"+operation,
                                         actor_id=actor["id"],target_id=other["id"],location_id=actor["current_location_id"] or actor["location_id"],summary=summary,payload=payload)
        for cid in (actor["id"],other["id"]):
            ActionService._record_memory(c,world_id=actor["world_id"],character_id=cid,event_id=event,memory_type="experienced",summary=summary,importance=8)
        IntimacyService.remember_event(c,actor["world_id"],actor["id"],other["id"],event,at,"milestone",summary)
        return event

    @classmethod
    def _check(cls,c,actor,other,at,kind,room_id,exclusive):
        from world_engine.interiors import InteriorService
        for person in (actor,other):
            if person["health"]<=0:raise ValueError("双方需要能够表达自己的选择")
            IntimacyService._adult(c,person,at)
        if kind=="marriage":
            if not c.execute("SELECT 1 FROM intimate_bonds WHERE world_id=? AND first_id=? AND second_id=? AND kind='romantic' AND status='active'",(actor["world_id"],*sorted((actor["id"],other["id"])))).fetchone():
                raise ValueError("请先由双方明确确认恋人关系，再讨论婚姻")
            for prior in c.execute("SELECT * FROM family_agreements WHERE world_id=? AND kind='marriage' AND status='active' AND (proposer_id IN (?,?) OR recipient_id IN (?,?))",(actor["world_id"],actor["id"],other["id"],actor["id"],other["id"])):
                if exclusive or prior["exclusive"]:raise ValueError("现有婚姻的排他约定尚未结束，不能另行确认冲突的婚姻")
        if kind=="cohabit":
            if not room_id:raise ValueError("同居需要明确已有住所")
            room=InteriorService.room(c,actor["world_id"],room_id)
            if not cls.room_valid(c,room,actor["id"],other["id"],at):raise ValueError("住所缺少所有者的共同居住许可，或正由第三方租用")
        if kind in {"family","parenthood","allowance"}:
            home=cls.pair(c,actor["world_id"],actor["id"],other["id"],"cohabit")
            if not home:raise ValueError("请先确认有真实住所的共同居住安排")
            room=InteriorService.room(c,actor["world_id"],home["room_id"])
            if not cls.room_valid(c,room,actor["id"],other["id"],at):raise ValueError("共同住所已经失效，请先重新安排住房")
        if kind in {"parenthood","allowance"} and not cls.pair(c,actor["world_id"],actor["id"],other["id"],"family"):
            raise ValueError("请先由双方确认共同家庭及责任，再作这项安排")

    @classmethod
    def apply(cls,c,actor,other,at,choice):
        if other is None or actor["world_id"]!=other["world_id"] or actor["id"]==other["id"]:raise ValueError("请选择同一世界的另一位人物")
        if actor["health"]<=0:raise ValueError("当前身体状态无法表达新的家庭选择")
        wid=actor["world_id"];op=choice.operation
        if op in {"authorize_residence","revoke_residence"}:
            from world_engine.interiors import InteriorService
            room=InteriorService.room(c,wid,choice.room_id)
            if room["owner_character_id"]!=actor["id"]:raise ValueError("只有住所所有者能表达这份居住许可")
            event=cls.record(c,actor,other,at,op,choice.statement,{"room_id":room["id"]})
            if op=="authorize_residence":c.execute("INSERT OR REPLACE INTO home_residency_permissions VALUES (?,?,?,?)",(room["id"],other["id"],actor["id"],event))
            else:c.execute("DELETE FROM home_residency_permissions WHERE room_id=? AND resident_id=? AND owner_id=?",(room["id"],other["id"],actor["id"]))
            return {"status":"applied","event_id":event,"summary":"居住许可已记录；人物仍需实际行走和进门。"}
        if op=="feed_child":return cls.feed_child(c,actor,other,at,choice.child_id)
        if op not in {"end","contribute","withdraw","register_birth"}:IntimacyService._reachable(c,actor,other)
        if op=="propose":
            if IntimacyService.paused(c,wid,actor["id"],other["id"]):raise ValueError("一方要求保留空间，暂不提出新的家庭承诺")
            cls._check(c,actor,other,at,choice.kind,choice.room_id,choice.exclusive)
            c.execute("UPDATE family_agreements SET status='expired' WHERE world_id=? AND status='pending' AND expires_at<=?",(wid,to_iso(at)))
            if c.execute("SELECT 1 FROM family_agreements WHERE world_id=? AND kind=? AND status IN ('pending','active') AND ((proposer_id=? AND recipient_id=?) OR (proposer_id=? AND recipient_id=?))",(wid,choice.kind,actor["id"],other["id"],other["id"],actor["id"])).fetchone():raise ValueError("双方已有同类有效约定，请回应现有约定")
            if choice.kind=="parenthood" and (not choice.child_name or not choice.child_species):raise ValueError("养育计划需要双方核对孩子姓名和已登记的种族")
            if choice.kind=="allowance" and choice.amount<=0:raise ValueError("资金拨付需要明确金额和用途")
            aid=str(uuid4());title=choice.title or FAMILY_LABELS[choice.kind]
            event=cls.record(c,actor,other,at,"proposed",f"一方提出{title}，等待另一方决定：{choice.statement}",{"agreement_id":aid,"kind":choice.kind})
            family=cls.pair(c,wid,actor["id"],other["id"],"family")
            rules={"version":1,"location_id":actor["current_location_id"] or actor["location_id"],"exclusive":choice.exclusive,"property_transfer":False}
            c.execute("INSERT INTO family_agreements(id,world_id,proposer_id,recipient_id,kind,status,title,statement,room_id,family_id,amount,exclusive,child_name,child_species,proposed_at,expires_at,source_event_id,rules_json) VALUES (?,?,?,?,?,'pending',?,?,?,?,?,?,?,?,?,?,?,?)",
                      (aid,wid,actor["id"],other["id"],choice.kind,title,choice.statement,choice.room_id,family["id"] if family else None,choice.amount,int(choice.exclusive),choice.child_name,choice.child_species,to_iso(at),to_iso(at+timedelta(days=7)),event,json.dumps(rules)))
            return {"status":"pending","agreement_id":aid,"event_id":event,"summary":"提议已保存，双方尚未确认。"}
        agreement=c.execute("SELECT * FROM family_agreements WHERE id=? AND world_id=? AND ((proposer_id=? AND recipient_id=?) OR (proposer_id=? AND recipient_id=?))",(choice.agreement_id,wid,actor["id"],other["id"],other["id"],actor["id"])).fetchone()
        if not agreement:raise ValueError("这份家庭约定不属于双方")
        if op in {"contribute","withdraw"}:return cls.money(c,actor,other,agreement,at,op,choice.amount)
        if op=="register_birth":return cls.register_birth(c,actor,other,agreement,at)
        if op in {"accept","decline"}:
            if agreement["recipient_id"]!=actor["id"] or agreement["status"]!="pending" or from_iso(agreement["expires_at"])<=at:raise ValueError("只有收到有效提议的另一方能回应")
            state="active" if op=="accept" else "declined"
            if state=="active":
                if IntimacyService.paused(c,wid,actor["id"],other["id"]):raise ValueError("一方仍要求留出空间，暂不确认家庭承诺")
                cls._check(c,actor,other,at,agreement["kind"],agreement["room_id"],bool(agreement["exclusive"]))
                if agreement["kind"] in {"parenthood","allowance"}:
                    current=cls.pair(c,wid,actor["id"],other["id"],"family")
                    if current["id"]!=agreement["family_id"]:raise ValueError("家庭安排已经变化，请重新核对这份承诺")
        else:
            if agreement["status"] not in {"active","suspended","pending"}:raise ValueError("这份家庭约定已经结束")
            if agreement["status"]=="pending" and actor["id"]!=agreement["proposer_id"]:raise ValueError("请拒绝对方的提议，而非代其撤回")
            state="ended"
        summary=f"{FAMILY_LABELS[agreement['kind']]}{'经双方确认' if state=='active' else '已结束' if state=='ended' else '被拒绝'}：{choice.statement}"
        event=cls.record(c,actor,other,at,"accepted" if state=="active" else state,summary,{"agreement_id":agreement["id"],"kind":agreement["kind"]})
        c.execute("UPDATE family_agreements SET status=?,response=?,resolved_at=?,source_event_id=? WHERE id=?",(state,choice.statement,to_iso(at),event,agreement["id"]))
        if state=="active" and agreement["kind"]=="family":
            for cid in (actor["id"],other["id"]):c.execute("INSERT OR IGNORE INTO household_accounts VALUES (?,?,0)",(agreement["id"],cid))
        if state=="active" and agreement["kind"]=="parenthood":
            registration=cls._register_plan(c,actor,other,agreement,event)
            c.execute("UPDATE family_agreements SET registration_id=? WHERE id=?",(registration,agreement["id"]))
            summary="共同养育计划已获准，孩子尚未出生；需要实际世界时间经过后再登记出生。"
        if state=="active" and agreement["kind"]=="allowance":
            cls.disburse(c,agreement,event,at)
            c.execute("UPDATE family_agreements SET status='completed' WHERE id=?",(agreement["id"],))
            state="completed";summary="双方同意的家庭资金已拨付给提出申请的人，用途和来源已记账。"
        if state=="ended" and agreement["kind"]=="family":cls.refund(c,agreement,event,at)
        if state=="ended" and agreement["kind"]=="parenthood" and agreement["registration_id"]:
            c.execute("UPDATE family_plans SET status='cancelled' WHERE registration_id=? AND status IN ('planned','due')",(agreement["registration_id"],))
        return {"status":state,"agreement_id":agreement["id"],"event_id":event,"summary":summary}

    @classmethod
    def money(cls,c,actor,other,family,at,op,amount):
        if family["kind"]!="family" or family["status"]!="active":raise ValueError("家庭资金需要有效的共同家庭约定")
        own=c.execute("SELECT balance FROM household_accounts WHERE family_id=? AND contributor_id=?",(family["id"],actor["id"])).fetchone()
        available=own[0] if own else 0
        current_money=c.execute("SELECT money FROM characters WHERE id=?",(actor["id"],)).fetchone()[0]
        if op=="contribute" and current_money<amount:raise ValueError("本人没有足够的真实资金")
        if op=="withdraw" and available<amount:raise ValueError("只能取回本人尚未使用的贡献，不能拿走另一方的资金")
        delta=amount if op=="contribute" else -amount
        c.execute("UPDATE characters SET money=money-? WHERE id=?",(delta,actor["id"]))
        c.execute("INSERT OR IGNORE INTO household_accounts VALUES (?,?,0)",(family["id"],actor["id"]))
        c.execute("UPDATE household_accounts SET balance=balance+? WHERE family_id=? AND contributor_id=?",(delta,family["id"],actor["id"]))
        event=cls.record(c,actor,other,at,op,f"一方{'投入' if op=='contribute' else '取回'}{amount}枚家庭资金。",{"agreement_id":family["id"],"amount":amount})
        c.execute("INSERT INTO household_ledger VALUES (?,?,?,?,?,?,?)",(str(uuid4()),family["id"],actor["id"],op,amount,event,to_iso(at)))
        return {"status":"applied","event_id":event,"summary":"家庭资金已按本人贡献记账。"}

    @staticmethod
    def disburse(c,agreement,event,at):
        accounts=c.execute("SELECT * FROM household_accounts WHERE family_id=? ORDER BY contributor_id",(agreement["family_id"],)).fetchall()
        if sum(row["balance"] for row in accounts)<agreement["amount"]:raise ValueError("家庭现有资金不足，不能承诺凭空拨付")
        remaining=agreement["amount"]
        for account in accounts:
            used=min(remaining,account["balance"])
            if used:
                c.execute("UPDATE household_accounts SET balance=balance-? WHERE family_id=? AND contributor_id=?",(used,agreement["family_id"],account["contributor_id"]))
                c.execute("INSERT INTO household_ledger VALUES (?,?,?,?,?,?,?)",(str(uuid4()),agreement["family_id"],account["contributor_id"],"disburse",used,event,to_iso(at)))
                remaining-=used
        c.execute("UPDATE characters SET money=money+? WHERE id=?",(agreement["amount"],agreement["proposer_id"]))

    @staticmethod
    def refund(c,family,event,at):
        for row in c.execute("SELECT * FROM household_accounts WHERE family_id=? AND balance>0",(family["id"],)).fetchall():
            c.execute("UPDATE characters SET money=money+? WHERE id=?",(row["balance"],row["contributor_id"]))
            c.execute("INSERT INTO household_ledger VALUES (?,?,?,?,?,?,?)",(str(uuid4()),family["id"],row["contributor_id"],"refund",row["balance"],event,to_iso(at)))
        c.execute("UPDATE household_accounts SET balance=0 WHERE family_id=?",(family["id"],))

    @staticmethod
    def _register_plan(c,actor,other,agreement,event):
        from world_engine.registration import WorldElementRegistry,ElementRegistrationSubmit,CharacterBirthSpec
        result=WorldElementRegistry().submit(c,world_id=actor["world_id"],request=ElementRegistrationSubmit(requested_by_character_id=actor["id"],source_event_id=event,
            idempotency_key="family-plan:"+agreement["id"],payload=CharacterBirthSpec(name=agreement["child_name"],parent_character_ids=[actor["id"],other["id"]],species=agreement["child_species"],birth_state="planned")))
        if result.status.value not in {"approved","applied"}:raise ValueError("家庭注册未获准："+(result.rejection_reason or result.status.value))
        return result.id

    @classmethod
    def register_birth(cls,c,actor,other,agreement,at):
        from world_engine.registration import WorldElementRegistry,ElementRegistrationSubmit,CharacterBirthSpec
        if agreement["kind"]!="parenthood" or agreement["status"]!="active" or not agreement["registration_id"]:raise ValueError("需要双方已确认且未取消的养育计划")
        cls._check(c,actor,other,at,"parenthood",None,False)
        family=cls.pair(c,actor["world_id"],actor["id"],other["id"],"family")
        if family["id"]!=agreement["family_id"]:raise ValueError("共同家庭已经变化，需要重新核对养育计划")
        home=cls.home(c,actor["world_id"],actor["id"],at)
        if not home or any(person["current_room_id"]!=home["id"] for person in (actor,other)):raise ValueError("登记出生时双方需要实际回到共同住所")
        plan=c.execute("SELECT * FROM family_plans WHERE registration_id=?",(agreement["registration_id"],)).fetchone()
        if not plan or from_iso(plan["due_at_world"])>at:raise ValueError("家庭计划尚未到达可登记出生的世界时间")
        event=cls.record(c,actor,other,at,"birth_requested","双方按已确认的养育计划申请登记出生。",{"agreement_id":agreement["id"],"room_id":home["id"]})
        result=WorldElementRegistry().submit(c,world_id=actor["world_id"],request=ElementRegistrationSubmit(requested_by_character_id=actor["id"],source_event_id=event,idempotency_key="family-birth:"+agreement["id"],
            payload=CharacterBirthSpec(name=agreement["child_name"],species=agreement["child_species"],parent_character_ids=[actor["id"],other["id"]],birth_state="born",parent_registration_id=agreement["registration_id"],location_id=home["location_id"])))
        if result.status.value!="applied":raise ValueError("出生尚未正式登记："+(result.rejection_reason or result.status.value))
        child=result.result_entity_id
        c.execute("UPDATE characters SET longitude=?,latitude=?,current_location_id=?,current_room_id=?,space_layer=?,altitude_m=? WHERE id=?",(actor["longitude"],actor["latitude"],home["location_id"],home["id"],actor["space_layer"],actor["altitude_m"],child))
        c.execute("INSERT INTO family_dependents VALUES (?,?,?,?)",(agreement["family_id"],child,agreement["id"],event))
        birth_event=ActionService._record_event(c,world_id=actor["world_id"],tick_id=str(uuid4()),occurred_at=at,event_type="social.family_child_born",
            actor_id=actor["id"],target_id=child,location_id=home["location_id"],summary=f"家庭迎来了已正式登记的孩子{agreement['child_name']}。",payload={"agreement_id":agreement["id"],"child_id":child,"room_id":home["id"]})
        for parent in (actor,other):
            c.execute("INSERT OR IGNORE INTO character_event_knowledge(character_id,event_id,world_id,source_kind,confidence,hops,learned_world_time) VALUES (?,?,?,'experienced',1,0,?)",(parent["id"],birth_event,actor["world_id"],to_iso(at)))
            c.execute("INSERT INTO character_acquaintances(observer_id,subject_id,world_id,known_name,encounters,first_seen,last_seen,source_event_id) VALUES (?,?,?,?,1,?,?,?) ON CONFLICT(observer_id,subject_id) DO UPDATE SET known_name=excluded.known_name,source_event_id=excluded.source_event_id",
                      (parent["id"],child,actor["world_id"],agreement["child_name"],to_iso(at),to_iso(at),birth_event))
        if home["owner_character_id"] in {actor["id"],other["id"]}:
            c.execute("INSERT OR REPLACE INTO home_residency_permissions VALUES (?,?,?,?)",(home["id"],child,home["owner_character_id"],birth_event))
        c.execute("UPDATE family_agreements SET status='completed',source_event_id=? WHERE id=?",(event,agreement["id"]))
        return {"status":"completed","event_id":event,"child_id":child,"summary":"孩子已通过正式出生登记加入家庭，亲子关系和实际出生位置已记录。"}

    @staticmethod
    def feed_child(c,actor,other,at,child_id):
        from world_engine.shared_life import SharedLifeService
        from world_engine.inventory import InventoryService
        from world_engine.food import FoodService
        from world_engine.life import LifeActivityService
        child=c.execute("SELECT * FROM characters WHERE id=? AND world_id=?",(child_id,actor["world_id"])).fetchone()
        if child is None or not c.execute("SELECT 1 FROM character_lineages WHERE parent_character_id=? AND child_character_id=?",(actor["id"],child_id)).fetchone():raise ValueError("只能照料自己已有正式亲子或监护关系的孩子")
        if not SharedLifeService._near(actor,child,"hug") or actor["health"]<=0 or child["health"]<=0:raise ValueError("需要实际走到孩子身边才能照料")
        LifeActivityService.assert_available(c,actor["id"],talking=True)
        item=SharedLifeService._food(c,actor,at)
        item=dict(item);item.update(c.execute("SELECT stack_limit,slot_size FROM item_types WHERE id=?",(item["item_type_id"],)).fetchone())
        before=InventoryService.snapshot(c,actor["world_id"])
        if c.execute("SELECT 1 FROM character_movements WHERE character_id IN (?,?) AND status='moving'",(actor["id"],child_id)).fetchone():raise ValueError("请先停下行程，再照料孩子")
        InventoryService.transfer(c,item,child_id,1)
        # 转移返回值由库存服务管理，再按实际归属读取孩子拥有的食物。
        food=SharedLifeService._food(c,child,at)
        FoodService.eat(c,child,food,at)
        event=ActionService._record_event(c,world_id=actor["world_id"],tick_id=str(uuid4()),occurred_at=at,event_type="social.family_child_fed",actor_id=actor["id"],target_id=child_id,location_id=actor["current_location_id"] or actor["location_id"],summary="家长用自己真实拥有的一份食物照料了孩子。",payload={"item_id":item["id"],"child_id":child_id})
        InventoryService.audit(c,actor["world_id"],event,before)
        return {"status":"applied","event_id":event,"summary":"已消耗一份真实食物，孩子的饱食变化由食物规则结算。"}

    @classmethod
    def maintain(cls,c,wid,at):
        for home in c.execute("SELECT * FROM family_agreements WHERE world_id=? AND kind='cohabit' AND status='active'",(wid,)).fetchall():
            room=c.execute("SELECT r.* FROM life_rooms r JOIN locations l ON l.id=r.location_id WHERE r.id=? AND l.is_active=1",(home["room_id"],)).fetchone()
            if not room or not cls.room_valid(c,room,home["proposer_id"],home["recipient_id"],at):
                first=c.execute("SELECT * FROM characters WHERE id=?",(home["proposer_id"],)).fetchone()
                second=c.execute("SELECT * FROM characters WHERE id=?",(home["recipient_id"],)).fetchone()
                event=cls.record(c,first,second,at,"housing_suspended","共同住所的有效许可已经变化，同居安排暂停；人物保留当前位置和自己的财物。",{"agreement_id":home["id"]})
                c.execute("UPDATE family_agreements SET status='suspended',source_event_id=? WHERE id=?",(event,home["id"]))

    @staticmethod
    def is_dependent(c,cid,at):
        from world_engine.demographics import age_years_at
        row=c.execute("SELECT p.birth_world_time,s.adult_age_world_years FROM family_dependents d JOIN characters p ON p.id=d.child_id JOIN species_profiles s ON s.id=p.species WHERE p.id=? LIMIT 1",(cid,)).fetchone()
        if not row:return False
        age=age_years_at(from_iso(row["birth_world_time"]) if row["birth_world_time"] else None,at)
        return age is None or age<row["adult_age_world_years"]

    @classmethod
    def care_dependents(cls,c,wid,at):
        for child in c.execute("SELECT DISTINCT p.* FROM family_dependents d JOIN characters p ON p.id=d.child_id WHERE p.world_id=? AND p.health>0 AND p.satiety<=40",(wid,)).fetchall():
            if not cls.is_dependent(c,child["id"],at):continue
            for parent in c.execute("SELECT p.* FROM character_lineages l JOIN characters p ON p.id=l.parent_character_id WHERE l.child_character_id=? AND p.world_id=? AND p.is_player=0 AND p.health>0 AND p.satiety>30 AND p.energy>10",(child["id"],wid)).fetchall():
                # 哥哥，NPC 家长只动用自己的食物；玩家的财物始终等待本人行动。
                c.execute("SAVEPOINT family_care")
                try:
                    cls.feed_child(c,parent,parent,at,child["id"])
                except ValueError:
                    c.execute("ROLLBACK TO family_care")
                else:
                    c.execute("RELEASE family_care")
                    break
                c.execute("RELEASE family_care")

    @classmethod
    def hungry_dependent(cls,c,parent,at):
        home=cls.home(c,parent["world_id"],parent["id"],at)
        if not home:return None
        for child in c.execute("SELECT p.id,p.current_room_id FROM character_lineages l JOIN family_dependents d ON d.child_id=l.child_character_id JOIN characters p ON p.id=d.child_id WHERE l.parent_character_id=? AND p.health>0 AND p.satiety<=40 AND p.current_room_id=?",(parent["id"],home["id"])):
            if cls.is_dependent(c,child["id"],at):return child
        return None

    @classmethod
    def care_intention(cls,c,parent,at,places,plan):
        from world_engine.shared_life import SharedLifeService
        from world_engine.economy import EconomyService
        from world_engine.daily_life import DailyLifeService
        from world_engine.geo import great_circle_distance_km

        home=cls.home(c,parent["world_id"],parent["id"],at)
        if not home:return "继续核对家庭的照护安排"
        try:SharedLifeService._food(c,parent,at)
        except ValueError:has_food=False
        else:has_food=True
        if has_food:
            if parent["current_room_id"]!=home["id"]:return DailyLifeService._rest_at_home(c,parent,at,1)
            return "留在共同住所照看孩子"
        if EconomyService.buy_food(c,parent,at):return "买到了照料孩子所需的真实食物，尚未代替已经喂食"
        if EconomyService.harvest(c,parent,at,food_only=True):return "为照料孩子收取了真实食物"
        obstacle=EconomyService.supply_obstacle(c,parent,at,food_only=True)
        if obstacle.kind=="insufficient_funds":return DailyLifeService._find_work(c,parent,at,places,plan)
        # 哥哥，照料孩子时也不能凭空知道远方食物的实时库存。
        ids=DailyLifeService.known_food_places(c,parent,at,[p["id"] for p in places])
        candidates=[p for p in places if p["id"] in ids and (parent["current_room_id"] or
            great_circle_distance_km(parent["longitude"],parent["latitude"],p["longitude"],p["latitude"])>0.1)]
        if not candidates and parent["current_room_id"]:
            # 哥哥，本人在熟悉地点的房间里可以先走出去看看，不据此断言外面有食物。
            local=next((p for p in places if p["id"]==parent["current_location_id"] and
                        p["world_id"]==parent["world_id"] and p["space_layer"]==parent["space_layer"]),None)
            if local:candidates=[local]
        if not candidates and not parent["current_room_id"]:
            familiar=DailyLifeService.familiar_place_to_search(c,parent,at,{p["id"]:p for p in places})
            if familiar:candidates=[familiar]
        if candidates:
            target=min(candidates,key=lambda p:great_circle_distance_km(parent["longitude"],parent["latitude"],p["longitude"],p["latitude"]))
            if parent["current_room_id"] or great_circle_distance_km(parent["longitude"],parent["latitude"],target["longitude"],target["latitude"])>0.1:
                return DailyLifeService._travel(c,parent,at,target)
        return "照护所需食物暂时不足，等待真实补给或家人协助"
