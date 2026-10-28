"""Settle one disputed or inconclusive finding by reading the policy for the exact case Z3 produced.

The two formal models disagree, or a rule was dropped in checking, so the solver's counterexample may come from a
translation error. Nemotron 3 Ultra gets the policy text and that single concrete case and answers one narrow
question: does the policy, as written, produce this outcome? It must quote the policy; code checks the quote.
It never edits a model (rewriting models from disagreements scored worse in our eval).
"""

from __future__ import annotations

from . import llm
from .verify import found, normalize

MAX_CHARS = 400_000

SYSTEM = """You check one finding of an automated legal audit of a hospital financial-assistance policy. A theorem
prover produced a concrete case from a formal model of the policy; the model may contain translation errors.
Read the policy itself and decide whether the policy, as written, really produces the outcome described.
Answer "holds" if it does, "refuted" if the policy text gives a different outcome for this case (e.g. it does
offer help to this household, or its deadline is different), "unclear" if the policy doesn't settle it.
Quote one or two sentences copied exactly from the policy that decide it. JSON only."""

TASK = """FINDING (the law's requirement, then the prover's case):
Requirement: {title}
Case: {case}

Does the policy as written produce this outcome?"""

SCHEMA = {"type": "object", "properties": {
    "verdict": {"type": "string", "enum": ["holds", "refuted", "unclear"]},
    "quote": {"type": "string"}, "why": {"type": "string"}}, "required": ["verdict", "quote", "why"]}


def adjudicate(text: str, finding: dict, meter: llm.Meter, model: str = "ultra") -> dict:
    case = finding["summary"].removeprefix("Inconclusive: ").split("Z3's case: ")[-1]
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"POLICY TEXT:\n{text[:MAX_CHARS]}\n\n" +
             TASK.format(title=finding["title"], case=case)}]
    raw = llm.chat_json(f"adjudicate ({llm.MODELS[model].label})", model, msgs, meter, schema=SCHEMA,
                        max_tokens=1500)
    verdict = raw.get("verdict", "unclear")
    quote_ok = found(raw.get("quote", ""), normalize(text))
    if not quote_ok:  # an unproven quote can't settle anything
        verdict = "unclear"
    return {"verdict": verdict, "quote": raw.get("quote", ""), "why": raw.get("why", ""), "quote_found": quote_ok,
            "model": llm.MODELS[model].label}
