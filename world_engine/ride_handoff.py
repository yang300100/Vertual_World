"""哥哥，换驾驶者必须另有有限许可、逐人同意和新驾驶者自己的接管。"""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from uuid import uuid4

from world_engine.contracts import ContractService
from world_engine.repository import from_iso, to_iso, utc_now

HANDOFF_OPERATIONS = {"propose_handoff", "authorize_handoff", "accept_handoff", "decline_handoff", "withdraw_handoff", "take_control", "revoke_handoff"}
HANDOFF_SCHEMA = """
CREATE TABLE IF NOT EXISTS vehicle_ride_handoffs (
 id TEXT PRIMARY KEY,ride_id TEXT NOT NULL REFERENCES vehicle_rides(id) ON DELETE CASCADE,
 world_id TEXT NOT NULL REFERENCES worlds(id),epoch INTEGER NOT NULL CHECK(epoch>0),
 proposer_id TEXT NOT NULL REFERENCES characters(id),previous_controller_id TEXT NOT NULL REFERENCES characters(id),
 controller_id TEXT NOT NULL REFERENCES characters(id),holder_id TEXT NOT NULL REFERENCES characters(id),
 movement_id TEXT NOT NULL REFERENCES character_movements(id),block_event_id TEXT NOT NULL REFERENCES world_events(id),
 status TEXT NOT NULL CHECK(status IN ('pending','accepted','declined','withdrawn','executed','expired','invalidated','revoked')),
 statement TEXT NOT NULL,proposed_at TEXT NOT NULL,deadline_at TEXT NOT NULL,terms_json TEXT NOT NULL,snapshot_json TEXT NOT NULL,
 proposal_event_id TEXT NOT NULL REFERENCES world_events(id),source_event_id TEXT NOT NULL REFERENCES world_events(id),
 authorized_by TEXT REFERENCES characters(id),granted_at TEXT,authorization_event_id TEXT REFERENCES world_events(id),
 executed_at TEXT,execution_event_id TEXT REFERENCES world_events(id),
 revoked_at TEXT,revoke_event_id TEXT REFERENCES world_events(id),reason TEXT NOT NULL DEFAULT '',UNIQUE(ride_id,epoch)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_vehicle_handoff_pending ON vehicle_ride_handoffs(ride_id) WHERE status IN ('pending','accepted');
CREATE INDEX IF NOT EXISTS idx_vehicle_handoff_history ON vehicle_ride_handoffs(ride_id,epoch);
CREATE TABLE IF NOT EXISTS vehicle_ride_handoff_members (
 handoff_id TEXT NOT NULL REFERENCES vehicle_ride_handoffs(id) ON DELETE CASCADE,
 character_id TEXT NOT NULL REFERENCES characters(id),ordinal INTEGER NOT NULL,
 decision TEXT NOT NULL DEFAULT 'pending' CHECK(decision IN ('pending','accepted','declined')),
 decision_at TEXT,decision_event_id TEXT REFERENCES world_events(id),
 PRIMARY KEY(handoff_id,character_id),UNIQUE(handoff_id,ordinal)
);
"""


