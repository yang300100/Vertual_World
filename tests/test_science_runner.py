"""哥哥，付费边界全用离线后端验收。"""

import importlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Event

import httpx
import pytest

from world_engine.agent_llm import AgentLLMError, ModelCompletion
from world_engine.collective_research import CollectiveResearchService as R

pytest_plugins = ["science_fixtures"]


def runner(db, backend, sid):
    return importlib.import_module("world_engine.science_runner").ScienceRunner(
        db, backend, session_id=sid
    )


class Backend:
    max_retries = 0

    def __init__(self, *, error=None, program=None, usage=(100, 50), diagnostics=None):
        self.calls = []
        self.error = error
        self.program = program or {"reason": "先等本人新的实测", "commands": []}
        self.tokens = usage
        self.diagnostics = diagnostics or {}

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return ModelCompletion(
            self.program,
            input_tokens=self.tokens[0],
            output_tokens=self.tokens[1],
            diagnostics=self.diagnostics,
        )


@pytest.fixture
def running(science_lab):
    db, wid, sid, actors, assets, at = science_lab
    with db.write() as c:
        c.execute("UPDATE science_sessions SET status='running' WHERE id=?", (sid,))
    return db, wid, sid, actors, assets, at


def test_no_backend_no_paid_calls(science_lab):
    db, wid, sid, _, _, at = science_lab
    r = runner(db, None, sid)
    assert r.decide(wid, at) is False
    assert r.usage()["calls"] == 0


def test_prepared_session_never_calls(science_lab):
    db, wid, sid, _, _, at = science_lab
    b = Backend()
    assert not runner(db, b, sid).decide(wid, at)
    assert not b.calls


def test_new_evidence_prioritized_and_idle_not_repeated(running):
    db, wid, sid, actors, _, at = running
    b = Backend()
    r = runner(db, b, sid)
    assert r.decide(wid, at)
    chosen = b.calls[0]["user_payload"]["actor_id"]
    with db.write() as c:
        R.record_observation(
            c, chosen, at, "本人看到不同的样品", {"kind": "observation", "value": 2}
        )
    assert r.decide(wid, at)
    assert b.calls[1]["user_payload"]["actor_id"] == chosen
    for _ in actors[1:]:
        assert r.decide(wid, at)
    assert not r.decide(wid, at + timedelta(hours=1))
    assert len(b.calls) == len(actors) + 1


def test_waiting_experiment_does_not_call(running):
    from test_science_programs import craft_program

    from world_engine.science_programs import ScienceService

    db, wid, sid, actors, assets, at = running
    with db.write() as c:
        for actor in actors:
            ScienceService.submit(c, sid, actor, craft_program(assets["actors"][actor]), at)
    b = Backend()
    assert not runner(db, b, sid).decide(wid, at)
    assert not b.calls


def test_retry_backend_refused(running):
    db, _, sid, _, _, _ = running
    b = Backend()
    b.max_retries = 1
    with pytest.raises(ValueError, match="重试"):
        runner(db, b, sid)


@pytest.mark.parametrize("usage", [(None, None), (100, None)])
def test_unknown_usage_conservatively_accounted_and_stops(running, usage):
    db, wid, sid, _, _, at = running
    b = Backend(usage=usage)
    r = runner(db, b, sid)
    assert r.decide(wid, at)
    u = r.usage()
    assert u["unknown_calls"] == 1
    with db.read() as c:
        row = c.execute("SELECT * FROM science_model_calls").fetchone()
    assert u["accounted_tokens"] >= row["reserved_tokens"]
    assert u["stop_reason"]
    assert not r.decide(wid, at)


def test_network_failure_no_retry_and_stop_survives_restart(running):
    db, wid, sid, _, _, at = running
    b = Backend(error=AgentLLMError("offline", input_tokens=30, output_tokens=40))
    r = runner(db, b, sid)
    assert r.decide(wid, at)
    assert r.usage()["known_tokens"] == 70
    assert r.usage()["stop_reason"]
    resumed = runner(db, b, sid)
    assert not resumed.decide(wid, at)
    assert len(b.calls) == 1


def test_contract_rejection_is_personal_and_no_repair(running):
    db, wid, sid, _, _, at = running
    b = Backend(program={"reason": "错误程序", "commands": [{"op": "unlock", "tech": "新法"}]})
    r = runner(db, b, sid)
    assert r.decide(wid, at)
    assert len(b.calls) == 1
    assert r.usage()["stop_reason"] is None
    with db.read() as c:
        assert c.execute("SELECT status FROM science_model_calls").fetchone()[0] == "rejected"
        assert c.execute(
            "SELECT 1 FROM collective_observations WHERE content_json LIKE '%unlock%'"
        ).fetchone()


