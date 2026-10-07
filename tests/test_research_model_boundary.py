"""哥哥，这些接口边界检查只使用离线 HTTP 响应，不会消耗你的 API。"""

import json
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import TypeAdapter

from world_engine.agent_llm import AgentLLMError, AgentModelBackend
from world_engine.research_types import ResearchProgram


def test_paid_reply_syntax_aliases_preserve_the_npc_commands():
    program = ResearchProgram.model_validate(
        {
            "reason": "按自己的判断读取并调节",
            "commands": [
                {"action": "measure"},
                {"action": "configure", "settings": {"feed": 0.9, "tune": 0.123}},
            ],
        }
    )
    assert [c.op for c in program.commands] == ["measure", "configure"]
    assert program.commands[1].settings == [{"feed": 0.9, "tune": 0.123}]


@pytest.mark.parametrize(
    "command",
    [
        {"op": "measure", "action": "send"},
        {"action": "experiment", "settings": {"tune": 0.2}},
        {"action": "configure", "settings": {"tune": 0.2}, "invented": True},
    ],
)
def test_syntax_aliases_do_not_infer_or_discard_ambiguous_commands(command):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ResearchProgram.model_validate({"reason": "本人决定", "commands": [command]})


def test_research_request_transmits_the_executable_json_contract(database, settings):
    from scripts.demo_autonomous_disaster import build_scenario
    from world_engine.research_runner import ResearchRunner

    requests = []

    def reply(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": '{"reason":"先读取仪表","commands":[{"op":"measure"}]}'
                        },
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 30},
            },
        )

    wid, _, at = build_scenario(database)
    backend = AgentModelBackend(
        replace(settings, deepseek_api_key="offline-secret", deepseek_max_retries=0),
        client=httpx.Client(
            base_url="https://research-test.invalid/", transport=httpx.MockTransport(reply)
        ),
    )
    try:
        assert ResearchRunner(database, backend).decide(wid, at)
        prompt = requests[0]["messages"][0]["content"]
        contract = json.loads(prompt.split("可执行 JSON Schema：\n", 1)[1])
        command = contract["$defs"]["ResearchCommand"]
        assert command["required"] == ["op"]
        assert command["properties"]["settings"]["type"] == "array"
        assert command["properties"]["op"]["enum"] == [
            "measure",
            "experiment",
            "configure",
            "send",
            "wait",
        ]
    finally:
        backend.close()


def truncated_response(request):
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {"content": "", "reasoning_content": "推理尚未完成"},
                }
            ],
            "usage": {
                "prompt_tokens": 1200,
                "completion_tokens": 1800,
                "completion_tokens_details": {"reasoning_tokens": 1800},
            },
        },
    )


def test_truncated_reply_preserves_finish_reason_usage_and_underlying_error(settings):
    backend = AgentModelBackend(
        replace(settings, deepseek_api_key="offline-secret", deepseek_max_retries=0),
        client=httpx.Client(
            base_url="https://research-test.invalid/",
            transport=httpx.MockTransport(truncated_response),
        ),
    )
    try:
        with pytest.raises(AgentLLMError) as caught:
            backend.complete(
                system_prompt="测量",
                user_payload={},
                schema=TypeAdapter(ResearchProgram),
                label="test",
                max_output_tokens=1800,
            )
        error = caught.value
        assert error.input_tokens == 1200 and error.output_tokens == 1800
        assert error.diagnostics["finish_reason"] == "length"
        assert error.diagnostics["http_status"] == 200
        assert error.diagnostics["reasoning_tokens"] == 1800
        assert error.diagnostics["error_code"] == "output_truncated"
    finally:
        backend.close()


