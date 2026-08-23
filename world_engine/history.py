from __future__ import annotations

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
                heartbeats = self.repository.list_heartbeats_ascending(
                    connection, safe_world_id
                )
                state_updates = self.repository.list_state_updates_ascending(
                    connection, safe_world_id
                )

            tick_groups = self._group_ticks(events)
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
                tick_path = tick_directory / f"{sequence:08d}_{self._safe_uuid(tick_id)}.json"
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
                snapshot,
                heartbeats,
                state_updates,
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
        """分钟心跳只同步技术状态日志，不重建世界编年史。"""

        safe_world_id = self._safe_uuid(world_id)
        world_directory = self.root_directory / safe_world_id
        world_directory.mkdir(parents=True, exist_ok=True)
        with self._export_lock(world_directory):
            with self.database.read() as connection:
                snapshot = self.repository.get_snapshot(connection, safe_world_id)
                heartbeats = self.repository.list_heartbeats_ascending(
                    connection, safe_world_id
                )
                state_updates = self.repository.list_state_updates_ascending(
                    connection, safe_world_id
                )
            self._write_state_logs(
                world_directory,
                snapshot,
                heartbeats,
                state_updates,
            )
        return len(heartbeats), len(state_updates)

    def _write_state_logs(
        self,
        world_directory: Path,
        snapshot,
        heartbeats: list[dict[str, object]],
        state_updates: list[dict[str, object]],
    ) -> None:
        self._atomic_write(
            world_directory / "heartbeats.jsonl",
            self._records_to_jsonl(heartbeats),
        )
        self._atomic_write(
            world_directory / "state_updates.jsonl",
            self._records_to_jsonl(state_updates),
        )
        self._atomic_write(
            world_directory / "state_manifest.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "world_id": snapshot.world.id,
                    "world_name": snapshot.world.name,
                    "heartbeat_count": len(heartbeats),
                    "state_update_count": len(state_updates),
                    "exported_at": to_iso(utc_now()),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
        character_names = {item.id: item.name for item in snapshot.characters}
        character_directory = world_directory / "characters"
        character_directory.mkdir(parents=True, exist_ok=True)
        for character_id, character_name in character_names.items():
            records = [
                item for item in state_updates if item["character_id"] == character_id
            ]
            self._atomic_write(
                character_directory / f"{self._safe_uuid(character_id)}.state.jsonl",
                self._records_to_jsonl(
                    [{"character_name": character_name, **item} for item in records]
                ),
            )

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
