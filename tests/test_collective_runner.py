"""哥哥，用假的外部后端检查花费边界，绝不为单元测试请求真实 API。"""

import importlib.util
import json
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from test_collective_research import lab  # noqa: F401

from world_engine.agent_llm import AgentLLMError, ModelCompletion


def runner_class():
    assert importlib.util.find_spec("world_engine.collective_runner"), "尚无公平的多人科研调度"
    from world_engine.collective_runner import CollectiveRunner

    return CollectiveRunner


class ExternalBackend:
    def __init__(self, error=None, program=None):
        self.calls = []
        self.error = error
        self.program = program or {"reason": "本人暂不采取行动，等候新的实测", "commands": []}

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return ModelCompletion(self.program, input_tokens=100, output_tokens=50)


def test_collective_long_reasoning_budget_reaches_http_and_is_accounted(lab, settings):
    from world_engine.agent_llm import AgentModelBackend

    db, wid, _, _, _, at = lab
    captured = []

    def reply(request):
        payload = json.loads(request.content)
        captured.append(payload)
        assert payload["max_tokens"] == 131072
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "reasoning_content": "离线模拟较长推理",
                            "content": '{"reason":"根据本人证据暂不采取动作","commands":[]}',
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 16669,
                    "completion_tokens": 42341,
                    "completion_tokens_details": {"reasoning_tokens": 32341},
                },
            },
        )

    config = replace(
        settings,
        deepseek_api_key="offline-secret",
        deepseek_max_retries=0,
        deepseek_timeout_seconds=600,
    )
    backend = AgentModelBackend(
        config,
        client=httpx.Client(
            base_url="https://research-test.invalid/", transport=httpx.MockTransport(reply)
        ),
    )
    runner = runner_class()(db, backend, max_output_tokens=131072)
    try:
        assert runner.decide(wid, at)
        assert len(captured) == 1 and runner.failure_reason(wid) is None
        assert runner.usage(wid)["known_tokens"] == 59010
        with db.read() as c:
            row = c.execute("SELECT * FROM collective_model_calls").fetchone()
            assert row["status"] == "succeeded"
            assert json.loads(row["diagnostics_json"])["reasoning_tokens"] == 32341
    finally:
        backend.close()


def test_in_transit_report_does_not_block_another_person(lab):
    Runner = runner_class()
    db, wid, actors, _, _, at = lab
    from world_engine.collective_messages import MessageService
    from world_engine.collective_research import CollectiveResearchService as R

    with db.write() as c:
        R.record_observation(
            c,
            actors[1],
            at,
            "本人也目睹本地异常",
            {"kind": "eyewitness", "appearance": "泥土覆有灰粉"},
        )
        c.execute("INSERT INTO collective_contacts VALUES (?,?,100)", (actors[0], actors[1]))
        MessageService.send(c, actors[0], actors[1], at, text="仍在途中")
    backend = ExternalBackend()
    runner = Runner(db, backend)
    assert runner.decide(wid, at)
    assert runner.decide(wid, at)
    assert {x["user_payload"]["actor_id"] for x in backend.calls} == set(actors)
    assert backend.calls[0]["max_output_tokens"] == 32768


def test_hunger_and_time_do_not_cause_repeated_model_request(lab):
    Runner = runner_class()
    db, wid, actors, _, _, at = lab
    backend = ExternalBackend()
    runner = Runner(db, backend)
    assert runner.decide(wid, at)
    with db.write() as c:
        c.execute("UPDATE characters SET satiety=60,energy=80 WHERE id=?", (actors[0],))
    assert not runner.decide(wid, at + timedelta(hours=8))
    assert len(backend.calls) == 1


def test_failed_call_usage_is_preserved_and_not_retried(lab):
    Runner = runner_class()
    db, wid, _, _, _, at = lab
    error = AgentLLMError(
        "正文截断",
        input_tokens=200,
        output_tokens=32768,
        diagnostics={"error_code": "truncated_output", "response_body": "原始截断记录"},
    )
    backend = ExternalBackend(error=error)
    runner = Runner(db, backend)
    assert runner.decide(wid, at)
    assert not runner.decide(wid, at + timedelta(hours=1))
    usage = runner.usage(wid)
    assert usage["calls"] == 1 and usage["failed_calls"] == 1 and usage["known_tokens"] == 32968
    assert runner.failure_reason(wid)
    with db.read() as c:
        row = c.execute("SELECT * FROM collective_model_calls").fetchone()
        assert "原始截断记录" in row["diagnostics_json"]


