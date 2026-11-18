"""两人分别选择使用已存在的公共升降通道。"""

import json
import math
from datetime import timedelta
from functools import wraps
from uuid import uuid4

from world_engine.actions import ActionService
from world_engine.epistemics import KnowledgeService
from world_engine.geo import great_circle_distance_km, nearby_coordinate_bounds
from world_engine.life import LifeActivityService
from world_engine.proximity import same_room
from world_engine.repository import WorldRepository, from_iso, to_iso
from world_engine.wonders import WonderService

CROSS_LAYER_SCHEMA = """
CREATE TABLE IF NOT EXISTS cross_layer_passages (
 id TEXT PRIMARY KEY,world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 player_id TEXT NOT NULL REFERENCES characters(id),npc_id TEXT NOT NULL REFERENCES characters(id),
 wonder_id TEXT NOT NULL REFERENCES world_wonders(id),direction TEXT NOT NULL CHECK(direction IN ('enter','exit')),
 idempotency_key TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('pending','npc_passed','completed','declined','cancelled')),
 proposed_at TEXT NOT NULL,expires_at TEXT NOT NULL,proposal_event_id TEXT NOT NULL,
 npc_event_id TEXT,player_event_id TEXT,decision_source_id TEXT,cancel_event_id TEXT,npc_target_event_id TEXT,
 UNIQUE(world_id,player_id,idempotency_key),CHECK(player_id<>npc_id)
);
CREATE INDEX IF NOT EXISTS idx_cross_layer_pair ON cross_layer_passages(world_id,player_id,npc_id,status);
"""


def install_cross_layer(c):
    for statement in CROSS_LAYER_SCHEMA.split(';'):
        if statement.strip():
            c.execute(statement)


def atomic(method):
    @wraps(method)
    def wrapped(cls, c, *args, **kwargs):
        name = 'cross_layer_' + uuid4().hex
        c.execute('SAVEPOINT ' + name)
        try:
            result = method(cls, c, *args, **kwargs)
            c.execute('RELEASE ' + name)
            return result
        except Exception:
            c.execute('ROLLBACK TO ' + name)
            c.execute('RELEASE ' + name)
            raise
    return wrapped