def test_failed_http_reply_redacts_credentials_and_keeps_status(settings):
    def unavailable(request):
        return httpx.Response(503, text="Authorization: Bearer offline-secret; temporary failure")

    backend = AgentModelBackend(
        replace(settings, deepseek_api_key="offline-secret", deepseek_max_retries=0),
        client=httpx.Client(
            base_url="https://research-test.invalid/", transport=httpx.MockTransport(unavailable)
        ),
    )
    try:
        with pytest.raises(AgentLLMError) as caught:
            backend.complete(
                system_prompt="测量",
                user_payload={},
                schema=TypeAdapter(ResearchProgram),
                label="test",
            )
        error = caught.value
        assert error.diagnostics["http_status"] == 503
        assert "offline-secret" not in json.dumps(error.diagnostics)
        assert "503" in str(error)
    finally:
        backend.close()


def test_response_metadata_and_warning_redact_echoed_credentials(settings, caplog):
    def echoed(request):
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "offline-secret", "message": {"content": ""}}],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 10,
                    "completion_tokens_details": {"reasoning_tokens": "offline-secret"},
                },
            },
        )

    backend = AgentModelBackend(
        replace(settings, deepseek_api_key="offline-secret", deepseek_max_retries=0),
        client=httpx.Client(
            base_url="https://research-test.invalid/", transport=httpx.MockTransport(echoed)
        ),
    )
    try:
        with pytest.raises(AgentLLMError) as caught:
            backend.complete(
                system_prompt="测量",
                user_payload={},
                schema=TypeAdapter(ResearchProgram),
                label="test",
            )
        assert "offline-secret" not in json.dumps(caught.value.diagnostics)
        assert "offline-secret" not in caplog.text
        assert caught.value.diagnostics["reasoning_tokens"] is None
    finally:
        backend.close()


def test_success_log_uses_validated_usage_instead_of_echoed_credentials(settings, caplog):
    import logging

    def reply(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"reason":"等待新证据","commands":[]}'},
                    }
                ],
                "usage": {"prompt_tokens": "offline-secret", "completion_tokens": 10},
            },
        )

    backend = AgentModelBackend(
        replace(settings, deepseek_api_key="offline-secret", deepseek_max_retries=0),
        client=httpx.Client(
            base_url="https://research-test.invalid/", transport=httpx.MockTransport(reply)
        ),
    )
    caplog.set_level(logging.INFO, logger="virtual-world.agent-llm")
    try:
        completion = backend.complete(
            system_prompt="测量", user_payload={}, schema=TypeAdapter(ResearchProgram), label="test"
        )
        assert completion.input_tokens is None and completion.output_tokens == 10
        assert "offline-secret" not in caplog.text
    finally:
        backend.close()


def test_malformed_choice_keeps_paid_usage_and_halts_research(database, settings):
    from scripts.demo_autonomous_disaster import build_scenario
    from world_engine.research_runner import ResearchRunner

    def malformed(request):
        return httpx.Response(
            200,
            json={"choices": [None], "usage": {"prompt_tokens": 1200, "completion_tokens": 900}},
        )

    wid, _, at = build_scenario(database)
    backend = AgentModelBackend(
        replace(settings, deepseek_api_key="offline-secret", deepseek_max_retries=0),
        client=httpx.Client(
            base_url="https://research-test.invalid/", transport=httpx.MockTransport(malformed)
        ),
    )
    runner = ResearchRunner(database, backend)
    try:
        assert runner.decide(wid, at)
        assert runner.failure_reason(wid)
        assert not runner.decide(wid, at)
        with database.read() as c:
            row = c.execute(
                "SELECT input_tokens,output_tokens,diagnostics_json FROM research_calls"
            ).fetchone()
            assert row["input_tokens"] + row["output_tokens"] == 2100
            assert json.loads(row["diagnostics_json"])["http_status"] == 200
    finally:
        backend.close()


