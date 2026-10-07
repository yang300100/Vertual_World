"""哥哥，报告、提案与样品都要实际送达，通信不会自动给别人安排任务。"""

import json
from datetime import timedelta
from uuid import uuid4

from world_engine.collective_evidence import evidence_packet
from world_engine.collective_research import CollectiveResearchService as R
from world_engine.materials import MaterialService as M
from world_engine.materials import encoded, material_transaction
from world_engine.repository import to_iso


class MessageService:
    @staticmethod
    def _load(c, iid):
        state = M.state(c, iid)
        if state is not None:
            return state.mass + state.water
        item = c.execute(
            "SELECT i.quantity,t.slot_size FROM item_instances i JOIN item_types t ON t.id=i.item_type_id WHERE i.id=?",
            (iid,),
        ).fetchone()
        if item is None:
            raise ValueError("在途实物丢失")
        return item["quantity"] * max(1, item["slot_size"])

    @staticmethod
    def send(
        c,
        sender_id,
        recipient_id,
        at,
        *,
        text,
        evidence_ids=(),
        proposal_id=None,
        version=None,
        batch_id=None,
        quantity=1,
        technology_id=None,
        technology_version=None,
    ):
        sender, recipient = R.actor(c, sender_id), R.actor(c, recipient_id)
        contact = c.execute(
            "SELECT * FROM collective_contacts WHERE sender_id=? AND recipient_id=?",
            (sender_id, recipient_id),
        ).fetchone()
        if sender["world_id"] != recipient["world_id"] or contact is None:
            raise ValueError("没有实际通信渠道")
        if (
            sender["health"] <= 0
            or c.execute(
                "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",
                (sender_id,),
            ).fetchone()
        ):
            raise ValueError("寄出报告须在真实现场，旅行中或死亡人物不能凭空委托邮路")
        origin = sender["current_location_id"] or sender["location_id"]
        destination = recipient["lab_location_id"]
        hours = contact["transit_hours"]
        if origin != sender["lab_location_id"]:
            if (
                origin != (recipient["current_location_id"] or recipient["location_id"])
                or sender["current_room_id"] != recipient["current_room_id"]
            ):
                raise ValueError("本人尚未抵达登记邮路的寄出地点")
            destination = origin
            hours = 0.25
        evidence = []
        for oid in evidence_ids:
            evidence.append(evidence_packet(c, sender_id, oid, at))
        body = {
            "text": text,
            "evidence": evidence,
            "proposal_id": proposal_id,
            "version": version,
            "sender_id": sender_id,
        }
        if technology_id is not None:
            from world_engine.science_knowledge import method_packet
            body["method_packet"] = method_packet(c, sender_id, technology_id, technology_version, at)
        if proposal_id:
            from world_engine.collective_cooperation import CooperativeService

            CooperativeService._known(c, sender_id, proposal_id, version)
            p = c.execute(
                "SELECT spec_json FROM collective_proposal_versions WHERE proposal_id=? AND version=?",
                (proposal_id, version),
            ).fetchone()
            if p is None:
                raise ValueError("该版本不存在")
            body["proposal"] = json.loads(p[0])
        mid = str(uuid4())
        cargo = None
        with material_transaction(c):
            if batch_id:
                item = c.execute(
                    "SELECT * FROM item_instances WHERE id=? AND owner_character_id=? AND container_type='character_inventory' AND container_id=?",
                    (batch_id, sender_id, sender_id),
                ).fetchone()
                if item is None:
                    raise ValueError("没有可实际寄出的自有样品")
                if (
                    isinstance(quantity, bool)
                    or not isinstance(quantity, int)
                    or not 1 <= quantity <= item["quantity"]
                ):
                    raise ValueError("真实运输数量须为现有实物的整数份")
                state = M.state(c, batch_id)
                limit = c.execute(
                    "SELECT max_load FROM collective_route_limits WHERE sender_id=? AND recipient_id=?",
                    (sender_id, recipient_id),
                ).fetchone()
                max_load = limit[0] if limit else 2
                in_flight = sum(
                    MessageService._load(c, r[0])
                    for r in c.execute(
                        "SELECT cargo_id FROM collective_messages WHERE sender_id=? AND recipient_id=? AND delivered_at IS NULL AND cargo_id IS NOT NULL",
                        (sender_id, recipient_id),
                    )
                )
                if (
                    MessageService._load(c, batch_id) * quantity / item["quantity"] + in_flight
                    > max_load + 1e-8
                ):
                    raise ValueError("真实邮路在途载荷超过运力；不能把货仓当作小样寄出")
                if state is None:
                    if quantity != item["quantity"]:
                        raise ValueError("邮寄器具须按完整实物运输")
                    c.execute(
                        "UPDATE item_instances SET container_type='collective_transit',container_id=? WHERE id=?",
                        (mid, batch_id),
                    )
                    cargo = batch_id
                else:
                    cargo = M.move(c, item, quantity, "collective_transit", mid, sender_id)
            c.execute(
                "INSERT INTO collective_messages VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    mid,
                    sender["world_id"],
                    sender_id,
                    recipient_id,
                    encoded(body),
                    to_iso(at),
                    to_iso(at + timedelta(hours=hours)),
                    None,
                    destination,
                    cargo,
                ),
            )
        return mid

    @staticmethod
    def deliver(c, world_id, at):
        rows = c.execute(
            "SELECT * FROM collective_messages WHERE world_id=? AND delivered_at IS NULL AND due_at<=? ORDER BY due_at,rowid",
            (world_id, to_iso(at)),
        ).fetchall()
        for row in rows:
            recipient = R.actor(c, row["recipient_id"])
            if c.execute(
                "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",
                (recipient["id"],),
            ).fetchone():
                continue
            if (recipient["current_location_id"] or recipient["location_id"]) != row[
                "destination_id"
            ] or recipient["health"] <= 0:
                continue
            data = json.loads(row["content_json"])
            with material_transaction(c):
                base = {
                    "kind": "received_report",
                    "message_id": row["id"],
                    "sender_id": row["sender_id"],
                    "text": data["text"],
                    "proposal_id": data.get("proposal_id"),
                    "version": data.get("version"),
                }
                if data.get("proposal"):
                    base["proposal"] = data["proposal"]
                if data["evidence"]:
                    for evidence in data["evidence"]:
                        R.record_observation(
                            c,
                            row["recipient_id"],
                            at,
                            "实际收到同伴的报告与实测记录",
                            {**base, "evidence": evidence},
                            source=evidence["id"],
                        )
                else:
                    R.record_observation(c, row["recipient_id"], at, "实际收到同伴来信", base)
                if row["cargo_id"]:
                    item = c.execute(
                        "SELECT * FROM item_instances WHERE id=?", (row["cargo_id"],)
                    ).fetchone()
                    if item:
                        if M.state(c, item["id"]) is None:
                            c.execute(
                                "UPDATE item_instances SET container_type='ground',container_id=?,owner_character_id=? WHERE id=?",
                                (row["destination_id"], row["recipient_id"], item["id"]),
                            )
                        else:
                            M.move(
                                c,
                                item,
                                item["quantity"],
                                "ground",
                                row["destination_id"],
                                row["recipient_id"],
                            )
                        R.record_observation(
                            c,
                            row["recipient_id"],
                            at,
                            "信件随附的样品已运到本地",
                            {
                                "kind": "cargo_arrived",
                                "batch_id": item["id"],
                                "message_id": row["id"],
                            },
                        )
                # 哥哥，运输完成后实物可以继续加工，历史来源留在到货观察与物质账。
                c.execute(
                    "UPDATE collective_messages SET delivered_at=?,cargo_id=NULL WHERE id=?",
                    (to_iso(at), row["id"]),
                )
                if data.get("method_packet"):
                    from world_engine.science_knowledge import receive_method
                    receive_method(c, row["id"], at)