class CrossLayerPassageService:
    # 哥哥，必须真正抵达通道节点，不能借原有100米访问半径横向瞬移。
    NODE_RADIUS_KM = 0.001
    APPROACH_RADIUS_KM = 0.1

    @staticmethod
    def _now(c, world_id, at):
        world = c.execute('SELECT w.current_time FROM worlds w WHERE w.id=?', (world_id,)).fetchone()
        if not world or from_iso(world[0]) != at:
            raise ValueError('世界时间已变化，请重新核对通道')

    @staticmethod
    def _wonder(c, world_id, wonder_id, *, candidate=None):
        wonder = candidate if candidate is not None and candidate['id']==wonder_id and candidate['world_id']==world_id else next((w for w in WonderService.view(c, world_id) if w['id'] == wonder_id), None)
        if not wonder or wonder['status'] != 'active' or not wonder['location_id'] or not wonder['completed_event_id']:
            raise ValueError('公共通道未开放')
        event = c.execute("SELECT occurred_at,location_id,payload_json FROM world_events WHERE id=? AND world_id=? AND event_type='world.wonder_completed'", (wonder['completed_event_id'],world_id)).fetchone()
        current = c.execute('SELECT w.current_time FROM worlds w WHERE w.id=?',(world_id,)).fetchone()
        try:
            payload=json.loads(event['payload_json']) if event else None
        except (ValueError,TypeError):
            payload=None
        if (not event or not current or from_iso(event['occurred_at']) > from_iso(current[0])
            or event['location_id']!=wonder['ground_location_id'] or not isinstance(payload,dict)
            or payload.get('wonder_id')!=wonder_id or payload.get('city_location_id')!=wonder['location_id']):
            raise ValueError('公共通道尚无已发生的开放来源')
        return wonder

    @classmethod
    def _person(cls, c, world_id, character_id, wonder, direction, *, radius_km=None):
        from world_engine.rides import RideService
        from world_engine.player_steering import PlayerSteeringService
        actor = c.execute('SELECT * FROM characters WHERE id=? AND world_id=?', (character_id, world_id)).fetchone()
        if not actor or actor['health'] <= 0 or actor['current_room_id'] or actor['current_fixture_id'] or actor['active_vehicle_id']:
            raise ValueError('本人当前不能使用公共通道')
        if actor['space_layer'] != ('ground' if direction == 'enter' else wonder['space_layer']):
            raise ValueError('本人不在通道出发空间层')
        if any(value is None or not math.isfinite(value) for value in
               (actor['longitude'],actor['latitude'],wonder['longitude'],wonder['latitude'])):
            raise ValueError('本人或公共通道缺少有效位置')
        distance = great_circle_distance_km(actor['longitude'], actor['latitude'], wonder['longitude'], wonder['latitude'])
        if distance > (cls.NODE_RADIUS_KM if radius_km is None else radius_km):
            raise ValueError('本人需要先实际走到公共通道附近')
        RideService.assert_not_aboard(c, character_id)
        LifeActivityService.assert_available(c, character_id)
        if PlayerSteeringService.active(c, character_id) or c.execute("SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'", (character_id,)).fetchone():
            raise ValueError('本人需要先停下移动')
        if c.execute("SELECT 1 FROM companion_sessions WHERE world_id=? AND status='active' AND (leader_id=? OR companion_id=?)", (world_id,character_id,character_id)).fetchone():
            raise ValueError('请先结束本人原短时同行，再商量跨层')
        if c.execute("SELECT 1 FROM joint_journeys WHERE world_id=? AND status IN ('accepted','active','paused','resume_pending') AND (proposer_id=? OR recipient_id=?)", (world_id,character_id,character_id)).fetchone():
            raise ValueError('请先结束本人原双人旅行，再商量跨层')
        if c.execute("SELECT 1 FROM group_journeys j JOIN group_journey_members m ON m.journey_id=j.id WHERE j.world_id=? AND m.character_id=? AND m.left_at IS NULL AND j.status IN ('accepted','active','paused','resume_pending')", (world_id,character_id)).fetchone():
            raise ValueError('请先退出本人原多人旅行，再商量跨层')
        return actor

    @staticmethod
    def _event(c, first, second, at, event_type, summary, payload):
        event = ActionService._record_event(c, world_id=first['world_id'], tick_id=str(uuid4()),
            occurred_at=at, event_type=event_type, actor_id=first['id'], target_id=second['id'],
            location_id=first['current_location_id'], summary=summary, payload=payload)
        for actor in (first, second):
            ActionService._record_memory(c, world_id=first['world_id'], character_id=actor['id'],
                event_id=event, memory_type='experienced', summary=summary, importance=4)
        return event

    @classmethod
    def _render(cls, c, row, at):
        data = dict(row)
        data['npc_name'] = KnowledgeService.known_label(c, row['player_id'], row['npc_id'])
        wonder = c.execute('SELECT name FROM world_structures WHERE id=? AND world_id=?', (row['wonder_id'],row['world_id'])).fetchone()
        data['wonder_name'] = wonder['name'] if wonder else '公共升降通道'
        data['expired'] = from_iso(row['expires_at']) <= at and row['status'] in {'pending', 'npc_passed'}
        data['event_id'] = row['cancel_event_id'] or row['player_event_id'] or row['npc_event_id'] or row['proposal_event_id']
        if row['status'] in {'npc_passed','completed'}:
            try:
                cls._npc_passage_witness(c,row,at)
            except (ValueError,LookupError):
                # 哥哥，缺少本人可观察的来源时不能把对方去向当作已知事实。
                data['status']='unverified'
                data['npc_event_id']=None
                data['decision_source_id']=None
                data['event_id']=row['player_event_id'] or row['proposal_event_id']
        data.update(player_at_node=False,player_can_approach=False,can_talk=False)
        if row['status'] in {'pending','npc_passed'} and not data['expired']:
            try:
                target=cls._wonder(c,row['world_id'],row['wonder_id'])
                player=cls._person(c,row['world_id'],row['player_id'],target,row['direction'],radius_km=cls.APPROACH_RADIUS_KM)
                distance=great_circle_distance_km(player['longitude'],player['latitude'],target['longitude'],target['latitude'])
                data['player_at_node']=distance<=cls.NODE_RADIUS_KM
                data['player_can_approach']=distance>cls.NODE_RADIUS_KM
                if row['status']=='pending':
                    try:
                        cls._proposal_source(c,row,at)
                        npc=cls._person(c,row['world_id'],row['npc_id'],target,row['direction'],radius_km=cls.APPROACH_RADIUS_KM)
                        from world_engine.intimacy import IntimacyService
                        data['can_talk']=(not IntimacyService.paused(c,row['world_id'],player['id'],npc['id'])
                            and same_room(player,npc) and great_circle_distance_km(player['longitude'],player['latitude'],npc['longitude'],npc['latitude'])<=0.02)
                    except (ValueError,LookupError):
                        pass
            except (ValueError,LookupError):
                pass
        data.pop('idempotency_key', None)
        # 哥哥，目的层事件仅供内部核验，源层玩家不能据此获得未见事件的编号。
        data.pop('npc_target_event_id', None)
        return data

    @classmethod
    def options(cls, c, world_id, player_id, at):
        cls._now(c, world_id, at)
        player = c.execute('SELECT * FROM characters WHERE id=? AND world_id=? AND is_player=1 AND is_pov=1', (player_id, world_id)).fetchone()
        if not player:
            raise ValueError('当前世界没有玩家视角')
        wonders = []
        for w in WonderService.view(c, world_id):
            if w['status'] != 'active' or not w['location_id']:
                continue
            try:
                w=cls._wonder(c,world_id,w['id'],candidate=w)
            except (ValueError,LookupError):
                continue
            direction = 'enter' if player['space_layer'] == 'ground' else 'exit' if player['space_layer'] == w['space_layer'] else None
            if not direction:
                continue
            try:
                cls._person(c, world_id, player_id, w, direction, radius_km=cls.APPROACH_RADIUS_KM)
            except ValueError:
                continue
            people = []
            south,north,ranges=nearby_coordinate_bounds(w['longitude'],w['latitude'],cls.APPROACH_RADIUS_KM)
            lon_sql=' OR '.join('(longitude BETWEEN ? AND ?)' for _ in ranges)
            bounds=[x for pair in ranges for x in pair]
            for npc in c.execute('SELECT * FROM characters WHERE world_id=? AND is_player=0 AND health>0 AND space_layer=? AND latitude BETWEEN ? AND ? AND ('+lon_sql+')', (world_id,player['space_layer'],south,north,*bounds)):
                try:
                    cls._person(c, world_id, npc['id'], w, direction, radius_km=cls.APPROACH_RADIUS_KM)
                except ValueError:
                    continue
                if not same_room(player, npc) or great_circle_distance_km(player['longitude'],player['latitude'],npc['longitude'],npc['latitude']) > 0.02:
                    continue
                from world_engine.intimacy import IntimacyService
                if IntimacyService.paused(c, world_id, player_id, npc['id']):
                    continue
                people.append({'id': npc['id'], 'name': KnowledgeService.known_label(c, player_id, npc['id'])})
            if people:
                wonders.append({'id': w['id'], 'name': w['name'], 'directions': [direction], 'people': people})
        return {'wonders': wonders}

    @classmethod
    def rows(cls, c, world_id, player_id, at):
        cls._now(c, world_id, at)
        rows = c.execute('SELECT * FROM cross_layer_passages WHERE world_id=? AND player_id=? ORDER BY rowid DESC LIMIT 20', (world_id, player_id)).fetchall()
        return [cls._render(c, row, at) for row in rows]

    @classmethod
    def _proposal_source(cls,c,row,at):
        """哥哥，NPC 只能看到自己真实参与且不早于来源的通道提议。"""
        event=c.execute('SELECT rowid,* FROM world_events WHERE id=? AND world_id=?',
                        (row['proposal_event_id'],row['world_id'])).fetchone()
        try:
            payload=json.loads(event['payload_json']) if event else None
            valid=(event and event['event_type']=='social.cross_layer_proposed'
                and event['actor_id']==row['player_id'] and event['target_id']==row['npc_id']
                and event['occurred_at']==row['proposed_at'] and from_iso(event['occurred_at'])<=at
                and isinstance(payload,dict) and all(payload.get(k)==v for k,v in
                    {'passage_id':row['id'],'wonder_id':row['wonder_id'],'direction':row['direction']}.items()))
        except (ValueError,TypeError):
            valid=False
        if not valid:
            raise ValueError('原通道提议来源无法核验')
        knowledge=c.execute("SELECT 1 FROM character_event_knowledge k JOIN event_observers o "
            "ON o.event_id=k.event_id AND o.character_id=k.character_id WHERE k.world_id=? AND k.event_id=? "
            "AND k.character_id=? AND k.source_kind='experienced' AND o.channel='participant' "
            "AND k.learned_world_time>=? AND k.learned_world_time<=?",
            (row['world_id'],event['id'],row['npc_id'],event['occurred_at'],to_iso(at))).fetchone()
        if not knowledge:
            raise ValueError('受邀人物没有原提议的真实获知记录')
        return event

    @classmethod
    def _npc_passage_witness(cls,c,row,at):
        proposed=cls._proposal_source(c,row,at)
        world_id=row['world_id'];player_id=row['player_id']
        source=c.execute("SELECT rowid,* FROM world_events WHERE id=? AND world_id=? AND event_type='action.socialize' AND actor_id=? AND target_id=?",
                         (row['decision_source_id'],world_id,player_id,row['npc_id'])).fetchone()
        witness=c.execute("SELECT rowid,* FROM world_events WHERE id=? AND world_id=? AND actor_id=? AND target_id=? AND event_type='social.cross_layer_passage_witnessed'",
                          (row['npc_event_id'],world_id,row['npc_id'],player_id)).fetchone()
        try:
            payload=json.loads(witness['payload_json']) if witness else None
            event=c.execute('SELECT rowid,* FROM world_events WHERE id=? AND world_id=? AND actor_id=? AND event_type=?',
                (row['npc_target_event_id'],world_id,row['npc_id'],
                 'action.wonder_enter' if row['direction']=='enter' else 'action.wonder_exit')).fetchone()
            target_payload=json.loads(event['payload_json']) if event else None
            knows=c.execute("SELECT 1 FROM character_event_knowledge k JOIN event_observers o ON o.event_id=k.event_id "
                "AND o.character_id=k.character_id WHERE k.world_id=? AND k.event_id=? AND k.character_id=? "
                "AND k.source_kind='experienced' AND o.channel='participant' AND k.learned_world_time>=? AND k.learned_world_time<=?",
                (world_id,row['npc_event_id'],player_id,witness['occurred_at'] if witness else '',to_iso(at))).fetchone()
            npc_knows=cls._npc_knows_dialogue(c,row,source,at)
            valid=(source and witness and event and knows and npc_knows and source['rowid']>proposed['rowid']
                and witness['rowid']>source['rowid'] and event['rowid']>witness['rowid']
                and from_iso(proposed['occurred_at'])<=from_iso(source['occurred_at'])<=from_iso(witness['occurred_at'])<=from_iso(event['occurred_at'])<=at
                and isinstance(payload,dict) and all(payload.get(k)==v for k,v in
                    {'passage_id':row['id'],'wonder_id':row['wonder_id'],'direction':row['direction'],'decision_source_id':row['decision_source_id']}.items())
                and isinstance(target_payload,dict) and target_payload.get('wonder_id')==row['wonder_id'])
        except (ValueError,TypeError):
            valid=False
        if not valid:
            raise ValueError('对方本人通过的原事件或可观察来源无法核验')
        return witness

    @staticmethod
    def _npc_knows_dialogue(c,row,source,at):
        """哥哥，本人必须真实参与并在当前时刻之前知道所依据的现场对话。"""
        if not source:
            return False
        return bool(c.execute(
            "SELECT 1 FROM character_event_knowledge k JOIN event_observers o "
            "ON o.event_id=k.event_id AND o.character_id=k.character_id "
            "WHERE k.world_id=? AND k.event_id=? AND k.character_id=? "
            "AND k.source_kind='experienced' AND o.channel='participant' "
            "AND k.learned_world_time>=? AND k.learned_world_time<=?",
            (row['world_id'],source['id'],row['npc_id'],source['occurred_at'],to_iso(at)),
        ).fetchone())

    @classmethod
    def npc_context(cls,c,world_id,npc_id,player_id,at):
        """只向受邀本人提供仍可在现场执行的有效提议。"""
        if not player_id:
            return []
        try:
            cls._now(c,world_id,at)
        except ValueError:
            return []
        result=[]
        for row in c.execute("SELECT * FROM cross_layer_passages WHERE world_id=? AND npc_id=? AND player_id=? AND status='pending' AND proposed_at<=? AND expires_at>? ORDER BY rowid DESC LIMIT 4", (world_id,npc_id,player_id,to_iso(at),to_iso(at))):
            try:
                cls._proposal_source(c,row,at)
                wonder=cls._wonder(c,world_id,row['wonder_id'])
                npc=cls._person(c,world_id,npc_id,wonder,row['direction'],radius_km=cls.APPROACH_RADIUS_KM)
                player=cls._person(c,world_id,player_id,wonder,row['direction'],radius_km=cls.APPROACH_RADIUS_KM)
                if not same_room(npc,player) or great_circle_distance_km(npc['longitude'],npc['latitude'],player['longitude'],player['latitude']) > 0.02:
                    continue
                from world_engine.intimacy import IntimacyService
                if IntimacyService.paused(c,world_id,player_id,npc_id):
                    continue
                npc_distance=great_circle_distance_km(npc['longitude'],npc['latitude'],wonder['longitude'],wonder['latitude'])
                player_distance=great_circle_distance_km(player['longitude'],player['latitude'],wonder['longitude'],wonder['latitude'])
                result.append({'id':row['id'],'wonder_name':wonder['name'],'direction':row['direction'],'expires_at':row['expires_at'],
                               'npc_at_node':npc_distance<=cls.NODE_RADIUS_KM,'player_at_node':player_distance<=cls.NODE_RADIUS_KM,
                               'npc_can_approach':npc_distance>cls.NODE_RADIUS_KM})
            except (ValueError,LookupError):
                continue
        return result

    @classmethod
    @atomic
    def propose(cls, c, world_id, player_id, npc_id, wonder_id, direction, idempotency_key, at):
        cls._now(c, world_id, at)
        if direction not in {'enter', 'exit'} or not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key)>128 or idempotency_key != idempotency_key.strip():
            raise ValueError('通道选择或请求编号无效')
        previous = c.execute('SELECT * FROM cross_layer_passages WHERE world_id=? AND player_id=? AND idempotency_key=?', (world_id, player_id, idempotency_key)).fetchone()
        if previous:
            if (previous['npc_id'], previous['wonder_id'], previous['direction']) != (npc_id, wonder_id, direction):
                raise ValueError('请求编号已用于另一份通道提议')
            return cls._render(c, previous, at)
        wonder = cls._wonder(c, world_id, wonder_id)
        player = cls._person(c, world_id, player_id, wonder, direction, radius_km=cls.APPROACH_RADIUS_KM)
        npc = cls._person(c, world_id, npc_id, wonder, direction, radius_km=cls.APPROACH_RADIUS_KM)
        if not player['is_player'] or not player['is_pov'] or npc['is_player']:
            raise ValueError('提议必须由玩家本人向当前世界的 NPC 提出')
        if not same_room(player,npc) or great_circle_distance_km(player['longitude'],player['latitude'],npc['longitude'],npc['latitude']) > 0.02:
            raise ValueError('请先实际走到对方附近再商量通道')
        from world_engine.intimacy import IntimacyService
        if IntimacyService.paused(c, world_id, player_id, npc_id):
            raise ValueError('一方已暂停联系')
        if c.execute("SELECT 1 FROM cross_layer_passages WHERE world_id=? AND player_id=? AND npc_id=? AND status IN ('pending','npc_passed') AND expires_at>?", (world_id,player_id,npc_id,to_iso(at))).fetchone():
            raise ValueError('双方已有尚未结束的通道提议')
        pid = str(uuid4())
        event = cls._event(c, player, npc, at, 'social.cross_layer_proposed', '你提出各自通过公共升降通道的邀请，等待对方独立决定。',
                           {'passage_id':pid,'wonder_id':wonder_id,'direction':direction})
        c.execute('INSERT INTO cross_layer_passages VALUES (?,?,?,?,?,?,?,\'pending\',?,?,?,NULL,NULL,NULL,NULL,NULL)',
                  (pid,world_id,player_id,npc_id,wonder_id,direction,idempotency_key,to_iso(at),to_iso(at+timedelta(minutes=120)),event))
        WorldRepository().bump_version(c,world_id)
        return cls._render(c, c.execute('SELECT * FROM cross_layer_passages WHERE id=?', (pid,)).fetchone(), at)

    @classmethod
    def _start_approach(cls,c,row,actor,other,wonder,at,*,key=None,source_event_id=None):
        """哥哥，每个人自己启动真实短距路线，不能把对方坐标改到通道。"""
        if great_circle_distance_km(actor['longitude'],actor['latitude'],wonder['longitude'],wonder['latitude']) <= cls.NODE_RADIUS_KM:
            raise ValueError('本人已经到达公共通道节点')
        from world_engine.movement import MovementService
        movement=MovementService().start(c,world_id=row['world_id'],character_id=actor['id'],
            destination_longitude=wonder['longitude'],destination_latitude=wonder['latitude'],
            world_time=at,minimum_distance_km=0)
        if movement.estimated_arrival_world > from_iso(row['expires_at']):
            raise ValueError('实际路线无法在原邀请期限内到达通道')
        payload={'passage_id':row['id'],'wonder_id':row['wonder_id'],'movement_id':movement.id}
        if key is not None:
            payload['idempotency_key']=key
        if source_event_id is not None:
            payload['source_event_id']=source_event_id
        summary=f"{actor['name']}本人选择沿实际路线走向公共通道。"
        event=ActionService._record_event(c,world_id=row['world_id'],tick_id=str(uuid4()),
            occurred_at=at,event_type='social.cross_layer_approach_started',actor_id=actor['id'],
            target_id=None,location_id=actor['current_location_id'],summary=summary,payload=payload)
        ActionService._record_memory(c,world_id=row['world_id'],character_id=actor['id'],
            event_id=event,memory_type='experienced',summary=summary,importance=3)
        WorldRepository().bump_version(c,row['world_id'])
        result=cls._render(c,row,at)
        result.update(approach_event_id=event,movement_id=movement.id,event_id=event,approach_status='applied')
        return result

    @classmethod
    @atomic
    def player_approach(cls,c,world_id,player_id,proposal_id,idempotency_key,at):
        cls._now(c,world_id,at)
        if not isinstance(idempotency_key,str) or not idempotency_key.strip() or len(idempotency_key)>128 or idempotency_key!=idempotency_key.strip():
            raise ValueError('本人路线请求编号无效')
        row=c.execute('SELECT * FROM cross_layer_passages WHERE id=? AND world_id=? AND player_id=?',(proposal_id,world_id,player_id)).fetchone()
        if not row:
            raise ValueError('通道提议不存在')
        prior=c.execute("SELECT * FROM world_events WHERE world_id=? AND actor_id=? AND event_type='social.cross_layer_approach_started' "
            "AND CASE WHEN json_valid(payload_json) THEN json_extract(payload_json,'$.idempotency_key')=? ELSE 0 END LIMIT 1",
            (world_id,player_id,idempotency_key)).fetchone()
        if prior:
            payload=json.loads(prior['payload_json'])
            if payload.get('passage_id')!=proposal_id or payload.get('wonder_id')!=row['wonder_id']:
                raise ValueError('本人路线请求编号已用于另一份通道提议')
            movement=c.execute('SELECT id FROM character_movements WHERE id=? AND world_id=? AND character_id=?',
                               (payload.get('movement_id'),world_id,player_id)).fetchone()
            if not movement:
                raise ValueError('本人原路线回执不可核验')
            result=cls._render(c,row,at)
            result.update(approach_event_id=prior['id'],movement_id=movement['id'],event_id=prior['id'],approach_status='already')
            return result
        if row['status'] not in {'pending','npc_passed'} or from_iso(row['expires_at'])<=at:
            raise ValueError('原通道提议已结束或过期')
        cls._proposal_source(c,row,at)
        wonder=cls._wonder(c,world_id,row['wonder_id'])
        actor=cls._person(c,world_id,player_id,wonder,row['direction'],radius_km=cls.APPROACH_RADIUS_KM)
        other=c.execute('SELECT * FROM characters WHERE id=? AND world_id=?',(row['npc_id'],world_id)).fetchone()
        if not other:
            raise ValueError('原受邀人物不存在')
        from world_engine.intimacy import IntimacyService
        if IntimacyService.paused(c,world_id,player_id,row['npc_id']):
            raise ValueError('一方已暂停联系')
        return cls._start_approach(c,row,actor,other,wonder,at,key=idempotency_key)

    @classmethod
    @atomic
    def npc_decide(cls, c, world_id, npc_id, proposal_id, operation, at, source_event_id):
        cls._now(c, world_id, at)
        row = c.execute('SELECT * FROM cross_layer_passages WHERE id=? AND world_id=? AND npc_id=?', (proposal_id,world_id,npc_id)).fetchone()
        prior=c.execute("SELECT * FROM world_events WHERE world_id=? AND actor_id=? AND event_type='social.cross_layer_approach_started' "
            "AND CASE WHEN json_valid(payload_json) THEN json_extract(payload_json,'$.source_event_id')=? ELSE 0 END LIMIT 1",
            (world_id,npc_id,source_event_id)).fetchone() if source_event_id else None
        if prior:
            payload=json.loads(prior['payload_json'])
            if not row or payload.get('passage_id')!=proposal_id or operation!='approach':
                raise ValueError('一次现场回应不能改成其他通道选择')
            movement=c.execute('SELECT id FROM character_movements WHERE id=? AND world_id=? AND character_id=?',
                               (payload.get('movement_id'),world_id,npc_id)).fetchone()
            if not movement:
                raise ValueError('本人原路线回执不可核验')
            result=cls._render(c,row,at)
            result.update(approach_event_id=prior['id'],movement_id=movement['id'],event_id=prior['id'],approach_status='already')
            return result
        if row and row['decision_source_id']==source_event_id and row['status'] in {'npc_passed','completed','declined'}:
            expected='decline' if row['status']=='declined' else row['direction']
            if operation != expected or not c.execute('SELECT 1 FROM world_events WHERE id=? AND world_id=?',(row['npc_event_id'],world_id)).fetchone():
                raise ValueError('原本人回应与本次选择不一致或回执不存在')
            return cls._render(c,row,at)
        if not row or row['status'] != 'pending' or from_iso(row['expires_at']) <= at:
            raise ValueError('本人没有可回应的有效通道邀请')
        source = c.execute("SELECT rowid,* FROM world_events WHERE id=? AND world_id=? AND event_type='action.socialize' AND actor_id=? AND target_id=?", (source_event_id,world_id,row['player_id'],npc_id)).fetchone()
        proposed = cls._proposal_source(c,row,at)
        if (not source or source['rowid'] <= proposed['rowid'] or from_iso(source['occurred_at']) < from_iso(proposed['occurred_at']) or from_iso(source['occurred_at']) > at
                or not cls._npc_knows_dialogue(c,row,source,at)):
            raise ValueError('本人回应缺少这次邀请之后的现场对话来源')
        wonder = cls._wonder(c, world_id, row['wonder_id'])
        radius = cls.APPROACH_RADIUS_KM if operation in {'approach','decline'} else cls.NODE_RADIUS_KM
        npc = cls._person(c, world_id, npc_id, wonder, row['direction'],radius_km=radius)
        player = cls._person(c, world_id, row['player_id'], wonder, row['direction'],radius_km=radius)
        if not same_room(npc,player) or great_circle_distance_km(npc['longitude'],npc['latitude'],player['longitude'],player['latitude']) > 0.02:
            raise ValueError('双方不在实际交谈距离内')
        from world_engine.intimacy import IntimacyService
        if IntimacyService.paused(c, world_id, player['id'], npc_id):
            raise ValueError('一方已暂停联系')
        if operation == 'approach':
            return cls._start_approach(c,row,npc,player,wonder,at,source_event_id=source_event_id)
        if operation == 'decline':
            event = cls._event(c,npc,player,at,'social.cross_layer_declined','对方婉拒了共同通过通道。',{'passage_id':proposal_id})
            c.execute("UPDATE cross_layer_passages SET status='declined',npc_event_id=?,decision_source_id=? WHERE id=?",(event,source_event_id,proposal_id))
            WorldRepository().bump_version(c,world_id)
        elif operation == row['direction']:
            # 哥哥，先在源层记录两人真实在场的观察；目标层的事件不会凭空给玩家知识。
            witness=cls._event(c,npc,player,at,'social.cross_layer_passage_witnessed',
                '对方本人选择并通过了公共通道，是否再次会合取决于双方后续行动。',
                {'passage_id':proposal_id,'wonder_id':row['wonder_id'],'direction':operation,
                 'decision_source_id':source_event_id})
            result = WonderService.traverse(c, world_id, npc_id, row['wonder_id'], operation, at, reason='本人在现场对话中明确选择通过公共通道。')
            c.execute("UPDATE cross_layer_passages SET status='npc_passed',npc_event_id=?,decision_source_id=?,npc_target_event_id=? WHERE id=?",
                      (witness,source_event_id,result['event_id'],proposal_id))
        else:
            raise ValueError('本人选择必须匹配原通道方向')
        return cls._render(c,c.execute('SELECT * FROM cross_layer_passages WHERE id=?',(proposal_id,)).fetchone(),at)

    @classmethod
    @atomic
    def player_traverse(cls, c, world_id, player_id, proposal_id, at):
        cls._now(c, world_id, at)
        row=c.execute('SELECT * FROM cross_layer_passages WHERE id=? AND world_id=? AND player_id=?',(proposal_id,world_id,player_id)).fetchone()
        if not row:
            raise ValueError('通道提议不存在')
        if row['status']=='completed':
            if not c.execute('SELECT 1 FROM world_events WHERE id=? AND world_id=?',(row['player_event_id'],world_id)).fetchone():
                raise ValueError('本人原通过回执不存在')
            return cls._render(c,row,at)
        if row['status']!='npc_passed' or from_iso(row['expires_at'])<=at:
            raise ValueError('对方尚未通过或本次邀请已过期')
        wonder=cls._wonder(c,world_id,row['wonder_id'])
        cls._npc_passage_witness(c,row,at)
        cls._person(c,world_id,player_id,wonder,row['direction'])
        # 哥哥，上一层的玩家只能依赖对方已经发生且已知的通过事件，不能探查其当前远处位置。
        result=WonderService.traverse(c,world_id,player_id,row['wonder_id'],row['direction'],at,reason='本人另行选择通过公共通道。')
        c.execute("UPDATE cross_layer_passages SET status='completed',player_event_id=? WHERE id=?",(result['event_id'],proposal_id))
        return cls._render(c,c.execute('SELECT * FROM cross_layer_passages WHERE id=?',(proposal_id,)).fetchone(),at)

    @classmethod
    @atomic
    def cancel(cls,c,world_id,player_id,proposal_id,at):
        cls._now(c,world_id,at)
        row=c.execute('SELECT * FROM cross_layer_passages WHERE id=? AND world_id=? AND player_id=?',(proposal_id,world_id,player_id)).fetchone()
        if not row:
            raise ValueError('通道提议不存在')
        if row['status']=='cancelled':
            return cls._render(c,row,at)
        if row['status'] not in {'pending','npc_passed'}:
            raise ValueError('这份通道选择已结束')
        player=c.execute('SELECT * FROM characters WHERE id=? AND world_id=?',(player_id,world_id)).fetchone()
        # 哥哥，撤回是玩家本人的事实；另一层的对方不能被强行记成在场参与者。
        summary='玩家撤回本人通道提议，已通过者保留真实位置。'
        event=ActionService._record_event(c,world_id=world_id,tick_id=str(uuid4()),occurred_at=at,
            event_type='social.cross_layer_cancelled',actor_id=player_id,target_id=None,
            location_id=player['current_location_id'],summary=summary,payload={'passage_id':proposal_id})
        ActionService._record_memory(c,world_id=world_id,character_id=player_id,event_id=event,
            memory_type='experienced',summary=summary,importance=3)
        c.execute("UPDATE cross_layer_passages SET status='cancelled',cancel_event_id=? WHERE id=?",(event,proposal_id))
        WorldRepository().bump_version(c,world_id)
        return cls._render(c,c.execute('SELECT * FROM cross_layer_passages WHERE id=?',(proposal_id,)).fetchone(),at)
