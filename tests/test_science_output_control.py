"""哥哥，输出模式只用离线传输验证，绝不花真实额度。"""

import json
from dataclasses import replace

import httpx
import pytest
from pydantic import TypeAdapter

from world_engine.agent_llm import AgentLLMError, AgentModelBackend
from world_engine.science_runner import ScienceRunner
from world_engine.science_types import ScienceProgram

pytest_plugins = ["science_fixtures"]


def response(disabled):
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "finish_reason": "stop" if disabled else "length",
                    "message": {
                        "content": '{"reason":"先观察本人生活需要","commands":[]}'
                        if disabled
                        else "",
                        "reasoning_content": "" if disabled else "思考" * 16685,
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 13112,
                "completion_tokens": 80 if disabled else 8192,
                "completion_tokens_details": {"reasoning_tokens": 0 if disabled else 8192},
            },
        },
    )


def backend(settings, handler):
    return AgentModelBackend(
        replace(
            settings,
            deepseek_api_key="offline",
            deepseek_model="deepseek-flash",
            deepseek_max_retries=0,
            deepseek_max_output_tokens=8192,
        ),
        client=httpx.Client(
            base_url="https://science-test.invalid/", transport=httpx.MockTransport(handler)
        ),
    )


def test_default_call_keeps_prior_behavior_and_preserves_length_failure(settings):
    sent = []

    def reply(request):
        sent.append(json.loads(request.content))
        return response(False)

    b = backend(settings, reply)
    try:
        with pytest.raises(AgentLLMError) as error:
            b.complete(
                system_prompt="sys",
                user_payload={},
                schema=TypeAdapter(ScienceProgram),
                label="ordinary",
            )
        assert len(sent) == 1 and "thinking" not in sent[0]
        assert error.value.input_tokens == 13112 and error.value.output_tokens == 8192
        assert error.value.diagnostics["finish_reason"] == "length"
    finally:
        b.close()


def test_science_disables_reasoning_without_truncation_or_retry(science_lab, settings):
    db, wid, sid, _, _, at = science_lab
    with db.write() as c:
        c.execute("UPDATE science_sessions SET status='running' WHERE id=?", (sid,))
    sent = []

    def reply(request):
        payload = json.loads(request.content)
        sent.append(payload)
        return response(payload.get("thinking") == {"type": "disabled"})

    b = backend(settings, reply)
    try:
        r = ScienceRunner(db, b, session_id=sid)
        assert r.decide(wid, at)
        with db.read() as c:
            call = c.execute("SELECT * FROM science_model_calls").fetchone()
        assert call["status"] == "succeeded"
        assert len(sent) == 1 and sent[0]["thinking"] == {"type": "disabled"}
        assert sent[0]["max_tokens"] == 8192
        assert json.loads(sent[0]["messages"][1]["content"]) == json.loads(call["context_json"])
        assert sent[0]["messages"][0]["content"] == call["system_prompt"]
        assert r.usage()["accounted_tokens"] == 13192
        assert r.usage()["unknown_calls"] == 0
        assert r.usage()["reserved_tokens"] == 0
        assert r.usage()["stop_reason"] is None
    finally:
        b.close()


def test_thinking_mode_is_frozen_and_change_refused_before_engine(
    lab, settings, tmp_path, monkeypatch
):
    from scripts import demo_science
    from world_engine.database import Database

    db, wid, _, _, _, _ = lab
    output = tmp_path / "prepared-output-mode"
    config = replace(settings, deepseek_api_key="offline", science_api_enabled=True)
    demo_science.prepare(
        db.path, wid, output, config, {"offline_passed": True, "independent_review_passed": True}
    )
    with Database(output / "world.db", legacy_world_defaults=False).read() as c:
        frozen = json.loads(c.execute("SELECT freeze_json FROM science_sessions").fetchone()[0])
    assert frozen["model_config"]["thinking"] == {"type": "disabled"}
    original = demo_science.science_model_config
    monkeypatch.setattr(
        demo_science,
        "science_model_config",
        lambda s: {**original(s), "thinking": {"type": "enabled"}},
    )

    def forbidden(*args, **kwargs):
        pytest.fail("哥哥，输出模式变化必须在构造引擎前拒绝")

    monkeypatch.setattr(demo_science, "WorldEngine", forbidden)
    with pytest.raises(ValueError, match="模型配置"):
        demo_science.run(output, config)