def test_retry_diagnostics_do_not_lose_paid_truncated_attempt(settings, monkeypatch):
    responses = iter([truncated_response(None), httpx.Response(503, text="temporary failure")])
    monkeypatch.setattr("world_engine.agent_llm.time.sleep", lambda _: None)
    backend = AgentModelBackend(
        replace(settings, deepseek_api_key="offline-secret", deepseek_max_retries=1),
        client=httpx.Client(
            base_url="https://research-test.invalid/",
            transport=httpx.MockTransport(lambda request: next(responses)),
        ),
    )
    try:
        with pytest.raises(AgentLLMError) as caught:
            backend.complete(
                system_prompt="测量",
                user_payload={},
                schema=TypeAdapter(ResearchProgram),
                label="test",
            )
        details = caught.value.diagnostics
        assert details["http_status"] == 503
        assert details["known_input_tokens"] == 1200
        assert details["known_output_tokens"] == 1800
        assert details["unknown_usage_attempts"] == 1
    finally:
        backend.close()


def test_roleplay_can_still_salvage_a_complete_reply_with_length_flag(settings):
    from world_engine.decisions import NpcReply

    def reply(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "length", "message": {"content": '{"reply":"我先去休息。"}'}}
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 1200},
            },
        )

    backend = AgentModelBackend(
        replace(settings, deepseek_api_key="offline-secret", deepseek_max_retries=0),
        client=httpx.Client(
            base_url="https://research-test.invalid/", transport=httpx.MockTransport(reply)
        ),
    )
    try:
        completion = backend.complete(
            system_prompt="台词",
            user_payload={},
            schema=TypeAdapter(NpcReply),
            label="dialogue",
            roleplay=True,
        )
        assert completion.data.reply == "我先去休息。"
        assert completion.output_tokens == 1200
    finally:
        backend.close()


def test_research_uses_configured_output_budget_and_accepts_complete_reply(database, settings):
    from scripts.demo_autonomous_disaster import build_scenario
    from world_engine.engine import WorldEngine

    def model(request):
        payload = json.loads(request.content)
        if payload["max_tokens"] < 8000:
            return truncated_response(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": '{"reason":"先取得本地读数","commands":[{"op":"measure"}]}'
                        },
                    }
                ],
                "usage": {"prompt_tokens": 1200, "completion_tokens": 6100},
            },
        )

    wid, _, at = build_scenario(database)
    config = replace(
        settings,
        decision_provider="rules",
        world_agent_enabled=False,
        research_api_enabled=True,
        deepseek_api_key="offline-secret",
        deepseek_max_output_tokens=8192,
        deepseek_max_retries=0,
    )
    engine = WorldEngine(database, config)
    original = engine.research_runner.backend
    engine.research_runner.backend = AgentModelBackend(
        config,
        client=httpx.Client(
            base_url="https://research-test.invalid/", transport=httpx.MockTransport(model)
        ),
    )
    try:
        assert engine.research_runner.decide(wid, at)
        with database.read() as c:
            row = c.execute(
                "SELECT status,input_tokens,output_tokens FROM research_calls"
            ).fetchone()
            assert row["status"] == "succeeded"
            assert row["input_tokens"] + row["output_tokens"] == 7300
            assert c.execute("SELECT count(*) FROM research_programs").fetchone()[0] == 1
    finally:
        engine.close()
        original.close()


def test_research_protocol_failure_is_metered_and_does_not_repeat_on_another_npc(
    database, settings
):
    from scripts.demo_autonomous_disaster import build_scenario
    from world_engine.research_runner import ResearchRunner

    wid, _, at = build_scenario(database)
    backend = AgentModelBackend(
        replace(settings, deepseek_api_key="offline-secret", deepseek_max_retries=0),
        client=httpx.Client(
            base_url="https://research-test.invalid/",
            transport=httpx.MockTransport(truncated_response),
        ),
    )
    runner = ResearchRunner(database, backend)
    try:
        assert runner.decide(wid, at)
        assert not runner.decide(wid, datetime(2040, 4, 1, 10, tzinfo=UTC))
        with database.read() as c:
            rows = c.execute("SELECT * FROM research_calls").fetchall()
            assert len(rows) == 1
            assert rows[0]["input_tokens"] + rows[0]["output_tokens"] == 3000
            assert json.loads(rows[0]["diagnostics_json"])["finish_reason"] == "length"
            assert runner.failure_reason(wid)
    finally:
        backend.close()
