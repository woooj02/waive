"""Tools the agent can call. This is the file you rewrite for each hackathon.

Each tool is a plain function decorated with @tool. The docstring becomes the
description Claude reads, so write it for the model: say what the tool does,
when to use it, and what comes back. Return anything JSON-serialisable.
"""

from __future__ import annotations

import ast
import operator
from datetime import datetime, timezone

TOOLS: dict[str, dict] = {}


def tool(**properties: dict):
    """Register a function as a tool. Keyword args are its JSON-schema properties.

    Every property is required and no others are allowed, which is what
    strict mode needs. Make an argument optional by allowing null in its type.
    """
    def register(fn):
        TOOLS[fn.__name__] = {
            "fn": fn,
            "spec": {
                "name": fn.__name__,
                "description": (fn.__doc__ or "").strip(),
                "strict": True,
                "input_schema": {
                    "type": "object",
                    "properties": properties,
                    "required": list(properties),
                    "additionalProperties": False,
                },
            },
        }
        return fn
    return register


def specs() -> list[dict]:
    return [t["spec"] for t in TOOLS.values()]


def call(name: str, args: dict):
    if name not in TOOLS:
        raise KeyError(f"unknown tool {name!r}")
    return TOOLS[name]["fn"](**args)


# --- example tools: replace these ------------------------------------------

@tool()
def now() -> dict:
    """Current date and time in UTC. Use when the question depends on today's date."""
    return {"utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}


_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.USub: operator.neg,
    ast.Mod: operator.mod,
}


def _eval(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise ValueError("only numbers and + - * / % ** are allowed")


@tool(expression={"type": "string", "description": "Arithmetic, e.g. (1.07 ** 10) * 2500"})
def calculate(expression: str) -> dict:
    """Evaluate an arithmetic expression exactly. Use it for any maths instead of
    working numbers out in your head."""
    return {"result": _eval(ast.parse(expression, mode="eval").body)}
