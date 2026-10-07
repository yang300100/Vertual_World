"""哥哥，每份合作提案都可被独立质疑、修改、拒绝；收到不代表同意。"""

from __future__ import annotations

import json
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from world_engine.collective_evidence import known_evidence
from world_engine.collective_research import CollectiveResearchService as R
from world_engine.material_physics import CONTROLS
from world_engine.materials import encoded, material_transaction
from world_engine.repository import to_iso


class WorkTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=60)
    worker_id: str
    operation: str
    batch_id: str
    quantity: int = Field(ge=1, le=1000000, strict=True)
    hours: float = Field(default=1, ge=0.25, le=12)
    settings: dict[str, float] = Field(default_factory=dict)
    apparatus_id: str | None = None
    energy_id: str | None = None
    auxiliary_ids: list[str] = Field(default_factory=list, max_length=8)
    auxiliary_quantities: dict[str, int] = Field(default_factory=dict)
    assistants: list[str] = Field(default_factory=list, max_length=8)
    depends_on: list[str] = Field(default_factory=list, max_length=12)
    purpose: str = Field(default="", max_length=2000)
    method: str | None = None
    instrument_id: str | None = None
    prediction: dict | None = None
    release_to: list[str] = Field(default_factory=list, max_length=12)
    location_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list, max_length=40)

    @model_validator(mode="after")
    def validate_assay_settings(self):
        if self.operation == "assay" and self.settings:
            raise ValueError("assay 只读取批次小样，不执行材料处理，settings 参数不会生效")
        return self


def physical_task(task):
    # 哥哥，改分工、措辞或签名不会变成新的物理研究贡献。
    return {
        k: task.get(k)
        for k in (
            "operation",
            "settings",
        "energy_id",
            "hours",
            "batch_id",
            "quantity",
            "auxiliary_ids",
            "auxiliary_quantities",
            "method",
            "prediction",
        )
    }


