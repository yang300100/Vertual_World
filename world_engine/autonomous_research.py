"""通用、持久化的自主研究执行层；Yuki 不在这里指定任何解法。"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from uuid import uuid4

from world_engine.event_log import record_event
from world_engine.geo import great_circle_distance_km
from world_engine.repository import from_iso, to_iso
from world_engine.research_physics import (
    CONTROL_BOUNDS,
    DEFAULT_CONTROLS,
    controls_changed,
    coupled_response,
    response,
)
from world_engine.research_types import ResearchProgram


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


class ResearchService:
    @staticmethod
    def create_network(c, wid, name, at):
        nid = str(uuid4())
        c.execute(
            "INSERT INTO research_networks(id,world_id,name) VALUES (?,?,?)", (nid, wid, name)
        )
        return nid

    @staticmethod
    def add_device(c, network, location, polity, name, *, physical):
        if not 0 < physical["width"] <= 2 or not -1 <= physical["center"] <= 1:
            raise ValueError("装置材料响应范围无效")
        if not c.execute(
            "SELECT 1 FROM research_networks n JOIN locations l ON l.world_id=n.world_id "
            "WHERE n.id=? AND l.id=?",
            (network, location),
        ).fetchone():
            raise ValueError("装置和地点必须属于同一世界")
        did = str(uuid4())
        c.execute(
            "INSERT INTO research_devices(id,network_id,location_id,polity_key,name,"
            "controls_json,physical_json) VALUES (?,?,?,?,?,?,?)",
            (did, network, location, polity, name, encoded(DEFAULT_CONTROLS), encoded(physical)),
        )
        return did

    @staticmethod
    def enroll(c, actor, device, *, capability):
        if not c.execute(
            "SELECT 1 FROM characters p JOIN research_networks n ON n.world_id=p.world_id "
            "JOIN research_devices d ON d.network_id=n.id WHERE p.id=? AND d.id=? "
            "AND p.is_player=0",
            (actor, device),
        ).fetchone():
            raise ValueError("研究者必须是同世界的 NPC")
        c.execute(
            "INSERT INTO research_people(character_id,device_id,capability) VALUES (?,?,?)",
            (actor, device, capability),
        )

    @staticmethod
    def add_contact(c, sender, recipient):
        if (
            sender == recipient
            or not c.execute(
                "SELECT 1 FROM characters a JOIN characters b ON a.world_id=b.world_id WHERE a.id=? AND b.id=?",
                (sender, recipient),
            ).fetchone()
        ):
            raise ValueError("通信联系人无效")
        c.execute("INSERT OR IGNORE INTO research_contacts VALUES (?,?)", (sender, recipient))

    @staticmethod
    def _actor(c, actor):
        row = c.execute(
            "SELECT p.*,r.device_id,r.capability,r.next_available,r.enabled "
            "FROM characters p JOIN research_people r ON r.character_id=p.id WHERE p.id=?",
            (actor,),
        ).fetchone()
        if not row or row["health"] <= 0 or not row["enabled"]:
            raise ValueError("研究者当前无法行动")
        return row

    @staticmethod
    def _device(c, did):
        return c.execute(
            "SELECT d.*,n.world_id,n.forcing,n.hazard_limit,n.service_min,n.status AS network_status "
            "FROM research_devices d JOIN research_networks n ON n.id=d.network_id WHERE d.id=?",
            (did,),
        ).fetchone()

    @classmethod
    def readings(cls, c, did, changes=None, *, isolated=False):
        device = cls._device(c, did)
        controls = json.loads(device["controls_json"])
        if changes:
            controls = controls_changed(controls, changes)
        if isolated:
            return {
                "settings": controls,
                **response(controls, json.loads(device["physical_json"]), device["forcing"]),
            }
        peers = c.execute(
            "SELECT * FROM research_devices WHERE network_id=?", (device["network_id"],)
        ).fetchall()
        background = 0.8 * max(
            response(
                json.loads(d["controls_json"]), json.loads(d["physical_json"]), device["forcing"]
            )["local_noise"]
            for d in peers
        )
        return {
            "settings": controls,
            **coupled_response(
                controls,
                json.loads(device["physical_json"]),
                device["forcing"],
                background,
                device["hazard_limit"],
            ),
        }

    @classmethod
    def _event(cls, c, actor, device, at, kind, summary, payload):
        event = record_event(
            c,
            world_id=device["world_id"],
            tick_id=str(uuid4()),
            occurred_at=at,
            event_type=f"research.{kind}",
            actor_id=actor,
            location_id=device["location_id"],
            summary=summary,
            payload=payload,
            importance="routine",
        )
        return event

    @classmethod
    def disturb(cls, c, network, *, forcing, at, description):
        if not 0 < forcing <= 100:
            raise ValueError("环境激励必须有限且为正")
        row = c.execute("SELECT * FROM research_networks WHERE id=?", (network,)).fetchone()
        if not row or row["status"] != "quiet":
            raise ValueError("只能一次性设置新的初始扰动")
        c.execute(
            "UPDATE research_networks SET forcing=?,status='active',started_at=? WHERE id=?",
            (forcing, to_iso(at), network),
        )
        for p in c.execute(
            "SELECT r.character_id,r.device_id FROM research_people r "
            "JOIN research_devices d ON d.id=r.device_id WHERE d.network_id=?",
            (network,),
        ):
            device = cls._device(c, p["device_id"])
            cls._event(
                c,
                p["character_id"],
                device,
                at,
                "observed",
                description,
                {
                    "device_id": device["id"],
                    "reading": cls.readings(c, device["id"]),
                    "method": "亲眼目击本地既有装置的仪表与供水表现",
                },
            )

    @staticmethod
    def duration(command):
        return (
            len(command.settings)
            if command.op == "experiment"
            else command.hours
            if command.op == "wait"
            else 1
        )

    @classmethod
    def _start_work(cls, c, row, at):
        from world_engine.life import LifeActivityService

        actor = cls._actor(c, row["character_id"])
        if actor["satiety"] <= 30 or actor["energy"] <= 25 or actor["health"] < 40:
            return False
        if not 6 <= at.hour < 22 or LifeActivityService.running(c, actor["id"]):
            return False
        device = cls._device(c, actor["device_id"])
        if (actor["current_location_id"] or actor["location_id"]) != device["location_id"] or actor[
            "current_room_id"
        ]:
            c.execute(
                "UPDATE research_programs SET status='blocked',updated_at=? WHERE id=?",
                (to_iso(at), row["id"]),
            )
            cls._event(
                c,
                actor["id"],
                device,
                at,
                "blocked",
                "人物不在装置可操作的现场，研究暂缓",
                {"program_id": row["id"]},
            )
            return False
        if c.execute(
            "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",
            (actor["id"],),
        ).fetchone():
            return False
        if c.execute(
            "SELECT 1 FROM research_programs p JOIN research_people r ON r.character_id=p.character_id "
            "JOIN character_life_activities a ON a.id=p.activity_id WHERE r.device_id=? AND a.status='running' AND p.status='active'",
            (device["id"],),
        ).fetchone():
            return False
        command = ResearchProgram.model_validate_json(row["spec_json"]).commands[row["pc"]]
        work = json.loads(row["work_json"])
        remaining = cls.duration(command) - (
            len(work.get("trials", [])) if command.op == "experiment" else work.get("elapsed", 0)
        )
        hours = min(4, remaining)
        cost = int(hours) if command.op == "experiment" else 1 if command.op == "configure" else 0
        if device["reserve"] < cost:
            c.execute("UPDATE research_programs SET status='blocked' WHERE id=?", (row["id"],))
            cls._event(
                c,
                actor["id"],
                device,
                at,
                "blocked",
                "当前储备不足，无法开始下一段操作",
                {"program_id": row["id"]},
            )
            return False
        activity = LifeActivityService.start(
            c, actor, at, "wait" if command.op == "wait" else "research", round(hours * 60)
        )
        c.execute("UPDATE research_devices SET reserve=reserve-? WHERE id=?", (cost, device["id"]))
        work.update({"batch_hours": hours, "reserved": cost, "reserve_before": device["reserve"]})
        c.execute(
            "UPDATE research_programs SET activity_id=?,work_json=?,updated_at=? WHERE id=?",
            (activity, encoded(work), to_iso(at), row["id"]),
        )
        c.execute(
            "UPDATE research_people SET next_available=? WHERE character_id=?",
            (to_iso(at + timedelta(hours=hours)), actor["id"]),
        )
        eid = cls._event(
            c,
            actor["id"],
            device,
            at,
            "work_started",
            "开始执行本人采纳程序的一段实际劳动",
            {
                "program_id": row["id"],
                "activity_id": activity,
                "operation": command.op,
                "duration_hours": hours,
                "reserve_before": device["reserve"],
                "reserve_after": device["reserve"] - cost,
            },
        )
        c.execute(
            "UPDATE character_life_activities SET source_event_id=?,reason=? WHERE id=?",
            (eid, "执行人物自行采纳的研究程序", activity),
        )
        return True

    @staticmethod
    def advance_work(c, activity, actor, effective, heartbeat_id):
        valid = c.execute(
            "SELECT 1 FROM research_programs p JOIN research_people r ON r.character_id=p.character_id "
            "WHERE p.activity_id=? AND p.status='active' AND r.enabled=1",
            (activity["id"],),
        ).fetchone()
        if not valid:
            valid = c.execute(
                "SELECT 1 FROM collective_jobs j JOIN collective_programs p ON p.id=j.program_id "
                "WHERE j.activity_id=? AND j.status='working' AND p.status='active'",
                (activity["id"],),
            ).fetchone()
        if not valid:
            valid = c.execute(
                "SELECT 1 FROM collective_work_people p JOIN collective_work_orders o ON o.id=p.order_id "
                "WHERE p.activity_id=? AND o.status='working'",
                (activity["id"],),
            ).fetchone()
        if not valid:
            valid = c.execute(
                "SELECT 1 FROM collective_domain_jobs WHERE activity_id=? AND status='working'",
                (activity['id'],),
            ).fetchone()
        if not valid:
            return False
        elapsed = (effective - from_iso(activity["started_world_time"])).total_seconds() / 3600
        charged = int(elapsed * 3)
        cost = max(0, charged - activity["energy_steps"])
        after = max(0, actor["energy"] - cost)
        c.execute("UPDATE characters SET energy=? WHERE id=?", (after, actor["id"]))
        c.execute(
            "UPDATE character_life_activities SET energy_steps=? WHERE id=?",
            (charged, activity["id"]),
        )
        if cost:
            c.execute(
                "INSERT INTO character_state_updates(id,world_id,heartbeat_id,character_id,world_time_before,"
                "world_time_after,changes_json,cause,created_at) VALUES (?,?,?,?,?,?,?,'research_time_passage',?)",
                (
                    str(uuid4()),
                    actor["world_id"],
                    heartbeat_id,
                    actor["id"],
                    activity["last_processed_world_time"],
                    to_iso(effective),
                    encoded(
                        {
                            "energy": {
                                "before": actor["energy"],
                                "after": after,
                                "delta": after - actor["energy"],
                            }
                        }
                    ),
                    to_iso(effective),
                ),
            )
        return actor["health"] >= 40 and actor["satiety"] > 10 and after > 10

    @classmethod
    def accept_program(
        cls, c, actor_id, spec, at, *, author=None, source_message=None, call_id=None
    ):
        actor = cls._actor(c, actor_id)
        program = ResearchProgram.model_validate(spec)
        for command in program.commands:
            for changes in command.settings:
                controls_changed(DEFAULT_CONTROLS, changes)
        if c.execute(
            "SELECT 1 FROM research_programs WHERE character_id=? AND status='active'", (actor_id,)
        ).fetchone():
            raise ValueError("人物已有尚未完成的程序")
        if (
            source_message
            and not c.execute(
                "SELECT 1 FROM research_messages WHERE id=? AND recipient_id=? AND delivered_event_id IS NOT NULL "
                "AND due_at<=?",
                (source_message, actor_id, to_iso(at)),
            ).fetchone()
        ):
            raise ValueError("人物尚未收到这个研究方法")
        pid, hashed = str(uuid4()), digest(program.model_dump(mode="json"))
        existing = c.execute(
            "SELECT id FROM research_programs WHERE character_id=? AND protocol_hash=?",
            (actor_id, hashed),
        ).fetchone()
        if existing:
            return existing[0]
        c.execute(
            "INSERT INTO research_programs(id,character_id,author_id,spec_json,protocol_hash,status,"
            "created_at,source_message_id,call_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                pid,
                actor_id,
                author or actor_id,
                program.model_dump_json(),
                hashed,
                "active" if program.commands else "complete",
                to_iso(at),
                source_message,
                call_id,
            ),
        )
        cls._event(
            c,
            actor_id,
            cls._device(c, actor["device_id"]),
            at,
            "program_adopted",
            program.reason,
            {
                "program_id": pid,
                "author_id": author or actor_id,
                "source_message_id": source_message,
                "call_id": call_id,
                "commands": program.model_dump(mode="json")["commands"],
            },
        )
        if program.commands:
            row = c.execute("SELECT * FROM research_programs WHERE id=?", (pid,)).fetchone()
            cls._start_work(c, row, at)
        return pid

    @classmethod
    def context(cls, c, actor_id, at):
        actor = cls._actor(c, actor_id)
        device = cls._device(c, actor["device_id"])
        events = c.execute(
            "SELECT e.id,e.event_type,e.summary,e.payload_json FROM world_events e "
            "JOIN character_event_knowledge k ON k.event_id=e.id WHERE k.character_id=? "
            "AND k.learned_world_time<=? AND e.event_type LIKE 'research.%' "
            "ORDER BY e.occurred_at DESC,e.rowid DESC LIMIT 12",
            (actor_id, to_iso(at)),
        ).fetchall()
        contacts = c.execute(
            "SELECT p.id,p.name,p.identity FROM research_contacts r JOIN characters p "
            "ON p.id=r.recipient_id WHERE r.sender_id=?",
            (actor_id,),
        ).fetchall()
        messages = c.execute(
            "SELECT id,sender_id,body,program_json,evidence_json FROM research_messages "
            "WHERE recipient_id=? AND delivered_event_id IS NOT NULL AND due_at<=? "
            "ORDER BY due_at DESC LIMIT 4",
            (actor_id, to_iso(at)),
        ).fetchall()
        return {
            "actor": {
                "id": actor_id,
                "name": actor["name"],
                "identity": actor["identity"],
                "skills": json.loads(actor["skills_json"]),
                "goals": json.loads(actor["goals_json"]),
            },
            "device": {
                "id": device["id"],
                "name": device["name"],
                "controls": json.loads(device["controls_json"]),
                "bounds": CONTROL_BOUNDS,
                "reserve": device["reserve"],
            },
            "public_safety_standard": {
                "hazard_max": device["hazard_limit"],
                "service_min": device["service_min"],
            },
            "observations": [
                {
                    "id": e["id"],
                    "kind": e["event_type"],
                    "text": e["summary"],
                    "evidence": json.loads(e["payload_json"]),
                }
                for e in events
            ],
            "contacts": [dict(p) for p in contacts],
            "messages": [dict(m) for m in messages],
        }

    @classmethod
    def _send(cls, c, actor, device, program_row, command, at):
        contacts = {
            r[0]
            for r in c.execute(
                "SELECT recipient_id FROM research_contacts WHERE sender_id=?", (actor["id"],)
            )
        }
        recipients = contacts if command.recipients == "contacts" else set(command.recipients)
        if not recipients <= contacts:
            raise ValueError("人物只能联系既有地址簿中的收件人")
        steps = c.execute(
            "SELECT event_id,result_json,operation FROM research_steps WHERE program_id=?",
            (program_row["id"],),
        ).fetchall()
        evidence = [s["event_id"] for s in steps]
        shared = program_row["spec_json"] if command.share_program else None
        if shared and not any(s["operation"] == "experiment" for s in steps):
            raise ValueError("未经本人实际实验的方法只能作为文字假说传递")
        sent = []
        source = c.execute(
            "SELECT * FROM locations WHERE id=?", (device["location_id"],)
        ).fetchone()
        for recipient in sorted(recipients):
            destination = c.execute(
                "SELECT p.current_location_id,p.location_id,l.* FROM characters p "
                "JOIN research_people r ON r.character_id=p.id JOIN research_devices d "
                "ON d.id=r.device_id JOIN locations l ON l.id=d.location_id WHERE p.id=?",
                (recipient,),
            ).fetchone()
            if not destination:
                continue
            if shared and (
                c.execute(
                    "SELECT 1 FROM research_programs WHERE character_id=? AND protocol_hash=?",
                    (recipient, program_row["protocol_hash"]),
                ).fetchone()
                or c.execute(
                    "SELECT 1 FROM research_messages WHERE recipient_id=? AND protocol_hash=?",
                    (recipient, program_row["protocol_hash"]),
                ).fetchone()
            ):
                continue
            hours = (
                2
                + great_circle_distance_km(
                    source["longitude"],
                    source["latitude"],
                    destination["longitude"],
                    destination["latitude"],
                )
                / 20
            )
            eid = cls._event(
                c,
                actor["id"],
                device,
                at,
                "message_sent",
                command.text,
                {
                    "recipient_id": recipient,
                    "evidence_event_ids": evidence,
                    "shared_program": bool(shared),
                    "travel_hours": hours,
                },
            )
            mid = str(uuid4())
            c.execute(
                "INSERT INTO research_messages(id,network_id,sender_id,recipient_id,body,program_json,"
                "protocol_hash,evidence_json,sent_at,due_at,destination_id,sent_event_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    mid,
                    device["network_id"],
                    actor["id"],
                    recipient,
                    command.text,
                    shared,
                    program_row["protocol_hash"] if shared else None,
                    encoded(evidence),
                    to_iso(at),
                    to_iso(at + timedelta(hours=hours)),
                    destination["id"],
                    eid,
                ),
            )
            sent.append(mid)
        return {"message_ids": sent}

    @classmethod
    def _execute(cls, c, actor, device, program_row, command, at):
        work = json.loads(program_row["work_json"])
        reserve_before = work.get("reserve_before", device["reserve"])
        cost = (
            len(command.settings)
            if command.op == "experiment"
            else 1
            if command.op == "configure"
            else 0
        )
        if "reserved" in work:
            cost = 0
        if reserve_before < cost:
            raise ValueError("实验或操作所需储备已经不足")
        if command.op == "experiment":
            result = {
                "trials": [
                    cls.readings(c, device["id"], changes, isolated=True)
                    for changes in command.settings
                ]
            }
        elif command.op == "configure":
            if command.settings:
                settings = command.settings[0]
            else:
                trials = [
                    trial
                    for result in json.loads(program_row["results_json"])
                    for trial in result.get("trials", [])
                ]
                if not trials:
                    raise ValueError("没有已完成实验，不能凭空选择结果")
                feasible = [t for t in trials if t["nominal_service"] >= command.minimum_service]
                if not feasible:
                    raise ValueError("本次实验没有满足人物指定条件的候选")
                if isinstance(command.select, int):
                    if command.select < 0 or command.select >= len(trials):
                        raise ValueError("实验索引不存在")
                    chosen = trials[command.select]
                    if chosen not in feasible:
                        raise ValueError("选中实验不满足人物指定条件")
                else:
                    chosen = (
                        min(feasible, key=lambda t: t["local_noise"])
                        if command.select == "lowest_noise"
                        else max(feasible, key=lambda t: t["nominal_service"])
                    )
                settings = chosen["settings"]
            changed = controls_changed(json.loads(device["controls_json"]), settings)
            c.execute(
                "UPDATE research_devices SET controls_json=?,updated_at=? WHERE id=?",
                (encoded(changed), to_iso(at), device["id"]),
            )
            result = {"reading": cls.readings(c, device["id"]), "installed": changed}
        elif command.op == "measure":
            result = {"reading": cls.readings(c, device["id"])}
        elif command.op == "send":
            result = cls._send(c, actor, device, program_row, command, at)
        else:
            result = {"waited_hours": command.hours}
        if cost:
            c.execute(
                "UPDATE research_devices SET reserve=reserve-? WHERE id=?", (cost, device["id"])
            )
        return {
            **result,
            "reserve_before": reserve_before,
            "reserve_after": device["reserve"] - cost,
        }

    @classmethod
    def _deliver(cls, c, wid, at):
        for message in c.execute(
            "SELECT m.* FROM research_messages m JOIN research_networks n ON n.id=m.network_id "
            "WHERE n.world_id=? AND m.delivered_event_id IS NULL AND m.due_at<=?",
            (wid, to_iso(at)),
        ).fetchall():
            if message["failed_event_id"]:
                continue
            actor = c.execute(
                "SELECT * FROM characters WHERE id=? AND health>0", (message["recipient_id"],)
            ).fetchone()
            wrong_address = (
                actor
                and (actor["current_location_id"] or actor["location_id"])
                != message["destination_id"]
            )
            if not actor or (
                wrong_address and at > from_iso(message["due_at"]) + timedelta(hours=24)
            ):
                eid = record_event(
                    c,
                    world_id=wid,
                    tick_id=str(uuid4()),
                    occurred_at=at,
                    event_type="research.message_undeliverable",
                    summary="驿件在既定地址无法交付，结束本次等待",
                    payload={
                        "message_id": message["id"],
                        "reason": "收件人已不具备收件条件或逾期仍未回址",
                    },
                )
                c.execute(
                    "UPDATE research_messages SET failed_event_id=? WHERE id=?",
                    (eid, message["id"]),
                )
                continue
            if wrong_address:
                continue
            device = c.execute(
                "SELECT device_id,enabled FROM research_people WHERE character_id=?", (actor["id"],)
            ).fetchone()
            if not device:
                continue
            eid = cls._event(
                c,
                actor["id"],
                cls._device(c, device[0]),
                at,
                "message_received",
                message["body"],
                {
                    "message_id": message["id"],
                    "sender_id": message["sender_id"],
                    "evidence_event_ids": json.loads(message["evidence_json"]),
                },
            )
            c.execute(
                "UPDATE research_messages SET delivered_event_id=? WHERE id=?", (eid, message["id"])
            )
            for origin in json.loads(message["evidence_json"]):
                if c.execute(
                    "SELECT 1 FROM character_event_knowledge WHERE character_id=? AND event_id=? "
                    "AND learned_world_time<=?",
                    (message["sender_id"], origin, message["sent_at"]),
                ).fetchone():
                    c.execute(
                        "INSERT OR IGNORE INTO character_event_knowledge VALUES (?,?,?,'reported',?,0.9,1,?)",
                        (actor["id"], origin, wid, message["sender_id"], to_iso(at)),
                    )
            # 通用的证据采纳：只有收到实测方法，且本地仍需研究、没有正在执行的程序才自行试验。
            if (
                not device["enabled"]
                or not message["program_json"]
                or c.execute(
                    "SELECT 1 FROM research_programs WHERE character_id=? AND status='active'",
                    (actor["id"],),
                ).fetchone()
            ):
                continue
            measured = cls.readings(c, device[0])
            d = cls._device(c, device[0])
            if (
                measured["local_noise"] <= d["hazard_limit"]
                and measured["nominal_service"] >= d["service_min"]
            ):
                continue
            proposed = ResearchProgram.model_validate_json(message["program_json"])
            # 他国设置不会直接照搬：首先必须存在本地实验，后续才允许按本地实测选参数。
            experiment_seen = False
            grounded = True
            for command in proposed.commands:
                experiment_seen |= command.op == "experiment"
                if command.op == "configure" and (
                    not experiment_seen or command.settings or command.select is None
                ):
                    grounded = False
            if grounded and experiment_seen:
                cls.accept_program(
                    c,
                    actor["id"],
                    proposed.model_dump(mode="json"),
                    at,
                    author=message["sender_id"],
                    source_message=message["id"],
                )

    @classmethod
    def tick(cls, c, wid, at, *, heartbeat_id=None):
        from world_engine.life import LifeActivityService

        for invalid in c.execute(
            "SELECT p.activity_id FROM research_programs p JOIN characters a ON a.id=p.character_id "
            "JOIN research_people r ON r.character_id=a.id WHERE a.world_id=? AND p.status='active' "
            "AND (a.health<=0 OR r.enabled=0) AND p.activity_id IS NOT NULL",
            (wid,),
        ).fetchall():
            activity = c.execute(
                "SELECT * FROM character_life_activities WHERE id=?", (invalid[0],)
            ).fetchone()
            if activity and activity["status"] == "running":
                LifeActivityService.finish(
                    c, activity, at, "interrupted", "人物当前无法继续研究，释放活动和装置"
                )
        # 正常时钟通常已推进活动；独立调用也沿相同活动结算入口，绝不以空等时间充当劳动。
        if heartbeat_id:
            LifeActivityService.advance(c, wid, at, heartbeat_id)
        c.execute(
            "UPDATE research_programs SET status='cancelled' WHERE status='active' AND character_id IN "
            "(SELECT p.id FROM characters p JOIN research_people r ON r.character_id=p.id "
            "WHERE p.world_id=? AND (p.health<=0 OR r.enabled=0))",
            (wid,),
        )
        cls._deliver(c, wid, at)
        rows = c.execute(
            "SELECT p.* FROM research_programs p JOIN research_people r ON r.character_id=p.character_id "
            "JOIN characters a ON a.id=p.character_id WHERE a.world_id=? AND p.status='active' "
            "AND r.enabled=1 AND a.health>0 ORDER BY p.created_at,p.id",
            (wid,),
        ).fetchall()
        for row in rows:
            try:
                actor = cls._actor(c, row["character_id"])
                device = cls._device(c, actor["device_id"])
                if not row["activity_id"]:
                    cls._start_work(c, row, at)
                    continue
                activity = c.execute(
                    "SELECT * FROM character_life_activities WHERE id=?", (row["activity_id"],)
                ).fetchone()
                if activity["status"] == "running":
                    continue
                if activity["status"] != "completed":
                    c.execute(
                        "UPDATE research_programs SET activity_id=NULL WHERE id=?", (row["id"],)
                    )
                    continue
                if (actor["current_location_id"] or actor["location_id"]) != device[
                    "location_id"
                ] or actor["current_room_id"]:
                    raise ValueError("人物不在可操作装置的现场")
                if (
                    c.execute(
                        "SELECT 1 FROM character_life_activities WHERE character_id=? AND status='running'",
                        (actor["id"],),
                    ).fetchone()
                    or c.execute(
                        "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",
                        (actor["id"],),
                    ).fetchone()
                ):
                    continue
                spec = ResearchProgram.model_validate_json(row["spec_json"])
                command = spec.commands[row["pc"]]
                work = json.loads(row["work_json"])
                if command.op == "experiment":
                    offset = len(work.get("trials", []))
                    executing = command.model_copy(
                        update={
                            "settings": command.settings[offset : offset + int(work["batch_hours"])]
                        }
                    )
                else:
                    executing = command
                c.execute("SAVEPOINT research_operation")
                try:
                    result = cls._execute(c, actor, device, row, executing, at)
                except Exception:
                    c.execute("ROLLBACK TO research_operation")
                    c.execute("RELEASE research_operation")
                    raise
                c.execute("RELEASE research_operation")
                if command.op == "experiment":
                    result["trials"] = work.get("trials", []) + result["trials"]
                    complete = len(result["trials"]) == len(command.settings)
                    interim = {"trials": result["trials"]}
                elif command.op == "wait":
                    elapsed = work.get("elapsed", 0) + work["batch_hours"]
                    complete = elapsed >= command.hours
                    result["waited_hours"] = elapsed
                    interim = {"elapsed": elapsed}
                else:
                    complete, interim = True, {}
                result["activity_id"] = activity["id"]
                if not complete:
                    cls._event(
                        c,
                        actor["id"],
                        device,
                        at,
                        "work_chunk_completed",
                        "完成自拟程序的一段真实劳动",
                        {"program_id": row["id"], **result},
                    )
                    c.execute(
                        "UPDATE research_programs SET activity_id=NULL,work_json=? WHERE id=?",
                        (encoded(interim), row["id"]),
                    )
                    next_row = c.execute(
                        "SELECT * FROM research_programs WHERE id=?", (row["id"],)
                    ).fetchone()
                    cls._start_work(c, next_row, at)
                    continue
                eid = cls._event(
                    c,
                    actor["id"],
                    device,
                    at,
                    command.op,
                    f"{actor['name']}完成{command.op}",
                    {"program_id": row["id"], "command": command.model_dump(mode="json"), **result},
                )
                c.execute(
                    "INSERT INTO research_steps VALUES (?,?,?,?,?,?,?,?)",
                    (
                        str(uuid4()),
                        row["id"],
                        command.op,
                        row["pc"],
                        command.model_dump_json(),
                        encoded(result),
                        eid,
                        to_iso(at),
                    ),
                )
                results = json.loads(row["results_json"]) + [result]
                pc = row["pc"] + 1
                c.execute(
                    "UPDATE research_programs SET pc=?,results_json=?,status=?,activity_id=NULL,work_json='{}' WHERE id=?",
                    (
                        pc,
                        encoded(results),
                        "complete" if pc == len(spec.commands) else "active",
                        row["id"],
                    ),
                )
                if pc < len(spec.commands):
                    next_row = c.execute(
                        "SELECT * FROM research_programs WHERE id=?", (row["id"],)
                    ).fetchone()
                    cls._start_work(c, next_row, at)
            except ValueError as exc:
                c.execute("UPDATE research_programs SET status='blocked' WHERE id=?", (row["id"],))
                actor = c.execute(
                    "SELECT device_id FROM research_people WHERE character_id=?",
                    (row["character_id"],),
                ).fetchone()
                cls._event(
                    c,
                    row["character_id"],
                    cls._device(c, actor[0]),
                    at,
                    "blocked",
                    str(exc),
                    {"program_id": row["id"], "command_index": row["pc"]},
                )
        for network in c.execute(
            "SELECT * FROM research_networks WHERE world_id=? AND status='active'", (wid,)
        ).fetchall():
            view = cls.network_view(c, network["id"])
            safe = bool(view["devices"]) and all(
                d["hazard"] <= network["hazard_limit"] and d["service"] >= network["service_min"]
                for d in view["devices"]
            )
            if not safe:
                c.execute(
                    "UPDATE research_networks SET safe_since=NULL WHERE id=?", (network["id"],)
                )
            elif not network["safe_since"]:
                c.execute(
                    "UPDATE research_networks SET safe_since=? WHERE id=?",
                    (to_iso(at), network["id"]),
                )
            elif at >= from_iso(network["safe_since"]) + timedelta(hours=network["stable_hours"]):
                c.execute(
                    "UPDATE research_networks SET status='resolved',resolved_at=? WHERE id=?",
                    (to_iso(at), network["id"]),
                )
                record_event(
                    c,
                    world_id=wid,
                    tick_id=str(uuid4()),
                    occurred_at=at,
                    event_type="research.network_stable",
                    summary="所有装置持续满足既有公共安全与服务标准",
                    payload={
                        "network_id": network["id"],
                        "stable_hours": network["stable_hours"],
                        "devices": view["devices"],
                    },
                )

    @classmethod
    def network_view(cls, c, network):
        row = c.execute("SELECT * FROM research_networks WHERE id=?", (network,)).fetchone()
        devices = c.execute(
            "SELECT id,polity_key,name,reserve FROM research_devices WHERE network_id=? ORDER BY polity_key",
            (network,),
        ).fetchall()
        return {**dict(row), "devices": [{**dict(d), **cls.readings(c, d["id"])} for d in devices]}


# 导出运行器保留便利入口；执行层和模型提示词分别存放，便于核对不存在预定解法。
from world_engine.research_runner import ResearchRunner  # noqa: E402,F401
