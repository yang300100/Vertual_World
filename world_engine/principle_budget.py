"""哥哥，后续科研继承旧费用，共用总额度和进程锁，不能换目录刷新预算。"""

import hashlib
import json
import sqlite3
from pathlib import Path


def snapshot(path, session_id):
    path = Path(path).resolve()
    c = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        c.row_factory = sqlite3.Row
        c.execute("BEGIN")
        session = dict(
            c.execute("SELECT * FROM principle_sessions WHERE id=?", (session_id,)).fetchone()
        )
        rows = [
            dict(r)
            for r in c.execute(
                "SELECT id,status,reserved_tokens,accounted_tokens FROM principle_calls "
                "WHERE session_id=? ORDER BY id",
                (session_id,),
            )
        ]
    finally:
        c.close()
    encoded = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    return {
        "path": str(path),
        "session_id": session_id,
        "status": session["status"],
        "calls": len(rows),
        "tokens": sum(r["accounted_tokens"] or 0 for r in rows),
        "reserved": sum(r["reserved_tokens"] for r in rows if r["status"] == "reserved"),
        "digest": hashlib.sha256(encoded).hexdigest(),
        "max_calls": session["max_calls"],
        "token_limit": session["token_limit"],
        "manifest": json.loads(session["manifest_json"]),
    }


def lineage_for(source, registry_root):
    source = Path(source).resolve()
    info = json.loads((source / "run.json").read_text(encoding="utf-8"))
    state = snapshot(source / info.get("database", "world.db"), info["session_id"])
    if state["status"] in {"running", "prepared"} or state["reserved"]:
        raise ValueError("费用来源尚未结束或有在途请求，不能另启付费实验")
    old = state["manifest"].get("budget_lineage")
    if old:
        return old
    registry = Path(registry_root).resolve() / (state["session_id"] + ".json")
    return {
        "registry": str(registry),
        "source": {k: state[k] for k in ("path", "session_id", "digest", "calls", "tokens")},
        "max_calls": state["max_calls"],
        "token_limit": state["token_limit"],
    }


def lock_path(lineage):
    return Path(lineage["registry"]).with_suffix(".lock")


def receipts_directory(lineage):
    return Path(lineage["registry"]).with_suffix("") / "registrations"


def usage(lineage, *, allow_initial=False, expected_run=None):
    source = lineage["source"]
    origin = snapshot(source["path"], source["session_id"])
    if origin["digest"] != source["digest"] or origin["status"] not in {"stopped", "completed"}:
        raise ValueError("继承费用来源被改变，停止新增请求")
    if lineage["max_calls"] > origin["max_calls"] or lineage["token_limit"] > origin["token_limit"]:
        raise ValueError("继承总额度超过原始授权")
    registry = Path(lineage["registry"])
    directory = receipts_directory(lineage)
    receipts = [json.loads(p.read_text(encoding="utf-8")) for p in directory.glob("*.json")]
    if not registry.exists():
        if not allow_initial or receipts or lineage.get("required_runs"):
            raise ValueError("累计预算注册表缺失，禁止重新创建空账目或新增请求")
        paths = []
    else:
        body = json.loads(registry.read_text(encoding="utf-8"))
        if body.get("source") != source:
            raise ValueError("累计预算注册表属于不同费用来源")
        paths = body["runs"]
        keys = {(r["path"], r["session_id"]) for r in paths}
        receipt_keys = {(r["run"]["path"], r["run"]["session_id"]) for r in receipts}
        if keys != receipt_keys or len(keys) != len(paths) or len(receipt_keys) != len(receipts):
            raise ValueError("费用注册表与独立登记凭证不一致，存在缺失或重复来源")
        if any(r.get("source") != source for r in receipts):
            raise ValueError("登记凭证来源不一致")
        for prior in lineage.get("required_runs", []):
            if (prior["path"], prior["session_id"]) not in keys:
                raise ValueError("准备时已知费用分支被遗漏")
        if expected_run is not None and expected_run not in keys:
            raise ValueError("当前科研分支未正确登记到累计预算")
    seen = {(source["path"], source["session_id"])}
    states = [origin]
    for run in paths:
        key = (run["path"], run["session_id"])
        if key in seen:
            raise ValueError("费用注册出现重复来源")
        seen.add(key)
        states.append(snapshot(*key))
    return {
        "calls": sum(s["calls"] for s in states),
        "accounted_tokens": sum(s["tokens"] for s in states),
        "reserved_tokens": sum(s["reserved"] for s in states),
        "max_calls": lineage["max_calls"],
        "token_limit": lineage["token_limit"],
    }


def register(lineage, path, session_id):
    # 哥哥，登记前也核对旧分支；丢失的摘要不能被下一次准备悄悄重建。
    usage(lineage, allow_initial=True)
    registry = Path(lineage["registry"])
    registry.parent.mkdir(parents=True, exist_ok=True)
    body = (
        json.loads(registry.read_text(encoding="utf-8"))
        if registry.exists()
        else {"source": lineage["source"], "runs": []}
    )
    run = {"path": str(Path(path).resolve()), "session_id": session_id}
    if run in body["runs"]:
        raise ValueError("费用注册出现重复来源")
    directory = receipts_directory(lineage)
    directory.mkdir(parents=True, exist_ok=True)
    receipt = directory / (session_id + ".json")
    if receipt.exists():
        raise ValueError("该分支已有独立登记凭证，不能覆盖")
    receipt.write_text(
        json.dumps({"source": lineage["source"], "run": run}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    body["runs"].append(run)
    temporary = registry.with_suffix(".tmp")
    temporary.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(registry)
    return body["runs"]