class RideHandoffService:
    @staticmethod
    def install(c):
        for statement in HANDOFF_SCHEMA.split(';'):
            if statement.strip(): c.execute(statement)

    @staticmethod
    def _rides():
        from world_engine.rides import RideService
        return RideService

    @staticmethod
    def _recovery():
        from world_engine.ride_recovery import RideRecoveryService
        return RideRecoveryService

    @staticmethod
    def _json(value):
        return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))

    @staticmethod
    def _terms(row): return json.loads(row['terms_json'])

    @staticmethod
    def _members(c,row):
        return c.execute('SELECT * FROM vehicle_ride_handoff_members WHERE handoff_id=? ORDER BY ordinal',(row['id'],)).fetchall()

    @classmethod
    def authority(cls,c,ride,at):
        """哥哥，只认真实车主或未到期租客，不把乘车许可变成驾驶权。"""
        rides=cls._rides();vehicle=rides._vehicle(c,ride)
        if not vehicle['is_available']: raise ValueError('原载具当前不可用')
        state=rides.state(c,ride['world_id'],ride['vehicle_id'])
        registration=c.execute("SELECT * FROM element_registration_requests WHERE id=? AND world_id=? AND status='applied' AND result_entity_id=?",(state['registration_id'],ride['world_id'],ride['vehicle_id'])).fetchone()
        if registration is None: raise ValueError('原载具没有有效的实物登记来源')
        event=c.execute('SELECT * FROM world_events WHERE id=? AND world_id=?',(registration['source_event_id'],ride['world_id'])).fetchone()
        if event is None or from_iso(event['occurred_at'])>at: raise ValueError('载具登记来源尚未在当前时刻成立')
        lease=c.execute("SELECT * FROM contract_fulfillments WHERE asset_id=? AND status='active'",(ride['vehicle_id'],)).fetchone()
        if lease:
            if lease['world_id']!=ride['world_id'] or lease['kind']!='lease' or not from_iso(lease['started_world_time'])<=at<from_iso(lease['due_world_time']):
                raise ValueError('当前租约尚未有效或已到期，不能凭交接延租')
            holder=lease['requester_id'];kind='lease';source=lease['request_id']
            result={'holder_id':holder,'source_kind':kind,'source_id':source,'owner_id':vehicle['owner_character_id'],
                    'started_at':lease['started_world_time'],'due_at':lease['due_world_time'],'registration_id':state['registration_id']}
        else:
            holder=vehicle['owner_character_id']
            if not holder: raise ValueError('当前没有可以授予驾驶许可的真实持有人')
            result={'holder_id':holder,'source_kind':'owner','source_id':state['registration_id'],'owner_id':holder,'registration_id':state['registration_id']}
        if ContractService.vehicle_for(c,ride['world_id'],holder,ride['vehicle_id'],at=at) is None:
            raise ValueError('驾驶许可持有人当前无真实使用权')
        rides._person(c,ride['world_id'],holder)
        return result

    @classmethod
    def control_row(cls,c,ride,at,observer=None):
        """哥哥，最后的真实接管不能因撤权而偷偷退回上一位驾驶者。"""
        rows=c.execute("SELECT h.*,e.rowid AS execution_order FROM vehicle_ride_handoffs h LEFT JOIN world_events e ON e.id=h.execution_event_id "
            "WHERE h.ride_id=? AND h.executed_at IS NOT NULL AND h.executed_at<=? ORDER BY COALESCE(e.rowid,9223372036854775807) DESC,h.epoch DESC",(ride['id'],to_iso(at))).fetchall()
        for row in rows:
            if observer and not c.execute('SELECT 1 FROM character_event_knowledge k JOIN world_events e ON e.id=k.event_id AND e.world_id=k.world_id WHERE k.character_id=? AND k.event_id=? AND k.world_id=? AND k.learned_world_time<=? AND e.occurred_at<=?',(observer,row['execution_event_id'],ride['world_id'],to_iso(at),to_iso(at))).fetchone():
                continue
            return row
        return None

    @classmethod
    def driver_id(cls,c,ride,at,observer=None):
        row=cls.control_row(c,ride,at,observer)
        return row['controller_id'] if row else ride['controller_id']

    @classmethod
    def control_source(cls,c,ride,at):
        row=cls.control_row(c,ride,at)
        return {'handoff_id':row['id'],'controller_id':row['controller_id'],'execution_event_id':row['execution_event_id']} if row else None

    @classmethod
    def _snapshot(cls,c,ride,at):
        # 哥哥，原恢复快照保留 canonical 路线锚；接管者另在控制来源里冻结。
        return cls._recovery()._snapshot(c,ride,at)

    @classmethod
    def _coherent(cls,ride,row):
        try:
            terms=cls._terms(row);snapshot=json.loads(row['snapshot_json'])
            expected={'handoff_id':row['id'],'ride_id':ride['id'],'world_id':ride['world_id'],'epoch':row['epoch'],
                'proposer_id':row['proposer_id'],'previous_controller_id':row['previous_controller_id'],'controller_id':row['controller_id'],
                'holder_id':row['holder_id'],'vehicle_id':ride['vehicle_id'],'movement_id':ride['movement_id'],
                'block_event_id':row['block_event_id'],'destination_location_id':ride['destination_location_id'],'deadline_at':row['deadline_at'],
                'snapshot_sha256':hashlib.sha256(cls._json(snapshot).encode()).hexdigest()}
            return all(terms.get(k)==v for k,v in expected.items()) and terms['member_ids']==[m['character_id'] for m in snapshot['members']] and terms['authority_source']['holder_id']==row['holder_id']
        except (ValueError,KeyError,TypeError): return False

    @classmethod
    def _source(cls,c,ride,row,eid,cid,operation,at):
        event=c.execute('SELECT * FROM world_events WHERE id=? AND world_id=?',(eid,ride['world_id'])).fetchone()
        if event is None:return False
        try:
            payload=json.loads(event['payload_json'])
            return bool(cls._coherent(ride,row) and event['actor_id']==cid and event['target_id'] is None and event['event_type']=='social.ride_handoff_'+operation
                and from_iso(row['proposed_at'])<=from_iso(event['occurred_at'])<=at
                and (operation in {'revoked','invalidated'} or from_iso(event['occurred_at'])<from_iso(row['deadline_at']))
                and payload.get('ride_id')==ride['id'] and payload.get('vehicle_id')==ride['vehicle_id'] and payload.get('handoff_id')==row['id']
                and payload.get('handoff_epoch')==row['epoch'] and payload.get('handoff_terms')==cls._terms(row)
                and c.execute("SELECT 1 FROM character_event_knowledge WHERE character_id=? AND event_id=? AND world_id=? AND source_kind='experienced' AND learned_world_time<=?",(cid,eid,ride['world_id'],to_iso(at))).fetchone())
        except (ValueError,KeyError,TypeError):return False

    @classmethod
    def _accepted(cls,c,ride,row,at):
        members=cls._members(c,row)
        return bool(cls._coherent(ride,row) and cls._terms(row)['member_ids']==[m['character_id'] for m in members] and all(
            m['decision']=='accepted' and m['decision_at'] and from_iso(m['decision_at'])<=at
            and cls._source(c,ride,row,m['decision_event_id'],m['character_id'],'proposed' if m['character_id']==row['proposer_id'] else 'accepted',at)
            and c.execute('SELECT occurred_at FROM world_events WHERE id=?',(m['decision_event_id'],)).fetchone()[0]==m['decision_at'] for m in members))

    @classmethod
    def _grant(cls,c,ride,row,at):
        if cls.authority(c,ride,at)!=cls._terms(row)['authority_source']:
            raise ValueError('原驾驶权来源已变化，旧有限授权不能继续使用')
        if row['authorized_by']!=row['holder_id'] or not row['granted_at'] or from_iso(row['granted_at'])>at or not cls._source(c,ride,row,row['authorization_event_id'],row['holder_id'],'authorized',at):
            raise ValueError('缺少原合法持有人本轮明确驾驶授权')
        if c.execute('SELECT occurred_at FROM world_events WHERE id=?',(row['authorization_event_id'],)).fetchone()[0]!=row['granted_at']:
            raise ValueError('驾驶授权时间与真实来源不一致')
        if row['revoked_at'] and from_iso(row['revoked_at'])<=at:
            raise ValueError('本轮有限驾驶授权已被持有人撤回')
        if at>from_iso(row['deadline_at']):raise ValueError('本轮有限驾驶授权已过期')

    @classmethod
    def _ready(cls,c,ride,row,at,*,grant=False):
        snapshot,movement=cls._snapshot(c,ride,at)
        if not cls._coherent(ride,row) or snapshot!=json.loads(row['snapshot_json']):
            raise ValueError('原挂起轮次、路线进度、在车名单或停止请求已变化')
        if cls.authority(c,ride,at)!=cls._terms(row)['authority_source']:raise ValueError('驾驶许可来源已变化')
        if row['holder_id'] not in cls._terms(row)['member_ids']:raise ValueError('本轮仅支持仍在原车上的真实持有人授权')
        rides=cls._rides();vehicle=rides._vehicle(c,ride)
        rides._driver_state(c,ride,at,row['controller_id'],vehicle)
        state=rides.state(c,ride['world_id'],ride['vehicle_id'])
        final=rides._place(c,ride['world_id'],ride['destination_location_id'],state['space_layer'],vehicle['movement_type'])
        distance,seconds=cls._recovery().remaining(movement)
        if at+timedelta(seconds=seconds)>from_iso(row['deadline_at']):raise ValueError('剩余真实路线无法在新交接期限内完成')
        authority=cls._terms(row)['authority_source']
        if authority.get('due_at') and from_iso(row['deadline_at'])>from_iso(authority['due_at']):raise ValueError('交接期限不能越过真实租约期限')
        from world_engine.group_journeys import GroupJourneyService
        for member in rides._members(c,ride):
            person=rides._person(c,ride['world_id'],member['character_id'])
            GroupJourneyService._knows(c,person,[final['id']],at);rides._commitments(c,person,at,seconds,final)
        if grant:cls._grant(c,ride,row,at)
        return movement,seconds

    @classmethod
    def _record(cls,c,ride,row,actor,at,operation,statement):
        return cls._rides()._event(c,ride,actor,at,'handoff_'+operation,reason=statement,
            extra={'handoff_id':row['id'],'handoff_epoch':row['epoch'],'handoff_terms':cls._terms(row)})

    @classmethod
    def invalidate(cls,c,ride,at,reason):
        for row in c.execute("SELECT * FROM vehicle_ride_handoffs WHERE ride_id=? AND status IN ('pending','accepted')",(ride['id'],)).fetchall():
            actor=cls._rides()._person(c,ride['world_id'],cls.driver_id(c,ride,at))
            event=cls._record(c,ride,row,actor,at,'invalidated',reason)
            c.execute("UPDATE vehicle_ride_handoffs SET status='invalidated',reason=?,source_event_id=? WHERE id=?",(reason,event,row['id']))

    @classmethod
    def maintain(cls,c,wid,at):
        for row in c.execute("SELECT * FROM vehicle_ride_handoffs WHERE world_id=? AND status IN ('pending','accepted')",(wid,)).fetchall():
            ride=c.execute('SELECT * FROM vehicle_rides WHERE id=?',(row['ride_id'],)).fetchone()
            if at>=from_iso(row['deadline_at']):
                c.execute("UPDATE vehicle_ride_handoffs SET status='expired',reason='本轮有限交接期限已过' WHERE id=?",(row['id'],))
            else:
                try:
                    snapshot,_=cls._snapshot(c,ride,at)
                    if snapshot!=json.loads(row['snapshot_json']) or cls.authority(c,ride,at)!=cls._terms(row)['authority_source']:raise ValueError('交接基础已经变化')
                except (ValueError,LookupError,TypeError):cls.invalidate(c,ride,at,'原驾驶权、挂起轮次或在车状态已变化，需要重新商量')

    @classmethod
    def controller(cls,c,ride,at):
        row=cls.control_row(c,ride,at)
        if row is None:return None
        if not cls._accepted(c,ride,row,at) or not cls._source(c,ride,row,row['execution_event_id'],row['controller_id'],'taken',at):
            raise ValueError('当前接管执行或逐人同意来源失效，不能退回旧驾驶者')
        cls._grant(c,ride,row,at)
        return cls._rides()._vehicle(c,ride),cls._rides()._person(c,ride['world_id'],row['controller_id'])

    @classmethod
    def governing(cls,c,ride,at,observer=None):
        # 哥哥，同一世界时刻仍按真实事件写入序号判断先后，不取最大期限。
        choices=[]
        for table,kind in (('vehicle_ride_handoffs','handoff'),('vehicle_ride_recoveries','recovery')):
            for row in c.execute('SELECT p.*,e.rowid AS execution_order FROM '+table+' p LEFT JOIN world_events e ON e.id=p.execution_event_id WHERE p.ride_id=? AND p.executed_at IS NOT NULL AND p.executed_at<=? ORDER BY COALESCE(e.rowid,9223372036854775807) DESC LIMIT 1',(ride['id'],to_iso(at))):
                if observer and not c.execute('SELECT 1 FROM character_event_knowledge k JOIN world_events e ON e.id=k.event_id AND e.world_id=k.world_id WHERE k.character_id=? AND k.event_id=? AND k.world_id=? AND k.learned_world_time<=? AND e.occurred_at<=?',(observer,row['execution_event_id'],ride['world_id'],to_iso(at),to_iso(at))).fetchone():continue
                choices.append((row['execution_order'] if row['execution_order'] is not None else 9223372036854775807,kind,row))
        if not choices:return None
        _,kind,row=max(choices,key=lambda item:item[0]);return kind,row

    @classmethod
    def assert_execution(cls,c,ride,at):
        row=cls.control_row(c,ride,at)
        if row is None:return
        cls.controller(c,ride,at)
        movement=c.execute('SELECT * FROM character_movements WHERE id=?',(ride['movement_id'],)).fetchone();snapshot=json.loads(row['snapshot_json'])
        if movement is None or cls._recovery()._route(movement)!=snapshot['route'] or movement['distance_travelled_km']<snapshot['distance_travelled_km']:
            raise ValueError('接管后原路线或实际进度失效')
        for member in cls._rides()._members(c,ride):
            if not member['live'] or not member['boarded_at'] or member['disembarked_at']:raise ValueError('接管后原完整在车名单已经变化')

    @classmethod
    def effective_deadline(cls,c,ride,at,observer=None):
        pending=c.execute("SELECT * FROM vehicle_ride_handoffs WHERE ride_id=? AND status='accepted' AND proposed_at<=? ORDER BY epoch DESC LIMIT 1",(ride['id'],to_iso(at))).fetchone()
        if pending:
            try:
                cls._ready(c,ride,pending,at)
                if cls._accepted(c,ride,pending,at) and (not observer or cls._known(c,pending['source_event_id'],observer,ride['world_id'],at)):
                    return from_iso(pending['deadline_at'])
            except (ValueError,LookupError):pass
        return None

    @staticmethod
    def _known(c,eid,cid,wid,at):
        return bool(c.execute('SELECT 1 FROM character_event_knowledge k JOIN world_events e ON e.id=k.event_id AND e.world_id=k.world_id WHERE k.character_id=? AND k.event_id=? AND k.world_id=? AND k.learned_world_time<=? AND e.occurred_at<=?',(cid,eid,wid,to_iso(at),to_iso(at))).fetchone())

    @classmethod
    def apply(cls,c,actor,at,choice):
        rides=cls._rides();ride=c.execute('SELECT * FROM vehicle_rides WHERE id=? AND world_id=?',(choice.ride_id,actor['world_id'])).fetchone()
        if ride is None or not c.execute('SELECT 1 FROM vehicle_ride_members WHERE ride_id=? AND character_id=? AND live=1 AND boarded_at IS NOT NULL AND disembarked_at IS NULL',(choice.ride_id,actor['id'])).fetchone():
            raise ValueError('只有原名单中仍实际在车的本人能够选择驾驶交接')
        if actor['health']<=0:raise ValueError('当前本人无法作出新的交接选择，不能代替失能者同意')
        cls.maintain(c,ride['world_id'],at)
        if choice.operation=='propose_handoff':
            if c.execute("SELECT 1 FROM vehicle_ride_recoveries WHERE ride_id=? AND status IN ('pending','accepted')",(ride['id'],)).fetchone() or c.execute("SELECT 1 FROM vehicle_ride_handoffs WHERE ride_id=? AND status IN ('pending','accepted')",(ride['id'],)).fetchone():
                raise ValueError('已有待处理的恢复或交接方案，请先明确处理原方案')
            snapshot,movement=cls._snapshot(c,ride,at);authority=cls.authority(c,ride,at)
            if not cls._known(c,snapshot['block_event_id'],actor['id'],ride['world_id'],at):raise ValueError('本人尚不知本轮挂起来源')
            if not at<choice.deadline_world_time<=at+timedelta(hours=24):raise ValueError('交接新期限须在现在之后且不超过24个世界小时')
            ids=[m['character_id'] for m in snapshot['members']]
            if choice.controller_id not in ids or authority['holder_id'] not in ids:raise ValueError('本轮新驾驶者和合法持有人均须仍在原车名单内')
            if choice.controller_id==cls.driver_id(c,ride,at) and cls.control_row(c,ride,at) is None:
                raise ValueError('原驾驶者继续驾驶请另提恢复；已有真实接管者可另行商量本轮有限驾驶许可')
            epoch=c.execute('SELECT COALESCE(MAX(epoch),0)+1 FROM vehicle_ride_handoffs WHERE ride_id=?',(ride['id'],)).fetchone()[0]
            hid=str(uuid4());distance,seconds=cls._recovery().remaining(movement)
            terms={'handoff_id':hid,'ride_id':ride['id'],'world_id':ride['world_id'],'epoch':epoch,'proposer_id':actor['id'],
                'previous_controller_id':cls.driver_id(c,ride,at),'controller_id':choice.controller_id,'holder_id':authority['holder_id'],
                'vehicle_id':ride['vehicle_id'],'movement_id':movement['id'],'block_event_id':snapshot['block_event_id'],
                'destination_location_id':ride['destination_location_id'],'deadline_at':to_iso(choice.deadline_world_time),'member_ids':ids,
                'authority_source':authority,'route_sha256':hashlib.sha256(movement['route_json'].encode()).hexdigest(),
                'remaining_seconds':seconds,'remaining_distance_km':distance,'stop_requested':snapshot['stop_requested'],
                'stop_policy':rides.terms(c,ride)['stop_policy'],'statement':choice.statement,'snapshot_sha256':hashlib.sha256(cls._json(snapshot).encode()).hexdigest()}
            row={'id':hid,'ride_id':ride['id'],'world_id':ride['world_id'],'epoch':epoch,'proposer_id':actor['id'],
                'previous_controller_id':terms['previous_controller_id'],'controller_id':choice.controller_id,'holder_id':authority['holder_id'],
                'movement_id':movement['id'],'block_event_id':snapshot['block_event_id'],'deadline_at':terms['deadline_at'],'proposed_at':to_iso(at),
                'terms_json':cls._json(terms),'snapshot_json':cls._json(snapshot)}
            cls._ready(c,ride,row,at);event=cls._record(c,ride,row,actor,at,'proposed',choice.statement)
            keys=('id','ride_id','world_id','epoch','proposer_id','previous_controller_id','controller_id','holder_id','movement_id','block_event_id','statement','proposed_at','deadline_at','terms_json','snapshot_json','proposal_event_id','source_event_id')
            row.update(statement=choice.statement,proposal_event_id=event,source_event_id=event)
            c.execute('INSERT INTO vehicle_ride_handoffs('+','.join(keys)+",status) VALUES ("+','.join('?' for _ in keys)+",'pending')",tuple(row[k] for k in keys))
            for index,cid in enumerate(ids):
                c.execute('INSERT INTO vehicle_ride_handoff_members VALUES (?,?,?,?,?,?)',(hid,cid,index,'accepted' if cid==actor['id'] else 'pending',to_iso(at) if cid==actor['id'] else None,event if cid==actor['id'] else None))
            if len(ids)==1:c.execute("UPDATE vehicle_ride_handoffs SET status='accepted' WHERE id=?",(hid,))
        else:
            row=c.execute('SELECT * FROM vehicle_ride_handoffs WHERE id=? AND ride_id=? AND world_id=?',(choice.handoff_id,ride['id'],ride['world_id'])).fetchone()
            own=c.execute('SELECT * FROM vehicle_ride_handoff_members WHERE handoff_id=? AND character_id=?',(choice.handoff_id,actor['id'])).fetchone()
            if row is None or own is None:raise ValueError('本人不属于这份驾驶交接方案')
            if not cls._source(c,ride,row,row['proposal_event_id'],row['proposer_id'],'proposed',at) or not cls._known(c,row['proposal_event_id'],actor['id'],ride['world_id'],at):raise ValueError('当前交接方案来源尚未有效')
            op=choice.operation
            if op in {'accept_handoff','decline_handoff'}:
                decision='accepted' if op=='accept_handoff' else 'declined'
                if own['decision']==decision:
                    if not cls._source(c,ride,row,own['decision_event_id'],actor['id'],'proposed' if actor['id']==row['proposer_id'] else decision,at):raise ValueError('本人旧回应来源无效')
                    return rides._result(c,ride,actor,at,own['decision_event_id'])
                if row['status']!='pending' or own['decision']!='pending' or at>=from_iso(row['deadline_at']):raise ValueError('本轮已不能回应，不能覆盖旧拒绝')
                if decision=='accepted':cls._ready(c,ride,row,at)
                event=cls._record(c,ride,row,actor,at,decision,choice.statement)
                c.execute('UPDATE vehicle_ride_handoff_members SET decision=?,decision_at=?,decision_event_id=? WHERE handoff_id=? AND character_id=?',(decision,to_iso(at),event,row['id'],actor['id']))
                status='declined' if decision=='declined' else 'accepted' if cls._accepted(c,ride,row,at) else 'pending'
                c.execute('UPDATE vehicle_ride_handoffs SET status=?,source_event_id=? WHERE id=?',(status,event,row['id']))
            elif op=='authorize_handoff':
                if actor['id']!=row['holder_id']:raise ValueError('只有原真实持有人能独立授予本轮有限驾驶许可')
                if row['authorization_event_id']:
                    if not cls._source(c,ride,row,row['authorization_event_id'],actor['id'],'authorized',at):raise ValueError('本人旧驾驶授权来源无效')
                    return rides._result(c,ride,actor,at,row['authorization_event_id'])
                if row['status'] not in {'pending','accepted'} or at>=from_iso(row['deadline_at']):raise ValueError('本轮已不能授予驾驶许可')
                cls._ready(c,ride,row,at);event=cls._record(c,ride,row,actor,at,'authorized',choice.statement)
                c.execute('UPDATE vehicle_ride_handoffs SET authorized_by=?,granted_at=?,authorization_event_id=?,source_event_id=? WHERE id=?',(actor['id'],to_iso(at),event,event,row['id']))
            elif op=='withdraw_handoff':
                if actor['id']!=row['proposer_id'] and own['decision']!='accepted' and row['authorized_by']!=actor['id']:raise ValueError('只能撤回本人的提议、同意或驾驶授权')
                if row['status']=='withdrawn':
                    if not cls._source(c,ride,row,row['source_event_id'],actor['id'],'withdrawn',at):raise ValueError('不能借用另一人的撤回回执')
                    return rides._result(c,ride,actor,at,row['source_event_id'])
                if row['status'] not in {'pending','accepted'}:raise ValueError('已执行的交接不能倒改旧同意')
                event=cls._record(c,ride,row,actor,at,'withdrawn',choice.statement)
                c.execute("UPDATE vehicle_ride_handoffs SET status='withdrawn',source_event_id=? WHERE id=?",(event,row['id']))
            elif op=='take_control':
                if actor['id']!=row['controller_id']:raise ValueError('只有新驾驶者能在全员同意和授权后亲自接管')
                if row['executed_at']:
                    if not cls._source(c,ride,row,row['execution_event_id'],actor['id'],'taken',at):raise ValueError('原接管执行来源无效')
                    return rides._result(c,ride,actor,at,row['execution_event_id'])
                if row['status']!='accepted' or not cls._accepted(c,ride,row,at):raise ValueError('须原在车全员分别有效接受交接条款')
                movement,seconds=cls._ready(c,ride,row,at,grant=True)
                event=cls._record(c,ride,row,actor,at,'taken',choice.statement)
                c.execute("UPDATE vehicle_ride_handoffs SET status='executed',executed_at=?,execution_event_id=?,source_event_id=? WHERE id=?",(to_iso(at),event,event,row['id']))
                c.execute("UPDATE character_movements SET status='moving',estimated_arrival_world=?,updated_at_world=?,updated_at=? WHERE id=? AND status='suspended'",(to_iso(at+timedelta(seconds=seconds)),to_iso(at),to_iso(utc_now()),movement['id']))
                c.execute("UPDATE vehicle_rides SET status='moving',reason='',source_event_id=? WHERE id=?",(event,ride['id']))
                c.execute("UPDATE vehicle_states SET status='moving',updated_at=? WHERE vehicle_id=?",(to_iso(at),ride['vehicle_id']))
            elif op=='revoke_handoff':
                if actor['id']!=row['holder_id'] or row['authorized_by']!=actor['id']:raise ValueError('只能撤销本人实际授出的驾驶许可')
                if row['revoked_at']:
                    if not cls._source(c,ride,row,row['revoke_event_id'],actor['id'],'revoked',at):raise ValueError('本人原撤权来源无效')
                    return rides._result(c,ride,actor,at,row['revoke_event_id'])
                if not cls._source(c,ride,row,row['authorization_event_id'],actor['id'],'authorized',at):raise ValueError('本轮尚无可撤销的真实驾驶许可')
                event=cls._record(c,ride,row,actor,at,'revoked',choice.statement)
                current=cls.control_row(c,ride,at)
                c.execute("UPDATE vehicle_ride_handoffs SET status='revoked',revoked_at=?,revoke_event_id=?,source_event_id=? WHERE id=?",(to_iso(at),event,event,row['id']))
                if current and current['id']==row['id']:rides._block(c,ride,at,'实际持有人已撤回本轮有限驾驶许可，原位挂起保留全部乘员')
            else:raise ValueError('未知驾驶交接操作')
        c.execute('UPDATE vehicle_rides SET source_event_id=? WHERE id=?',(event,ride['id']))
        return rides._result(c,c.execute('SELECT * FROM vehicle_rides WHERE id=?',(ride['id'],)).fetchone(),actor,at,event)

    @classmethod
    def view(cls,c,ride,at,observer=None):
        from world_engine.society import SocietyService
        rows=c.execute('SELECT * FROM vehicle_ride_handoffs WHERE ride_id=? AND proposed_at<=? ORDER BY epoch DESC',(ride['id'],to_iso(at))).fetchall()
        known=next((r for r in rows if cls._source(c,ride,r,r['proposal_event_id'],r['proposer_id'],'proposed',at) and (not observer or cls._known(c,r['proposal_event_id'],observer,ride['world_id'],at))),None)
        if known is None:return None
        events=c.execute("SELECT e.* FROM world_events e WHERE e.world_id=? AND e.event_type LIKE 'social.ride_handoff_%' AND json_extract(e.payload_json,'$.handoff_id')=? AND e.occurred_at<=? ORDER BY e.occurred_at,e.rowid",(ride['world_id'],known['id'],to_iso(at))).fetchall()
        events=[e for e in events if not observer or cls._known(c,e['id'],observer,ride['world_id'],at)]
        ids={e['id'] for e in events};members=[dict(m) for m in cls._members(c,known)]
        for m in members:
            m['name']=SocietyService.label(c,observer,m['character_id']) if observer else '在车成员'
            op='proposed' if m['character_id']==known['proposer_id'] else m['decision']
            if not m['decision_at'] or from_iso(m['decision_at'])>at or m['decision_event_id'] not in ids or not cls._source(c,ride,known,m['decision_event_id'],m['character_id'],op,at):m.update(decision='pending',decision_at=None,decision_event_id=None)
        status='accepted' if all(m['decision']=='accepted' for m in members) else 'pending'
        for e in events:
            op=e['event_type'].removeprefix('social.ride_handoff_')
            if op in {'declined','withdrawn','invalidated','revoked'}:status=op
            elif op=='taken':status='executed'
        if status in {'pending','accepted'} and at>=from_iso(known['deadline_at']):status='expired'
        authority={k:cls._terms(known)['authority_source'][k] for k in ('holder_id','source_kind','source_id')}
        for key,event_key in (('granted_at','authorization_event_id'),('revoked_at','revoke_event_id')):
            valid=known[event_key] in ids and known[key] and from_iso(known[key])<=at
            authority[key]=known[key] if valid else None
            authority['grant_event_id' if key=='granted_at' else 'revoke_event_id']=known[event_key] if valid else None
        result={k:known[k] for k in ('id','ride_id','epoch','proposer_id','previous_controller_id','controller_id','holder_id','movement_id','proposed_at','deadline_at')}
        result.update(status=status,terms=cls._terms(known),members=members,authority=authority,source_event_id=events[-1]['id'] if events else known['proposal_event_id'],
            can_accept=False,can_decline=False,can_withdraw=False,can_authorize=False,can_take_control=False,can_revoke=False)
        now=from_iso(c.execute('SELECT w.current_time FROM worlds w WHERE id=?',(ride['world_id'],)).fetchone()[0])
        own=next((m for m in members if m['character_id']==observer),None)
        if own and at>=now and cls._aboard(c,ride,observer) and cls._rides()._person(c,ride['world_id'],observer)['health']>0:
            result['can_decline']=status=='pending' and own['decision']=='pending'
            result['can_withdraw']=status in {'pending','accepted'} and (observer==known['proposer_id'] or own['decision']=='accepted' or known['authorized_by']==observer)
            result['can_revoke']=status in {'pending','accepted','executed'} and observer==known['holder_id'] and bool(authority['grant_event_id'])
            try:
                cls._ready(c,ride,known,at)
                result['can_accept']=status=='pending' and own['decision']=='pending'
                result['can_authorize']=status in {'pending','accepted'} and observer==known['holder_id'] and not known['authorization_event_id']
                if status=='accepted' and observer==known['controller_id'] and cls._accepted(c,ride,known,at):
                    cls._grant(c,ride,known,at);result['can_take_control']=True
            except (ValueError,LookupError,TypeError):pass
        return result

    @staticmethod
    def _aboard(c,ride,cid):
        return bool(c.execute('SELECT 1 FROM vehicle_ride_members WHERE ride_id=? AND character_id=? AND live=1 AND boarded_at IS NOT NULL AND disembarked_at IS NULL',(ride['id'],cid)).fetchone())

    @classmethod
    def can_propose(cls,c,ride,at,observer):
        if not observer or not cls._aboard(c,ride,observer) or cls._rides()._person(c,ride['world_id'],observer)['health']<=0 or c.execute("SELECT 1 FROM vehicle_ride_handoffs WHERE ride_id=? AND status IN ('pending','accepted')",(ride['id'],)).fetchone() or c.execute("SELECT 1 FROM vehicle_ride_recoveries WHERE ride_id=? AND status IN ('pending','accepted')",(ride['id'],)).fetchone():return False
        try:
            snapshot,_=cls._snapshot(c,ride,at);authority=cls.authority(c,ride,at)
            ids=[m['character_id'] for m in snapshot['members']]
            return authority['holder_id'] in ids and (cls.control_row(c,ride,at) is not None or any(cid!=cls.driver_id(c,ride,at) for cid in ids))
        except (ValueError,LookupError,TypeError):return False
