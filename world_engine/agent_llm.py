from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx
from pydantic import TypeAdapter, ValidationError

from world_engine.config import Settings
from world_engine.roleplay import build_npc_reply_messages

LOGGER = logging.getLogger("virtual-world.agent-llm")

# NPC 社交动作枚举；模型给不出合法值时退回 answer，不因此丢掉整条台词。
_SOCIAL_MOVES = frozenset(
    {"answer", "question", "evade", "boundary", "refuse", "offer"}
)


class AgentLLMError(RuntimeError):
    pass


class ModelCompletion:
    """一次 Agent 模型调用的结构化结果与可观测指标。"""

    def __init__(
        self,
        data: Any = None,
        *,
        model_name: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        latency_ms: int | None = None,
        error: str | None = None,
    ) -> None:
        self.data = data
        self.model_name = model_name
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.latency_ms = latency_ms
        self.error = error


class AgentModelBackend:
    """DeepSeek 兼容接口的结构化输出后端。

    每个调用有独立 timeout；构建带重试与退避；只返回受 Pydantic 校验的
    结构化类型，绝不直接改数据库。未配置密钥时，由调用方明确告知功能不可用。
    """

    def __init__(
        self,
        settings: Settings,
        client: httpx.Client | None = None,
    ) -> None:
        if not settings.deepseek_api_key:
            raise ValueError("Agent 模型后端需要 DEEPSEEK_API_KEY")
        self.model = settings.deepseek_model
        # HTTP 层用模型自己的超时预算。`world_agent_timeout_seconds` 是编排整体预算
        # （由 orchestration 与 Future.result 强制），拿它当单次请求超时会让
        # 稍慢的响应直接失败。
        self.timeout_seconds = settings.deepseek_timeout_seconds
        self.max_retries = settings.deepseek_max_retries
        self.max_tokens = settings.deepseek_max_output_tokens
        self.client = client or httpx.Client(
            base_url=f"{settings.deepseek_base_url}/",
            headers={
                "Authorization": f"Bearer {settings.deepseek_api_key}",
                "Content-Type": "application/json",
            },
            timeout=self.timeout_seconds,
        )

    def complete(
        self,
        *,
        system_prompt: str,
        user_payload: dict[str, object],
        schema: TypeAdapter[Any],
        label: str,
        roleplay: bool = False,
    ) -> ModelCompletion:
        """发起一次结构化解码；失败时抛出 AgentLLMError 由调用方回退规则。"""
        started = time.monotonic()
        payload: dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(user_payload, ensure_ascii=False, separators=(",", ":")),
                },
            ],
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        if roleplay:
            payload["messages"] = build_npc_reply_messages(system_prompt, user_payload)
            # 刻意不发 response_format=json_object：带推理的模型在该模式下会概率性把正文
            # 吐成空白（实测某句话 3/3 全空，finish_reason 仍是 stop、token 远未用尽），
            # 而空正文要连中三次重试才会失败。台词改由 _salvage_npc_reply 宽容解析。
            payload["max_tokens"] = min(self.max_tokens, 1200)
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.post("chat/completions", json=payload)
                if response.status_code in (429, 500, 502, 503, 504):
                    raise AgentLLMError(f"Agent[{label}]暂时不可用，HTTP {response.status_code}")
                response.raise_for_status()
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                if not isinstance(content, str) or not content.strip():
                    raise AgentLLMError(f"Agent[{label}]返回了空的决策内容")
                parsed = (
                    self._salvage_npc_reply(content, schema)
                    if roleplay
                    else self._parse(content, schema)
                )
                usage = data.get("usage") or {}
                return ModelCompletion(
                    data=parsed,
                    model_name=self.model,
                    input_tokens=usage.get("prompt_tokens"),
                    output_tokens=usage.get("completion_tokens"),
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
            except (
                httpx.HTTPError,
                KeyError,
                IndexError,
                TypeError,
                json.JSONDecodeError,
                ValueError,
                AgentLLMError,
            ) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                time.sleep(min(0.5 * (2**attempt), 2.0))
        raise AgentLLMError(f"Agent[{label}]模型调用失败") from last_error

    def close(self) -> None:
        self.client.close()

    @staticmethod
    def _salvage_npc_reply(content: str, schema: TypeAdapter[Any]) -> Any:
        """把一次 NPC 回复整理成结构化结果，优先保住台词本身。

        模型在自由文本下常常直接说出台词，也可能惯性输出 {"reply": ...}，或被
        ``` 围栏包住。两种都接受：认得出 reply 就取它（顺带保留 social_move），
        认不出就把整段正文当作台词。**降级而不是报错**——不要为格式牺牲内容。
        """
        text = content.strip()
        if text.startswith("```"):
            text = "\n".join(
                line for line in text.splitlines() if not line.strip().startswith("```")
            ).strip()
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            try:
                raw = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                raw = None
            if isinstance(raw, dict) and isinstance(raw.get("reply"), str) and raw["reply"].strip():
                # 只挑出 schema 认识的字段：NpcReply 是 extra="forbid"，
                # 模型多写一个 topic 就会让整段 JSON 掉进下面的降级分支。
                move = raw.get("social_move")
                if move not in _SOCIAL_MOVES:
                    move = "answer"
                try:
                    return schema.validate_python(
                        {"reply": raw["reply"].strip(), "social_move": move}
                    )
                except ValidationError:
                    pass
        return schema.validate_python({"reply": text[:500], "social_move": "answer"})

    @staticmethod
    def _parse(content: str, schema: TypeAdapter[Any]) -> Any:
        text = content.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end < start:
            raise AgentLLMError("Agent响应中没有JSON对象")
        raw = json.loads(text[start : end + 1])
        return schema.validate_python(raw)


def build_agent_model_backend(settings: Settings) -> AgentModelBackend | None:
    """只要配置了模型密钥就构建后端，供 NPC 对话和可选世界编排共用。"""
    if settings.deepseek_api_key:
        try:
            return AgentModelBackend(settings)
        except Exception:
            # 后端构建失败只降级为规则决策，但要留下可排查的痕迹。
            LOGGER.exception("Agent 模型后端构建失败，本次运行将回退规则决策")
            return None
    return None
