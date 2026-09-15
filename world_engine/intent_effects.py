"""将明确的自然语言交易/学习意图转换为受规则约束的存档效果。"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from uuid import uuid4

from world_engine.inventory import InventoryError, InventoryService
from world_engine.repository import to_iso, utc_now


@dataclass(frozen=True)
class IntentEffectResult:
    summary: str
    applied: bool


class IntentEffectService:
    """模型不能直写状态；这里只接受可解析、可验证的显式交易与学习语句。"""

    _PURCHASE = re.compile(r"(?:支付|付)(?P<price>\d+)铜币.*?(?:购买|买下|换取)(?P<item>[\w\u4e00-\u9fff]+)")
    _SELL = re.compile(r"(?:出售|卖出)(?P<item>[\w\u4e00-\u9fff]+).*?(?:获得|收取)(?P<price>\d+)铜币")
    _GIVE = re.compile(r"(?:赠与|送给|归还)(?P<item>[\w\u4e00-\u9fff]+)")
    _LEARN = re.compile(r"(?:向[^，。；\s]{1,60})?学习(?P<skill>[\w\u4e00-\u9fff]+)(?:.*?(?:支付|付)(?P<fee>\d+)铜币)?")
    _PRACTICE = re.compile(r"(?:练习|训练)(?P<skill>[\w\u4e00-\u9fff]+)")
    _REPAIR = re.compile(r"(?:修理|维修)(?P<item>[\w\u4e00-\u9fff]+)")
    _HEAL = re.compile(r"(?:治疗|医治|看病)")

    def apply(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        source_event_id: str,
        actor_id: str,
        target_id: str | None,
        intent: str,
    ) -> list[IntentEffectResult]:
        if not target_id:
            return []
        results: list[IntentEffectResult] = []
        purchase = self._PURCHASE.search(intent)
        if purchase:
            results.append(self._purchase(connection, world_id, source_event_id, actor_id, target_id, purchase))
        sell = self._SELL.search(intent)
        if sell:
            results.append(self._sell(connection, world_id, source_event_id, actor_id, target_id, sell))
        give = self._GIVE.search(intent)
        if give:
            results.append(self._transfer(connection, world_id, source_event_id, actor_id, target_id, give))
        # 学习必须经过 NPC 同意的长期事务确认，不能由一句自然语言即时授予技能。
        practice = self._PRACTICE.search(intent)
        if practice:
            results.append(self._practice(connection, world_id, source_event_id, actor_id, target_id, practice))
        repair = self._REPAIR.search(intent)
        if repair:
            results.append(self._repair(connection, world_id, source_event_id, actor_id, target_id, repair))
        if self._HEAL.search(intent):
            results.append(self._heal(connection, world_id, source_event_id, actor_id, target_id))
        return results

    def _owned_item(self, connection, world_id, owner_id, name):  # type: ignore[no-untyped-def]
        item = InventoryService.find(connection, world_id, owner_id, name)
        return item if InventoryService.owned(item, owner_id) else None

    def _sell(self, connection, world_id, event_id, actor_id, buyer_id, match):  # type: ignore[no-untyped-def]
        item_name, price = match["item"].strip(), int(match["price"])
        seller, buyer = self._participants(connection, world_id, actor_id, buyer_id)
        item = self._owned_item(connection, world_id, actor_id, item_name)
        if item is None or price <= 0 or int(buyer["money"]) < price or not any(x in (buyer["identity"] or "") for x in ("商", "贩", "店")):
            return self._record(connection, world_id, event_id, "sell", actor_id, buyer_id, "rejected", {"item":item_name,"price":price}, "出售条件不成立")
        try:
            profile = connection.execute("SELECT price FROM world_item_profiles WHERE world_id=? AND item_type_id=?", (world_id,item["item_type_id"])).fetchone()
            if profile and price != max(1, profile["price"] // 2):
                return self._record(connection,world_id,event_id,"sell",actor_id,buyer_id,"rejected",{},"报价与登记收购价格不符")
            self._move_item(connection, world_id, event_id, item, buyer_id)
        except InventoryError as exc:
            return self._record(connection, world_id, event_id, "sell", actor_id, buyer_id,
                                "rejected", {}, str(exc))
        connection.execute("UPDATE characters SET money=money+? WHERE id=?", (price, actor_id))
        connection.execute("UPDATE characters SET money=money-? WHERE id=?", (price, buyer_id))
        return self._record(connection, world_id, event_id, "sell", actor_id, buyer_id, "applied", {"item":item_name,"price":price}, f"出售{item_name}，获得{price}铜币")

    def _transfer(self, connection, world_id, event_id, actor_id, target_id, match):  # type: ignore[no-untyped-def]
        item_name=match["item"].strip(); self._participants(connection,world_id,actor_id,target_id); item=self._owned_item(connection,world_id,actor_id,item_name)
        if item is None: return self._record(connection,world_id,event_id,"transfer",actor_id,target_id,"rejected",{"item":item_name},"没有可转交的物品")
        try:
            self._move_item(connection, world_id, event_id, item, target_id)
        except InventoryError as exc:
            return self._record(connection, world_id, event_id, "transfer", actor_id, target_id,
                                "rejected", {}, str(exc))
        return self._record(connection,world_id,event_id,"transfer",actor_id,target_id,"applied",{"item":item_name},f"转交了{item_name}")

    def _practice(self, connection, world_id, event_id, actor_id, target_id, match):  # type: ignore[no-untyped-def]
        skill=match["skill"].strip(); actor,_=self._participants(connection,world_id,actor_id,target_id)
        if skill not in set(json.loads(actor["skills_json"] or "[]")) or int(actor["energy"])<5: return self._record(connection,world_id,event_id,"practice_skill",actor_id,target_id,"rejected",{"skill":skill},"技能或精力条件不满足")
        connection.execute("UPDATE characters SET energy=energy-5 WHERE id=?",(actor_id,)); connection.execute("INSERT INTO character_skill_proficiencies(character_id,world_id,skill_name,proficiency,source_event_id,updated_at) VALUES (?,?,?,5,?,?) ON CONFLICT(character_id,skill_name) DO UPDATE SET proficiency=MIN(100,character_skill_proficiencies.proficiency+5),source_event_id=excluded.source_event_id,updated_at=excluded.updated_at",(actor_id,world_id,skill,event_id,to_iso(utc_now())))
        return self._record(connection,world_id,event_id,"practice_skill",actor_id,target_id,"applied",{"skill":skill},f"练习{skill}，熟练度提升5点")

    def _repair(self, connection, world_id, event_id, actor_id, target_id, match):  # type: ignore[no-untyped-def]
        item_name=match["item"].strip(); actor,smith=self._participants(connection,world_id,actor_id,target_id); item=self._owned_item(connection,world_id,actor_id,item_name)
        if item is None or int(item["condition"])>=100 or int(actor["money"])<2 or not any(x in (smith["identity"] or "") for x in ("铁匠","修理","马具")): return self._record(connection,world_id,event_id,"repair",actor_id,target_id,"rejected",{"item":item_name},"修理条件不成立")
        connection.execute("UPDATE characters SET money=money-2 WHERE id=?",(actor_id,)); connection.execute("UPDATE characters SET money=money+2 WHERE id=?",(target_id,)); connection.execute("UPDATE item_instances SET condition=MIN(100,condition+30) WHERE id=?",(item["id"],))
        return self._record(connection,world_id,event_id,"repair",actor_id,target_id,"applied",{"item":item_name,"price":2},f"修理{item_name}，耐久恢复30点")

    def _heal(self, connection, world_id, event_id, actor_id, target_id):  # type: ignore[no-untyped-def]
        actor,healer=self._participants(connection,world_id,actor_id,target_id)
        if int(actor["health"])>=100 or int(actor["money"])<3 or not any(x in (healer["identity"] or "") for x in ("药","医","店")): return self._record(connection,world_id,event_id,"heal_service",actor_id,target_id,"rejected",{},"治疗条件不成立")
        connection.execute("UPDATE characters SET money=money-3,health=MIN(100,health+30) WHERE id=?",(actor_id,)); connection.execute("UPDATE characters SET money=money+3 WHERE id=?",(target_id,))
        return self._record(connection,world_id,event_id,"heal_service",actor_id,target_id,"applied",{"price":3},"接受治疗，生命恢复30点")

    def _purchase(self, connection, world_id, event_id, actor_id, seller_id, match):  # type: ignore[no-untyped-def]
        price, item_name = int(match["price"]), match["item"].strip()
        player, seller = self._participants(connection, world_id, actor_id, seller_id)
        item = self._owned_item(connection, world_id, seller_id, item_name)
        if price <= 0 or item is None or not any(word in (seller["identity"] or "") for word in ("商", "贩", "店")):
            return self._record(connection, world_id, event_id, "purchase", actor_id, seller_id, "rejected", {"item": item_name, "price": price}, "交易条件不成立")
        if int(player["money"]) < price:
            return self._record(connection, world_id, event_id, "purchase", actor_id, seller_id, "rejected", {"item": item_name, "price": price}, "铜币不足")
        profile = connection.execute("SELECT price FROM world_item_profiles WHERE world_id=? AND item_type_id=?", (world_id,item["item_type_id"])).fetchone()
        from world_engine.food import FoodService
        from world_engine.repository import from_iso

        at = from_iso(connection.execute("SELECT occurred_at FROM world_events WHERE id=?", (event_id,)).fetchone()[0])
        if FoodService.spoiled(item, at):
            return self._record(connection,world_id,event_id,"purchase",actor_id,seller_id,"rejected",{},"食物已变质，不能按正常商品购买")
        if profile and price != profile["price"]:
            return self._record(connection,world_id,event_id,"purchase",actor_id,seller_id,"rejected",{},"报价与登记售价不符")
        try:
            self._move_item(connection, world_id, event_id, item, actor_id)
        except InventoryError as exc:
            return self._record(connection, world_id, event_id, "purchase", actor_id, seller_id,
                                "rejected", {}, str(exc))
        connection.execute("UPDATE characters SET money = money - ? WHERE id = ?", (price, actor_id))
        connection.execute("UPDATE characters SET money = money + ? WHERE id = ?", (price, seller_id))
        return self._record(connection, world_id, event_id, "purchase", actor_id, seller_id, "applied", {"item": item_name, "price": price}, f"支付{price}铜币，获得{item_name}")

    @staticmethod
    def _move_item(connection, world_id, event_id, item, recipient_id):
        before = InventoryService.snapshot(connection, world_id)
        InventoryService.transfer(connection, item, recipient_id)
        InventoryService.audit(connection, world_id, event_id, before)

    def _learn(self, connection, world_id, event_id, actor_id, teacher_id, match):  # type: ignore[no-untyped-def]
        skill, fee = match["skill"].strip(), int(match["fee"] or 0)
        player, teacher = self._participants(connection, world_id, actor_id, teacher_id)
        teacher_skills = set(json.loads(teacher["skills_json"] or "[]"))
        if skill not in teacher_skills:
            return self._record(connection, world_id, event_id, "learn_skill", actor_id, teacher_id, "rejected", {"skill": skill, "fee": fee}, "对方不具备该技能")
        if fee < 0 or int(player["money"]) < fee:
            return self._record(connection, world_id, event_id, "learn_skill", actor_id, teacher_id, "rejected", {"skill": skill, "fee": fee}, "铜币不足")
        skills = list(json.loads(player["skills_json"] or "[]"))
        if skill not in skills:
            skills.append(skill)
            connection.execute("UPDATE characters SET skills_json = ? WHERE id = ?", (json.dumps(skills, ensure_ascii=False), actor_id))
        if fee:
            connection.execute("UPDATE characters SET money = money - ? WHERE id = ?", (fee, actor_id))
            connection.execute("UPDATE characters SET money = money + ? WHERE id = ?", (fee, teacher_id))
        connection.execute("""INSERT INTO character_skill_proficiencies(character_id, world_id, skill_name, proficiency, source_event_id, updated_at) VALUES (?, ?, ?, 10, ?, ?) ON CONFLICT(character_id, skill_name) DO UPDATE SET proficiency = MIN(100, character_skill_proficiencies.proficiency + 10), source_event_id = excluded.source_event_id, updated_at = excluded.updated_at""", (actor_id, world_id, skill, event_id, to_iso(utc_now())))
        return self._record(connection, world_id, event_id, "learn_skill", actor_id, teacher_id, "applied", {"skill": skill, "fee": fee}, f"学会{skill}，熟练度提升10点")

    @staticmethod
    def _participants(connection, world_id, player_id, target_id):  # type: ignore[no-untyped-def]
        player = connection.execute("SELECT * FROM characters WHERE id = ? AND world_id = ?", (player_id, world_id)).fetchone()
        target = connection.execute("SELECT * FROM characters WHERE id = ? AND world_id = ?", (target_id, world_id)).fetchone()
        if player is None or target is None:
            raise ValueError("交易或学习对象不存在")
        from world_engine.proximity import same_room

        if not same_room(player, target):
            raise ValueError("双方不在同一室内外空间，不能当面交易")
        return player, target

    @staticmethod
    def _record(connection, world_id, event_id, effect_type, actor_id, target_id, status, payload, summary):  # type: ignore[no-untyped-def]
        connection.execute("INSERT INTO action_effects(id, world_id, source_event_id, effect_type, actor_character_id, target_character_id, status, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (str(uuid4()), world_id, event_id, effect_type, actor_id, target_id, status, json.dumps(payload, ensure_ascii=False), to_iso(utc_now())))
        return IntentEffectResult(summary=summary, applied=status == "applied")
