import json

import agent
import agent_loop
import csv_normalize
import gates
import llm
import runner_client
from conftest import read_sample


def _setup(name="synthetic_640_trades_passes.csv"):
    parsed = csv_normalize.normalize(read_sample(name))
    r = gates.grade(parsed, 20)
    return parsed, agent.build_facts(parsed, r, None, 20)


class ScriptedLLM(llm.BaseLLM):
    """Replays fixed raw model replies through the real JSON protocol parser."""
    name, model = "scripted", "scripted-1"

    def __init__(self, replies):
        self.replies, self.seen = list(replies), []

    def complete(self, system, messages, max_tokens=1500):
        self.seen.append([dict(m) for m in messages])
        return self.replies.pop(0) if self.replies else '{"tool": "finish", "summary": "- done"}'


def _run(code):
    return json.dumps({"tool": "run_python", "why": "test", "code": code})


def test_mock_loop_is_deterministic_and_bounded():
    parsed, facts = _setup()
    a = agent_loop.run_agent_loop(parsed, facts, llm.MockLLM(), limits={"max_steps": 4})
    b = agent_loop.run_agent_loop(parsed, facts, llm.MockLLM(), limits={"max_steps": 4})
    assert [s["result"] for s in a["steps"]] == [s["result"] for s in b["steps"]]
    assert a["summary"] == b["summary"] and a["stopped"] == "model finished"
    assert len(a["steps"]) == 3 and all(s["ok"] for s in a["steps"])
    assert {s["result"]["check"] for s in a["steps"]} == {"edge_decay", "concentration", "drawdown_reshuffle"}
    assert "Drawdown stress" in a["summary"]


def test_step_limit_enforced():
    parsed, facts = _setup()
    l = ScriptedLLM([_run("print('{}')")] * 10)
    out = agent_loop.run_agent_loop(parsed, facts, l, limits={"max_steps": 2})
    assert len(out["steps"]) == 2
    assert len(l.seen) == 3  # max_steps + 1 final "finish please" call
    assert "Step budget used" in l.seen[-1][-1]["content"]


def test_mock_respects_smaller_step_limit():
    parsed, facts = _setup()
    out = agent_loop.run_agent_loop(parsed, facts, llm.MockLLM(), limits={"max_steps": 2})
    assert len(out["steps"]) == 2 and out["stopped"] == "model finished"


def test_disabled_with_zero_steps():
    parsed, facts = _setup()
    out = agent_loop.run_agent_loop(parsed, facts, llm.MockLLM(), limits={"max_steps": 0})
    assert out["steps"] == [] and out["stopped"].startswith("disabled")


def test_per_step_timeout_and_loop_continues():
    parsed, facts = _setup()
    l = ScriptedLLM([_run("while True:\n    pass\n"), _run("import json\nprint(json.dumps({'x': 1}))")])
    out = agent_loop.run_agent_loop(parsed, facts, l, limits={"max_steps": 2, "step_timeout_s": 1.0})
    s1, s2 = out["steps"]
    assert s1["timed_out"] and not s1["ok"]
    assert s2["ok"] and s2["result"] == {"x": 1}
    # the timeout was fed back to the model
    assert any("timed_out=True" in m["content"] for m in l.seen[1] if m["role"] == "user")


def test_output_is_capped():
    parsed, facts = _setup()
    l = ScriptedLLM([_run("print('A' * 50000)")])
    out = agent_loop.run_agent_loop(parsed, facts, l, limits={"max_steps": 1, "max_output": 500})
    s = out["steps"][0]
    assert s["ok"] and len(s["stdout"]) < 600 and "truncated" in s["stdout"]
    fed = [m["content"] for m in l.seen[-1] if m["role"] == "user" and m["content"].startswith("TOOL RESULT")][0]
    assert len(fed) < 1500


def test_code_too_long_is_not_executed():
    parsed, facts = _setup()
    l = ScriptedLLM([_run("x = 1\n" * 500)])
    out = agent_loop.run_agent_loop(parsed, facts, l, limits={"max_steps": 1, "max_code": 300})
    assert not out["steps"][0]["ok"] and "too long" in out["steps"][0]["stderr"]


