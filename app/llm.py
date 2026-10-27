"""Nemotron on Nebius Token Factory: one client, a model registry, and a meter.

Every call records model, latency, tokens (including cached prefix tokens) and
cost, so the UI can show which model did what and what it cost. A daily budget
stops live calls before the account runs dry; cached results cost nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from openai import APIError, APIStatusError, OpenAI

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "llm-cache"
SPEND_FILE = ROOT / "data" / "spend.json"


@dataclass(frozen=True)
class Model:
    key: str
    id: str
    label: str
    price_in: float  # $ per 1M uncached input tokens
    price_cached: float
    price_out: float
    vision: bool = False


# Prices from tokenfactory.nebius.com/model-catalog.md, read 2026-10-27.
MODELS = {m.key: m for m in [
    Model("nano", "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B", "Nemotron 3 Nano", 0.06, 0.012, 0.24),
    Model("lightning", "nvidia/Nemotron-3_5-Lightning", "Nemotron 3.5 Lightning", 0.06, 0.03, 0.24),
    Model("super", "nvidia/nemotron-3-super-120b-a12b", "Nemotron 3 Super", 0.30, 0.06, 0.90),
    Model("ultra", "nvidia/Nemotron-3-Ultra-550b-a55b", "Nemotron 3 Ultra", 1.00, 0.12, 3.00),
    Model("vl", "nvidia/Nemotron-3.5-Super-VL", "Nemotron 3.5 Super VL", 0.30, 0.06, 0.90, vision=True),
]}


class BudgetExceeded(Exception):
    pass


class LLMError(Exception):
    pass


@dataclass
class Call:
    step: str
    model: str
    label: str
    seconds: float
    tokens_in: int
    tokens_cached: int
    tokens_out: int
    cost_usd: float
    cached_result: bool = False
    error: str = ""


@dataclass
class Meter:
    """Collects the calls of one request; the UI renders it as the trace and receipt."""

    calls: list[Call] = field(default_factory=list)

    def add(self, call: Call) -> None:
        self.calls.append(call)

    @property
    def cost_usd(self) -> float:
        return round(sum(c.cost_usd for c in self.calls), 6)

    def as_dicts(self) -> list[dict]:
        return [asdict(c) for c in self.calls]


_lock = threading.Lock()
_client: OpenAI | None = None


def configured() -> bool:
    return bool(os.environ.get("NEBIUS_API_KEY"))


def client() -> OpenAI:
    global _client
    if _client is None:
        if not configured():
            raise LLMError("NEBIUS_API_KEY is not set")
        _client = OpenAI(base_url=os.environ.get("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1/"),
                         api_key=os.environ["NEBIUS_API_KEY"], timeout=240, max_retries=0)
    return _client


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def spent_today() -> float:
    try:
        data = json.loads(SPEND_FILE.read_text())
    except (OSError, ValueError):
        return 0.0
    return data.get(_today(), 0.0)


def _record_spend(cost: float) -> None:
    with _lock:
        SPEND_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = json.loads(SPEND_FILE.read_text())
        except (OSError, ValueError):
            data = {}
        data[_today()] = round(data.get(_today(), 0.0) + cost, 6)
        SPEND_FILE.write_text(json.dumps(data, indent=1))


def budget() -> float:
    return float(os.environ.get("DAILY_BUDGET_USD", "0.50"))


def cost_of(m: Model, usage) -> tuple[int, int, int, float]:
    tin = getattr(usage, "prompt_tokens", 0) or 0
    tout = getattr(usage, "completion_tokens", 0) or 0
    details = getattr(usage, "prompt_tokens_details", None)
    cached = (getattr(details, "cached_tokens", 0) or 0) if details else 0
    cost = ((tin - cached) * m.price_in + cached * m.price_cached + tout * m.price_out) / 1_000_000
    return tin, cached, tout, round(cost, 6)


def strip_json(text: str) -> str:
    """Models sometimes wrap JSON in reasoning or code fences; keep the outermost object."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start >= 0 and end > start else text


def _key(model: str, messages: list, schema: dict | None, max_tokens: int) -> str:
    blob = json.dumps([model, messages, schema, max_tokens], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def _create(kwargs: dict):
    """One retry on rate limits and server errors; drop the thinking switch if a model rejects it."""
    for attempt in range(3):
        try:
            return client().chat.completions.create(**kwargs)
        except APIStatusError as exc:
            if exc.status_code == 400 and "extra_body" in kwargs:
                kwargs.pop("extra_body")
                continue
            if exc.status_code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
            time.sleep(3 * (attempt + 1))
        except APIError:
            if attempt == 2:
                raise
            time.sleep(3)
    raise LLMError("unreachable")


def chat(step: str, model_key: str, messages: list, meter: Meter | None = None, schema: dict | None = None,
         max_tokens: int = 4096, temperature: float = 0.0, use_cache: bool = True, reasoning: bool = False) -> str:
    """One call with retry, disk cache, cost metering and the daily budget. Returns the text content."""
    m = MODELS[model_key]
    meter = meter if meter is not None else Meter()
    key = _key(m.id, messages, schema, max_tokens)
    path = CACHE / f"{key}.json"
    if use_cache and path.exists():
        hit = json.loads(path.read_text())
        meter.add(Call(step, m.key, m.label, 0.0, hit["tokens_in"], hit["tokens_cached"], hit["tokens_out"], 0.0,
                       cached_result=True))
        return hit["content"]
    if spent_today() >= budget():
        raise BudgetExceeded(f"daily live-model budget of ${budget():.2f} reached")

    kwargs: dict = {"model": m.id, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
    if schema:
        kwargs["response_format"] = {"type": "json_schema", "json_schema": {"name": "out", "schema": schema}}
    if not reasoning:
        # Nemotron 3 reasons by default; extraction doesn't need a long hidden chain of thought.
        kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}

    started = time.monotonic()
    try:
        resp = _create(kwargs)
    except APIError as exc:
        meter.add(Call(step, m.key, m.label, round(time.monotonic() - started, 2), 0, 0, 0, 0.0,
                       error=f"{type(exc).__name__}: {exc}"[:300]))
        raise LLMError(f"{m.label} failed: {exc}") from exc

    seconds = round(time.monotonic() - started, 2)
    content = resp.choices[0].message.content or ""
    tin, cached, tout, cost = cost_of(m, resp.usage)
    _record_spend(cost)
    meter.add(Call(step, m.key, m.label, seconds, tin, cached, tout, cost))
    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"model": m.id, "content": content, "tokens_in": tin, "tokens_cached": cached,
                                "tokens_out": tout, "cost_usd": cost, "seconds": seconds}))
    return content


def chat_json(step: str, model_key: str, messages: list, meter: Meter | None = None, schema: dict | None = None,
              **kw) -> dict:
    """chat() that must return a JSON object; one repair retry if the first answer doesn't parse."""
    text = chat(step, model_key, messages, meter, schema, **kw)
    try:
        return json.loads(strip_json(text))
    except ValueError:
        fix = messages + [{"role": "assistant", "content": text},
                          {"role": "user", "content": "That was not valid JSON. Reply with only the JSON object."}]
        return json.loads(strip_json(chat(step + " (repair)", model_key, fix, meter, schema, **kw)))
