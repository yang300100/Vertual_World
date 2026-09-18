"""`respond_to_player` 必须走宽容解析，而不是严格 JSON 契约。

回归自：`DeepSeekDecisionProvider.respond_to_player` 原先发
`response_format={"type": "json_object"}` 并做严格 JSON 校验。带推理的模型
在该模式下会**概率性把正文吐成空白或纯文本**（实测约 1/6），于是抛
「DeepSeek返回了空的NPC对话内容」，重试三次后玩家看到
「NPC 对话模型暂时不可用，请稍后重试」。

`agent_llm` 的 roleplay 路径在提交 5a91ed4 已经修过；玩家对话这条路径漏了。

解析行为本身由 `tests/test_roleplay.py::test_npc_reply_salvage_keeps_the_line`
覆盖（参数化用例），这里只验证 **provider 端到端** 确实接上了宽容解析，
以及错误处理没有被放宽。
"""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest

from world_engine.decisions import DecisionProviderError, DeepSeekDecisionProvider


@pytest.fixture
def llm_settings(settings):
    """DeepSeekDecisionProvider 要求配置密钥才肯构造；这里用假密钥走注入的 client。"""
    return replace(settings, deepseek_api_key="test-key-not-real")


class _FakeResponse:
    status_code = 200

    def __init__(self, content: str) -> None:
        self._content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {"choices": [{"message": {"content": self._content}}], "usage": {}}


class _FakeClient:
    def __init__(self, content: str) -> None:
        self._content = content

    def post(self, *args: object, **kwargs: object) -> _FakeResponse:
        return _FakeResponse(self._content)

    def close(self) -> None:
        return None


def _ask(llm_settings, content: str):
    provider = DeepSeekDecisionProvider(llm_settings, client=_FakeClient(content))
    try:
        return provider.respond_to_player(
            npc=None, player=None, context={"player_text": "你好呀"}
        )
    finally:
        provider.close()


def test_respond_to_player_keeps_plain_text_line(llm_settings) -> None:
    """模型直接说出台词（无任何 JSON）时，provider 也应返回这句话。"""
    reply = _ask(llm_settings, "你好呀，旅人。河边风大，进来坐坐。")
    assert reply.reply == "你好呀，旅人。河边风大，进来坐坐。"
    assert reply.social_move == "answer"


def test_respond_to_player_truncates_overlong_line(llm_settings) -> None:
    """超长台词截断到上限，而不是整句丢弃。"""
    reply = _ask(llm_settings, "很长的台词。" * 200)
    assert 0 < len(reply.reply) <= 500


def test_respond_to_player_rejects_truly_empty_content(llm_settings) -> None:
    """宽容不等于放过空响应——正文真的为空时仍要失败。"""
    with pytest.raises(DecisionProviderError, match="空"):
        _ask(llm_settings, "   \n  ")


def test_respond_to_player_does_not_swallow_timeout(llm_settings) -> None:
    """网络层错误不应被宽容解析吞掉，否则会把断网伪装成正常对话。"""

    class _TimeoutClient:
        def post(self, *args: object, **kwargs: object):
            raise httpx.ReadTimeout("模拟超时")

        def close(self) -> None:
            return None

    provider = DeepSeekDecisionProvider(llm_settings, client=_TimeoutClient())  # type: ignore[arg-type]
    try:
        with pytest.raises(DecisionProviderError):
            provider.respond_to_player(npc=None, player=None, context={"player_text": "你好"})
    finally:
        provider.close()