def test_infeasible_model_command_becomes_personal_evidence_without_assistant_fix(lab):
    Runner = runner_class()
    db, wid, actors, _, _, at = lab
    backend = ExternalBackend(
        program={
            "reason": "本人尝试取样",
            "hypotheses": [
                {
                    "explanation": "假说",
                    "predictions": [{"metric": "extractable_salt", "low": 0, "high": 1}],
                }
            ],
            "commands": [
                {
                    "op": "experiment",
                    "batch_id": "not-a-local-batch",
                    "instrument_id": "not-a-real-tool",
                    "method": "extractable_salt",
                    "hypothesis_index": 0,
                }
            ],
        }
    )
    runner = Runner(db, backend)
    assert runner.decide(wid, at)
    assert not runner.failure_reason(wid)
    with db.read() as c:
        assert c.execute("SELECT status FROM collective_model_calls").fetchone()[0] == "rejected"
        assert (
            c.execute(
                "SELECT COUNT(*) FROM collective_observations WHERE character_id=? AND kind='blocked'",
                (actors[0],),
            ).fetchone()[0]
            == 1
        )
        assert c.execute("SELECT COUNT(*) FROM collective_jobs").fetchone()[0] == 0


def test_unknown_usage_reserves_full_request_and_budget_stops_before_call(lab):
    Runner = runner_class()
    db, wid, _, _, _, at = lab
    backend = ExternalBackend(error=AgentLLMError("连接中断"))
    runner = Runner(db, backend, token_limit=1)
    assert not runner.decide(wid, at)
    assert backend.calls == [] and runner.usage(wid)["calls"] == 0


def test_world_engine_uses_collective_runner_for_registered_mode(lab):
    Runner = runner_class()
    db, wid, _, _, _, at = lab
    from world_engine.config import Settings
    from world_engine.engine import WorldEngine

    with db.write() as c:
        c.execute("INSERT INTO material_modes VALUES (?,?)", (wid, '{"mode":"collective"}'))
    backend = ExternalBackend()
    engine = WorldEngine(
        db,
        Settings(
            database_path=db.path,
            collective_api_enabled=True,
            knowledge_enabled=False,
            history_logging_enabled=False,
        ),
    )
    engine.collective_runner = Runner(db, backend)
    engine.heartbeat(wid, elapsed_seconds=0)
    assert len(backend.calls) == 1
    engine.close()


def test_same_rejected_action_does_not_create_infinite_new_fingerprints(lab):
    db, wid, actors, _, _, at = lab
    from world_engine.collective_research import CollectiveResearchService as R
    from world_engine.collective_runner import evidence_fingerprint

    with db.write() as c:
        R.record_observation(
            c,
            actors[0],
            at,
            "同一障碍",
            {"kind": "blocked", "call_id": "first-call", "reason": "实际没有材料"},
        )
        first = evidence_fingerprint(R.context(c, actors[0], at))
        R.record_observation(
            c,
            actors[0],
            at,
            "同一障碍",
            {"kind": "blocked", "call_id": "second-call", "reason": "实际没有材料"},
        )
        second = evidence_fingerprint(R.context(c, actors[0], at))
        assert first == second


def test_unfinished_paid_call_stops_resume_instead_of_silently_calling_again(lab):
    Runner = runner_class()
    db, wid, actors, _, _, at = lab
    with db.write() as c:
        c.execute(
            "INSERT INTO collective_model_calls(id,world_id,actor_id,fingerprint,reason,context_json,system_prompt,reserved_tokens,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "unfinished",
                wid,
                actors[0],
                "earlier-evidence",
                "尚未完成的请求",
                "{}",
                "测试系统",
                40000,
                at.isoformat(),
            ),
        )
    backend = ExternalBackend()
    runner = Runner(db, backend)
    assert not runner.decide(wid, at)
    assert backend.calls == [] and runner.failure_reason(wid)


