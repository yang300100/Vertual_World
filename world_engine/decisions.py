from __future__ import annotations

from typing import Protocol

from world_engine.domain import ActionProposal, ActionType, CharacterState, WorldSnapshot


class DecisionProvider(Protocol):
    """人物决策器协议；第三方模型只能通过该边界提交动作。"""

    name: str

    def propose(
        self, snapshot: WorldSnapshot, characters: list[CharacterState]
    ) -> list[ActionProposal]: ...


class RuleDecisionProvider:
    """无需模型即可长期运行的确定性规则决策器。"""

    name = "rules"

    def propose(
        self, snapshot: WorldSnapshot, characters: list[CharacterState]
    ) -> list[ActionProposal]:
        proposals: list[ActionProposal] = []
        for character in characters:
            proposals.append(self._decide(snapshot, character))
        return proposals

    def _decide(self, snapshot: WorldSnapshot, character: CharacterState) -> ActionProposal:
        if character.hunger >= 70 and character.money >= 3:
            return ActionProposal(
                actor_id=character.id,
                action=ActionType.EAT,
                reason="饥饿已经明显影响状态，决定先寻找食物。",
            )
        if character.energy <= 30:
            return ActionProposal(
                actor_id=character.id,
                action=ActionType.REST,
                reason="精力不足，决定暂时休息恢复体力。",
            )
        if character.money < 10 and character.energy >= 35:
            return ActionProposal(
                actor_id=character.id,
                action=ActionType.WORK,
                reason="手头的钱不多，决定通过工作获得收入。",
            )

        nearby = [
            item
            for item in snapshot.characters
            if item.id != character.id and item.location_id == character.location_id
        ]
        if nearby and snapshot.world.tick_count % 3 == 0:
            target = sorted(nearby, key=lambda item: item.name)[0]
            return ActionProposal(
                actor_id=character.id,
                action=ActionType.SOCIALIZE,
                target_id=target.id,
                reason=f"注意到{target.name}也在附近，决定与对方交流。",
            )

        destinations = sorted(
            [item for item in snapshot.locations if item.id != character.location_id],
            key=lambda item: item.name,
        )
        if destinations and snapshot.world.tick_count % 4 == 3:
            destination = destinations[0]
            return ActionProposal(
                actor_id=character.id,
                action=ActionType.TRAVEL,
                destination_id=destination.id,
                reason=f"当前没有紧急事务，决定前往{destination.name}看看。",
            )

        return ActionProposal(
            actor_id=character.id,
            action=ActionType.IDLE,
            reason="暂时没有更紧迫的目标，选择观察周围并保存精力。",
        )

