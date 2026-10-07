"""哥哥，一致性副本与事务封存把合法执行和外部干预分开留证。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from world_engine.database import Database
from world_engine.materials import encoded


def backup_world(source_path, destination_path):
    source, destination = Path(source_path).resolve(), Path(destination_path).resolve()
    if source == destination or destination.exists() or not source.is_file():
        raise ValueError("备份必须从现有来源写入新的独立文件")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # 独占创建目的文件，避免检查之后的竞争覆盖。
    with destination.open("xb"):
        pass
    try:
        with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src:
            with sqlite3.connect(destination) as dst:
                src.backup(dst)
    except BaseException:
        # 哥哥，失败的副本保留，不能误认为已完成备份再覆盖它。
        raise


def _paths(paths):
    return [str(Path(p).resolve()) for p in paths]


def file_manifest(paths):
    result = {}
    for name in paths:
        root = Path(name)
        if not root.exists():
            raise ValueError("冻结文件或目录不存在：" + str(root))
        files = (
            [root]
            if root.is_file()
            else [
                p
                for p in root.rglob("*")
                if p.is_file()
                and p.suffix.lower() in {".py", ".js", ".html", ".css", ".json", ".md", ".toml"}
                and "__pycache__" not in p.parts
            ]
        )
        for p in sorted(files):
            result[str(p.resolve())] = hashlib.sha256(p.read_bytes()).hexdigest()
    return result


def state_digest(c):
    digest = hashlib.sha256()
    schema = c.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE "
        "'sqlite_%' ORDER BY type,name"
    ).fetchall()
    digest.update(encoded([list(r) for r in schema]).encode())
    for table in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE "
        "'sqlite_%' ORDER BY name"
    ):
        name = table[0]
        if name == "science_runtime_seals":
            continue
        fields = [r[1] for r in c.execute('PRAGMA table_info("' + name.replace('"', '""') + '")')]
        if name == "science_sessions":
            fields.remove("cursor_json")
        select = ",".join('"' + f.replace('"', '""') + '"' for f in fields)
        rows = []
        for row in c.execute("SELECT " + select + ' FROM "' + name.replace('"', '""') + '"'):
            rows.append(
                encoded([{"blob_hex": v.hex()} if isinstance(v, bytes) else v for v in row])
            )
        digest.update(encoded([name, sorted(rows)]).encode())
    return digest.hexdigest()


def seal_state(c, session_id, *, reason="正常执行事务"):
    s = c.execute("SELECT * FROM science_sessions WHERE id=?", (session_id,)).fetchone()
    previous = c.execute(
        "SELECT * FROM science_runtime_seals WHERE session_id=? ORDER BY sequence DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    sequence = previous["sequence"] + 1 if previous else 1
    digest = state_digest(c)
    previous_hash = previous["chain_hash"] if previous else ""
    chain = hashlib.sha256(
        encoded([session_id, sequence, digest, previous_hash, reason]).encode()
    ).hexdigest()
    at = c.execute("SELECT w.current_time FROM worlds w WHERE w.id=?", (s["world_id"],)).fetchone()[
        0
    ]
    c.execute(
        "INSERT INTO science_runtime_seals VALUES (?,?,?,?,?,?,?)",
        (session_id, sequence, digest, previous_hash, chain, at, reason),
    )
    c.execute(
        "UPDATE science_sessions SET cursor_json=? WHERE id=?",
        (encoded({"sequence": sequence, "state_hash": digest, "chain_hash": chain}), session_id),
    )


def freeze_session(
    c, session_id, *, source_paths, world_paths, model_config=None, verification=None
):
    s = c.execute("SELECT * FROM science_sessions WHERE id=?", (session_id,)).fetchone()
    if s is None or s["status"] != "prepared" or json.loads(s["freeze_json"]):
        raise ValueError("会话已经冻结或不处于准备状态，不能重新冻结")
    if c.execute("SELECT 1 FROM science_model_calls WHERE session_id=?", (session_id,)).fetchone():
        raise ValueError("付费调用前必须冻结")
    wid = s["world_id"]
    initial = {
        "world_id": wid,
        "state_hash_before_freeze": state_digest(c),
        "characters": [
            dict(r)
            for r in c.execute(
                (
                    "SELECT id,name,identity,skills_json,goals_json,traits_json FROM "
                    "characters WHERE world_id=? ORDER BY id"
                ),
                (wid,),
            )
        ],
        "technologies": c.execute(
            "SELECT COUNT(*) FROM technologies WHERE world_id=?", (wid,)
        ).fetchone()[0],
        "programs": c.execute(
            "SELECT COUNT(*) FROM science_programs WHERE world_id=?", (wid,)
        ).fetchone()[0],
        "memories": [
            dict(r)
            for r in c.execute(
                "SELECT * FROM character_memories WHERE world_id=? ORDER BY id", (wid,)
            )
        ],
        "relationships": [
            dict(r)
            for r in c.execute(
                (
                    "SELECT * FROM relationships WHERE world_id=? ORDER BY "
                    "source_character_id,target_character_id"
                ),
                (wid,),
            )
        ],
        "items": [
            dict(r)
            for r in c.execute("SELECT * FROM item_instances WHERE world_id=? ORDER BY id", (wid,))
        ],
    }
    workshop = c.execute(
        "SELECT seed,rules_json,assets_json FROM science_workshops WHERE world_id=?", (wid,)
    ).fetchone()
    config = {
        k: v
        for k, v in (model_config or {}).items()
        if k
        in {"model", "base_url", "timeout_seconds", "max_retries", "max_output_tokens", "thinking"}
    }
    manifest = {
        "source_paths": _paths(source_paths),
        "world_paths": _paths(world_paths),
        "source_files": file_manifest(source_paths),
        "world_files": file_manifest(world_paths),
        "budget": {k: s[k] for k in ("max_calls", "token_limit", "max_output_tokens")},
        "model_config": config,
        "verification": verification or {},
        "initial": initial,
        "natural_rules": json.loads(workshop["rules_json"]) if workshop else {},
        "workshop_assets": json.loads(workshop["assets_json"]) if workshop else {},
        "seed": workshop["seed"] if workshop else s["seed"],
        "scheduler": (
            "new_non_rejection_evidence_then_uncalled_then_oldest_call_then_actor_id;no_retries"
        ),
        "scope": "有限自然模型中的能力；真实运行不保证突破",
    }
    c.execute(
        "UPDATE science_sessions SET freeze_json=? WHERE id=?", (encoded(manifest), session_id)
    )
    seal_state(c, session_id, reason="首次冻结")
    return manifest


def verify_freeze(c, session_id, *, source_paths=(), world_paths=()):
    s = c.execute("SELECT * FROM science_sessions WHERE id=?", (session_id,)).fetchone()
    if s is None:
        return False
    frozen = json.loads(s["freeze_json"])
    if not frozen:
        return False
    if source_paths and _paths(source_paths) != frozen["source_paths"]:
        return False
    if world_paths and _paths(world_paths) != frozen["world_paths"]:
        return False
    try:
        if (
            file_manifest(frozen["source_paths"]) != frozen["source_files"]
            or file_manifest(frozen["world_paths"]) != frozen["world_files"]
        ):
            return False
    except (OSError, ValueError):
        return False
    if {k: s[k] for k in frozen["budget"]} != frozen["budget"]:
        return False
    cursor = json.loads(s["cursor_json"])
    seal = c.execute(
        "SELECT * FROM science_runtime_seals WHERE session_id=? ORDER BY sequence DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    if (
        not seal
        or cursor.get("state_hash") != seal["state_hash"]
        or cursor.get("chain_hash") != seal["chain_hash"]
        or cursor.get("sequence") != seal["sequence"]
    ):
        return False
    return state_digest(c) == seal["state_hash"]


class FrozenScienceDatabase(Database):
    def __init__(self, path, *, session_id):
        super().__init__(Path(path), legacy_world_defaults=False)
        self.session_id = session_id

    @contextmanager
    def read(self):
        with super().read() as c:
            c.execute("BEGIN")
            if not verify_freeze(c, self.session_id):
                raise RuntimeError("冻结读取检测到外部修改，须停止本轮")
            yield c

    @contextmanager
    def write(self):
        with super().write() as c:
            if not verify_freeze(c, self.session_id):
                c.execute(
                    (
                        "UPDATE science_sessions SET "
                        "status='stopped',stop_reason='冻结检测到外部修改，禁止继续本轮' WHERE id=?"
                    ),
                    (self.session_id,),
                )
                # 哥哥，停机事实须落盘，不能随着后面的异常回滚。
                c.commit()
                raise RuntimeError("冻结执行检测到外部修改，已停止本轮")
            yield c
            seal_state(c, self.session_id)