def test_blocked_code_reports_error_and_invalid_reply_is_tolerated():
    parsed, facts = _setup()
    l = ScriptedLLM(["I think we should look at coins.", _run("import socket\nsocket.socket()")])
    out = agent_loop.run_agent_loop(parsed, facts, l, limits={"max_steps": 2})
    assert out["steps"][0]["tool"] == "invalid" and "invalid reply" in out["steps"][0]["stderr"]
    assert not out["steps"][1]["ok"] and "blocked" in out["steps"][1]["stderr"]


def test_llm_error_stops_loop_but_audit_continues():
    class Boom(llm.BaseLLM):
        name, model = "boom", "x"

        def complete(self, *a, **k):
            raise llm.LLMError("down")

        def write_analysis_code(self):
            return llm.MOCK_ANALYSIS_CODE

    res = agent.run_audit(read_sample("synthetic_42_trades_small_lucky.csv"), llm=Boom())
    assert res["agent_trace"]["stopped"].startswith("LLM error")
    assert "## Verdict" in res["report"]


def test_parse_agent_action_variants():
    p = llm.parse_agent_action
    assert p('{"tool":"finish","summary":"ok"}') == {"tool": "finish", "summary": "ok"}
    a = p('Sure!\n{"tool": "run_python", "why": "w", "code": "print(1)"}\nthanks')
    assert a["tool"] == "run_python" and a["code"] == "print(1)"
    a = p('{"name": "run_python", "arguments": {"code": "print(2)", "why": "nested"}}')
    assert a["code"] == "print(2)" and a["why"] == "nested"
    assert p("```python\nprint(3)\n```")["code"] == "print(3)"
    assert p("no idea")["tool"] == "invalid"
    assert p('{"tool": "run_python", "code": ""}')["tool"] == "invalid"


def test_openai_compatible_agent_request_shape():
    l = llm.OpenAICompatibleLLM(llm.VULTR_BASE_URL, "vk", "some-chat-model")
    calls = []

    def fake(url, headers, body, timeout=60.0):
        calls.append((url, headers, body))
        return {"choices": [{"message": {"content": '{"tool": "finish", "summary": "- fine"}'}}]}

    l.transport = fake
    act = l.next_agent_action([{"role": "user", "content": "FACTS"}], 1, 4)
    assert act == {"tool": "finish", "summary": "- fine"}
    url, headers, body = calls[0]
    assert url == "https://api.vultrinference.com/v1/chat/completions"
    assert headers["Authorization"] == "Bearer vk"
    assert body["messages"][0]["role"] == "system" and "run_python" in body["messages"][0]["content"]
    assert l.endpoint_label == "Vultr Serverless Inference"


def test_vultr_provider_alias(monkeypatch):
    for k in ("LLM_BASE_URL", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "vultr")
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_MODEL", "m")
    l = llm.get_llm()
    assert l.name == "openai_compatible" and l.base_url == llm.VULTR_BASE_URL and l.model == "m"
    monkeypatch.delenv("LLM_MODEL")
    try:
        llm.get_llm()
        assert False, "should require LLM_MODEL"
    except llm.LLMError:
        pass


def test_full_audit_shows_agent_steps_in_stream(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    seen = []
    res = agent.run_audit(read_sample("synthetic_bybit_closed_pnl.csv"), risk_usd=20, on_progress=seen.append)
    ids = [s["id"] for s in res["steps"]]
    assert ids.index("agent") < ids.index("agent-1") < ids.index("agent-3") < ids.index("report")
    assert any(s["id"] == "agent-1" and s["status"] == "running" for snap in seen for s in snap)
    assert res["facts"]["agent"]["steps"] and "## Agent follow-up analyses" in res["report"]
    assert res["agent_trace"]["executor"].startswith("in-process")


def test_runner_selection(monkeypatch):
    monkeypatch.delenv("RUNNER_URL", raising=False)
    assert isinstance(runner_client.get_runner(), runner_client.LocalRunner)
    monkeypatch.setenv("RUNNER_URL", "http://127.0.0.1:9")  # nothing listens: must fail closed
    monkeypatch.delenv("RUNNER_FALLBACK_LOCAL", raising=False)
    r = runner_client.get_runner().run_python("print(1)")
    assert not r.ok and r.stderr.startswith("runner unreachable")
    monkeypatch.setenv("RUNNER_FALLBACK_LOCAL", "1")
    r = runner_client.get_runner().run_python("print(1)")
    assert r.ok and r.stdout.strip() == "1" and r.limits["executor"].startswith("in-process")
