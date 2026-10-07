"""哥哥，阶段程序只执行人物提交的动作，实测与解释分别留证。"""

from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

from world_engine import collective_resources as resources
from world_engine.action_checks import CheckService
from world_engine.actions import ActionService
from world_engine.collective_evidence import known_evidence
from world_engine.collective_types import CollectiveProgram
from world_engine.life import LifeActivityService
from world_engine.material_physics import process
from world_engine.materials import MaterialService as M
from world_engine.materials import encoded, material_transaction
from world_engine.repository import from_iso, to_iso


class CollectiveResearchService:
    @staticmethod
    def _owned_at_site(c, item, actor):
        # 哥哥，现场自有实物能被真实预留；远处、在途及他人背包不会变成本人的材料。
        return bool(
            item is not None
            and item["owner_character_id"] == actor["id"]
            and (
                item["container_type"] == "ground"
                or (
                    item["container_type"] == "character_inventory"
                    and item["container_id"] == actor["id"]
                )
            )
            and resources.accessible(c, item, actor)
        )

    @staticmethod
    def actor(c, actor_id):
        row = c.execute(
            "SELECT a.*,p.lab_location_id FROM characters a JOIN collective_people p "
            "ON p.character_id=a.id WHERE a.id=? AND p.enabled=1",
            (actor_id,),
        ).fetchone()
        if row is None:
            raise ValueError("人物没有登记研究场所")
        return row

    @classmethod
    def record_observation(cls, c, actor_id, at, summary, content, *, source=None):
        actor = cls.actor(c, actor_id)
        eid = ActionService._record_event(
            c,
            world_id=actor["world_id"],
            tick_id="collective:" + str(uuid4()),
            occurred_at=at,
            event_type="collective." + content.get("kind", "observation"),
            actor_id=actor_id,
            target_id=None,
            location_id=actor["current_location_id"] or actor["location_id"],
            summary=summary,
            payload=content,
        )
        oid = str(uuid4())
        c.execute(
            "INSERT INTO collective_observations VALUES (?,?,?,?,?,?,?,?,?)",
            (
                oid,
                actor["world_id"],
                actor_id,
                content.get("kind", "observation"),
                summary,
                encoded(content),
                to_iso(at),
                eid,
                source,
            ),
        )
        return oid

    @classmethod
    def context(cls, c, actor_id, at):
        actor = cls.actor(c, actor_id)
        moving = c.execute(
            "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",
            (actor_id,),
        ).fetchone()
        observations = [
            dict(x)
            for x in c.execute(
                "SELECT id,kind,summary,content_json,world_time FROM collective_observations "
                "WHERE character_id=? ORDER BY world_time,rowid",
                (actor_id,),
            )
        ]
        for row in observations:
            row["content"] = json.loads(row.pop("content_json"))
        batches = [
            dict(x)
            for x in c.execute(
                "SELECT i.id,t.name,i.quantity,i.condition,i.container_type FROM item_instances i "
                "JOIN item_types t ON t.id=i.item_type_id JOIN material_batches b ON b.item_id=i.id "
                "WHERE i.owner_character_id=? AND ((i.container_type='character_inventory' "
                "AND i.container_id=?) OR (i.container_type='ground' AND i.container_id=?))",
                (actor_id, actor_id, actor["current_location_id"] or actor["location_id"]),
            )
        ]
        instruments = [
            dict(x)
            for x in c.execute(
                "SELECT s.*,t.name,i.condition FROM collective_instruments s "
                "JOIN item_instances i ON i.id=s.item_id JOIN item_types t ON t.id=i.item_type_id "
                "WHERE i.owner_character_id=? AND i.condition>0 AND ((i.container_type='character_inventory' "
                "AND i.container_id=?) OR (i.container_type='ground' AND i.container_id=?))",
                (actor_id, actor_id, actor["current_location_id"] or actor["location_id"]),
            )
        ]
        for tool in instruments:
            tool["supplies"] = json.loads(tool.pop("supplies_json"))
        natural_sources = (
            []
            if moving or actor["current_room_id"]
            else [
                dict(x)
                for x in c.execute(
                    "SELECT i.id,t.name,i.quantity,i.condition FROM item_instances i JOIN item_types t ON t.id=i.item_type_id JOIN material_batches b ON b.item_id=i.id WHERE i.world_id=? AND i.owner_character_id=? AND i.container_type='natural_resource' AND i.container_id=?",
                    (
                        actor["world_id"],
                        actor_id,
                        actor["current_location_id"] or actor["location_id"],
                    ),
                )
            ]
        )
        contacts = [
            dict(r)
            for r in c.execute(
                "SELECT a.id,a.name,a.identity,p.lab_location_id,ct.transit_hours FROM collective_contacts ct JOIN characters a ON a.id=ct.recipient_id JOIN collective_people p ON p.character_id=a.id WHERE ct.sender_id=?",
                (actor_id,),
            )
        ]
        apparatus = [
            dict(r)
            for r in c.execute(
                "SELECT s.*,t.name,i.condition FROM collective_apparatus s JOIN item_instances i ON i.id=s.item_id JOIN item_types t ON t.id=i.item_type_id WHERE i.owner_character_id=? AND ((i.container_type='character_inventory' AND i.container_id=?) OR (i.container_type='ground' AND i.container_id=?))",
                (actor_id, actor_id, actor["current_location_id"] or actor["location_id"]),
            )
        ]
        for tool in apparatus:
            for field in ("operations_json", "supplies_json", "stations_json"):
                tool[field.removesuffix("_json")] = json.loads(tool.pop(field))
        regions = [
            dict(r)
            for r in c.execute(
                "SELECT r.id,l.name,r.location_id,r.population,r.daily_nutrition,r.baseline_soil_mass,r.soil_id,r.water_id,r.growth_source_id,r.manager_id FROM collective_regions r JOIN locations l ON l.id=r.location_id WHERE r.world_id=? AND (r.manager_id=? OR EXISTS(SELECT 1 FROM collective_region_permissions p WHERE p.region_id=r.id AND p.actor_id=?))",
                (actor["world_id"], actor_id, actor_id),
            )
        ]
        for region in regions:
            if (
                not moving
                and not actor["current_room_id"]
                and region["location_id"] == (actor["current_location_id"] or actor["location_id"])
            ):
                records = []
                for day in c.execute(
                    "SELECT consumed_at,material_json,shortage FROM collective_food_days WHERE region_id=? AND consumed_at<=? ORDER BY consumed_at",
                    (region["id"], to_iso(at)),
                ):
                    records.append(
                        {
                            "consumed_at": day["consumed_at"],
                            "shortage_observed": day["shortage"] > 0,
                            "issued": [
                                {"batch_id": entry["batch_id"], "quantity": entry["quantity"]}
                                for entry in json.loads(day["material_json"])
                            ],
                        }
                    )
                region["food_account"] = {
                    "status": "observed_here",
                    "stock": [
                        dict(x)
                        for x in c.execute(
                            "SELECT i.id,t.name,i.quantity,i.condition,i.owner_character_id FROM item_instances i JOIN item_types t ON t.id=i.item_type_id WHERE i.world_id=? AND i.container_type='region_food' AND i.container_id=? ORDER BY i.rowid",
                            (actor["world_id"], region["id"]),
                        )
                    ],
                    "issue_records": records,
                    "recorded_deaths": c.execute(
                        "SELECT deaths FROM collective_regions WHERE id=?", (region["id"],)
                    ).fetchone()[0],
                }
            else:
                region["food_account"] = {"status": "unobserved_here"}
            region["permissions"] = (
                ["ration", "plant", "harvest", "land", "water", "gather", "dispose", "maintain"]
                if region["manager_id"] == actor_id
                else [
                    r[0]
                    for r in c.execute(
                        "SELECT operation FROM collective_region_permissions WHERE region_id=? AND actor_id=? ORDER BY operation",
                        (region["id"], actor_id),
                    )
                ]
            )
        crops = [
            dict(r)
            for r in c.execute(
                "SELECT id,region_id,status,planted_at,due_at,harvested_at,food_id,seed_id FROM collective_crops WHERE actor_id=?",
                (actor_id,),
            )
        ]
        for crop in crops:
            location = c.execute(
                "SELECT location_id FROM collective_regions WHERE id=?", (crop["region_id"],)
            ).fetchone()[0]
            if moving or location != (actor["current_location_id"] or actor["location_id"]):
                crop["status"] = "unknown"
        sites = [
            dict(r)
            for r in c.execute(
                "SELECT id,region_id,capacity,liner_type_id,created_at,last_maintained_at,observation_window_hours FROM collective_waste_sites WHERE owner_id=?",
                (actor_id,),
            )
        ]
        routines = [
            dict(r)
            for r in c.execute(
                "SELECT id,spec_json,next_due_at,remaining,status,error FROM collective_recurring_jobs WHERE actor_id=?",
                (actor_id,),
            )
        ]
        own_submissions = []
        for row in c.execute(
            "SELECT m.id,m.status,m.created_at,m.response_json,m.error,p.status program_status,p.pc next_command_index,p.error program_error,p.bindings_json FROM collective_model_calls m LEFT JOIN collective_programs p ON p.call_id=m.id WHERE m.actor_id=? AND m.response_json IS NOT NULL ORDER BY m.rowid",
            (actor_id,),
        ):
            data = dict(row)
            data["submitted_program"] = json.loads(data.pop("response_json"))
            data["actual_result_bindings"] = json.loads(data.pop("bindings_json") or "{}")
            own_submissions.append(data)
        return {
            "actor_id": actor_id,
            "name": actor["name"],
            "identity": actor["identity"],
            "goals": json.loads(actor["goals_json"]),
            "skills": [
                dict(x)
                for x in c.execute(
                    "SELECT skill_name,proficiency FROM character_skill_proficiencies WHERE character_id=?",
                    (actor_id,),
                )
            ],
            "world_time": to_iso(at),
            "energy": actor["energy"],
            "satiety": actor["satiety"],
            "health": actor["health"],
            "location_id": actor["current_location_id"] or actor["location_id"],
            "inventory_capacity": actor["inventory_capacity"],
            "own_submissions": own_submissions,
            "observations": observations,
            "batches": batches,
            "natural_sources": natural_sources,
            "instruments": instruments,
            "apparatus": apparatus,
            "contacts": contacts,
            "regions": regions,
            "crops": crops,
            "waste_sites": sites,
            "routines": routines,
        }

    @classmethod
    def submit(cls, c, actor_id, program, at, call_id=None):
        spec = (
            program
            if isinstance(program, CollectiveProgram)
            else CollectiveProgram.model_validate(program)
        )
        actor = cls.actor(c, actor_id)
        known = known_evidence(c, actor_id, at)
        referenced = {x for h in spec.hypotheses for x in h.evidence_ids}
        prior_results = set()
        for command in spec.commands:
            for eid in command.evidence_ids:
                if eid.startswith("$"):
                    if eid[1:].split(":", 1)[0] not in prior_results:
                        raise ValueError("证据结果引用必须指向本程序已经安排的前置动作")
                else:
                    referenced.add(eid)
            if command.result_key:
                prior_results.add(command.result_key)
        if referenced - known:
            raise ValueError("不能引用本人尚未取得的证据")
        if c.execute(
            "SELECT 1 FROM collective_programs WHERE character_id=? AND status='active'",
            (actor_id,),
        ).fetchone():
            raise ValueError("本人已有正在执行的阶段程序")
        pid = str(uuid4())
        with material_transaction(c):
            c.execute(
                "INSERT INTO collective_programs(id,world_id,character_id,spec_json,created_at,call_id) "
                "VALUES (?,?,?,?,?,?)",
                (pid, actor["world_id"], actor_id, spec.model_dump_json(), to_iso(at), call_id),
            )
            for index, hypothesis in enumerate(spec.hypotheses):
                c.execute(
                    "INSERT INTO collective_hypotheses VALUES (?,?,?,?,?)",
                    (str(uuid4()), pid, index, hypothesis.model_dump_json(), to_iso(at)),
                )
            cls._drive(c, pid, at)
        return pid

    @classmethod
    def _drive(cls, c, pid, at):
        row = c.execute("SELECT * FROM collective_programs WHERE id=?", (pid,)).fetchone()
        spec = CollectiveProgram.model_validate_json(row["spec_json"])
        bindings = json.loads(row["bindings_json"])
        for source in c.execute(
            "SELECT s.*,p.status FROM collective_component_sources s JOIN collective_proposals p ON p.id=s.proposal_id WHERE s.program_id=? ORDER BY s.command_index",
            (pid,),
        ).fetchall():
            original = spec.commands[source["command_index"]]
            if original.op != "produce" or original.participants:
                continue
            if source["status"] in {"proposed", "executing"}:
                return
            if source["status"] != "completed":
                # 哥哥，外层回滚会移除工单反馈，先把本人应知道的实际原因带出事务。
                reasons = [
                    f"{failed['task_key']}：{failed['error']}"
                    for failed in c.execute(
                        "SELECT task_key,json_extract(result_json,'$.error') error FROM collective_work_orders WHERE proposal_id=? AND version=? AND status='blocked' ORDER BY rowid",
                        (source["proposal_id"], source["version"]),
                    )
                    if failed["error"]
                ]
                message = "本人安排的生产未完成，不能取得产物或开始后续处理"
                if reasons:
                    message += "：" + "；".join(reasons)
                raise ValueError(message)
            if original.result_key and isinstance(bindings.get(original.result_key), str):
                output = c.execute(
                    "SELECT outputs_json FROM collective_work_orders WHERE proposal_id=? AND version=? AND task_key='production'",
                    (source["proposal_id"], source["version"]),
                ).fetchone()
                bindings[original.result_key] = json.loads(output[0])
        c.execute(
            "UPDATE collective_programs SET bindings_json=? WHERE id=?", (encoded(bindings), pid)
        )
        if c.execute(
            "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",
            (row["character_id"],),
        ).fetchone():
            return
        if c.execute(
            "SELECT 1 FROM collective_domain_jobs WHERE program_id=? AND status='working'", (pid,)
        ).fetchone():
            return
        if row["pc"] >= len(spec.commands):
            c.execute("UPDATE collective_programs SET status='completed' WHERE id=?", (pid,))
            return
        if c.execute(
            "SELECT 1 FROM collective_jobs WHERE program_id=? AND status IN ('working','incubating')",
            (pid,),
        ).fetchone():
            return
        command = spec.commands[row["pc"]]

        def resolve(value):
            if isinstance(value, str) and value.startswith("$"):
                parts = value[1:].split(":")
                if parts[0] not in bindings:
                    raise ValueError("尚未取得这个程序结果：" + value)
                result = bindings[parts[0]]
                for index in parts[1:]:
                    if (
                        not index.isdigit()
                        or not isinstance(result, list)
                        or int(index) >= len(result)
                    ):
                        raise ValueError("程序结果没有这个产物编号：" + value)
                    result = result[int(index)]
                return result
            if isinstance(value, list):
                return [resolve(v) for v in value]
            if isinstance(value, dict):
                return {k: resolve(v) for k, v in value.items()}
            return value

        command = type(command).model_validate(resolve(command.model_dump(mode="json")))
        known = known_evidence(c, row["character_id"], at)
        if set(command.evidence_ids) - known:
            raise ValueError("动作引用的证据尚未由本人实际取得")
        if command.op in {"experiment", "inspect", "observe", "wait", "rest"}:
            cls._start(c, row, spec, command, at)
        else:
            result = cls._dispatch(c, row["character_id"], spec, command, at, program_id=pid)
            if command.result_key:
                bindings[command.result_key] = result
            c.execute(
                "UPDATE collective_programs SET pc=pc+1,bindings_json=? WHERE id=?",
                (encoded(bindings), pid),
            )
            cls._drive(c, pid, at)

    @classmethod
    def _dispatch(cls, c, actor_id, spec, command, at, program_id=None):
        from world_engine.collective_cooperation import CooperativeService as C
        from world_engine.collective_messages import MessageService

        body = command.model_dump(mode="json")
        body["reason"] = command.text or spec.reason
        if command.op in {"propose", "accept", "revise", "reject", "start"}:
            result = getattr(C, command.op)(c, actor_id, body, at)
            if command.op in {"propose", "revise"} and program_id:
                pid = result if command.op == "propose" else command.proposal_id
                C.record_source(c, pid, program_id)
            return result
        if command.op == "produce":
            actor = cls.actor(c, actor_id)
            task = {
                k: body[k]
                for k in (
                    "batch_id",
                    "quantity",
                    "hours",
                    "operation",
                    "settings",
                    "apparatus_id",
                    "auxiliary_ids",
                    "auxiliary_quantities",
                    "location_id",
                )
            }
            task.update(
                key="production",
                worker_id=actor_id,
                assistants=command.participants,
                purpose=body["reason"],
            )
            proposal = C.propose(
                c,
                actor_id,
                {"reason": body["reason"], "tasks": [task], "evidence_ids": command.evidence_ids},
                at,
            )
            if program_id:
                C.record_source(c, proposal, program_id)
            C.accept(
                c,
                actor_id,
                {
                    "proposal_id": proposal,
                    "version": 1,
                    "task_keys": ["production"],
                    "reason": body["reason"],
                },
                at,
            )
            if not command.participants:
                C.start(c, actor_id, {"proposal_id": proposal, "version": 1}, at)
            return proposal
        if command.op == "send":
            if command.batch_id and len(command.recipients) != 1:
                raise ValueError("同一份实物只能寄给一个接收者")
            return [
                MessageService.send(
                    c,
                    actor_id,
                    recipient,
                    at,
                    text=command.text,
                    evidence_ids=command.evidence_ids,
                    proposal_id=command.proposal_id,
                    version=command.version,
                    batch_id=command.batch_id,
                    quantity=command.quantity,
                )
                for recipient in command.recipients
            ]
        if command.op == "grant_region":
            from world_engine.collective_regions import RegionService

            for recipient in command.recipients:
                RegionService.grant(
                    c,
                    actor_id,
                    command.region_id,
                    recipient,
                    command.permissions,
                    body["reason"],
                    at,
                )
            return command.region_id
        if command.op == "give":
            actor = cls.actor(c, actor_id)
            LifeActivityService.assert_available(c, actor_id)
            if len(command.recipients) != 1 or actor["health"] <= 0:
                raise ValueError("现场交接需要一位实际接收者与可行动的身体")
            recipient = cls.actor(c, command.recipients[0])
            from world_engine.collective_resources import accessible

            item = c.execute(
                "SELECT * FROM item_instances WHERE id=? AND owner_character_id=? AND world_id=?",
                (command.batch_id, actor_id, actor["world_id"]),
            ).fetchone()
            loc = actor["current_location_id"] or actor["location_id"]
            if (
                recipient["health"] <= 0
                or recipient["world_id"] != actor["world_id"]
                or (recipient["current_location_id"] or recipient["location_id"]) != loc
                or recipient["current_room_id"] != actor["current_room_id"]
                or item is None
                or not accessible(c, item, actor)
                or command.quantity > item["quantity"]
            ):
                raise ValueError("交接双方与真实自有材料必须在同一个实际地点")
            if c.execute(
                "SELECT 1 FROM character_movements WHERE character_id IN (?,?) AND status='moving'",
                (actor_id, recipient["id"]),
            ).fetchone():
                raise ValueError("旅行途中不能现场交接")
            if M.state(c, item["id"]) is None:
                if command.quantity != item["quantity"]:
                    raise ValueError("器具交接须保留完整真实实物，不能拆成假器具")
                c.execute(
                    "UPDATE item_instances SET container_type='ground',container_id=?,owner_character_id=? WHERE id=?",
                    (loc, recipient["id"], item["id"]),
                )
                output = item["id"]
            else:
                output = M.move(c, item, command.quantity, "ground", loc, recipient["id"])
            for witness in (actor_id, recipient["id"]):
                cls.record_observation(
                    c,
                    witness,
                    at,
                    "本人目击现场实物交接",
                    {
                        "kind": "handover",
                        "from": actor_id,
                        "to": recipient["id"],
                        "batch_id": output,
                        "quantity": command.quantity,
                        "location_id": loc,
                    },
                )
            return output
        if command.op == "collect":
            actor = cls.actor(c, actor_id)
            LifeActivityService.assert_available(c, actor_id)
            loc = actor["current_location_id"] or actor["location_id"]
            item = c.execute(
                "SELECT i.*,t.stack_limit,t.slot_size FROM item_instances i JOIN item_types t ON t.id=i.item_type_id WHERE i.id=? AND i.owner_character_id=? AND i.container_type='ground' AND i.container_id=?",
                (command.batch_id, actor_id, loc),
            ).fetchone()
            if item is None or actor["current_room_id"]:
                raise ValueError("没有可实际收取的本地自有材料")
            from world_engine.inventory import InventoryService

            InventoryService._capacity(c, actor_id, item, command.quantity, allow_merge=False)
            if M.state(c, item["id"]) is not None:
                return M.move(c, item, command.quantity, "character_inventory", actor_id, actor_id)
            if command.quantity != item["quantity"]:
                raise ValueError("非材料器具须按完整实物收取")
            InventoryService.transfer(c, item, actor_id, command.quantity, preserve_identity=True)
            return item["id"]
        if command.op == "move":
            from world_engine.movement import MovementService

            actor = cls.actor(c, actor_id)
            known = {actor["lab_location_id"], actor["current_location_id"] or actor["location_id"]}
            known.update(
                r[0]
                for r in c.execute(
                    "SELECT p.lab_location_id FROM collective_contacts ct JOIN collective_people p ON p.character_id=ct.recipient_id WHERE ct.sender_id=?",
                    (actor_id,),
                )
            )
            if command.location_id not in known:
                raise ValueError("本人尚不知道这个地点的实际地址")
            destination = c.execute(
                "SELECT longitude,latitude FROM locations WHERE id=? AND world_id=?",
                (command.location_id, actor["world_id"]),
            ).fetchone()
            movement = MovementService().start(
                c,
                world_id=actor["world_id"],
                character_id=actor_id,
                destination_longitude=destination["longitude"],
                destination_latitude=destination["latitude"],
                world_time=at,
            )
            return movement.id
        if command.op in {
            "plant",
            "harvest",
            "ration",
            "land",
            "water",
            "gather",
            "diet",
            "dispose",
            "maintain",
        }:
            from world_engine.collective_production import ProductionService

            return ProductionService.start(c, actor_id, command, at, program_id=program_id)
        raise ValueError("未登记的动作：" + command.op)

    @classmethod
    def _start(cls, c, program, spec, command, at):
        actor = cls.actor(c, program["character_id"])
        if actor["current_room_id"] or (
            command.location_id
            and (actor["current_location_id"] or actor["location_id"]) != command.location_id
        ):
            raise ValueError("请抵达本人实际研究地点")
        if command.op not in {"rest", "wait"} and (
            actor["energy"] < 15 or actor["satiety"] <= 10 or actor["health"] < 40
        ):
            raise ValueError("身体需要先休息或用饭")
        instrument = None
        batch = None
        supplies = []
        auxiliaries = []
        control = None
        apparatus = None
        duration = min(12, command.hours) if command.op in {"wait", "rest"} else 1
        if command.op in {"experiment", "observe"}:
            instrument = c.execute(
                "SELECT s.*,i.condition,i.owner_character_id,i.container_type,i.container_id FROM collective_instruments s "
                "JOIN item_instances i ON i.id=s.item_id WHERE s.item_id=? AND s.world_id=? "
                "AND i.owner_character_id=?",
                (command.instrument_id, actor["world_id"], actor["id"]),
            ).fetchone()
            if (
                instrument is None
                or not cls._owned_at_site(c, instrument, actor)
                or instrument["condition"] <= 0
                or command.method != instrument["method"]
            ):
                raise ValueError("检验器具不在本人手中，或不支持该方法")
            if (
                CheckService.skill(c, actor, instrument["skill_name"])
                < instrument["min_proficiency"]
            ):
                raise ValueError("检验熟练度不足")
            duration = instrument["duration_hours"]
            if duration > 12:
                raise ValueError("连续人工操作不可超过12小时；培养应登记为等待观察")
            batch = c.execute(
                "SELECT * FROM item_instances WHERE id=? AND world_id=? AND owner_character_id=?",
                (command.batch_id, actor["world_id"], actor["id"]),
            ).fetchone()
            if (
                batch is None
                or not cls._owned_at_site(c, batch, actor)
                or M.state(c, batch["id"]) is None
                or batch["quantity"] < command.quantity
            ):
                raise ValueError("没有该批次材料或使用许可")
            cls._measurement(M.state(c, batch["id"]), command.method)
            if command.quantity > instrument["sample_capacity"]:
                raise ValueError("实验超过检验台实际样品容量，应使用生产设备")
            if command.operation:
                if (
                    len(command.auxiliary_ids) != len(set(command.auxiliary_ids))
                    or command.batch_id in command.auxiliary_ids
                ):
                    raise ValueError("辅助材料不能重复或充当同一份样品")
                for iid in command.auxiliary_ids:
                    auxiliary = c.execute(
                        "SELECT * FROM item_instances WHERE id=? AND world_id=? "
                        "AND owner_character_id=?",
                        (iid, actor["world_id"], actor["id"]),
                    ).fetchone()
                    count = command.auxiliary_quantities.get(
                        iid, auxiliary["quantity"] if auxiliary else 0
                    )
                    if (
                        auxiliary is None
                        or not cls._owned_at_site(c, auxiliary, actor)
                        or M.state(c, iid) is None
                        or isinstance(count, bool)
                        or not isinstance(count, int)
                        or not 1 <= count <= auxiliary["quantity"]
                    ):
                        raise ValueError("辅助材料实际库存或许可不足")
                    auxiliaries.append((auxiliary, count))
                if command.operation == "wash" and not auxiliaries:
                    raise ValueError("浸洗需要真实水源，不能从参数凭空造水")
                if command.operation not in {"wash", "mix", "store"}:
                    apparatus = c.execute(
                        "SELECT s.*,i.condition,i.container_type,i.container_id,i.owner_character_id FROM collective_apparatus s JOIN item_instances i "
                        "ON i.id=s.item_id WHERE s.item_id=? AND i.owner_character_id=?",
                        (command.apparatus_id, actor["id"]),
                    ).fetchone()
                    if not cls._owned_at_site(
                        c, apparatus, actor
                    ) or command.operation not in json.loads(apparatus["operations_json"]):
                        raise ValueError("没有支持该处理操作的实际器具")
                    if apparatus["condition"] <= 0 or not resources.accessible(c, apparatus, actor):
                        raise ValueError("处理器具不在实际研究地点或已损坏")
                    state = M.state(c, batch["id"])
                    load = (state.mass + state.water) * command.quantity / batch["quantity"]
                    if (
                        load > apparatus["capacity"]
                        or command.hours < load / apparatus["throughput"]
                    ):
                        raise ValueError("实验处理量超出器具容量或吞吐量")
                    stations = [
                        s
                        for s in json.loads(apparatus["stations_json"])
                        if load >= s.get("minimum_load", 0)
                    ]
                    if len(stations) > 1:
                        raise ValueError("这份处理负荷需要实际多人操作，应提出合作任务")
                    if (
                        stations
                        and CheckService.skill(c, actor, stations[0]["skill_name"])
                        < stations[0]["min_proficiency"]
                    ):
                        raise ValueError("器具操作位置熟练度不足")
                    if (
                        CheckService.skill(c, actor, apparatus["skill_name"])
                        < apparatus["min_proficiency"]
                    ):
                        raise ValueError("处理器具操作熟练度不足")
                    if command.settings.get("power", 1) > apparatus["max_power"]:
                        raise ValueError("供能超过器具的真实能力")
                duration += command.hours
            if command.control_batch_id:
                control = c.execute(
                    "SELECT * FROM item_instances WHERE id=? AND world_id=? "
                    "AND owner_character_id=?",
                    (command.control_batch_id, actor["world_id"], actor["id"]),
                ).fetchone()
                if not cls._owned_at_site(c, control, actor) or M.state(c, control["id"]) is None:
                    raise ValueError("对照材料不存在或没有许可")
                needed = command.quantity * (
                    2 if command.control_batch_id == command.batch_id else 1
                )
                if control["quantity"] < needed:
                    raise ValueError("不足以实际取出处理样品和对照")
                duration += instrument["duration_hours"]
            seen = set()
            for requirement in json.loads(instrument["supplies_json"]):
                tid, count = (
                    requirement["item_type_id"],
                    requirement["quantity"] * (2 if control is not None else 1),
                )
                if (
                    tid in seen
                    or isinstance(count, bool)
                    or not isinstance(count, int)
                    or count < 1
                ):
                    raise ValueError("检验耗材登记不合法")
                seen.add(tid)
                supply = None
                for candidate in c.execute(
                    "SELECT * FROM item_instances WHERE world_id=? "
                    "AND owner_character_id=? "
                    "AND item_type_id=? AND quantity>=? AND id!=? AND id!=? "
                    "AND (spoils_world_time IS NULL OR spoils_world_time>?) ORDER BY quantity DESC",
                    (
                        actor["world_id"],
                        actor["id"],
                        tid,
                        count,
                        batch["id"],
                        instrument["item_id"],
                        to_iso(at),
                    ),
                ):
                    if cls._owned_at_site(c, candidate, actor) and candidate["id"] not in (
                        {s["id"] for s, _ in auxiliaries} | ({control["id"]} if control else set())
                    ):
                        supply = candidate
                        break
                if supply is None:
                    raise ValueError("检验实际耗材不足")
                supplies.append((supply, count))
            if apparatus:
                supplies.extend(
                    resources.supplies(
                        c,
                        actor,
                        json.loads(apparatus["supplies_json"]),
                        at,
                        excluded=[
                            batch["id"],
                            instrument["item_id"],
                            *[s["id"] for s, n in supplies],
                            *[s["id"] for s, n in auxiliaries],
                        ],
                    )
                )
            if command.operation:
                from world_engine.material_physics import fraction

                process(
                    fraction(M.state(c, batch["id"]), command.quantity / batch["quantity"]),
                    command.operation,
                    command.settings,
                    command.hours,
                    auxiliaries=tuple(
                        fraction(M.state(c, s["id"]), n / s["quantity"]) for s, n in auxiliaries
                    ),
                )
        if duration > 12:
            raise ValueError("实验连续劳动超过12小时，应分成可观察的阶段")
        kind = command.op if command.op in {"wait", "rest"} else "research"
        aid = LifeActivityService.start(c, actor, at, kind, max(1, round(duration * 60)))
        jid = str(uuid4())
        sample_id = None
        if batch is not None:
            sample_id = M.move(c, batch, command.quantity, "collective_sample", jid, actor["id"])
            c.execute(
                "UPDATE item_instances SET container_type='collective_instrument',container_id=? WHERE id=?",
                (jid, instrument["item_id"]),
            )
        due = at + timedelta(
            hours=(command.hours if command.op == "wait" else duration)
            + (instrument["incubation_hours"] if instrument else 0)
        )
        data = command.model_dump(mode="json")
        if instrument:
            data["instrument_original_place"] = {
                "container_type": instrument["container_type"],
                "container_id": instrument["container_id"],
            }
        if apparatus:
            data["apparatus_original_place"] = {
                "container_type": apparatus["container_type"],
                "container_id": apparatus["container_id"],
            }
            c.execute(
                "UPDATE item_instances SET container_type='collective_instrument',container_id=? WHERE id=?",
                (jid, apparatus["item_id"]),
            )
        from world_engine.activity_tasks import TaskService

        data["supply_ids"] = [
            TaskService._reserve(c, actor, supply, count, jid) for supply, count in supplies
        ]
        data["auxiliary_samples"] = [
            M.move(c, x, n, "collective_sample", jid, actor["id"]) for x, n in auxiliaries
        ]
        data["control_sample"] = None
        if control is not None:
            actual = c.execute(
                "SELECT * FROM item_instances WHERE id=?", (control["id"],)
            ).fetchone()
            data["control_sample"] = M.move(
                c, actual, command.quantity, "collective_sample", jid, actor["id"]
            )
        data["sample_original_batch_id"] = command.batch_id
        if instrument is not None:
            samples = [sample_id, *([data["control_sample"]] if data["control_sample"] else [])]
            resources.hydrate(
                c,
                samples,
                data["supply_ids"],
                json.loads(instrument["supplies_json"]),
                command.method,
            )
        data["hypothesis"] = (
            spec.hypotheses[command.hypothesis_index].model_dump(mode="json")
            if command.hypothesis_index is not None
            else None
        )
        c.execute(
            "INSERT INTO collective_jobs(id,program_id,command_index,activity_id,spec_json,sample_id,instrument_id,started_at,due_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                jid,
                program["id"],
                program["pc"],
                aid,
                encoded(data),
                sample_id,
                instrument["item_id"] if instrument else None,
                to_iso(at),
                to_iso(due),
            ),
        )

    @staticmethod
    def _measurement(state, method):
        mass = max(0.000001, state.mass + (state.water if state.carrier == "water" else 0))
        values = {
            "extractable_salt": state.mobile / mass,
            "particles": state.particles / mass,
            "moisture": state.water / max(0.000001, state.mass + state.water),
            "germination": state.viability,
            "soil_ph": state.ph,
        }
        if method not in values:
            raise ValueError("登记器具没有可结算的实际测量方法")
        return values[method]

    @classmethod
    def tick(cls, c, world_id, at):
        jobs = c.execute(
            "SELECT j.*,p.character_id FROM collective_jobs j JOIN collective_programs p "
            "ON p.id=j.program_id WHERE p.world_id=? AND j.status IN ('working','incubating')",
            (world_id,),
        ).fetchall()
        for job in jobs:
            activity = c.execute(
                "SELECT * FROM character_life_activities WHERE id=?", (job["activity_id"],)
            ).fetchone()
            command = json.loads(job["spec_json"])
            if activity["status"] == "running":
                if (
                    command.get("operation")
                    and not command.get("processing_applied_hours")
                    and at >= from_iso(job["started_at"]) + timedelta(hours=command["hours"])
                ):
                    cls._apply_processing(c, job, command, command["hours"])
                continue
            if activity["status"] != "completed":
                cls._return_tools(c, job)
                if (
                    command.get("operation")
                    and job["sample_id"]
                    and not command.get("processing_applied_hours")
                ):
                    elapsed = (
                        from_iso(activity["finished_world_time"]) - from_iso(job["started_at"])
                    ).total_seconds() / 3600
                    cls._apply_processing(c, job, command, min(command["hours"], max(0, elapsed)))
                    job = c.execute(
                        "SELECT j.*,p.character_id FROM collective_jobs j JOIN collective_programs p ON p.id=j.program_id WHERE j.id=?",
                        (job["id"],),
                    ).fetchone()
                    command = json.loads(job["spec_json"])
                if job["sample_id"]:
                    sample = c.execute(
                        "SELECT * FROM item_instances WHERE id=?", (job["sample_id"],)
                    ).fetchone()
                    M.move(
                        c,
                        sample,
                        sample["quantity"],
                        "ground",
                        activity["location_id"],
                        job["character_id"],
                    )
                for iid in command.get("supply_ids", []):
                    c.execute(
                        "UPDATE item_instances SET container_type='ground',container_id=? WHERE id=?",
                        (activity["location_id"], iid),
                    )
                for iid in [*command.get("auxiliary_samples", []), command.get("control_sample")]:
                    if iid:
                        c.execute(
                            "UPDATE item_instances SET container_type='ground',container_id=? WHERE id=?",
                            (activity["location_id"], iid),
                        )
                c.execute(
                    "UPDATE collective_jobs SET status='interrupted' WHERE id=?", (job["id"],)
                )
                c.execute(
                    "UPDATE collective_programs SET status='interrupted',error=? WHERE id=?",
                    ("实际劳动中断，样品保留于原地", job["program_id"]),
                )
                cls.record_observation(
                    c,
                    job["character_id"],
                    at,
                    "研究劳动中断，未取得结果",
                    {"kind": "blocked", "job_id": job["id"]},
                )
                continue
            if (
                command.get("operation")
                and job["sample_id"]
                and not command.get("processing_applied_hours")
            ):
                cls._apply_processing(c, job, command, command["hours"])
                job = c.execute(
                    "SELECT j.*,p.character_id FROM collective_jobs j JOIN collective_programs p ON p.id=j.program_id WHERE j.id=?",
                    (job["id"],),
                ).fetchone()
                command = json.loads(job["spec_json"])
            if at < from_iso(job["due_at"]):
                c.execute("UPDATE collective_jobs SET status='incubating' WHERE id=?", (job["id"],))
                continue
            reader = cls.actor(c, job["character_id"])
            if job["sample_id"] and (
                (reader["current_location_id"] or reader["location_id"]) != activity["location_id"]
                or reader["current_room_id"]
                or reader["health"] < 40
                or LifeActivityService.running(c, reader["id"])
                or c.execute(
                    "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",
                    (reader["id"],),
                ).fetchone()
            ):
                continue
            result = {"kind": "observation", "job_id": job["id"]}
            if job["sample_id"]:
                if command.get("operation") and not command.get("processing_applied_hours"):
                    cls._apply_processing(c, job, command, command["hours"])
                    job = c.execute(
                        "SELECT j.*,p.character_id FROM collective_jobs j JOIN collective_programs p ON p.id=j.program_id WHERE j.id=?",
                        (job["id"],),
                    ).fetchone()
                    command = json.loads(job["spec_json"])
                state = M.state(c, job["sample_id"])
                tool = c.execute(
                    "SELECT * FROM collective_instruments WHERE item_id=?", (job["instrument_id"],)
                ).fetchone()
                raw = cls._measurement(state, command["method"])
                value = (
                    None
                    if raw < tool["detection_limit"]
                    else round(raw / tool["precision"]) * tool["precision"]
                )
                hypothesis = command["hypothesis"]
                prediction = next(
                    (
                        p
                        for p in (hypothesis or {}).get("predictions", [])
                        if p["metric"] == command["method"]
                    ),
                    None,
                )
                result.update(
                    kind="measurement",
                    batch_id=command["sample_original_batch_id"],
                    metric=command["method"],
                    value=value,
                    precision=tool["precision"],
                    detection_limit=tool["detection_limit"],
                    label="低于检出限" if value is None else "实际测量",
                    prediction=prediction,
                    prediction_recorded_at=job["started_at"],
                    measured_at=to_iso(at),
                )
                result["outputs"] = command.get("output_ids", [])
                if command.get("control_sample"):
                    control_state = M.state(c, command["control_sample"])
                    control_raw = cls._measurement(control_state, command["method"])
                    result["control"] = {
                        "batch_id": command["control_batch_id"],
                        "value": None
                        if control_raw < tool["detection_limit"]
                        else round(control_raw / tool["precision"]) * tool["precision"],
                    }
                    control_item = c.execute(
                        "SELECT * FROM item_instances WHERE id=?", (command["control_sample"],)
                    ).fetchone()
                    M.consume(c, control_item, control_item["quantity"], "lab-waste:" + job["id"])
                # 先解除实物外键，已消耗样品的来源保留在任务 JSON 与物质账里。
                c.execute("UPDATE collective_jobs SET sample_id=NULL WHERE id=?", (job["id"],))
                sample = c.execute(
                    "SELECT * FROM item_instances WHERE id=?", (job["sample_id"],)
                ).fetchone()
                if command.get("operation"):
                    M.move(
                        c,
                        sample,
                        sample["quantity"],
                        "ground",
                        activity["location_id"],
                        job["character_id"],
                    )
                    result["outputs"] = [sample["id"], *result["outputs"]]
                else:
                    M.consume(c, sample, sample["quantity"], "lab-waste:" + job["id"])
                from world_engine.inventory import InventoryService

                for iid in command.get("supply_ids", []):
                    supply = c.execute("SELECT * FROM item_instances WHERE id=?", (iid,)).fetchone()
                    if supply is not None:
                        InventoryService.consume(
                            c, supply, supply["quantity"], destination="lab-waste:" + job["id"]
                        )
                cls._return_tools(c, job)
                c.execute(
                    "UPDATE item_instances SET condition=MAX(0,condition-1) WHERE id=?",
                    (job["instrument_id"],),
                )
            elif command["op"] == "inspect":
                result["appearance"] = "已完成本地物品与人员现场查看；具体读数须使用实际器具测量"
            else:
                result["elapsed_hours"] = (at - from_iso(job["started_at"])).total_seconds() / 3600
            oid = cls.record_observation(
                c, job["character_id"], at, "本人实际完成一段研究劳动", result
            )
            c.execute(
                "UPDATE collective_jobs SET status='completed',result_json=?,observation_id=? WHERE id=?",
                (encoded(result), oid, job["id"]),
            )
            c.execute("UPDATE collective_programs SET pc=pc+1 WHERE id=?", (job["program_id"],))
            if command.get("result_key"):
                bindings = json.loads(
                    c.execute(
                        "SELECT bindings_json FROM collective_programs WHERE id=?",
                        (job["program_id"],),
                    ).fetchone()[0]
                )
                # 哥哥，纯测量返回真实证据ID；只有做过物理处理才返回实物数组。
                bindings[command["result_key"]] = (
                    result["outputs"] if command.get("operation") else oid
                )
                c.execute(
                    "UPDATE collective_programs SET bindings_json=? WHERE id=?",
                    (encoded(bindings), job["program_id"]),
                )
            try:
                with material_transaction(c):
                    cls._drive(c, job["program_id"], at)
            except ValueError as exc:
                c.execute(
                    "UPDATE collective_programs SET status='blocked',error=? WHERE id=?",
                    (str(exc), job["program_id"]),
                )
                cls.record_observation(
                    c,
                    job["character_id"],
                    at,
                    "阶段程序的下一动作无法执行",
                    {"kind": "blocked", "reason": str(exc)},
                )

    @staticmethod
    def _return_tools(c, job):
        command = json.loads(job["spec_json"])
        location = c.execute(
            "SELECT location_id FROM character_life_activities WHERE id=?", (job["activity_id"],)
        ).fetchone()[0]
        if job["instrument_id"]:
            resources.return_tool(
                c,
                job["instrument_id"],
                command.get(
                    "instrument_original_place",
                    {"container_type": "character_inventory", "container_id": job["character_id"]},
                ),
                location,
            )
        if command.get("apparatus_original_place"):
            place = command["apparatus_original_place"]
            resources.return_tool(c, command["apparatus_id"], place, location)

    @staticmethod
    def _apply_processing(c, job, command, hours):
        if hours <= 0:
            return
        sample = c.execute(
            "SELECT * FROM item_instances WHERE id=?", (job["sample_id"],)
        ).fetchone()
        auxiliaries = command.get("auxiliary_samples", [])
        inputs = [sample["id"], *auxiliaries]
        output_states = process(
            M.state(c, sample["id"]),
            command["operation"],
            command["settings"],
            hours,
            auxiliaries=tuple(M.state(c, iid) for iid in auxiliaries),
        )
        specs = [
            {
                "item_type_id": sample["item_type_id"],
                "quantity": sample["quantity"],
                "state": output_states[0],
            }
        ]
        if len(output_states) > 1:
            waste_type = "collective-waste:" + sample["world_id"]
            c.execute(
                "INSERT OR IGNORE INTO item_types(id,name,category,stack_limit,slot_size,usable) VALUES (?,'处理副产物','material',1000000,1,0)",
                (waste_type,),
            )
            specs += [
                {"item_type_id": waste_type, "quantity": 1, "state": state}
                for state in output_states[1:]
            ]
        c.execute("UPDATE collective_jobs SET sample_id=NULL WHERE id=?", (job["id"],))
        location = c.execute(
            "SELECT location_id FROM character_life_activities WHERE id=?", (job["activity_id"],)
        ).fetchone()[0]
        ids = M.transform(
            c,
            inputs,
            specs,
            container_type="ground",
            container_id=location,
            owner_id=job["character_id"],
        )
        from world_engine.collective_audit import record_processing

        record_processing(
            c, inputs, ids, "research", job["id"], command["operation"], command["settings"], hours
        )
        command["output_ids"] = ids[1:]
        command["auxiliary_samples"] = []
        command["processing_applied_hours"] = hours
        sample = c.execute("SELECT * FROM item_instances WHERE id=?", (ids[0],)).fetchone()
        M.move(c, sample, sample["quantity"], "collective_sample", job["id"], job["character_id"])
        c.execute(
            "UPDATE collective_jobs SET sample_id=?,spec_json=? WHERE id=?",
            (ids[0], encoded(command), job["id"]),
        )
