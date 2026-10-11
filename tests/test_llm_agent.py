import json

import pytest

import agent
import llm
from conftest import read_sample


def test_mock_is_default_without_keys(monkeypatch):
    for k in ("LLM_PROVIDER", "ANTHROPIC_API_KEY", "LLM_BASE_URL", "LLM_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert llm.get_llm().name == "mock"


def test_provider_selection(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    l = llm.get_llm()
    assert l.name == "anthropic" and l.model == "claude-sonnet-5-5"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.vultrinference.com/v1")
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_MODEL", "some-model")
    assert llm.get_llm().name == "openai_compatible"


def test_mock_audit_end_to_end(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    seen = []
    res = agent.run_audit(read_sample("synthetic_42_trades_small_lucky.csv"), trials=20, on_progress=seen.append)
    assert res["provider"] == "mock"
    rep = res["report"]
    for h in ("## Verdict", "## Gates that failed and why", "## What to test next", "## Risk warnings"):
        assert h in rep
    assert "G1 Sufficient sample" in rep
    assert "Not financial advice" in rep
    assert all(s["status"] in ("done", "skip", "warn") for s in res["steps"])
    assert seen and any(s["status"] == "running" for s in seen[0])
    assert res["facts"]["analysis"]["monthly"], "sandboxed analysis should produce monthly rows"
    assert res["analysis_trace"]["attempts"][0]["source"] == "llm:mock"
    # deterministic
    res2 = agent.run_audit(read_sample("synthetic_42_trades_small_lucky.csv"), trials=20)
    assert res2["report"] == rep


def test_mock_pro_report_is_longer():
    l = llm.MockLLM()
    res = agent.run_audit(read_sample("synthetic_640_trades_passes.csv"), llm=l)
    pro = l.write_report(res["facts"], pro=True)
    assert len(pro) > len(res["report"]) * 1.5
    assert "## Month-by-month review" in pro and "## Robustness checklist (10 items)" in pro


def test_report_never_promises_profit():
    l = llm.MockLLM()
    for name in ("synthetic_42_trades_small_lucky.csv", "synthetic_640_trades_passes.csv"):
        res = agent.run_audit(read_sample(name), llm=l)
        text = (res["report"] + l.write_report(res["facts"], pro=True)).lower()
        for bad in ("guarantee", "will make money", "you should buy", "increase your position"):
            assert bad not in text.replace("not a guarantee", "")


def test_mock_chat_explains_gate():
    l = llm.MockLLM()
    res = agent.run_audit(read_sample("synthetic_42_trades_small_lucky.csv"), llm=l)
    ans = l.chat(res["facts"], res["report"], [], "Why did G1 fail?")
    assert "G1" in ans and "failed" in ans


class FakeHTTP:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def __call__(self, url, headers, body, timeout=60.0):
        self.calls.append((url, headers, body))
        return self.reply(url, body) if callable(self.reply) else self.reply


def test_anthropic_request_shape():
    l = llm.AnthropicLLM("sk-test")
    l.transport = FakeHTTP({"content": [{"type": "text", "text": "## Verdict\nok"}]})
    out = l.write_report({"gates": {}}, pro=False)
    url, headers, body = l.transport.calls[0]
    assert url == "https://api.anthropic.com/v1/messages"
    assert headers["x-api-key"] == "sk-test" and "anthropic-version" in headers
    assert body["model"] == "claude-sonnet-5-5" and body["system"].startswith("You are BOTLAB Auditor")
    assert out.endswith(llm.DISCLAIMER + "_")


def test_openai_compatible_request_shape():
    l = llm.OpenAICompatibleLLM("https://api.vultrinference.com/v1/", "vk", "m1")
    l.transport = FakeHTTP({"choices": [{"message": {"content": "```python\nprint(1)\n```"}}]})
    assert l.write_analysis_code() == "print(1)"
    url, headers, body = l.transport.calls[0]
    assert url == "https://api.vultrinference.com/v1/chat/completions"
    assert headers["Authorization"] == "Bearer vk" and body["messages"][0]["role"] == "system"


def test_bad_llm_code_falls_back_to_builtin_in_sandbox():
    l = llm.OpenAICompatibleLLM("https://x.invalid/v1", "k", "m")

    def reply(url, body):
        content = body["messages"][-1]["content"]
        if "Python 3 script" in content:
            return {"choices": [{"message": {"content": "```python\nimport socket\nsocket.socket()\n```"}}]}
        return {"choices": [{"message": {"content": "## Verdict\nfine"}}]}

    l.transport = FakeHTTP(reply)
    res = agent.run_audit(read_sample("synthetic_42_trades_small_lucky.csv"), llm=l)
    atts = res["analysis_trace"]["attempts"]
    assert atts[0]["ok"] is False and "blocked" in atts[0]["stderr"]
    assert atts[1]["source"] == "builtin" and atts[1]["ok"] is True
    assert res["facts"]["analysis"]["monthly"]


def test_llm_failure_falls_back_to_mock_report():
    l = llm.AnthropicLLM("k")

    def boom(*a, **k):
        raise llm.LLMError("down")

    l.transport = boom
    res = agent.run_audit(read_sample("synthetic_42_trades_small_lucky.csv"), llm=l)
    assert res["report_source"].startswith("mock (fallback")
    assert "## Verdict" in res["report"]