class CooperativeService:
    @staticmethod
    def record_source(c, proposal_id, program_id):
        version = c.execute(
            "SELECT current_version FROM collective_proposals WHERE id=?", (proposal_id,)
        ).fetchone()[0]
        program = c.execute(
            "SELECT pc FROM collective_programs WHERE id=?", (program_id,)
        ).fetchone()
        c.execute(
            "INSERT INTO collective_component_sources VALUES (?,?,?,?)",
            (proposal_id, version, program_id, program["pc"]),
        )
        body = json.loads(
            c.execute(
                "SELECT spec_json FROM collective_proposal_versions WHERE proposal_id=? AND version=?",
                (proposal_id, version),
            ).fetchone()[0]
        )
        old = c.execute(
            "SELECT spec_json FROM collective_proposal_versions WHERE proposal_id=? AND version=?",
            (proposal_id, version - 1),
        ).fetchone()
        old_tasks = {t["key"]: t for t in json.loads(old[0])["tasks"]} if old else {}
        for task in body["tasks"]:
            previous = old_tasks.get(task["key"])
            inherited = c.execute(
                "SELECT * FROM collective_task_sources WHERE proposal_id=? AND version=? AND task_key=?",
                (proposal_id, version - 1, task["key"]),
            ).fetchone()
            if inherited and previous and physical_task(previous) == physical_task(task):
                values = (
                    inherited["program_id"],
                    inherited["origin_version"],
                    inherited["previous_task_json"],
                    inherited["task_json"],
                    inherited["evidence_ids_json"],
                )
            else:
                values = (
                    program_id,
                    version,
                    encoded(previous) if previous else None,
                    encoded(task),
                    encoded(task.get("evidence_ids", []) or body.get("evidence_ids", [])),
                )
            c.execute(
                "INSERT INTO collective_task_sources VALUES (?,?,?,?,?,?,?,?)",
                (proposal_id, version, task["key"], *values),
            )

    @staticmethod
    def _validate_tasks(c, actor, spec, at):
        tasks = [WorkTask.model_validate(t).model_dump(mode="json") for t in spec["tasks"]]
        if not tasks or len({t["key"] for t in tasks}) != len(tasks):
            raise ValueError("合作任务须有不重复的具体标识")
        known = known_evidence(c, actor["id"], at)
        if set(spec.get("evidence_ids", [])) - known or any(
            set(t["evidence_ids"]) - known for t in tasks
        ):
            raise ValueError("方案引用的实验证据尚未实际取得")
        keys = {t["key"] for t in tasks}
        for t in tasks:
            from world_engine.science_work import enabled
            from world_engine.craft_physics import CRAFT_CONTROLS
            controls = CRAFT_CONTROLS if enabled(c, actor["world_id"]) else CONTROLS
            if t["operation"] not in {*controls, "assay"}:
                raise ValueError("任务操作未登记")
            if t["key"] in t["depends_on"] or set(t["depends_on"]) - keys:
                raise ValueError("任务依赖不存在或依赖自身")
            for ref in [t["batch_id"], *t["auxiliary_ids"]]:
                if ref.startswith("task:"):
                    parts = ref.split(":")
                    if len(parts) != 3 or parts[1] not in t["depends_on"] or not parts[2].isdigit():
                        raise ValueError("前置产物引用必须声明明确依赖与非负产物编号")
            for worker in [t["worker_id"], *t["assistants"]]:
                if (
                    c.execute(
                        "SELECT 1 FROM characters WHERE id=? AND world_id=?",
                        (worker, actor["world_id"]),
                    ).fetchone()
                    is None
                ):
                    raise ValueError("参与者不属于当前世界")
        remaining = {t["key"]: set(t["depends_on"]) for t in tasks}
        while remaining:
            ready = {k for k, v in remaining.items() if not v}
            if not ready:
                raise ValueError("合作任务存在循环依赖")
            remaining = {k: v - ready for k, v in remaining.items() if k not in ready}
        return tasks

    @staticmethod
    def _known(c, actor_id, proposal_id, version):
        row = c.execute("SELECT * FROM collective_proposals WHERE id=?", (proposal_id,)).fetchone()
        if row is None:
            raise ValueError("尚未收到或不存在此提案")
        version_author = c.execute(
            "SELECT author_id FROM collective_proposal_versions WHERE proposal_id=? AND version=?",
            (proposal_id, version),
        ).fetchone()
        if version_author is not None and version_author[0] == actor_id:
            return row
        for obs in c.execute(
            "SELECT content_json FROM collective_observations WHERE character_id=?", (actor_id,)
        ):
            data = json.loads(obs[0])
            if data.get("proposal_id") == proposal_id and data.get("version") == version:
                return row
        raise ValueError("本人尚未收到这一版本的合作提案")

    @classmethod
    def propose(cls, c, actor_id, spec, at):
        actor = R.actor(c, actor_id)
        tasks = cls._validate_tasks(c, actor, spec, at)
        pid = str(uuid4())
        with material_transaction(c):
            c.execute(
                "INSERT INTO collective_proposals(id,world_id,author_id,created_at) VALUES (?,?,?,?)",
                (pid, actor["world_id"], actor_id, to_iso(at)),
            )
            actual = {**spec, "tasks": tasks}
            c.execute(
                "INSERT INTO collective_proposal_versions VALUES (?,?,?,?,?,?)",
                (pid, 1, actor_id, spec["reason"], encoded(actual), to_iso(at)),
            )
            R.record_observation(
                c,
                actor_id,
                at,
                "本人提出待同伴评议的方案",
                {"kind": "proposal", "proposal_id": pid, "version": 1, "proposal": actual},
            )
        return pid

    @classmethod
    def accept(cls, c, actor_id, spec, at):
        pid, version = spec["proposal_id"], spec["version"]
        proposal = cls._known(c, actor_id, pid, version)
        if proposal["current_version"] != version or proposal["status"] != "proposed":
            raise ValueError("必须独立审阅当前未开工的提案版本")
        body = json.loads(
            c.execute(
                "SELECT spec_json FROM collective_proposal_versions WHERE proposal_id=? AND version=?",
                (pid, version),
            ).fetchone()[0]
        )
        tasks = {t["key"]: t for t in body["tasks"]}
        keys = spec.get("task_keys", [])
        resource_keys = spec.get("resource_task_keys", [])
        if len(resource_keys) != len(set(resource_keys)) or set(resource_keys) - tasks.keys():
            raise ValueError("资源许可须明确指定已知任务")
        if len(keys) != len(set(keys)) or set(keys) - tasks.keys():
            raise ValueError("同意的任务标识无效")
        if not spec.get("reason", "").strip():
            raise ValueError("同意需要本人独立判断的理由")
        with material_transaction(c):
            for key in keys:
                t = tasks[key]
                if actor_id not in [t["worker_id"], *t["assistants"]]:
                    raise ValueError("只能同意本人参与的实际工作")
                c.execute(
                    "INSERT OR REPLACE INTO collective_acceptances VALUES (?,?,?,?,?,?,NULL)",
                    (pid, version, key, actor_id, spec["reason"], to_iso(at)),
                )
                c.execute(
                    "DELETE FROM collective_rejections WHERE proposal_id=? AND version=? AND task_key=? AND actor_id=?",
                    (pid, version, key, actor_id),
                )
            for key in set(keys) | set(resource_keys):
                t = tasks[key]
                for iid in [
                    t["batch_id"],
                    *t["auxiliary_ids"],
                    t["apparatus_id"],
                    t["energy_id"],
                    t["instrument_id"],
                ]:
                    if (
                        iid
                        and c.execute(
                            "SELECT 1 FROM item_instances WHERE id=? AND owner_character_id=?",
                            (iid, actor_id),
                        ).fetchone()
                    ):
                        for worker in [t["worker_id"], *t["assistants"]]:
                            c.execute(
                                "INSERT OR IGNORE INTO collective_resource_permissions VALUES (?,?,?,?,?,?)",
                                (pid, version, iid, actor_id, worker, to_iso(at)),
                            )
            R.record_observation(
                c,
                actor_id,
                at,
                "本人审阅后同意明确工作及自有资源使用",
                {
                    "kind": "acceptance",
                    "proposal_id": pid,
                    "version": version,
                    "task_keys": keys,
                    "reason": spec["reason"],
                },
            )

    @classmethod
    def reject(cls, c, actor_id, spec, at):
        pid, version = spec["proposal_id"], spec["version"]
        p = cls._known(c, actor_id, pid, version)
        if (
            p["status"] != "proposed"
            or p["current_version"] != version
            or not spec.get("reason", "").strip()
        ):
            raise ValueError("只能有理由地拒绝已知的当前未开工版本")
        body = json.loads(
            c.execute(
                "SELECT spec_json FROM collective_proposal_versions WHERE proposal_id=? AND version=?",
                (pid, version),
            ).fetchone()[0]
        )
        tasks = {t["key"]: t for t in body["tasks"]}
        with material_transaction(c):
            for key in spec.get("task_keys", []):
                if key not in tasks or actor_id not in [
                    tasks[key]["worker_id"],
                    *tasks[key]["assistants"],
                ]:
                    raise ValueError("不能代替他人拒绝参与")
                c.execute(
                    "DELETE FROM collective_acceptances WHERE proposal_id=? AND version=? AND task_key=? AND actor_id=?",
                    (pid, version, key, actor_id),
                )
                c.execute(
                    "INSERT OR REPLACE INTO collective_rejections VALUES (?,?,?,?,?,?)",
                    (pid, version, key, actor_id, spec["reason"], to_iso(at)),
                )
            R.record_observation(
                c,
                actor_id,
                at,
                "本人独立拒绝当前工作，保留具体质疑",
                {
                    "kind": "rejection",
                    "proposal_id": pid,
                    "version": version,
                    "task_keys": spec.get("task_keys", []),
                    "reason": spec["reason"],
                },
            )

    @classmethod
    def revise(cls, c, actor_id, spec, at):
        pid, version = spec["proposal_id"], spec["version"]
        proposal = cls._known(c, actor_id, pid, version)
        actor = R.actor(c, actor_id)
        if (
            proposal["world_id"] != actor["world_id"]
            or proposal["current_version"] != version
            or proposal["status"] != "proposed"
        ):
            raise ValueError("只能修订本人已知的当前未开工版本")
        new_tasks = cls._validate_tasks(c, actor, spec, at)
        old = json.loads(
            c.execute(
                "SELECT spec_json FROM collective_proposal_versions WHERE proposal_id=? AND version=?",
                (pid, version),
            ).fetchone()[0]
        )
        unchanged = {t["key"] for t in new_tasks if any(t == previous for previous in old["tasks"])}
        with material_transaction(c):
            c.execute(
                "INSERT INTO collective_proposal_versions VALUES (?,?,?,?,?,?)",
                (
                    pid,
                    version + 1,
                    actor_id,
                    spec["reason"],
                    encoded({**spec, "tasks": new_tasks}),
                    to_iso(at),
                ),
            )
            c.execute(
                "UPDATE collective_proposals SET current_version=? WHERE id=?", (version + 1, pid)
            )
            for accepted in c.execute(
                "SELECT * FROM collective_acceptances WHERE proposal_id=? AND version=?",
                (pid, version),
            ).fetchall():
                if accepted["task_key"] in unchanged:
                    c.execute(
                        "INSERT INTO collective_acceptances VALUES (?,?,?,?,?,?,?)",
                        (
                            pid,
                            version + 1,
                            accepted["task_key"],
                            accepted["actor_id"],
                            accepted["reason"],
                            accepted["accepted_at"],
                            version,
                        ),
                    )
            for task in new_tasks:
                if task["key"] not in unchanged:
                    continue
                for iid in [
                    task["batch_id"],
                    *task["auxiliary_ids"],
                    task["apparatus_id"],
                    task["instrument_id"],
                ]:
                    if not iid:
                        continue
                    for worker in [task["worker_id"], *task["assistants"]]:
                        for permit in c.execute(
                            "SELECT * FROM collective_resource_permissions WHERE proposal_id=? AND version=? AND item_id=? AND allowed_actor_id=?",
                            (pid, version, iid, worker),
                        ).fetchall():
                            c.execute(
                                "INSERT OR IGNORE INTO collective_resource_permissions VALUES (?,?,?,?,?,?)",
                                (
                                    pid,
                                    version + 1,
                                    iid,
                                    permit["owner_id"],
                                    worker,
                                    permit["accepted_at"],
                                ),
                            )
            R.record_observation(
                c,
                actor_id,
                at,
                "本人根据评议修改方案，变更工作须重新征得同意",
                {
                    "kind": "proposal",
                    "proposal_id": pid,
                    "version": version + 1,
                    "proposal": {**spec, "tasks": new_tasks},
                },
            )
        return version + 1

    @classmethod
    def start(cls, c, actor_id, spec, at):
        pid, version = spec["proposal_id"], spec["version"]
        proposal = cls._known(c, actor_id, pid, version)
        if proposal["current_version"] != version or proposal["status"] != "proposed":
            raise ValueError("方案版本已改变或已开工")
        body = json.loads(
            c.execute(
                "SELECT spec_json FROM collective_proposal_versions WHERE proposal_id=? AND version=?",
                (pid, version),
            ).fetchone()[0]
        )
        for t in body["tasks"]:
            for worker in [t["worker_id"], *t["assistants"]]:
                if (
                    c.execute(
                        "SELECT 1 FROM collective_acceptances WHERE proposal_id=? AND version=? AND task_key=? AND actor_id=?",
                        (pid, version, t["key"], worker),
                    ).fetchone()
                    is None
                ):
                    raise ValueError("实际参与者尚未同意此版本的明确工作")
        with material_transaction(c):
            for t in body["tasks"]:
                c.execute(
                    "INSERT INTO collective_work_orders(id,proposal_id,version,task_key,worker_id,spec_json) VALUES (?,?,?,?,?,?)",
                    (str(uuid4()), pid, version, t["key"], t["worker_id"], encoded(t)),
                )
            c.execute("UPDATE collective_proposals SET status='executing' WHERE id=?", (pid,))
            from world_engine.collective_work import WorkService

            WorkService.tick(c, proposal["world_id"], at)
        return pid
