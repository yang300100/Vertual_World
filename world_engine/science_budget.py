"""哥哥，新副本只获得总授权中尚未使用的额度。"""

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from world_engine.science_freeze import verify_freeze

TOTAL_LIMITS = {"max_calls": 12, "token_limit": 200000}
AUTHORIZATION_DIRECTORY = Path(__file__).resolve().parents[1] / ".context/science-authorizations"


def authorization_record(authorization):
    if not isinstance(authorization, dict) or set(authorization) != {
        "id",
        "max_calls",
        "token_limit",
    }:
        raise ValueError("新独立预算须显式提供授权编号、调用与token上限")
    if not isinstance(authorization["id"], str) or not re.fullmatch(
        r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", authorization["id"]
    ):
        raise ValueError("独立预算授权编号无效")
    for field in ("max_calls", "token_limit"):
        value = authorization[field]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("独立预算额度须为正整数")
    return dict(authorization)


def authorization_lock(authorization):
    record = authorization_record(authorization)
    AUTHORIZATION_DIRECTORY.mkdir(parents=True, exist_ok=True)
    return AUTHORIZATION_DIRECTORY / (record["id"] + ".lock")


def allocation_state(record):
    """哥哥，独立凭证保留已知分支；汇总文件丢失时不能重新分配满额。"""
    path = AUTHORIZATION_DIRECTORY / (record["id"] + ".json")
    directory = AUTHORIZATION_DIRECTORY / record["id"] / "allocations"
    receipts = [json.loads(p.read_text(encoding="utf-8")) for p in directory.glob("*.json")]
    if not path.exists():
        if receipts:
            raise ValueError("预算汇总缺失但仍有独立分配凭证，禁止重置账目")
        return {"authorization": record, "runs": []}
    body = json.loads(path.read_text(encoding="utf-8"))
    receipt_runs = [r["run"] for r in receipts]
    if (
        body["authorization"] != record
        or any(r["authorization"] != record for r in receipts)
        or set(body["runs"]) != set(receipt_runs)
        or len(body["runs"]) != len(receipt_runs)
        or len(set(body["runs"])) != len(body["runs"])
    ):
        raise ValueError("预算汇总与独立分配凭证不一致，账目存在遗漏或重复")
    return body


def register_allocation(authorization, output, prior_runs):
    record = authorization_record(authorization)
    AUTHORIZATION_DIRECTORY.mkdir(parents=True, exist_ok=True)
    path = AUTHORIZATION_DIRECTORY / (record["id"] + ".json")
    before = allocation_state(record)
    if before["authorization"] != record or set(before["runs"]) != {
        str(Path(p).resolve()) for p in prior_runs
    }:
        raise ValueError("独立预算授权或已登记账目不一致，不能重置或遗漏调用")
    target = str(Path(output).resolve())
    if target in before["runs"]:
        raise ValueError("此运行已分配预算，不能再次准备")
    directory = AUTHORIZATION_DIRECTORY / record["id"] / "allocations"
    directory.mkdir(parents=True, exist_ok=True)
    receipt = directory / (hashlib.sha256(target.encode()).hexdigest() + ".json")
    with receipt.open("x", encoding="utf-8") as handle:
        json.dump({"authorization": record, "run": target}, handle, ensure_ascii=False, indent=2)
    before["runs"].append(target)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(before, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def budget_lineage(prior_runs, world_id, *, authorization=None, current_run=None):
    limits = dict(TOTAL_LIMITS)
    record = None
    if authorization is not None:
        record = authorization_record(authorization)
        limits = {k: record[k] for k in TOTAL_LIMITS}
        path = AUTHORIZATION_DIRECTORY / (record["id"] + ".json")
        registered = allocation_state(record)
        if path.exists():
            expected = set(registered["runs"])
            if current_run is not None:
                target = str(Path(current_run).resolve())
                if target not in expected:
                    raise ValueError("当前运行没有本次预算分配凭证")
                expected.remove(target)
            if registered["authorization"] != record or expected != {
                str(Path(p).resolve()) for p in prior_runs
            }:
                raise ValueError("本次独立预算的已登记账目不完整或授权发生改变")
        elif current_run is not None:
            raise ValueError("本次独立预算分配登记缺失")
    records, ancestors, seen = [], [], set()
    for output in sorted({str(Path(p).resolve()) for p in prior_runs}):
        path = Path(output)
        try:
            info = json.loads((path / "run.json").read_text(encoding="utf-8"))
            sid = info["session_id"]
            if info["world_id"] != world_id:
                raise ValueError("跨轮预算来源必须属于同一世界")
            if sid in seen:
                raise ValueError("跨轮预算来源会话重复")
            seen.add(sid)
            with closing(sqlite3.connect((path / "world.db").as_uri() + "?mode=ro", uri=True)) as c:
                c.row_factory = sqlite3.Row
                c.execute("BEGIN")
                session = c.execute(
                    "SELECT * FROM science_sessions WHERE id=? AND world_id=?", (sid, world_id)
                ).fetchone()
                if session is None or session["status"] != "stopped":
                    raise ValueError("跨轮预算来源尚未停止，不能分配下一轮额度")
                if not verify_freeze(c, sid, archived_run=path):
                    raise ValueError("跨轮预算来源冻结核验失败")
                calls = c.execute(
                    "SELECT * FROM science_model_calls WHERE session_id=? ORDER BY id", (sid,)
                ).fetchall()
                for row in calls:
                    known = (row["input_tokens"] or 0) + (row["output_tokens"] or 0)
                    cost = row["accounted_tokens"]
                    if row["status"] == "reserved" or cost is None or cost < known:
                        raise ValueError("跨轮预算来源有未结算或不完整费用")
                    if row["status"] == "unknown" and cost < row["reserved_tokens"]:
                        raise ValueError("跨轮预算未知费用必须保留全部预留额度")
                frozen = json.loads(session["freeze_json"])
                inherited = frozen["verification"].get("budget_lineage")
                if inherited:
                    if (
                        inherited["total_limits"] != limits
                        or inherited.get("authorization") != record
                    ):
                        raise ValueError("跨轮预算总授权不一致")
                    ancestors.extend(inherited["prior_runs"])
                elif record:
                    raise ValueError("不能把旧授权的费用归入新的独立预算")
                records.append(
                    {
                        "output_path": output,
                        "session_id": sid,
                        "world_id": world_id,
                        "state_hash": json.loads(session["cursor_json"])["state_hash"],
                        "freeze_sha256": hashlib.sha256(
                            session["freeze_json"].encode()
                        ).hexdigest(),
                        "calls": len(calls),
                        "accounted_tokens": sum(r["accounted_tokens"] for r in calls),
                    }
                )
        except (OSError, sqlite3.Error, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("跨轮预算来源缺失或格式错误") from exc
    # 哥哥，同一路径重复也拒绝，不能静默去重掩盖错误。
    if len(records) != len(prior_runs):
        raise ValueError("跨轮预算来源路径重复")
    by_id = {r["session_id"]: r for r in records}
    if any(by_id.get(r["session_id"]) != r for r in ancestors):
        raise ValueError("跨轮预算祖先费用来源缺失或已经改变")
    remaining = {
        "max_calls": limits["max_calls"] - sum(r["calls"] for r in records),
        "token_limit": limits["token_limit"] - sum(r["accounted_tokens"] for r in records),
    }
    if remaining["max_calls"] <= 0 or remaining["token_limit"] <= 0:
        raise ValueError("跨轮预算总授权已用尽，禁止准备新的付费实验")
    result = {"total_limits": limits, "prior_runs": records, "remaining": remaining}
    if record:
        result["authorization"] = record
    return result
