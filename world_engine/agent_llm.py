from __future__ import annotations

import json
import logging
import time
from typing import Any
from uuid import uuid4

import httpx
from pydantic import TypeAdapter

from world_engine.config import Settings
from world_engine.diagnostic_logging import diagnostic_json, redact_sensitive_text
from world_engine.model_availability import (
    ModelBillingError,
    check_model_billing,
    record_model_billing_failure,
)
from world_engine.roleplay import salvage_npc_reply
from world_engine.roleplay_pipeline import (
    SamplingIntent,
    build_deepseek_roleplay_payload,
    build_npc_prompt,
)

LOGGER = logging.getLogger("virtual-world.agent-llm")


class AgentLLMError(RuntimeError):
    """哥哥，接口失败时也保留可核验的原因与已知用量。"""

    def __init__(self, message, *, input_tokens=None, output_tokens=None, diagnostics=None):
        super().__init__(message)
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.diagnostics = diagnostics or {}


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
        diagnostics: dict[str, object] | None = None,
    ) -> None:
        self.data = data
        self.model_name = model_name
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.latency_ms = latency_ms
        self.error = error
        self.diagnostics = diagnostics or {}


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
        self.base_url = settings.deepseek_base_url
        # HTTP 层用模型自己的超时预算。`world_agent_timeout_seconds` 是编排整体预算
        # （由 orchestration 与 Future.result 强制），拿它当单次请求超时会让
        # 稍慢的响应直接失败。
        self.timeout_seconds = settings.deepseek_timeout_seconds
        self.max_retries = settings.deepseek_max_retries
        self.max_tokens = settings.deepseek_max_output_tokens
        self.roleplay_input_tokens = settings.dialogue_context_max_tokens
        self.roleplay_temperature = settings.npc_temperature
        self.roleplay_top_p = settings.npc_top_p
        self.debug_model_io = settings.debug_model_io
        self._diagnostic_secrets = (settings.deepseek_api_key,)
        LOGGER.setLevel(logging.DEBUG if self.debug_model_io else logging.NOTSET)
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
        max_output_tokens: int | None = None,
        thinking: bool | None = None,
    ) -> ModelCompletion:
        """发起一次结构化解码；失败时抛出 AgentLLMError 由调用方回退规则。"""
        started = time.monotonic()
        call_id = str(uuid4())
        payload: dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(user_payload, ensure_ascii=False, separators=(",", ":")),
                },
            ],
            "max_tokens": max_output_tokens or self.max_tokens,
            "stream": False,
        }
        # 哥哥，只有显式选择的结构化请求覆盖推理模式，其余调用保留原行为。
        if not roleplay and thinking is not None:
            payload["thinking"] = {"type": "enabled" if thinking else "disabled"}
        if roleplay:
            prompt = build_npc_prompt(
                system_prompt,
                user_payload,
                max_input_tokens=self.roleplay_input_tokens,
            )
            # 刻意不发 response_format=json_object：带推理的模型在该模式下会概率性把正文
            # 吐成空白（实测某句话 3/3 全空，finish_reason 仍是 stop、token 远未用尽），
            # 而空正文要连中三次重试才会失败。台词改由 _salvage_npc_reply 宽容解析。
            payload = build_deepseek_roleplay_payload(
                prompt,
                self.model,
                SamplingIntent(
                    max_output_tokens=min(max_output_tokens or self.max_tokens, 1200),
                    temperature=self.roleplay_temperature,
                    top_p=self.roleplay_top_p,
                ),
            )
        if self.debug_model_io:
            LOGGER.debug(
                "模型请求已组装 call_id=%s label=%s model=%s payload=%s",
                call_id,
                label,
                self.model,
                diagnostic_json(payload, self._diagnostic_secrets),
            )
        last_error: Exception | None = None
        input_tokens = output_tokens = None
        details: dict[str, object] = {}
        attempt_history = []

        def preserve_attempt_usage():
            attempt_history.append(
                {
                    "attempt": details["attempts"],
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "http_status": details.get("http_status"),
                    "finish_reason": details.get("finish_reason"),
                    "error_code": details.get("error_code"),
                }
            )
            details["attempt_history"] = attempt_history
            details["known_input_tokens"] = sum(
                item["input_tokens"] or 0 for item in attempt_history
            )
            details["known_output_tokens"] = sum(
                item["output_tokens"] or 0 for item in attempt_history
            )
            details["unknown_usage_attempts"] = sum(
                item["input_tokens"] is None or item["output_tokens"] is None
                for item in attempt_history
            )

        for attempt in range(self.max_retries + 1):
            input_tokens = output_tokens = None
            details = {
                "stage": "request",
                "requested_max_tokens": payload["max_tokens"],
                "attempts": attempt + 1,
            }
            try:
                check_model_billing(self.base_url)
                response = self.client.post("chat/completions", json=payload)
                details.update(
                    {
                        "stage": "response",
                        "http_status": response.status_code,
                        "response_body": redact_sensitive_text(
                            getattr(response, "text", ""), self._diagnostic_secrets
                        )[:64000],
                    }
                )
                if self.debug_model_io:
                    LOGGER.debug(
                        "模型 HTTP 原始响应 call_id=%s label=%s attempt=%s status=%s body=%s",
                        call_id,
                        label,
                        attempt + 1,
                        response.status_code,
                        diagnostic_json(response.text, self._diagnostic_secrets),
                    )
                if response.status_code == 402:
                    record_model_billing_failure(self.base_url)
                if response.status_code in (429, 500, 502, 503, 504):
                    details["error_code"] = "http_error"
                    raise AgentLLMError(f"Agent[{label}]暂时不可用，HTTP {response.status_code}")
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise AgentLLMError("模型响应不是 JSON 对象")
                # 用量先于内容解析保存；截断或格式不合格也不能被记成未知或零消耗。
                usage = data.get("usage") or {}
                if not isinstance(usage, dict):
                    usage = {}
                input_tokens = usage.get("prompt_tokens")
                output_tokens = usage.get("completion_tokens")
                if (
                    not isinstance(input_tokens, int)
                    or isinstance(input_tokens, bool)
                    or input_tokens < 0
                ):
                    input_tokens = None
                if (
                    not isinstance(output_tokens, int)
                    or isinstance(output_tokens, bool)
                    or output_tokens < 0
                ):
                    output_tokens = None
                choice = data["choices"][0]
                finish_reason = choice.get("finish_reason")
                details["finish_reason"] = (
                    redact_sensitive_text(finish_reason, self._diagnostic_secrets)
                    if isinstance(finish_reason, str)
                    else None
                )
                token_details = usage.get("completion_tokens_details") or {}
                reasoning_tokens = (
                    token_details.get("reasoning_tokens")
                    if isinstance(token_details, dict)
                    else None
                )
                details["reasoning_tokens"] = (
                    reasoning_tokens
                    if isinstance(reasoning_tokens, int)
                    and not isinstance(reasoning_tokens, bool)
                    and reasoning_tokens >= 0
                    else None
                )
                content = data["choices"][0]["message"]["content"]
                reasoning = choice["message"].get("reasoning_content") or ""
                details["reasoning_chars"] = len(reasoning) if isinstance(reasoning, str) else None
                details["content"] = redact_sensitive_text(content, self._diagnostic_secrets)[
                    :64000
                ]
                if choice.get("finish_reason") == "length" and not roleplay:
                    details["error_code"] = "output_truncated"
                    raise AgentLLMError(
                        f"Agent[{label}]输出被截断（finish_reason=length，输出上限{payload['max_tokens']}）"
                    )
                if self.debug_model_io:
                    LOGGER.debug(
                        "模型正文 call_id=%s label=%s attempt=%s content=%s",
                        call_id,
                        label,
                        attempt + 1,
                        diagnostic_json(content, self._diagnostic_secrets),
                    )
                if not isinstance(content, str) or not content.strip():
                    details["error_code"] = "empty_content"
                    raise AgentLLMError(f"Agent[{label}]返回了空的决策内容")
                details["stage"] = "parse"
                parsed = (
                    salvage_npc_reply(content, schema) if roleplay else self._parse(content, schema)
                )
                if self.debug_model_io:
                    parsed_data = (
                        parsed.model_dump(mode="json") if hasattr(parsed, "model_dump") else parsed
                    )
                    LOGGER.debug(
                        "模型解析结果 call_id=%s label=%s result=%s",
                        call_id,
                        label,
                        diagnostic_json(parsed_data, self._diagnostic_secrets),
                    )
                LOGGER.info(
                    "模型请求完成 label=%s model=%s prompt_tokens=%s "
                    "completion_tokens=%s attempts=%d",
                    label,
                    self.model,
                    input_tokens,
                    output_tokens,
                    attempt + 1,
                )
                preserve_attempt_usage()
                return ModelCompletion(
                    data=parsed,
                    model_name=self.model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    diagnostics=details,
                )
            except ModelBillingError as exc:
                details.update(
                    {
                        "error_code": "billing_error",
                        "cause_type": type(exc).__name__,
                        "cause": str(exc),
                    }
                )
                preserve_attempt_usage()
                raise AgentLLMError(
                    str(exc),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    diagnostics=details,
                ) from exc
            except (
                httpx.HTTPError,
                KeyError,
                IndexError,
                TypeError,
                json.JSONDecodeError,
                ValueError,
                AgentLLMError,
                AttributeError,
            ) as exc:
                last_error = exc
                details.setdefault(
                    "error_code",
                    "transport_error"
                    if details["stage"] == "request"
                    else "invalid_program"
                    if details["stage"] == "parse"
                    else "invalid_response",
                )
                details["cause_type"] = type(exc).__name__
                details["cause"] = redact_sensitive_text(str(exc), self._diagnostic_secrets)
                preserve_attempt_usage()
                LOGGER.warning(
                    "模型请求失败 label=%s model=%s attempt=%d/%d error=%s "
                    "cause=%s finish_reason=%s",
                    label,
                    self.model,
                    attempt + 1,
                    self.max_retries + 1,
                    type(exc).__name__,
                    details["cause"],
                    details.get("finish_reason"),
                )
                if attempt >= self.max_retries:
                    break
                time.sleep(min(0.5 * (2**attempt), 2.0))
        raise AgentLLMError(
            f"Agent[{label}]模型调用失败：{details.get('cause', '')}",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            diagnostics=details,
        ) from last_error

    def close(self) -> None:
        self.client.close()

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