def test_reasoning_is_not_double_counted_and_http_prompt_is_exact(running, settings):
    from world_engine.agent_llm import AgentModelBackend

    db, wid, sid, _, _, at = running
    sent = []

    def reply(request):
        payload = json.loads(request.content)
        sent.append(payload)
        assert payload["max_tokens"] == 8192
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"reason":"先观察本人生活需要","commands":[]}'},
                    }
                ],
                "usage": {
                    "prompt_tokens": 200,
                    "completion_tokens": 80,
                    "completion_tokens_details": {"reasoning_tokens": 60},
                },
            },
        )

    b = AgentModelBackend(
        replace(settings, deepseek_api_key="offline", deepseek_max_retries=0),
        client=httpx.Client(
            base_url="https://science-test.invalid/", transport=httpx.MockTransport(reply)
        ),
    )
    try:
        r = runner(db, b, sid)
        assert r.decide(wid, at)
        assert len(sent) == 1 and r.usage()["accounted_tokens"] == 280
        with db.read() as c:
            row = c.execute("SELECT * FROM science_model_calls").fetchone()
        assert json.loads(sent[0]["messages"][1]["content"]) == json.loads(row["context_json"])
        assert sent[0]["messages"][0]["content"] == row["system_prompt"]
        assert row["reasoning_tokens"] == 60
    finally:
        b.close()


def test_budget_atomic_last_call(running):
    db, wid, sid, actors, _, at = running
    with db.write() as c:
        for index in range(11):
            c.execute(
                (
                    "INSERT INTO "
                    "science_model_calls(id,session_id,world_id,actor_id,fingerprint,status,r"
                    "eason,context_json,system_prompt,reserved_tokens,accounted_tokens,input_"
                    "tokens,output_tokens,created_at) VALUES "
                    "(?,?,?,?,?,'succeeded','old','{}','',1,1,1,0,?)"
                ),
                (f"old{index}", sid, wid, actors[0], f"old{index}", at.isoformat()),
            )
    entered, released = Event(), Event()
    b = Backend()
    original = b.complete

    def blocking(**kwargs):
        entered.set()
        assert released.wait(10)
        return original(**kwargs)

    b.complete = blocking
    r1, r2 = runner(db, b, sid), runner(db, b, sid)
    with ThreadPoolExecutor(2) as pool:
        f = pool.submit(r1.decide, wid, at)
        assert entered.wait(10)
        assert not r2.decide(wid, at)
        released.set()
        assert f.result()
    assert r1.usage()["calls"] == 12
    assert not r2.decide(wid, at)
    assert len(b.calls) == 1


def test_token_limit_prevents_request_before_http(running):
    db, wid, sid, actors, _, at = running
    with db.write() as c:
        c.execute(
            (
                "INSERT INTO "
                "science_model_calls(id,session_id,world_id,actor_id,fingerprint,status,r"
                "eason,context_json,system_prompt,reserved_tokens,accounted_tokens,input_"
                "tokens,output_tokens,created_at) VALUES "
                "('old',?,?,?,'old','succeeded','old','{}','',199999,199999,199999,0,?)"
            ),
            (sid, wid, actors[0], at.isoformat()),
        )
    b = Backend()
    r = runner(db, b, sid)
    assert not r.decide(wid, at)
    assert not b.calls and r.usage()["calls"] == 1
    assert r.usage()["stop_reason"]


def test_crashed_request_unknown_no_resend(running):
    db, wid, sid, actors, _, at = running
    with db.write() as c:
        c.execute(
            (
                "INSERT INTO "
                "science_model_calls(id,session_id,world_id,actor_id,fingerprint,reason,c"
                "ontext_json,system_prompt,reserved_tokens,created_at,diagnostics_json) "
                "VALUES ('crash',?,?,?,'old','old','{}','',10000,?,'{}')"
            ),
            (sid, wid, actors[0], at.isoformat()),
        )
    b = Backend()
    r = runner(db, b, sid)
    assert not r.decide(wid, at)
    assert r.usage()["unknown_calls"] == 1
    assert r.usage()["accounted_tokens"] == 10000
    assert not b.calls


@pytest.mark.parametrize("crash", [False, True])
def test_real_process_reservation_and_crash_recovery(running, crash):
    import subprocess
    import sys

    db, wid, sid, _, _, at = running
    script = """
import sys
from datetime import datetime
from pathlib import Path
from world_engine.database import Database
from world_engine.science_runner import ScienceRunner
from world_engine.agent_llm import ModelCompletion
class Offline:
    max_retries=0
    def complete(self,**kw):
        print('reserved', flush=True)
        sys.stdin.readline()
        return ModelCompletion({'reason':'wait','commands':[]},input_tokens=100,output_tokens=50)
r=ScienceRunner(Database(Path(sys.argv[1])),Offline(),session_id=sys.argv[2])
r.decide(sys.argv[3],datetime.fromisoformat(sys.argv[4]))
"""
    p = subprocess.Popen(
        [sys.executable, "-c", script, str(db.path), sid, wid, at.isoformat()],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert p.stdout.readline().strip() == "reserved"
        b = Backend()
        r = runner(db, b, sid)
        assert not r.decide(wid, at)
        assert r.usage()["reserved_tokens"] > 0 and not b.calls
        if crash:
            p.terminate()
            p.wait(timeout=10)
            resumed = runner(db, b, sid)
            assert resumed.usage()["unknown_calls"] == 1
            assert resumed.usage()["stop_reason"]
            assert not resumed.decide(wid, at)
        else:
            p.communicate("complete\n", timeout=15)
            assert p.returncode == 0
            assert r.usage()["calls"] == 1
            assert r.usage()["accounted_tokens"] == 150
    finally:
        if p.poll() is None:
            p.terminate()
            p.wait(timeout=10)
