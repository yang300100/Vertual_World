"""哥哥，新副本只获得总授权中尚未使用的额度。"""

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from world_engine.science_freeze import verify_freeze

TOTAL_LIMITS = {"max_calls": 12, "token_limit": 200000}


def budget_lineage(prior_runs, world_id):
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
                if not verify_freeze(c, sid):
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
                    if inherited["total_limits"] != TOTAL_LIMITS:
                        raise ValueError("跨轮预算总授权不一致")
                    ancestors.extend(inherited["prior_runs"])
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
        "max_calls": TOTAL_LIMITS["max_calls"] - sum(r["calls"] for r in records),
        "token_limit": TOTAL_LIMITS["token_limit"] - sum(r["accounted_tokens"] for r in records),
    }
    if remaining["max_calls"] <= 0 or remaining["token_limit"] <= 0:
        raise ValueError("跨轮预算总授权已用尽，禁止准备新的付费实验")
    return {"total_limits": dict(TOTAL_LIMITS), "prior_runs": records, "remaining": remaining}
