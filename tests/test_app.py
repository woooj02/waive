"""Runs offline on the mock engine. Keep it passing: deploy.sh refuses to ship otherwise."""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import agent, replay, tools
from app.main import app


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("REPLAY", raising=False)
    monkeypatch.setattr(replay, "DIR", tmp_path)


client = TestClient(app)


def test_index_and_health():
    assert client.get("/").status_code == 200
    h = client.get("/api/health").json()
    assert h["engine"] == "mock" and "calculate" in h["tools"]


def test_run_mock():
    d = client.post("/api/run", json={"prompt": "hi"}).json()
    assert d["engine"] == "mock" and "hi" in d["answer"]


def test_empty_prompt_rejected():
    assert client.post("/api/run", json={"prompt": ""}).status_code == 422


def test_tool_specs_are_strict():
    for spec in tools.specs():
        schema = spec["input_schema"]
        assert spec["description"]
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])


def test_calculate():
    assert tools.call("calculate", {"expression": "2 ** 10 + 1"}) == {"result": 1025}
    with pytest.raises(ValueError):
        tools.call("calculate", {"expression": "__import__('os')"})


def test_replay_roundtrip(monkeypatch):
    result = {"answer": "42", "steps": [], "engine": "claude", "ms": 5}
    monkeypatch.setenv("REPLAY", "record")
    replay.save("What is it?", result)
    monkeypatch.setenv("REPLAY", "play")
    got = replay.get("  what is it?")
    assert got["answer"] == "42" and got["replayed"] is True


def test_claude_loop_runs_tools(monkeypatch):
    """Drive the real loop with a fake client: one tool call, then an answer."""
    replies = iter([
        SimpleNamespace(stop_reason="tool_use", content=[
            SimpleNamespace(type="tool_use", id="t1", name="calculate", input={"expression": "6*7"}),
            SimpleNamespace(type="tool_use", id="t2", name="nope", input={}),
        ]),
        SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="42")]),
    ])
    sent = []

    def create(**kw):
        sent.append(json.loads(json.dumps(kw["messages"][-1], default=lambda o: "<block>")))
        return next(replies)

    fake = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda: fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")

    d = agent.run("six times seven")
    assert d["answer"] == "42" and d["engine"] == "claude"
    assert d["steps"][0]["output"] == {"result": 42}
    assert "unknown tool" in d["steps"][1]["error"]
    # both tool results returned together in one user message
    assert len(sent[1]["content"]) == 2 and sent[1]["content"][1]["is_error"] is True
