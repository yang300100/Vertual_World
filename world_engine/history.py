from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from world_engine.database import Database
from world_engine.repository import WorldRepository, to_iso, utc_now


class HistoryExportResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    world_id: str
    world_name: str
    tick_count: int
    event_count: int
    directory: str
    markdown_file: str
    jsonl_file: str
    tick_files: int
    heartbeat_count: int
    state_update_count: int
    heartbeat_file: str
    state_update_file: str


class WorldHistoryLogger:
    """从客观事件表生成可读、可回填、跨进程安全的世界历史日志。"""

    def __init__(self, database: Database, root_directory: Path) -> None:
        self.database = database
        self.root_directory = root_directory
        self.repository = WorldRepository()

    def sync_world(self, world_id: str) -> HistoryExportResult:
        safe_world_id = self._safe_uuid(world_id)
        world_directory = self.root_directory / safe_world_id
        world_directory.mkdir(parents=True, exist_ok=True)
        with self._export_lock(world_directory):
            with self.database.read() as connection:
                snapshot = self.repository.get_snapshot(connection, safe_world_id)
                events = self.repository.list_all_events_ascending(connection, safe_world_id)
                from world_engine.event_history import EventHistoryService
                for event in events:
                    event["history"]=EventHistoryService.view(connection,event["id"])
                    profile=connection.execute("SELECT scope_type,scope_id FROM event_profiles WHERE event_id=?",(event["id"],)).fetchone()
                    event["history_scope"]=dict(profile) if profile else {"scope_type":"interpersonal","scope_id":None}
                heartbeats = self.repository.list_heartbeats_since(
                    connection, safe_world_id, 0
                )
                state_updates = self.repository.list_state_updates_since(
                    connection, safe_world_id, 0
                )

            tick_groups = self._group_ticks(events)
            scope_groups={}
            for event in events:
                scope=event["history_scope"]
                kind=scope["scope_type"]
                slug=hashlib.sha256(str(scope["scope_id"] or safe_world_id).encode()).hexdigest()[:16]
                relative="global" if kind=="global" else "interpersonal" if kind=="interpersonal" else f"{'regions' if kind=='regional' else 'locations'}/{slug}"
                scope_groups.setdefault(relative,[]).append(event)
            for relative,scoped in scope_groups.items():
                destination=world_directory / relative
                destination.mkdir(parents=True,exist_ok=True)
                self._atomic_write(destination / "history.jsonl",self._render_jsonl(snapshot.world.name,self._group_ticks(scoped)))
                self._atomic_write(destination / "history.md",self._render_markdown(snapshot.world.name,safe_world_id,self._group_ticks(scoped),{item.id:item.name for item in snapshot.characters}))
            self._atomic_write(world_directory / "scope_manifest.json",json.dumps({"world_id":safe_world_id,"files":[{"directory":key,"event_count":len(value)} for key,value in scope_groups.items()]},ensure_ascii=False,indent=2))
            tick_directory = world_directory / "ticks"
            tick_directory.mkdir(parents=True, exist_ok=True)
            for sequence, (tick_id, tick_events) in enumerate(tick_groups.items(), start=1):
                record = self._tick_record(
                    sequence=sequence,
                    world_id=safe_world_id,
                    world_name=snapshot.world.name,
                    tick_id=tick_id,
                    events=tick_events,
                )
                tick_path = tick_directory / f"{sequence:08d}_{self._tick_file_id(tick_id)}.json"
                self._atomic_write(
                    tick_path,
                    json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                )

            markdown_path = world_directory / "history.md"
            jsonl_path = world_directory / "history.jsonl"
            heartbeat_path = world_directory / "heartbeats.jsonl"
            state_update_path = world_directory / "state_updates.jsonl"
            manifest_path = world_directory / "manifest.json"
            self._atomic_write(
                markdown_path,
                self._render_markdown(
                    snapshot.world.name,
                    safe_world_id,
                    tick_groups,
                    {item.id: item.name for item in snapshot.characters},
                ),
            )
            self._atomic_write(
                jsonl_path,
                self._render_jsonl(snapshot.world.name, tick_groups),
            )
            self._write_state_logs(
                world_directory,
                world_id=safe_world_id,
                world_name=snapshot.world.name,
                character_names={item.id: item.name for item in snapshot.characters},
                heartbeats=heartbeats,
                state_updates=state_updates,
                totals=(len(heartbeats), len(state_updates)),
                rebuild=True,
            )
            manifest = {
                "schema_version": 1,
                "world_id": safe_world_id,
                "world_name": snapshot.world.name,
                "tick_count": len(tick_groups),
                "event_count": len(events),
                "heartbeat_count": len(heartbeats),
                "state_update_count": len(state_updates),
                "exported_at": to_iso(utc_now()),
            }
            self._atomic_write(
                manifest_path,
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            )

        return HistoryExportResult(
            world_id=safe_world_id,
            world_name=snapshot.world.name,
            tick_count=len(tick_groups),
            event_count=len(events),
            directory=str(world_directory),
            markdown_file=str(markdown_path),
            jsonl_file=str(jsonl_path),
            tick_files=len(tick_groups),
            heartbeat_count=len(heartbeats),
            state_update_count=len(state_updates),
            heartbeat_file=str(heartbeat_path),
            state_update_file=str(state_update_path),
        )

    def sync_state_logs(self, world_id: str) -> tuple[int, int]:
        """分钟心跳只同步技术状态日志，不重建世界编年史。

        用 rowid 水位线做增量追加：每次只读取并写入上次导出之后的新记录，
        避免每 tick 重写整个 JSONL（旧实现随历史增长呈 O(n²) 的 I/O）。
        清单或日志缺失时回退为一次全量重建，因此旧存档可自愈。
        """

        safe_world_id = self._safe_uuid(world_id)
        world_directory = self.root_directory / safe_world_id
        world_directory.mkdir(parents=True, exist_ok=True)
        with self._export_lock(world_directory):
            watermark = self._load_state_watermark(
                world_directory / "state_manifest.json",
                world_directory / "heartbeats.jsonl",
                world_directory / "state_updates.jsonl",
            )
            with self.database.read() as connection:
                world_row = connection.execute(
                    'SELECT id, name FROM worlds WHERE id = ?', (safe_world_id,)
                ).fetchone()
                if world_row is None:
                    raise ValueError("世界不存在")
                character_names = {
                    row["id"]: row["name"]
                    for row in connection.execute(
                        "SELECT id, name FROM characters WHERE world_id = ?",
                        (safe_world_id,),
                    ).fetchall()
                }
                heartbeats = self.repository.list_heartbeats_since(
                    connection, safe_world_id, watermark["heartbeat_rowid"]
                )
                state_updates = self.repository.list_state_updates_since(
                    connection, safe_world_id, watermark["state_update_rowid"]
                )
                totals = self.repository.state_log_totals(connection, safe_world_id)

            self._write_state_logs(
                world_directory,
                world_id=world_row["id"],
                world_name=world_row["name"],
                character_names=character_names,
                heartbeats=heartbeats,
                state_updates=state_updates,
                totals=totals,
                rebuild=bool(watermark["rebuild"]),
                previous_watermark=(
                    int(watermark["heartbeat_rowid"]),
                    int(watermark["state_update_rowid"]),
                ),
            )
        return totals

    def _write_state_logs(
        self,
        world_directory: Path,
        *,
        world_id: str,
        world_name: str,
        character_names: dict[str, str],
        heartbeats: list[tuple[int, dict[str, object]]],
        state_updates: list[tuple[int, dict[str, object]]],
        totals: tuple[int, int],
        rebuild: bool,
        previous_watermark: tuple[int, int] = (0, 0),
    ) -> tuple[int, int]:
        """写入心跳与状态差值：重建时覆盖写，增量时追加写；返回新水位线。"""

        heartbeat_path = world_directory / "heartbeats.jsonl"
        state_update_path = world_directory / "state_updates.jsonl"
        heartbeat_records = [item for _, item in heartbeats]
        state_update_records = [item for _, item in state_updates]

        if rebuild:
            self._atomic_write(heartbeat_path, self._records_to_jsonl(heartbeat_records))
            self._atomic_write(
                state_update_path, self._records_to_jsonl(state_update_records)
            )
        else:
            self._append_records(heartbeat_path, heartbeat_records)
            self._append_records(state_update_path, state_update_records)

        heartbeat_watermark = (
            heartbeats[-1][0] if heartbeats else (0 if rebuild else previous_watermark[0])
        )
        state_update_watermark = (
            state_updates[-1][0]
            if state_updates
            else (0 if rebuild else previous_watermark[1])
        )

        character_directory = world_directory / "characters"
        character_directory.mkdir(parents=True, exist_ok=True)
        grouped: dict[str, list[dict[str, object]]] = {}
        for _, item in state_updates:
            grouped.setdefault(str(item["character_id"]), []).append(item)
        if rebuild:
            # 全量重建：先清掉可能属于已删除角色的残留档案，再为当前角色各写一份。
            for stale in character_directory.glob("*.state.jsonl"):
                stale.unlink()
            for character_id, character_name in character_names.items():
                self._atomic_write(
                    character_directory / f"{self._safe_uuid(character_id)}.state.jsonl",
                    self._records_to_jsonl(
                        [
                            {"character_name": character_name, **item}
                            for item in grouped.get(character_id, [])
                        ]
                    ),
                )
        else:
            for character_id, items in grouped.items():
                self._append_records(
                    character_directory / f"{self._safe_uuid(character_id)}.state.jsonl",
                    [
                        {"character_name": character_names.get(character_id, ""), **item}
                        for item in items
                    ],
                )

        self._atomic_write(
            world_directory / "state_manifest.json",
            json.dumps(
                {
                    "schema_version": 2,
                    "world_id": world_id,
                    "world_name": world_name,
                    "heartbeat_count": totals[0],
                    "state_update_count": totals[1],
                    "heartbeat_rowid": heartbeat_watermark,
                    "state_update_rowid": state_update_watermark,
                    "exported_at": to_iso(utc_now()),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
        return heartbeat_watermark, state_update_watermark

    @staticmethod
    def _load_state_watermark(
        manifest_path: Path, heartbeat_path: Path, state_update_path: Path
    ) -> dict[str, object]:
        """读取增量水位线；清单或日志缺失、损坏时要求全量重建以自愈。"""
        present = (
            manifest_path.exists() and heartbeat_path.exists() and state_update_path.exists()
        )
        if not present:
            return {"rebuild": True, "heartbeat_rowid": 0, "state_update_rowid": 0}
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            return {
                "rebuild": False,
                "heartbeat_rowid": int(manifest["heartbeat_rowid"]),
                "state_update_rowid": int(manifest["state_update_rowid"]),
            }
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError):
            # 旧版清单没有水位线字段：重建一次即可完成升级。
            return {"rebuild": True, "heartbeat_rowid": 0, "state_update_rowid": 0}

    @staticmethod
    def _append_records(path: Path, records: list[dict[str, object]]) -> None:
        if not records:
            return
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(WorldHistoryLogger._records_to_jsonl(records))
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _group_ticks(
        events: list[dict[str, object]],
    ) -> dict[str, list[dict[str, object]]]:
        groups: dict[str, list[dict[str, object]]] = {}
        for event in events:
            tick_id = str(event["tick_id"])
            groups.setdefault(tick_id, []).append(event)
        return groups

    @staticmethod
    def _tick_record(
        *,
        sequence: int,
        world_id: str,
        world_name: str,
        tick_id: str,
        events: list[dict[str, object]],
    ) -> dict[str, object]:
        tick_event = next(
            (
                item
                for item in events
                if item["event_type"] in {"world.tick", "world.adjudication"}
            ),
            None,
        )
        payload = tick_event["payload"] if tick_event else {}
        return {
            "schema_version": 1,
            "sequence": sequence,
            "world_id": world_id,
            "world_name": world_name,
            "tick_id": tick_id,
            "world_time": tick_event["occurred_at"] if tick_event else None,
            "decision_provider": payload.get("provider", "unknown"),
            "events": events,
        }

    def _render_markdown(
        self,
        world_name: str,
        world_id: str,
        tick_groups: dict[str, list[dict[str, object]]],
        character_names: dict[str, str],
    ) -> str:
        lines = [
            f"# {world_name} · 世界运行历史",
            "",
            f"- 世界ID：`{world_id}`",
            f"- 已记录轮次：{len(tick_groups)}",
            f"- 最后导出：{to_iso(utc_now())}",
            "- 事实来源：SQLite `world_events` 客观事件表",
            "",
        ]
        for sequence, (tick_id, events) in enumerate(tick_groups.items(), start=1):
            tick_event = next(
                (
                    item
                    for item in events
                    if item["event_type"] in {"world.tick", "world.adjudication"}
                ),
                None,
            )
            representative = tick_event or events[-1]
            world_time = representative["occurred_at"]
            payload = tick_event["payload"] if tick_event else {}
            lines.extend(
                [
                    f"## 第 {sequence} 轮 · {world_time}",
                    "",
                    f"- 轮次ID：`{tick_id}`",
                    f"- 决策器：`{payload.get('provider', 'unknown')}`",
                    "",
                ]
            )
            for event in events:
                event_type = str(event["event_type"])
                if event_type in {"world.tick", "world.adjudication"}:
                    continue
                summary = str(event["summary"])
                actor_id = event.get("actor_id")
                if actor_id and str(actor_id) in character_names:
                    summary = summary.replace(
                        str(actor_id), character_names[str(actor_id)], 1
                    )
                lines.append(
                    f"- **{self._event_label(event_type)}**（`{event_type}`）：{summary}"
                )
                reason = event["payload"].get("reason")
                if reason:
                    lines.append(f"  - 行动理由：{reason}")
            if tick_event:
                lines.append(
                    f"- **{self._event_label(str(tick_event['event_type']))}**"
                    f"（`{tick_event['event_type']}`）：{tick_event['summary']}"
                )
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _render_jsonl(
        world_name: str,
        tick_groups: dict[str, list[dict[str, object]]],
    ) -> str:
        lines: list[str] = []
        for sequence, events in enumerate(tick_groups.values(), start=1):
            for event in events:
                record = {
                    "schema_version": 1,
                    "sequence": sequence,
                    "world_name": world_name,
                    **event,
                }
                lines.append(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        return "\n".join(lines) + ("\n" if lines else "")

    @staticmethod
    def _records_to_jsonl(records: list[dict[str, object]]) -> str:
        lines = [
            json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            for item in records
        ]
        return "\n".join(lines) + ("\n" if lines else "")

    @staticmethod
    def _event_label(event_type: str) -> str:
        return {
            "action.rest": "休息",
            "action.eat": "进食",
            "action.work": "工作",
            "action.travel": "旅行",
            "action.socialize": "交流",
            "action.idle": "观察",
            "action.rejected": "行动失败",
            "world.tick": "时间推进",
            "world.adjudication": "模型裁判",
            "world.clock_rate_changed": "时间比例调整",
        }.get(event_type, "世界事件")

    @staticmethod
    def _tick_file_id(value: str) -> str:
        try:
            return str(UUID(value))
        except ValueError:
            # 活动和契约有可读轮次标识，只对文件名散列；原标识仍保留在 JSON 中。
            return "named-"+hashlib.sha256(value.encode()).hexdigest()[:32]

    @staticmethod
    def _safe_uuid(value: str) -> str:
        try:
            return str(UUID(value))
        except ValueError as exc:
            raise ValueError("世界或轮次ID不是合法UUID") from exc

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    @contextmanager
    def _export_lock(self, world_directory: Path) -> Iterator[None]:
        lock_directory = world_directory / ".history-write.lock"
        deadline = time.monotonic() + 10.0
        while True:
            try:
                lock_directory.mkdir()
                break
            except FileExistsError:
                try:
                    age = time.time() - lock_directory.stat().st_mtime
                    if age > 60:
                        lock_directory.rmdir()
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError("等待世界历史日志写锁超时") from None
                time.sleep(0.1)
        try:
            yield
        finally:
            try:
                lock_directory.rmdir()
            except FileNotFoundError:
                pass
