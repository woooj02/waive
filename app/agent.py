"""The agent loop: Claude plus the tools in tools.py, with a recorded trace.

Two engines:
  claude: the real thing, used whenever ANTHROPIC_API_KEY is set.
  mock:   no key, no network. It returns a canned answer so the UI and the
          tests work offline. It is never what you demo.

The trace (every tool call, its input and its output) goes back to the UI.
Judges trust what they can watch happen, so show it.
"""

from __future__ import annotations

import json
import os
import time

from . import tools

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
# Opus 5.5 defaults to medium. Raise it for hard reasoning, drop it to low
# when the demo has to feel instant.
EFFORT = os.environ.get("CLAUDE_EFFORT", "medium")
MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "8"))

SYSTEM = os.environ.get("AGENT_SYSTEM") or (
    "You are the assistant inside a hackathon demo. Use the tools whenever they "
    "can give a more exact answer than you could. Keep the final answer short "
    "and concrete. Lead with the answer, then one or two lines on how you got it."
)


def engine() -> str:
    return "claude" if os.environ.get("ANTHROPIC_API_KEY") else "mock"


def run(prompt: str) -> dict:
    started = time.monotonic()
    if engine() == "mock":
        result = _run_mock(prompt)
    else:
        result = _run_claude(prompt)
    result["engine"] = engine()
    result["ms"] = int((time.monotonic() - started) * 1000)
    return result


def _run_mock(prompt: str) -> dict:
    return {
        "answer": f"(mock engine: set ANTHROPIC_API_KEY for real answers) You asked: {prompt}",
        "steps": [],
        "stop": "mock",
    }


def _run_claude(prompt: str) -> dict:
    import anthropic

    client = anthropic.Anthropic()
    messages: list = [{"role": "user", "content": prompt}]
    steps: list[dict] = []

    for _ in range(MAX_STEPS):
        # The beta endpoint is used only for the server-side refusal fallback.
        # If a safety classifier declines a benign request mid-demo, the API
        # retries it on another model inside the same call, so no error ever
        # reaches the stage.
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM,
            tools=tools.specs(),
            messages=messages,
            output_config={"effort": EFFORT},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        # Append the content unchanged. Thinking blocks have to come back
        # exactly as they were sent, or the next request is rejected.
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "pause_turn":
            continue
        if response.stop_reason != "tool_use":
            return {"answer": _text(response), "steps": steps, "stop": response.stop_reason}

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            step = {"tool": block.name, "input": block.input}
            try:
                output = tools.call(block.name, block.input)
                step["output"] = output
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": json.dumps(output, default=str)})
            except Exception as exc:  # the model sees the error and can recover
                step["error"] = str(exc)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": f"error: {exc}", "is_error": True})
            steps.append(step)
        # All results go back in one message. Splitting them teaches the model
        # to stop calling tools in parallel.
        messages.append({"role": "user", "content": results})

    return {"answer": "Stopped after the step limit without a final answer.",
            "steps": steps, "stop": "max_steps"}


def _text(response) -> str:
    if response.stop_reason == "refusal":
        return "The model declined this request."
    return "".join(b.text for b in response.content if b.type == "text").strip()
