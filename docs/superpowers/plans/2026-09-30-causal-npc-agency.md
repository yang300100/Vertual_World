# 通用 NPC 因果行动 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 NPC 从可知事实自主生成可验证目标，持续执行现有行动，并记录每次尝试的原因、真实后果与重规划。

**Architecture:** 新增持久目标及步骤账本；目标生成器只读人物已知事实；有界行动规划器产生一项可执行动作，仍由现有领域服务结算。事件层用现有 `event_causes` 和 `action_effects` 连接动机、行动、结果。

**Tech Stack:** Python 3、SQLite、FastAPI、Pytest。

**Spec:** `docs/superpowers/specs/2026-09-30-npc-adventures-game-view-design.md`

## Global Constraints

- 不覆盖工作区已有未提交改动。
- 行动必须由现有规则与领域服务结算；目标完成由真实状态判断。
- 所有新注释用中文；人物情报不能从全知快照中偷看。
- 悬赏只是跨领域验收例子，规划器不得写死悬赏流程。

## Review Focus

- 同一心跳重试：一项目标最多执行一次动作，事件与奖励不重复。
- 目标对象死亡或消失：不能继续攻击或冒领成果，要重规划。
- NPC 正在移动或工作：新的行动不能覆盖既有活动。
- 消息仍未传给 NPC：不能因此生成目标。
- 拒绝行动：原因与后续替代行动可追溯，不能无限重试。

---

### Task 1: 持久目标与动作记录

**Files:** Create `world_engine/npc_agency.py`; modify `world_engine/schema.py`, `world_engine/database.py`; test `tests/test_npc_agency.py`.

**Interfaces:** Produce `NpcAgencyService.adopt(connection, world_id, actor_id, kind, predicate, motive, at, source_event_id=None) -> str` and `view(connection, actor_id) -> list[dict]`.

- [ ] Write a failing test: call `adopt` twice for the same active actor/predicate and assert one goal with its motive and source; reopen the database and assert it persists.
- [ ] Run `pytest tests/test_npc_agency.py::test_goal_adoption_is_idempotent_and_persistent -q` and observe the missing API failure.
- [ ] Add `npc_agency_goals` and `npc_agency_steps` to a new `AGENCY_SCHEMA` in `schema.py`, execute it in `Database.initialize`, and implement `adopt`/`view` with uniqueness, state and timestamps.
- [ ] Run the targeted test, `ruff check world_engine/npc_agency.py --select F,I`, then inspect the actual rows.

### Task 2: 普通行动的因果与后果记录

**Files:** Modify `world_engine/actions.py`, `world_engine/event_log.py`; test `tests/test_npc_agency.py`.

**Interfaces:** Consume optional `proposal.metadata['goal_id']` and `proposal.metadata['cause_event_ids']`; produce `action_effects` rows and `event_causes` edges for accepted and rejected attempts.

- [ ] Write a failing test: execute work and a rejected buy/use attempt with goal metadata; assert the action event links to the goal cause, and the effect row contains observed money/health/location changes or a rejection reason.
- [ ] Run `pytest tests/test_npc_agency.py::test_actions_record_real_effects_and_causes -q` and confirm the absent effect/edge.
- [ ] Snapshot only relevant actor/target fields before and after `ActionService.execute`; store verified deltas or `no_state_change`, link valid causes using `EventHistoryService.link`, and record rejection without inventing success.
- [ ] Add a coverage test that iterates existing executable `ActionType` values and fails if an action lacks an effect adapter; run targeted tests and existing `tests/test_action_checks.py`.

### Task 3: 有界目标搜索与失败反馈

**Files:** Create `world_engine/npc_operators.py`; modify `world_engine/npc_agency.py`; test `tests/test_npc_agency.py`.

**Interfaces:** Produce `NpcAgencyService.propose(connection, actor, at) -> ActionProposal | None` and `observe(connection, goal_id, outcome, at) -> None`; operators expose typed preconditions, expected predicates, estimated cost and a proposal builder.

- [ ] Write failing tests for money→work→food purchase→eat and for unavailable food→alternate supply/work; assert actual `world_events`/money/food after each step.
- [ ] Run the two targeted tests and confirm no planner exists.
- [ ] Implement bounded search (depth 8, beam 16) using typed predicates and operator candidates derived from known places, inventories and `character_event_knowledge`; only emit the first action; verify real effects before advancing, back off repeated rejections.
- [ ] Run targeted tests plus `tests/test_npc_goals.py`; inspect planner output for a reachable and an unreachable goal.

### Task 4: 世界时钟集成与多领域验收

**Files:** Modify `world_engine/daily_life.py`, `world_engine/society.py`; test `tests/test_npc_agency.py`.

**Interfaces:** `NpcAgencyService.tick(connection, world_id, actor, at) -> str | None` runs after urgent care and before ordinary leisure; returns a committed action summary or `None`.

- [ ] Write failing heartbeat tests: a role based NPC adopts a goal without player input, a recipient acts only after receiving news, and an active movement prevents a second action; repeat the heartbeat and check no duplicate step.
- [ ] Run the tests and observe the absent autonomous loop.
- [ ] Call `tick` within the existing NPC life scheduling transaction, use world time and current persisted facts, respect urgent survival/appointments/active activity, and persist next retry time.
- [ ] Run related clock, social, routine, combat and movement tests; repair any conflicting scheduling order.

### Task 5: 观察接口与跨领域端到端证明

**Files:** Modify `world_engine/api_world.py`, `world_engine/web/app.js`; test `tests/test_npc_agency.py`, `tests/test_api.py`.

**Interfaces:** `GET /api/worlds/{world_id}/npc-agency/{character_id}` returns goal motive, status, current step, true effect event IDs, and cause IDs.

- [ ] Write failing API tests for knowledge restrictions and goal/action/cause chronology; add independent integration scenarios for resource→craft and opportunity→travel→combat→reward using the same planner interfaces.
- [ ] Run the tests and confirm missing endpoint or chain.
- [ ] Add a read-only query and NPC detail panel; add only data records and domain adapters needed for opportunity/contract actions, with atomic idempotent payout and no quest-specific planner branch.
- [ ] Run `pytest -q`, `ruff check world_engine tests/test_npc_agency.py --select F,I`, and `git diff --check`; review all new effects and cause edges against the spec.
