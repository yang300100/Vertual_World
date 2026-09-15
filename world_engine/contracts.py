"""长期事务的确定性履约、托管、还款与到期处理。"""

import json
from datetime import timedelta
from uuid import uuid4

from world_engine.geo import great_circle_distance_km
from world_engine.repository import from_iso, to_iso, utc_now

KINDS = {
    "委托": "commission",
    "雇佣": "employment",
    "借贷": "loan",
    "租赁": "lease",
    "约定": "appointment",
    "学习": "learning",
    "住房": "lodging",
    "约定改期": "reschedule_appointment",
}


class ContractError(ValueError):
    pass


class ContractService:
    @staticmethod
    def vehicle_for(connection, world_id, character_id, vehicle_id):
        vehicle = connection.execute(
            "SELECT * FROM vehicles WHERE id=? AND world_id=? AND is_available=1",
            (vehicle_id, world_id),
        ).fetchone()
        if vehicle is None:
            return None
        lease = connection.execute(
            "SELECT * FROM contract_fulfillments WHERE asset_id=? AND status='active'",
            (vehicle_id,),
        ).fetchone()
        if lease:
            now = connection.execute(
                'SELECT "current_time" FROM worlds WHERE id=?', (world_id,)
            ).fetchone()[0]
            return (
                vehicle
                if lease["requester_id"] == character_id
                and from_iso(now) < from_iso(lease["due_world_time"])
                else None
            )
        return vehicle if vehicle["owner_character_id"] == character_id else None

    @staticmethod
    def normalize(kind, terms):
        kind = KINDS.get(kind, kind)
        result = dict(terms)
        for key, default, maximum in (
            ("payment", result.get("amount", 0), 1_000_000),
            ("duration_days", 7, 3650),
            ("work_units", 1, 1000),
        ):
            value = result.get(key, default)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
                raise ContractError(f"{key} 必须是范围内的整数")
            result[key] = value
        if result["duration_days"] < 1 or result["work_units"] < 1:
            raise ContractError("期限和工作次数必须大于零")
        if kind == "loan":
            result.setdefault("direction", "borrow")
            if result["direction"] not in {"borrow", "lend"} or result["payment"] <= 0:
                raise ContractError("借贷必须指定正数本金与借入或借出方向")
        if kind == "lease" and not result.get("vehicle_id"):
            raise ContractError("租赁必须指定对方拥有的载具")
        if kind == "lodging" and not result.get("room_id"):
            raise ContractError("住房约定需要明确房间")
        if kind in {"commission", "employment"}:
            if result.get("fulfillment_action") != "work":
                raise ContractError("当前可验证的委托和雇佣须明确选择工作履约")
        return result

    @staticmethod
    def _pay(connection, payer, recipient, amount):
        if amount == 0:
            return
        changed = connection.execute(
            "UPDATE characters SET money=money-? WHERE id=? AND money>=?",
            (amount, payer, amount),
        ).rowcount
        if changed != 1:
            raise ContractError("付款方资金不足，未产生转账")
        if recipient:
            connection.execute(
                "UPDATE characters SET money=money+? WHERE id=?", (amount, recipient)
            )

    @classmethod
    def start(cls, connection, request, terms, world_time):
        kind = KINDS.get(request["operation_type"], request["operation_type"])
        if kind == "learning":
            return None
        terms = cls.normalize(kind, terms)
        from world_engine.schedules import ScheduleError, ScheduleService
        try:
            if kind == "reschedule_appointment":
                return ScheduleService.apply_amendment(connection, request, terms, from_iso(world_time))
            window = ScheduleService.validate(connection, request["world_id"], request["requester_id"], request["recipient_id"], terms, from_iso(world_time)) if kind == "appointment" and terms.get("meeting_world_time") else None
        except ScheduleError as exc:
            raise ContractError(str(exc)) from exc
        player, npc = request["requester_id"], request["recipient_id"]
        amount = terms["payment"]
        escrow = 0
        lender = borrower = asset = None
        due = to_iso(from_iso(world_time) + timedelta(days=terms["duration_days"]))
        if window:
            due = to_iso(window[1])
        if kind == "loan":
            lender, borrower = (npc, player) if terms["direction"] == "borrow" else (player, npc)
            cls._pay(connection, lender, borrower, amount)
        elif kind == "lease":
            asset = terms["vehicle_id"]
            vehicle = connection.execute(
                "SELECT * FROM vehicles WHERE id=? AND world_id=? AND owner_character_id=? "
                "AND is_available=1",
                (asset, request["world_id"], npc),
            ).fetchone()
            in_use = connection.execute(
                "SELECT 1 FROM contract_fulfillments WHERE asset_id=? AND status='active'", (asset,)
            ).fetchone()
            moving = connection.execute(
                "SELECT 1 FROM character_movements WHERE vehicle_id=? AND status='moving'", (asset,)
            ).fetchone()
            if vehicle is None or in_use or moving:
                raise ContractError("载具不存在、不归对方所有或正在使用中")
            cls._pay(connection, player, npc, amount)
        elif kind == "lodging":
            asset=terms["room_id"]
            room=connection.execute("SELECT r.*,p.nightly_rate FROM life_rooms r JOIN room_rental_rates p ON p.room_id=r.id JOIN locations l ON l.id=r.location_id WHERE r.id=? AND r.world_id=? AND r.owner_character_id=? AND l.is_active=1",(asset,request["world_id"],npc)).fetchone()
            if room is None:raise ContractError("该房间没有有效的出租约定或不属于对方")
            if connection.execute("SELECT 1 FROM contract_fulfillments WHERE kind='lodging' AND asset_id=? AND status='active'",(asset,)).fetchone():raise ContractError("房间已被租用")
            if amount<room["nightly_rate"]*terms["duration_days"]:raise ContractError("租金低于登记的当前房价")
            parent=room["parent_room_id"]
            while parent:
                outer=connection.execute("SELECT * FROM life_rooms WHERE id=?",(parent,)).fetchone()
                if outer is None or outer["access_policy"]!="public" or outer["door_locked"]:raise ContractError("房间外层通道没有可用的公共通行条件")
                parent=outer["parent_room_id"]
            cls._pay(connection,player,npc,amount)
            terms={**terms,"location_id":room["location_id"]}
        else:
            # 报酬先托管；真实工作或见面证据成立后才交给 NPC。
            cls._pay(connection, player, None, amount)
            escrow = amount
        location_id = (
            terms.get("location_id")
            or connection.execute(
                "SELECT location_id FROM characters WHERE id=?", (npc,)
            ).fetchone()["location_id"]
        )
        if kind in {"commission", "employment"}:
            workplaces = connection.execute(
                "SELECT * FROM locations WHERE world_id=? AND (kind='workplace' OR id IN (SELECT location_id FROM workplace_accounts)) AND is_active=1",
                (request["world_id"],),
            ).fetchall()
            npc_row = connection.execute("SELECT * FROM characters WHERE id=?", (npc,)).fetchone()
            if not workplaces:
                raise ContractError("当前世界没有可用工作场所")
            if not terms.get("location_id"):
                location_id = min(
                    workplaces,
                    key=lambda row: great_circle_distance_km(
                        npc_row["longitude"], npc_row["latitude"], row["longitude"], row["latitude"]
                    ),
                )["id"]
            if location_id not in {row["id"] for row in workplaces}:
                raise ContractError("工作履约地点必须是有效工作场所")
        if (
            connection.execute(
                "SELECT 1 FROM locations WHERE id=? AND world_id=?",
                (location_id, request["world_id"]),
            ).fetchone()
            is None
        ):
            raise ContractError("履约地点不属于本世界")
        connection.execute(
            """INSERT INTO contract_fulfillments(request_id,world_id,kind,status,
               requester_id,recipient_id,lender_id,borrower_id,principal,escrow,
               asset_id,location_id,required_units,started_world_time,due_world_time,created_at)
               VALUES (?,?,?,'active',?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                request["id"],
                request["world_id"],
                kind,
                player,
                npc,
                lender,
                borrower,
                amount if kind == "loan" else 0,
                escrow,
                asset,
                location_id,
                terms["work_units"],
                world_time,
                due,
                to_iso(utc_now()),
            ),
        )
        if window:
            connection.execute("INSERT INTO appointment_windows VALUES (?,?,?,?,1)", (request["id"],request["world_id"],to_iso(window[0]),to_iso(window[1])))
        return due

    @staticmethod
    def record(connection, contract, status, world_time):
        event_id = str(uuid4())
        labels = {
            "completed": "已履约",
            "overdue": "已逾期",
            "expired": "已到期",
            "cancelled": "已结清并结束",
        }
        summary = f"长期事务{labels[status]}。"
        connection.execute(
            """INSERT INTO world_events(id,world_id,tick_id,occurred_at,event_type,
               actor_id,target_id,summary,payload_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                event_id,
                contract["world_id"],
                f"contract:{event_id}",
                world_time,
                f"contract.{status}",
                contract["requester_id"],
                contract["recipient_id"],
                summary,
                json.dumps({"request_id": contract["request_id"]}),
                to_iso(utc_now()),
            ),
        )
        for cid in (contract["requester_id"], contract["recipient_id"]):
            connection.execute(
                """INSERT INTO character_memories(id,world_id,character_id,event_id,memory_type,
                   summary,importance,confidence,created_at)
                   VALUES (?,?,?,?,'experienced',?,6,1,?)""",
                (str(uuid4()), contract["world_id"], cid, event_id, summary, to_iso(utc_now())),
            )
        from world_engine.society import SocietyService
        SocietyService.observe_event(connection,event_id)
        connection.execute(
            "UPDATE contract_fulfillments SET status=? WHERE request_id=?",
            (status, contract["request_id"]),
        )
        if status in {"completed", "expired", "cancelled"}:
            todo_status = "done" if status == "completed" else "cancelled"
            connection.execute(
                "UPDATE npc_todos SET status=?,updated_at=? WHERE contract_id=?",
                (todo_status, to_iso(utc_now()), contract["request_id"]),
            )

    @classmethod
    def repay(cls, connection, contract, world_time):
        if contract["kind"] != "loan" or contract["status"] not in {"active", "overdue"}:
            raise ContractError("该事务没有待偿还的借款")
        cls._pay(connection, contract["borrower_id"], contract["lender_id"], contract["principal"])
        connection.execute(
            "UPDATE contract_fulfillments SET principal=0 WHERE request_id=?",
            (contract["request_id"],),
        )
        cls.record(connection, contract, "completed", world_time)

    @classmethod
    def cancel(cls, connection, contract, world_time):
        if contract is None or contract["status"] in {"completed", "expired", "cancelled"}:
            return
        if contract["kind"] == "loan":
            cls.repay(connection, contract, world_time)
            return
        if contract["kind"]=="lodging":
            stored=connection.execute("SELECT 1 FROM item_instances i LEFT JOIN life_fixtures f ON f.id=i.container_id WHERE i.owner_character_id=? AND ((i.container_type='fixture_storage' AND f.room_id=?) OR (i.container_type='room_ground' AND i.container_id=?)) LIMIT 1",(contract["requester_id"],contract["asset_id"],contract["asset_id"])).fetchone()
            if stored:raise ContractError("请先取回租住空间里的个人物品，再结束住房约定")
        if contract["escrow"]:
            connection.execute(
                "UPDATE characters SET money=money+? WHERE id=?",
                (contract["escrow"], contract["requester_id"]),
            )
        if contract["kind"] == "lease":
            cls._release_vehicle(connection, contract)
        connection.execute(
            "UPDATE contract_fulfillments SET escrow=0 WHERE request_id=?",
            (contract["request_id"],),
        )
        cls.record(connection, contract, "cancelled", world_time)

    @staticmethod
    def _release_vehicle(connection, contract):
        # 到期停止租用中的行程，保留当前位置，避免返还导致瞬移。
        connection.execute(
            "UPDATE character_movements SET status='cancelled' WHERE vehicle_id=? "
            "AND status='moving'",
            (contract["asset_id"],),
        )
        connection.execute(
            "UPDATE characters SET active_vehicle_id=NULL, movement_type='land', "
            "movement_speed_kmh=5 WHERE active_vehicle_id=?",
            (contract["asset_id"],),
        )

    @classmethod
    def advance(cls, connection, world_id, world_time):
        changed = 0
        rows = connection.execute(
            "SELECT * FROM contract_fulfillments WHERE world_id=? "
            "AND status IN ('active','overdue')",
            (world_id,),
        ).fetchall()
        for contract in rows:
            window = connection.execute("SELECT * FROM appointment_windows WHERE contract_id=?",(contract["request_id"],)).fetchone()
            if contract["kind"] in {"commission", "employment", "appointment"}:
                desired = "action.socialize" if contract["kind"] == "appointment" else "action.work"
                events = connection.execute(
                    """SELECT * FROM world_events WHERE world_id=?
                       AND (actor_id=? OR target_id=?) AND event_type=?
                       AND location_id=? AND created_at>=? AND occurred_at<=?
                       AND id NOT IN (SELECT event_id FROM contract_receipts)
                       ORDER BY created_at,id""",
                    (
                        world_id,
                        contract["recipient_id"],
                        contract["recipient_id"],
                        desired,
                        contract["location_id"],
                        contract["created_at"],
                        contract["due_world_time"],
                    ),
                ).fetchall()
                for event in events:
                    if window and from_iso(event["occurred_at"]) < from_iso(window["starts_world_time"]):
                        continue
                    if window and from_iso(event["occurred_at"]) >= from_iso(window["ends_world_time"]):
                        continue
                    if contract["kind"] == "appointment":
                        if {event["actor_id"], event["target_id"]} != {
                            contract["requester_id"],
                            contract["recipient_id"],
                        }:
                            continue
                    elif event["actor_id"] != contract["recipient_id"] or not json.loads(
                        event["payload_json"]
                    ).get("work_completed"):
                        continue
                    if from_iso(event["occurred_at"]) > from_iso(world_time):
                        continue
                    connection.execute(
                        "INSERT INTO contract_receipts(event_id,request_id) VALUES (?,?)",
                        (event["id"], contract["request_id"]),
                    )
                    count = connection.execute(
                        "SELECT COUNT(*) FROM contract_receipts WHERE request_id=?",
                        (contract["request_id"],),
                    ).fetchone()[0]
                    connection.execute(
                        "UPDATE npc_todos SET status='doing' WHERE contract_id=? AND status='open'",
                        (contract["request_id"],),
                    )
                    if count >= contract["required_units"]:
                        connection.execute(
                            "UPDATE characters SET money=money+? WHERE id=?",
                            (contract["escrow"], contract["recipient_id"]),
                        )
                        connection.execute(
                            "UPDATE contract_fulfillments SET escrow=0 WHERE request_id=?",
                            (contract["request_id"],),
                        )
                        cls.record(connection, contract, "completed", world_time)
                        changed += 1
                        break
                else:
                    count = 0
                if count >= contract["required_units"]:
                    continue
            if from_iso(world_time) < from_iso(contract["due_world_time"]):
                continue
            if contract["kind"]=="lodging":
                cls.record(connection,contract,"expired",world_time)
                changed+=1
                continue
            if contract["kind"] == "loan":
                try:
                    cls.repay(connection, contract, world_time)
                except ContractError:
                    if contract["status"] != "overdue":
                        cls.record(connection, contract, "overdue", world_time)
                        changed += 1
                else:
                    changed += 1
            elif contract["kind"] == "lease":
                cls._release_vehicle(connection, contract)
                cls.record(connection, contract, "expired", world_time)
                changed += 1
            elif contract["status"] != "overdue":
                cls.record(connection, contract, "overdue", world_time)
                changed += 1
        return changed