@pytest.mark.parametrize("through_backend", [False, True])
def test_complete_but_invalid_plan_returns_to_author_without_world_stop(lab, through_backend):
    # 哥哥，这复现正式响应里的指标标识错误，不替人物补写预测。
    Runner = runner_class()
    db, wid, actors, _, _, at = lab
    raw = {
        "reason": "本人拟测量颗粒占比",
        "hypotheses": [
            {
                "explanation": "表面有颗粒",
                "predictions": [{"metric": "颗粒质量百分比", "low": 0, "high": 5}],
            }
        ],
        "commands": [
            {
                "op": "experiment",
                "batch_id": "sample",
                "method": "particles",
                "instrument_id": "tool",
                "hypothesis_index": 0,
            }
        ],
    }
    backend = ExternalBackend(program=raw)
    if through_backend:
        backend.error = AgentLLMError(
            "实验指标必须有事先预测",
            input_tokens=100,
            output_tokens=50,
            diagnostics={
                "error_code": "invalid_program",
                "stage": "parse",
                "finish_reason": "stop",
                "http_status": 200,
                "content": json.dumps(raw, ensure_ascii=False),
            },
        )
    runner = Runner(db, backend)
    assert runner.decide(wid, at)
    assert not runner.failure_reason(wid)
    assert len(backend.calls) == 1  # 没有后端自动重试。
    with db.read() as c:
        call = c.execute("SELECT * FROM collective_model_calls").fetchone()
        assert call["status"] == "rejected"
        assert call["input_tokens"] == 100 and call["output_tokens"] == 50
        observations = c.execute(
            "SELECT content_json FROM collective_observations WHERE character_id=? AND kind='blocked'",
            (actors[0],),
        ).fetchall()
        assert len(observations) == 1
        evidence = json.loads(observations[0][0])
        assert evidence["stage"] == "execution_contract"
        assert json.loads(evidence["submitted_program"]) == raw
        assert c.execute("SELECT COUNT(*) FROM collective_programs").fetchone()[0] == 0
    assert runner.decide(wid, at + timedelta(hours=1))  # 本人只收到拒绝事实，再独立判断。
    assert not runner.decide(wid, at + timedelta(hours=2))  # 同一拒绝不能无穷触发。
    assert runner.usage(wid)["known_tokens"] == 300


@pytest.mark.parametrize("finish_reason,known_usage", [("length", True), ("stop", False)])
def test_incomplete_or_unknown_usage_is_not_reclassified_as_personal_rejection(
    lab, finish_reason, known_usage
):
    Runner = runner_class()
    db, wid, _, _, _, at = lab
    backend = ExternalBackend(
        error=AgentLLMError(
            "无完整可核验程序",
            input_tokens=100 if known_usage else None,
            output_tokens=50 if known_usage else None,
            diagnostics={
                "error_code": "invalid_program",
                "stage": "parse",
                "finish_reason": finish_reason,
                "http_status": 200,
            },
        )
    )
    runner = Runner(db, backend)
    assert runner.decide(wid, at)
    assert runner.failure_reason(wid)
    assert not runner.decide(wid, at + timedelta(hours=1))


def test_real_backend_parse_rejection_preserves_usage_and_next_independent_request(lab, settings):
    from world_engine.agent_llm import AgentModelBackend

    db, wid, _, _, _, at = lab
    requests = []
    raw = {"reason": "本人提交的指令不合法", "commands": [{"op": "invented"}]}

    def reply(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(raw, ensure_ascii=False)},
                    }
                ],
                "usage": {"prompt_tokens": 200, "completion_tokens": 80},
            },
        )

    backend = AgentModelBackend(
        replace(settings, deepseek_api_key="offline-secret", deepseek_max_retries=0),
        client=httpx.Client(
            base_url="https://collective-test.invalid/", transport=httpx.MockTransport(reply)
        ),
    )
    runner = runner_class()(db, backend)
    try:
        assert runner.decide(wid, at)
        assert len(requests) == 1 and not runner.failure_reason(wid)
        assert runner.usage(wid)["known_tokens"] == 280
        assert runner.decide(wid, at + timedelta(hours=1))
        assert len(requests) == 2
        observations = requests[1]["messages"][1]["content"]
        assert "execution_contract" in observations and "invented" in observations
        assert not runner.decide(wid, at + timedelta(hours=2))
    finally:
        backend.close()
