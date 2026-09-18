"""`AdjudicationMixin`：裁判编排——活跃人物筛选、提案生成、战斗结算与裁决落库。

从 `WorldEngine` 切出的职责切片。方法体、签名与 `self` 语义一字未改；
事务边界仍由 `adjudicate` 内部持有的 `with self.database.write()` 单独决定。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from uuid import uuid4

from world_engine.config import PROJECT_ROOT
from world_engine.contracts import ContractService
from world_engine.domain import (
    ActionOutcome,
    ActionProposal,
    ActionType,
    CharacterState,
    EventSeed,
    TerrainContext,
    TickResult,
    WorldSnapshot,
)
from world_engine.engine_errors import ConcurrentWorldUpdateError
from world_engine.orchestration import (
    AgentContext,
    AgentName,
    AgentProposalRecord,
    AgentRunRecord,
    CombatTacticalAgent,
)
from world_engine.repository import WorldNotFoundError, from_iso, to_iso, utc_now
from world_engine.time_utils import next_adjudication_boundary

LOGGER = logging.getLogger("virtual-world.engine")


class AdjudicationMixin:
    """`WorldEngine` 的裁判编排职责切片。"""

    def adjudicate(
        self,
        world_id: str,
        *,
        trigger: str,
        character_ids: list[str] | None = None,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> TickResult:
        started_at = utc_now()
        with self.database.write() as connection:
            row = connection.execute(
                "SELECT \"current_time\" AS current_time FROM worlds WHERE id = ?",
                (world_id,),
            ).fetchone()
            if row is None:
                raise WorldNotFoundError(world_id)
            self.activation.refresh(
                connection,
                world_id=world_id,
                world_time=from_iso(row["current_time"]),
            )
        adjudication_id = str(uuid4())

        # 版本冲突时最多重试一次：重新取快照并重新决策，随后仍冲突则回退规则失败。
        for attempt in range(2):
            with self.database.read() as connection:
                snapshot = self.repository.get_snapshot(connection, world_id)
                # 待办是 NPC 自己的长期计划属性；仅作为只读决策上下文注入，
                # Agent 仍只能提出动作，不能直接改写待办状态或世界事实。
                open_todos = connection.execute(
                    """
                    SELECT character_id, title, details FROM npc_todos
                    WHERE world_id = ? AND status IN ('open', 'doing')
                    ORDER BY CASE status WHEN 'doing' THEN 0 ELSE 1 END, updated_at
                    """,
                    (world_id,),
                ).fetchall()
                recent_events = (
                    self.repository.list_events(connection, world_id, limit=40)
                    if self.settings.world_agent_enabled
                    else []
                )

            if open_todos:
                snapshot = snapshot.model_copy(deep=True)
                todo_by_character: dict[str, list[str]] = {}
                for todo in open_todos:
                    detail = str(todo["details"] or "").strip()
                    text = f"待办：{todo['title']}" + (f"（{detail[:120]}）" if detail else "")
                    todo_by_character.setdefault(str(todo["character_id"]), []).append(text)
                for character in snapshot.characters:
                    additions = todo_by_character.get(character.id, [])[:3]
                    if additions:
                        character.goals = list(dict.fromkeys([*character.goals, *additions]))[:8]

            active_characters = self._select_active_characters(snapshot, character_ids)

            director_seeds: list[EventSeed] = []
            narrative: str | None = None
            agent_run_records: list[AgentRunRecord] = []
            agent_proposal_records: list[AgentProposalRecord] = []
            if self.settings.world_agent_enabled:
                orchestration = self.coordinator.orchestrate(
                    snapshot=snapshot,
                    trigger=trigger,
                    active_characters=active_characters,
                    recent_events=recent_events,
                    model_backend=self.agent_model_backend,
                    provider=self.decision_provider,
                    terrain=self._build_terrain_context(world_id, snapshot),
                )
                proposal_by_actor = orchestration.proposal_by_actor
                provider_name = orchestration.provider_name
                fallback_used = orchestration.fallback_used
                provider_error = orchestration.provider_error
                director_seeds = orchestration.seeds
                narrative = orchestration.narrative
                agent_run_records = orchestration.run_records
                agent_proposal_records = orchestration.proposal_records
            else:
                proposals, provider_name, fallback_used, provider_error = self._propose(
                    snapshot, active_characters
                )
                proposal_by_actor = self._normalize_proposals(
                    snapshot, active_characters, proposals
                )
            # 战术偏好先在事务外生成，提交时仍使用当前事实校验战斗。
            combat_preferences = {}
            if self.settings.world_agent_combat_enabled:
                for candidate in proposal_by_actor.values():
                    if candidate.action is ActionType.ATTACK:
                        encounter = {"participants_json": json.dumps([
                            candidate.actor_id, candidate.target_id
                        ]), "location_id": snapshot.character_by_id(candidate.actor_id).location_id}
                        combat_preferences[candidate.actor_id] = self._combat_intents(snapshot, encounter)
            resolved_window_start = window_start or snapshot.world.current_time
            resolved_window_end = window_end or snapshot.world.current_time

            try:
                with self.database.write() as connection:
                    current_snapshot = self.repository.get_snapshot(connection, world_id)
                    if current_snapshot.world.version != snapshot.world.version:
                        if attempt < 1:
                            # 版本变化：重新取快照并重新决策，最多一次。
                            continue
                        raise ConcurrentWorldUpdateError(
                            f"状态修订号已经从{snapshot.world.version}变化为"
                            f"{current_snapshot.world.version}，本轮必须重新决策"
                        )

                    connection.execute(
                        """
                        UPDATE world_runtime
                        SET last_tick_started_at = ?, last_tick_status = 'running'
                        WHERE world_id = ?
                        """,
                        (to_iso(started_at), world_id),
                    )
                    outcomes = []
                    self._create_npc_owned_plans(
                        connection, world_id=world_id, characters=active_characters
                    )
                    for character in active_characters:
                        proposal = proposal_by_actor[character.id]
                        if (
                            proposal.action is ActionType.ATTACK
                            and self.settings.world_agent_combat_enabled
                        ):
                            outcome = self._resolve_combat_proposal(
                                connection,
                                world_id=world_id,
                                tick_id=adjudication_id,
                                occurred_at=snapshot.world.current_time,
                                proposal=proposal,
                                snapshot=snapshot,
                                prepared_intents=combat_preferences.get(proposal.actor_id, []),
                            )
                            outcomes.append(outcome)
                        else:
                            outcome = self.actions.execute(
                                connection,
                                world_id=world_id,
                                tick_id=adjudication_id,
                                occurred_at=snapshot.world.current_time,
                                proposal=proposal,
                            )
                            if outcome.accepted and outcome.event_id is not None:
                                self.intent_effects.apply(
                                    connection,
                                    world_id=world_id,
                                    source_event_id=outcome.event_id,
                                    actor_id=proposal.actor_id,
                                    target_id=proposal.target_id,
                                    intent=proposal.dialogue or proposal.reason,
                                )
                            outcomes.append(outcome)
                    if agent_run_records:
                        self._write_agent_audit(
                            connection, world_id, agent_run_records, agent_proposal_records
                        )

                    ContractService.advance(connection, world_id, to_iso(snapshot.world.current_time))
                    new_version = snapshot.world.version + 1
                    completed_at = utc_now()
                    connection.execute(
                        """
                        UPDATE worlds
                        SET version = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (new_version, to_iso(completed_at), world_id),
                    )
                    connection.execute(
                        """
                        UPDATE world_runtime
                        SET tick_count = tick_count + 1,
                            last_tick_finished_at = ?,
                            last_tick_status = 'completed'
                        WHERE world_id = ?
                        """,
                        (to_iso(completed_at), world_id),
                    )
                    if trigger == "scheduled_12h":
                        next_boundary = next_adjudication_boundary(
                            resolved_window_end,
                            snapshot.world.adjudication_interval_minutes,
                        )
                        connection.execute(
                            """
                            UPDATE world_clock
                            SET last_adjudication_world_time = ?,
                                next_adjudication_world_time = ?,
                                clock_revision = clock_revision + 1,
                                updated_at = ?
                            WHERE world_id = ?
                            """,
                            (
                                to_iso(resolved_window_end),
                                to_iso(next_boundary),
                                to_iso(completed_at),
                                world_id,
                            ),
                        )
                    elif trigger == "player_intervention":
                        connection.execute(
                            """
                            UPDATE world_clock
                            SET last_player_intervention_world_time = ?, updated_at = ?
                            WHERE world_id = ?
                            """,
                            (
                                to_iso(snapshot.world.current_time),
                                to_iso(completed_at),
                                world_id,
                            ),
                        )

                    adjudication_event_id = str(uuid4())
                    connection.execute(
                        """
                        INSERT INTO world_events(
                            id, world_id, tick_id, occurred_at, event_type,
                            summary, payload_json, created_at
                        ) VALUES (?, ?, ?, ?, 'world.adjudication', ?, ?, ?)
                        """,
                        (
                            adjudication_event_id,
                            world_id,
                            adjudication_id,
                            to_iso(snapshot.world.current_time),
                            "世界在当前时间完成了一次人物与事件裁判。",
                            json.dumps(
                                {
                                    "trigger": trigger,
                                    "provider": provider_name,
                                    "fallback_used": fallback_used,
                                    "provider_error": provider_error,
                                    "active_character_count": len(active_characters),
                                    "accepted_action_count": sum(
                                        1 for item in outcomes if item.accepted
                                    ),
                                    "previous_version": snapshot.world.version,
                                    "current_version": new_version,
                                    "window_start": to_iso(resolved_window_start),
                                    "window_end": to_iso(resolved_window_end),
                                },
                                ensure_ascii=False,
                            ),
                            to_iso(completed_at),
                        ),
                    )
                    proposal_records = [
                        proposal_by_actor[item.id].model_dump(mode="json")
                        for item in active_characters
                    ]
                    rejections = [
                        item.model_dump(mode="json")
                        for item in outcomes
                        if not item.accepted
                    ]
                    final_event_ids = [
                        item.event_id for item in outcomes if item.event_id is not None
                    ] + [adjudication_event_id]
                    connection.execute(
                        """
                        INSERT INTO adjudication_runs(
                            id, world_id, trigger_type, window_start, window_end,
                            provider, selected_character_ids_json, proposals_json,
                            rule_rejections_json, final_event_ids_json,
                            fallback_used, status, started_at, completed_at, error_text
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?)
                        """,
                        (
                            adjudication_id,
                            world_id,
                            trigger,
                            to_iso(resolved_window_start),
                            to_iso(resolved_window_end),
                            provider_name,
                            json.dumps(
                                [item.id for item in active_characters], ensure_ascii=False
                            ),
                            json.dumps(proposal_records, ensure_ascii=False),
                            json.dumps(rejections, ensure_ascii=False),
                            json.dumps(final_event_ids, ensure_ascii=False),
                            int(fallback_used),
                            to_iso(started_at),
                            to_iso(completed_at),
                            provider_error,
                        ),
                    )

                result = TickResult(
                    world_id=world_id,
                    tick_id=adjudication_id,
                    started_at=started_at,
                    completed_at=completed_at,
                    previous_time=snapshot.world.current_time,
                    current_time=snapshot.world.current_time,
                    previous_version=snapshot.world.version,
                    current_version=new_version,
                    outcomes=outcomes,
                    trigger=trigger,
                    provider=provider_name,
                    fallback_used=fallback_used,
                    narrative=narrative,
                    director_seeds=director_seeds,
                    agent_runs=self._run_records_to_views(agent_run_records),
                )
                proposal_outcomes = [
                    (outcome, proposal_by_actor[outcome.actor_id])
                    for outcome in outcomes
                    if outcome.actor_id in proposal_by_actor
                ]
                if self._tombstone_destroyed_targets(world_id, proposal_outcomes):
                    with self.database.read() as connection:
                        new_version = int(
                            connection.execute(
                                "SELECT version FROM worlds WHERE id = ?", (world_id,)
                            ).fetchone()["version"]
                        )
                    result.current_version = new_version
                self._sync_history_safely(world_id)
                if self.settings.world_agent_enabled:
                    self._sync_memory_candidates_safely(world_id)
                return result
            except ConcurrentWorldUpdateError:
                if attempt >= 1:
                    raise
                # 否则进入下一次循环重新取快照。
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower():
                    raise
                self._mark_failed(world_id)
                raise
            except Exception:
                self._mark_failed(world_id)
                raise
        raise ConcurrentWorldUpdateError("状态修订号在重试后仍不一致，已交由规则引擎降级")


    @staticmethod
    def _create_npc_owned_plans(
        connection: sqlite3.Connection,
        *,
        world_id: str,
        characters: list[CharacterState],
    ) -> None:
        """把 NPC 已有的角色目标转为其自行维护的可见计划。"""
        now = to_iso(utc_now())
        npc_characters = [item for item in characters if not item.is_player]
        if not npc_characters:
            return
        # 一次性取回全部 NPC 的既有计划，避免逐个角色查询（原先每个 NPC 一次 SELECT）。
        placeholders = ",".join("?" for _ in npc_characters)
        existing_titles: dict[str, set[str]] = {}
        for row in connection.execute(
            f"""
            SELECT character_id, title FROM npc_todos
            WHERE world_id = ? AND character_id IN ({placeholders})
              AND status IN ('open', 'doing')
            """,  # noqa: S608 - 占位符由上方按角色数生成，不含用户输入
            (world_id, *(item.id for item in npc_characters)),
        ).fetchall():
            existing_titles.setdefault(str(row["character_id"]), set()).add(str(row["title"]))

        pending: list[tuple[object, ...]] = []
        for character in npc_characters:
            known = existing_titles.setdefault(character.id, set())
            for goal in character.goals[:3]:
                normalized_goal = str(goal).strip()
                if not normalized_goal:
                    continue
                title = f"推进：{normalized_goal}"[:160]
                if title in known:
                    continue
                known.add(title)
                pending.append(
                    (
                        str(uuid4()),
                        world_id,
                        character.id,
                        title,
                        f"{character.name}根据自身目标自行安排。",
                        now,
                        now,
                    )
                )
        if pending:
            connection.executemany(
                """
                INSERT INTO npc_todos(id, world_id, character_id, title, details, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                pending,
            )


    def _resolve_combat_proposal(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        tick_id: str,
        occurred_at: datetime,
        proposal: ActionProposal,
        snapshot: WorldSnapshot,
        prepared_intents: list[object] | None = None,
    ) -> ActionOutcome:
        encounter = self.combat.ensure_encounter(
            connection,
            world_id=world_id,
            actor_id=proposal.actor_id,
            target_id=proposal.target_id or "",
            occurred_at=occurred_at,
        )
        intents: list[object] = []
        if encounter:
            intents = prepared_intents or []
            self.combat.apply_combat_intent_preference(
                connection,
                world_id=world_id,
                encounter_id=encounter["id"],
                intents=intents,
                occurred_at=occurred_at,
            )
        return self.combat.resolve_proposal(
            connection,
            world_id=world_id,
            tick_id=tick_id,
            occurred_at=occurred_at,
            proposal=proposal,
            intents=intents,
        ).outcome


    def _combat_intents(
        self, snapshot: WorldSnapshot, encounter: dict[str, object]
    ) -> list[object]:
        try:
            participants = [
                snapshot.character_by_id(cid)
                for cid in json.loads(encounter["participants_json"])
            ]
        except (json.JSONDecodeError, TypeError, KeyError):
            return []
        participants = [item for item in participants if item is not None]
        if not participants:
            return []
        location = (
            snapshot.location_by_id(encounter["location_id"])
            if encounter.get("location_id")
            else None
        )
        scene = self.assembler.assemble(snapshot, trigger="combat")
        ctx = AgentContext(
            name=AgentName.COMBAT_TACTICAL,
            scene=scene,
            snapshot=snapshot,
            model_backend=self.agent_model_backend,
        )
        agent = CombatTacticalAgent()
        intents, status, _ = self.coordinator._run_single(
            lambda: agent.run(ctx, participants=participants, encounter_location=location)
        )
        return intents if status == "ok" else agent._run_rules(ctx, participants)


    def _build_terrain_context(
        self, world_id: str, snapshot: WorldSnapshot
    ) -> TerrainContext | None:
        if not self.settings.world_agent_enabled:
            return None
        pov = next((item for item in snapshot.characters if item.is_pov), None)
        if pov is None:
            return None
        try:
            with self.database.read() as connection:
                row = connection.execute(
                    """
                    SELECT asset_root FROM navigation_datasets
                    WHERE world_id = ? AND review_status = 'approved'
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (world_id,),
                ).fetchone()
            if row is None:
                return None
            from world_engine.navigation import TerrainService

            service = TerrainService(PROJECT_ROOT / row["asset_root"])
            sample = service.sample(pov.longitude, pov.latitude)
            if sample is None:
                return None
            return TerrainContext(
                dataset_id=row["asset_root"],
                location_id=pov.location_id,
                surface=sample.get("surface_type"),
                speed_multiplier=sample.get("road_speed_multiplier", 1.0),
                terrain_features=[sample.get("surface_type")] if sample.get("surface_type") else [],
                reachable=True,
            )
        except Exception:
            return None


    def _select_active_characters(
        self,
        snapshot: WorldSnapshot,
        character_ids: list[str] | None = None,
    ) -> list[CharacterState]:
        moving_ids = {
            movement.character_id
            for movement in snapshot.movements
            if movement.status == "moving"
        }
        if character_ids is not None:
            allowed = set(character_ids)
            selected = [
                item
                for item in snapshot.characters
                if item.id in allowed
                and item.id not in moving_ids
                and not item.is_player
                and item.health > 0
                and item.activation_state == "active"
            ]
            return selected[: self.settings.active_character_limit]

        def priority(character: CharacterState) -> tuple[int, str]:
            urgency = (100 - character.satiety) + (100 - character.energy)
            if character.money < 10:
                urgency += 15
            if character.is_core:
                urgency += 40
            if character.goals:
                urgency += 10  # 有明确目标的人物更主动、更常行动
            return (-urgency, character.name)

        autonomous_characters = [
            item
            for item in snapshot.characters
            if not item.is_player
            and item.id not in moving_ids
            and item.health > 0
            and item.activation_state == "active"
        ]
        return sorted(autonomous_characters, key=priority)[
            : self.settings.active_character_limit
        ]


    def _propose(
        self, snapshot: WorldSnapshot, characters: list[CharacterState]
    ) -> tuple[list[ActionProposal], str, bool, str | None]:
        try:
            return (
                self.decision_provider.propose(snapshot, characters),
                self.decision_provider.name,
                False,
                None,
            )
        except Exception as exc:
            LOGGER.warning(
                "决策器%s调用失败，当前裁判降级为规则引擎：%s",
                self.decision_provider.name,
                exc,
            )
            return (
                self.fallback_provider.propose(snapshot, characters),
                self.fallback_provider.name,
                True,
                str(exc),
            )


    def _normalize_proposals(
        self,
        snapshot: WorldSnapshot,
        characters: list[CharacterState],
        proposals: list[ActionProposal],
    ) -> dict[str, ActionProposal]:
        allowed_ids = {item.id for item in characters}
        normalized: dict[str, ActionProposal] = {}
        for proposal in proposals:
            if proposal.actor_id in allowed_ids and proposal.actor_id not in normalized:
                normalized[proposal.actor_id] = proposal

        missing = [item for item in characters if item.id not in normalized]
        for fallback in self.fallback_provider.propose(snapshot, missing):
            normalized[fallback.actor_id] = fallback
        return normalized
