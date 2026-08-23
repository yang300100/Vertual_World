from __future__ import annotations

import json
import time
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from world_engine.config import Settings
from world_engine.domain import ActionProposal, ActionType, CharacterState, WorldSnapshot


class DecisionProviderError(RuntimeError):
    pass


class DecisionBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decisions: list[ActionProposal] = Field(default_factory=list)


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


class DeepSeekDecisionProvider:
    """通过DeepSeek兼容接口批量生成结构化人物行动。"""

    name = "deepseek"

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        if not settings.deepseek_api_key:
            raise ValueError("WORLD_DECISION_PROVIDER=deepseek时必须配置DEEPSEEK_API_KEY")
        self.model = settings.deepseek_model
        self.max_retries = settings.deepseek_max_retries
        self.max_output_tokens = settings.deepseek_max_output_tokens
        self.client = client or httpx.Client(
            base_url=f"{settings.deepseek_base_url}/",
            headers={
                "Authorization": f"Bearer {settings.deepseek_api_key}",
                "Content-Type": "application/json",
            },
            timeout=settings.deepseek_timeout_seconds,
        )

    def propose(
        self, snapshot: WorldSnapshot, characters: list[CharacterState]
    ) -> list[ActionProposal]:
        if not characters:
            return []
        response_data = self._request(self._build_request(snapshot, characters))
        try:
            content = response_data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise DecisionProviderError("DeepSeek响应缺少choices.message.content") from exc
        if not isinstance(content, str) or not content.strip():
            raise DecisionProviderError("DeepSeek返回了空的决策内容")
        return self._parse_decisions(content)

    def close(self) -> None:
        self.client.close()

    def _request(self, payload: dict[str, object]) -> dict[str, object]:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.post("chat/completions", json=payload)
                if response.status_code == 429 or response.status_code >= 500:
                    raise DecisionProviderError(
                        f"DeepSeek暂时不可用，HTTP {response.status_code}"
                    )
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise DecisionProviderError("DeepSeek响应不是JSON对象")
                return data
            except (httpx.HTTPError, ValueError, DecisionProviderError) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                time.sleep(min(0.5 * (2**attempt), 2.0))
        raise DecisionProviderError("DeepSeek决策请求失败，已交由规则引擎降级") from last_error

    def _build_request(
        self, snapshot: WorldSnapshot, characters: list[CharacterState]
    ) -> dict[str, object]:
        active_ids = {item.id for item in characters}
        context = {
            "world": {
                "id": snapshot.world.id,
                "name": snapshot.world.name,
                "current_time": snapshot.world.current_time.isoformat(),
                "tick_count": snapshot.world.tick_count,
            },
            "locations": [item.model_dump(mode="json") for item in snapshot.locations],
            "active_characters": [
                {
                    **item.model_dump(mode="json"),
                    "nearby_characters": [
                        {"id": nearby.id, "name": nearby.name}
                        for nearby in snapshot.characters
                        if nearby.id != item.id and nearby.location_id == item.location_id
                    ],
                }
                for item in characters
            ],
            "allowed_actor_ids": sorted(active_ids),
        }
        system_prompt = (
            "你是虚拟世界中的人物决策器，不是世界裁判。"
            "只能为allowed_actor_ids中的每个人物提出一个行动，不能宣告行动成功。"
            "允许的action只有rest、eat、work、travel、socialize、idle。"
            "travel必须填写有效destination_id；socialize必须填写同地点人物target_id；"
            "其他行动的target_id和destination_id使用null。"
            "只输出一个JSON对象，不要Markdown、解释或代码围栏。"
            "格式必须是：{\"decisions\":[{\"actor_id\":\"...\","
            "\"action\":\"idle\",\"reason\":\"...\",\"target_id\":null,"
            "\"destination_id\":null,\"metadata\":{}}]}。"
        )
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(context, ensure_ascii=False, separators=(",", ":")),
                },
            ],
            "max_tokens": self.max_output_tokens,
            "stream": False,
        }

    @staticmethod
    def _parse_decisions(content: str) -> list[ActionProposal]:
        normalized = content.strip()
        if normalized.startswith("```"):
            lines = normalized.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            normalized = "\n".join(lines).strip()
        start = normalized.find("{")
        end = normalized.rfind("}")
        if start < 0 or end < start:
            raise DecisionProviderError("DeepSeek响应中没有JSON对象")
        try:
            payload = json.loads(normalized[start : end + 1])
            return DecisionBatch.model_validate(payload).decisions
        except (json.JSONDecodeError, ValidationError) as exc:
            raise DecisionProviderError("DeepSeek决策未通过结构化校验") from exc


def build_decision_provider(settings: Settings) -> DecisionProvider:
    if settings.decision_provider == "rules":
        return RuleDecisionProvider()
    if settings.decision_provider == "deepseek":
        return DeepSeekDecisionProvider(settings)
    raise ValueError(f"不支持的WORLD_DECISION_PROVIDER：{settings.decision_provider}")
