"""有界事实搜索；行动的预测只用于规划，不写入世界。"""

from __future__ import annotations

from dataclasses import dataclass, field
from heapq import heapify, heappop, heappush, nsmallest
from itertools import count
from typing import Literal

from world_engine.domain import ActionProposal, ActionType

FactValue = int | str | bool


@dataclass(frozen=True)
class Condition:
    key: str
    value: FactValue
    comparison: Literal["eq", "ne", "ge", "le"] = "eq"

    def holds(self, state: dict[str, FactValue]) -> bool:
        actual = state.get(self.key, 0)
        if self.comparison == "eq":
            return actual == self.value
        if self.comparison == "ne":
            return actual != self.value
        if self.comparison == "ge":
            return actual >= self.value
        return actual <= self.value


@dataclass(frozen=True)
class Effect:
    key: str
    value: FactValue
    additive: bool = False
    minimum: int | None = None
    maximum: int | None = None

    def apply(self, state: dict[str, FactValue]) -> None:
        value = state.get(self.key, 0) + self.value if self.additive else self.value
        if self.minimum is not None:
            value = max(self.minimum, value)
        if self.maximum is not None:
            value = min(self.maximum, value)
        state[self.key] = value


@dataclass(frozen=True)
class DomainOperation:
    name: str
    action_type: ActionType
    arguments: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class PlanningOperator:
    name: str
    preconditions: tuple[Condition, ...]
    effects: tuple[Effect, ...]
    action: ActionProposal | DomainOperation
    cost: int = 1

    def predict(self, state: dict[str, FactValue]) -> dict[str, FactValue] | None:
        if not all(condition.holds(state) for condition in self.preconditions):
            return None
        predicted = dict(state)
        for effect in self.effects:
            effect.apply(predicted)
        return predicted if predicted != state else None


def search(
    initial: dict[str, FactValue], goals: tuple[Condition, ...],
    operators: list[PlanningOperator], *, max_depth: int = 8, beam_width: int = 16,
) -> tuple[PlanningOperator, ...]:
    """Yuki 只找有限步候选；执行后必须重新从真实事实搜索。"""
    sequence = count()
    frontier = [(0, 0, next(sequence), initial, ())]
    visited: set[tuple[tuple[str, FactValue], ...]] = set()

    def deficit(state):
        total = 0.0
        for goal in goals:
            value = state.get(goal.key, 0)
            if goal.comparison == "ge":
                total += max(0, goal.value - value) / max(1, abs(goal.value))
            elif goal.comparison == "le":
                total += max(0, value - goal.value) / max(1, abs(goal.value))
            else:
                total += int(not goal.holds(state))
        return total

    initial_deficit = deficit(initial)
    best_progress = 0.0
    best_path = ()
    while frontier:
        cost, depth, _stable, state, path = heappop(frontier)
        if all(goal.holds(state) for goal in goals):
            return path
        remaining = deficit(state)
        progress = (initial_deficit - remaining) / max(1, cost)
        if path and progress > best_progress:
            best_progress, best_path = progress, path
        fingerprint = tuple(sorted(state.items()))
        if fingerprint in visited or depth >= max_depth:
            continue
        visited.add(fingerprint)
        for operator in sorted(operators, key=lambda item: (item.cost, item.name)):
            predicted = operator.predict(state)
            if predicted is not None:
                heappush(frontier, (cost + operator.cost, depth + 1, next(sequence),
                                    predicted, path + (operator,)))
        if len(frontier) > beam_width:
            frontier[:] = nsmallest(beam_width, frontier)
            heapify(frontier)
    # 长期目标超出搜索窗口时，Yuki 仍允许先做可验证的改善，再从真实结果重规划。
    return best_path
