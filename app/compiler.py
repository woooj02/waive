"""Policy text → verified rule program.

1. Ultra compiles the policy (long context, the hardest reading job).
2. Super compiles it independently.
3. Code diffs the two on every field that can change a verdict.
4. Ultra settles each disagreement, quoting the policy.
5. verify.py drops any rule whose quote isn't in the text.

The policy text always opens the prompt, so the follow-up call on the same
policy reuses Token Factory's cached prefix.
"""

from __future__ import annotations

import json

from . import llm
from .schema import RuleProgram, Tier
from .verify import verify

MAX_CHARS = 400_000  # ~100k tokens; the longest Colorado policy we've seen is a fraction of this

SYSTEM = """You are a careful paralegal who turns hospital financial-assistance (charity care) policies into data.
Rules:
- Use only what the policy says. If it doesn't say, leave the field empty or null and add the question to "unknowns".
- Every "quote" must be copied character for character from the policy: one or two full sentences, no paraphrase,
  no ellipses. A program checks each quote against the text and deletes rules whose quote isn't found.
- Percentages are of the Federal Poverty Level/Guidelines (FPL/FPG). "At or below 250%" means max_fpl 250.
- A tier is one income band with one discount. Free care is discount_pct 100. "Amounts generally billed" (AGB)
  limits are not a percent discount: put the AGB rule in unknowns unless the policy gives a percent.
- If the policy gives dollar income tables instead of percentages, convert using the policy's own stated % (look
  for a heading like "250% of FPL") and quote that heading line.
- providers.scope: "facility_only" if only the hospital's own charges are covered and physicians/groups bill
  separately; "listed" if it lists covered providers; "all" if all providers are covered.
- Reply with JSON only, matching the schema."""

TASK = """TASK: Compile the policy above into the JSON rule program. Hospital name as the policy states it.
Schema:
{schema}"""

ADJUDICATE = """TASK: Two independent readings of the policy above disagree on these fields:
{diffs}
For each field, decide what the policy actually says. Return JSON {{"fields": [{{"field": ..., "value": ...,
"quote": ...}}]}} where value uses the same shape as in the readings and quote is copied verbatim from the policy.
If the policy is genuinely silent or ambiguous, set value to null and say why in "quote" starting with "UNCLEAR:"."""


def _strip(schema: dict) -> dict:
    """The model never fills bookkeeping fields: drop them from the schema it sees."""
    if isinstance(schema, dict):
        props = schema.get("properties")
        if isinstance(props, dict):
            for k in ("verified", "dropped"):
                props.pop(k, None)
            if "required" in schema:
                schema["required"] = [r for r in schema["required"] if r not in ("verified", "dropped")]
        for v in schema.values():
            _strip(v)
    elif isinstance(schema, list):
        for v in schema:
            _strip(v)
    return schema


SCHEMA = _strip(RuleProgram.model_json_schema())


def _messages(policy_text: str, task: str) -> list[dict]:
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"POLICY TEXT:\n{policy_text[:MAX_CHARS]}\n\n{task}"}]


def compile_once(policy_text: str, model_key: str, meter: llm.Meter, source_url: str = "") -> RuleProgram:
    data = llm.chat_json(f"compile ({llm.MODELS[model_key].label})", model_key,
                         _messages(policy_text, TASK.format(schema=json.dumps(SCHEMA))), meter, schema=SCHEMA,
                         max_tokens=6000)
    data.pop("dropped", None)
    prog = RuleProgram.model_validate(data)
    prog.source_url = source_url or prog.source_url
    return prog


def signature(p: RuleProgram) -> dict:
    """The fields that can change a verdict, in a comparable form."""
    return {
        "tiers": sorted((round(t.max_fpl), round(t.discount_pct)) for t in p.tiers),
        "asset_test.applies": p.asset_test.applies,
        "asset_test.limit_usd": p.asset_test.limit_usd,
        "residency.kind": p.residency.kind,
        "insured.eligible": p.insured.eligible,
        "providers.scope": p.providers.scope,
        "hardship.bill_pct_of_income": p.hardship.bill_pct_of_income,
        "application.window_days": p.application.window_days,
        "presumptive": sorted(x.program.lower() for x in p.presumptive),
    }


def diff(a: RuleProgram, b: RuleProgram) -> dict:
    sa, sb = signature(a), signature(b)
    return {k: {"reading_1": sa[k], "reading_2": sb[k]} for k in sa if sa[k] != sb[k]}


def _apply(prog: RuleProgram, field: str, value, quote: str) -> None:
    if field == "tiers" and isinstance(value, list):
        old = {round(t.max_fpl): t for t in prog.tiers}
        tiers = []
        for item in value:
            mx, pct = (item if isinstance(item, (list, tuple)) else (item.get("max_fpl"), item.get("discount_pct")))
            t = old.get(round(mx))
            if t is None:
                t = Tier(max_fpl=mx, discount_pct=pct, quote=quote)
            t.discount_pct = pct
            tiers.append(t)
        tiers.sort(key=lambda t: t.max_fpl)
        for prev, t in zip([None] + tiers[:-1], tiers):
            if prev and t.min_fpl < prev.max_fpl:
                t.min_fpl = prev.max_fpl
        prog.tiers = tiers
    elif field == "presumptive":
        return  # list of programs: keep reading 1's entries; verify() proves each by its own quote
    elif "." in field:
        obj, attr = field.split(".", 1)
        target = getattr(prog, obj)
        setattr(target, attr, value)
        if quote and not quote.startswith("UNCLEAR:"):
            target.quote = quote


def compile_policy(policy_text: str, meter: llm.Meter, source_url: str = "", cross_check: bool = True,
                   primary: str = "ultra", secondary: str = "super") -> tuple[RuleProgram, dict]:
    """Returns the verified program and a report of the cross-check."""
    first = compile_once(policy_text, primary, meter, source_url)
    report: dict = {"primary": primary, "secondary": None, "disagreements": {}, "resolved": []}
    if cross_check:
        try:
            second = compile_once(policy_text, secondary, meter, source_url)
        except (llm.LLMError, ValueError) as exc:
            report["secondary_error"] = str(exc)[:200]
        else:
            report["secondary"] = secondary
            d = diff(first, second)
            report["disagreements"] = d
            if d:
                out = llm.chat_json("adjudicate (Nemotron 3 Ultra)", primary,
                                    _messages(policy_text, ADJUDICATE.format(diffs=json.dumps(d, indent=1))),
                                    meter, max_tokens=3000)
                for f in out.get("fields", []):
                    name, value, quote = f.get("field"), f.get("value"), f.get("quote", "")
                    if name in d:
                        if value is None:
                            first.unknowns.append(f"{name}: {quote}")
                        else:
                            _apply(first, name, value, quote)
                        report["resolved"].append({"field": name, "value": value, "quote": quote})
    return verify(first, policy_text), report
