"""额度错误应立即返回明确原因，且短时间内不重复请求。"""

from dataclasses import replace

import pytest
from pydantic import BaseModel, TypeAdapter

from world_engine.agent_llm import AgentLLMError, AgentModelBackend
from world_engine.decisions import DecisionProviderError, DeepSeekDecisionProvider


class BillingResponse:
    status_code = 402

    def raise_for_status(self):
        raise AssertionError("402 应在常规 HTTP 错误处理之前被识别")


class BillingClient:
    def __init__(self):
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        return BillingResponse()


class Sample(BaseModel):
    name: str


def test_billing_error_skips_retries_and_shared_backend_calls(settings):
    config = replace(
        settings,
        decision_provider="deepseek",
        deepseek_api_key="test-secret",
        deepseek_base_url="https://billing-test.invalid",
        deepseek_max_retries=2,
    )
    client = BillingClient()
    provider = DeepSeekDecisionProvider(config, client=client)
    backend = AgentModelBackend(config, client=client)

    with pytest.raises(DecisionProviderError, match="额度"):
        provider._request({"model": "test"})
    assert client.calls == 1
    with pytest.raises(AgentLLMError, match="额度"):
        backend.complete(
            system_prompt="test", user_payload={}, schema=TypeAdapter(Sample), label="test"
        )
    assert client.calls == 1
