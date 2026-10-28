"""Policy text → formal policy model, with Z3 in the loop.

1. Nemotron 3 Ultra writes the policy as a PolicyModel (CNF rules over a fixed vocabulary).
2. Code rejects unknown variables; Z3 lints the model (rules that can never fire, requirements nobody meets)
   and the lint goes back to the model once for a repair.
3. verify.py keeps only clauses whose quote is in the policy text.
4. Nemotron 3 Super formalises independently. Z3 decides whether the two models are logically equivalent;
   if not, each difference comes with a concrete household where they disagree.
5. Counterexample-guided repair: Ultra gets the policy, both models and those households, and returns a
   corrected model. Z3 re-checks. Whatever still differs is reported, never hidden.

The policy text opens every prompt so follow-up calls on the same policy reuse Token Factory's prefix cache.
"""

from __future__ import annotations

import json

import z3

from . import llm
from .formal import BOOL_VARS, NUM_VARS, Encoded, Household, PolicyModel
from .law import equivalent
from .verify import found, normalize

MAX_CHARS = 400_000

VOCAB = "\n".join([f"  {k} (yes/no): {v}" for k, v in BOOL_VARS.items()] +
                  [f"  {k} (number): {v}" for k, v in NUM_VARS.items()])

SYSTEM = f"""You are a careful legal engineer. You translate a hospital's financial-assistance (charity care) policy
into a formal model that a theorem prover (Z3) will check against Colorado and federal law. Precision matters
more than coverage: a wrong clause produces a false legal finding.

The model describes WHO gets help and HOW MUCH, using only these household facts:
{VOCAB}

Shape:
- "requires": conditions EVERY form of assistance needs (residency, service area, care type, applying for
  Medicaid first...). If the policy only says assistance "may" be denied for something, set discretionary true.
- "rules": each way to get help: an income tier, presumptive eligibility, catastrophic/medical hardship.
  "when" is a list of clauses that must ALL hold; each clause is a list of atoms of which ANY may hold
  (conjunctive normal form). Example: uninsured or underinsured AND at or below 250% FPL is
  [[{{"var":"insured","op":"false"}},{{"var":"underinsured","op":"true"}}],[{{"var":"fpl_pct","op":"<=","value":250}}]].
- Yes/no atoms use op "true" or "false" and no value. Number atoms use <=, <, >=, >, == with a value.
- Write income bands exactly as worded: "250% or less" is <= 250; "between 251% and 400%" is >= 251 and <= 400;
  "in excess of 250%" is > 250. Do not "fix" gaps between bands: the prover reports them.
- "discount_pct": 100 for free care; the stated percent if one applies to all facilities; null when the amount
  is a table, a state rate, AGB, or not stated.
- "timing.application": when the application window ends, as days after an anchor: "service" (date of service
  or discharge) or "first_statement" (first post-discharge bill). If the policy says "the later of", list each.
- "timing.eca_not_before": the earliest day the policy allows extraordinary collection actions (lawsuits,
  garnishment, liens, credit reporting), same anchors. "timing.first_statement_from_day": the earliest day after
  service the policy says bills may be sent, if stated.
- "payment_plan": only numbers the policy states.
- "nonprofit": false only if the policy or hospital is clearly for-profit.
- Every requirement and rule needs a "quote": one sentence or phrase copied character for character from the
  policy (at least 5 words, no ellipses). A program deletes anything whose quote it can't find.
- If something matters but doesn't fit the vocabulary, describe it in "notes" instead of forcing it in.
Reply with JSON only, matching the schema."""

TASK = """TASK: Write the formal model of the policy above. "doc" is "{doc}". Schema:
{schema}"""

REPAIR = """Your model has problems found by the checker:
{problems}
Return the full corrected JSON model. Keep everything that was right."""

RECONCILE = """TASK: Two independent formal models of the policy above disagree. A theorem prover found these
households, where the two models give different answers:
{cases}

MODEL A:
{a}

MODEL B:
{b}

For each household, read the policy and decide what it actually grants. Then return ONE corrected full JSON model
of the policy (same schema) that gets every one of these households right and keeps everything else the policy
says. Quotes must still be copied exactly from the policy."""


def _inline(schema: dict) -> dict:
    """Resolve $refs: some structured-output backends reject them."""
    defs = schema.pop("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(dict(defs[node["$ref"].split("/")[-1]]))
            return {k: walk(v) for k, v in node.items() if k not in ("title", "default")}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node
    return walk(schema)


SCHEMA = _inline(PolicyModel.model_json_schema())


def _messages(text: str, task: str) -> list[dict]:
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"POLICY TEXT:\n{text[:MAX_CHARS]}\n\n{task}"}]


def lint(pm: PolicyModel) -> list[str]:
    """Problems a correct model can't have. Vocabulary errors first, then Z3: dead rules and empty requirements."""
    problems = []
    names = set(BOOL_VARS) | set(NUM_VARS)
    for item in [*pm.requires, *pm.rules]:
        clauses = item.cond if hasattr(item, "cond") else item.when
        for clause in clauses:
            for a in clause:
                if a.var not in names:
                    problems.append(f"{item.id}: unknown variable '{a.var}' (use only the listed facts)")
                elif a.var in BOOL_VARS and a.op not in ("true", "false"):
                    problems.append(f"{item.id}: {a.var} is yes/no, use op true or false")
                elif a.var in NUM_VARS and (a.op in ("true", "false") or a.value is None):
                    problems.append(f"{item.id}: {a.var} is a number, give an op like <= and a value")
    if problems:
        return problems
    h = Household()
    enc = Encoded(pm, h)
    s = z3.Solver()
    s.add(*h.axioms())
    if s.check(enc.eligible) == z3.unsat:
        problems.append("the requirements together exclude every household")
    for r in pm.rules:
        if s.check(enc.eligible, enc.rule_terms[r.id]) == z3.unsat:
            problems.append(f"rule {r.id} can never apply to anyone (its clauses contradict each other or the "
                            f"requirements)")
    if not pm.rules:
        problems.append("no rules: the model grants help to nobody")
    return problems


def prove_quotes(pm: PolicyModel, text: str) -> tuple[PolicyModel, list[dict]]:
    """Drop requirements, rules and deadlines whose quote isn't in the policy. Returns the kept model + drops."""
    src = normalize(text)
    dropped = []

    def ok(item, kind):
        if found(item.quote, src):
            return True
        dropped.append({"kind": kind, "id": getattr(item, "id", kind), "quote": item.quote})
        return False

    out = pm.model_copy(deep=True)
    out.requires = [r for r in out.requires if ok(r, "requirement")]
    out.rules = [r for r in out.rules if ok(r, "rule")]
    out.timing.application = [d for d in out.timing.application if ok(d, "application window")]
    out.timing.eca_not_before = [d for d in out.timing.eca_not_before if ok(d, "collection timing")]
    if out.payment_plan.quote and not found(out.payment_plan.quote, src):
        dropped.append({"kind": "payment plan", "id": "payment_plan", "quote": out.payment_plan.quote})
        out.payment_plan = type(out.payment_plan)()
    return out, dropped


def formalize_once(text: str, model_key: str, meter: llm.Meter, doc: str = "policy",
                   source_url: str = "") -> tuple[PolicyModel, dict]:
    """One model's formalisation, with one lint-driven repair. Returns the quote-proven model and a report."""
    label = llm.MODELS[model_key].label
    msgs = _messages(text, TASK.format(doc=doc, schema=json.dumps(SCHEMA)))
    raw = llm.chat_json(f"formalise ({label})", model_key, msgs, meter, schema=SCHEMA, max_tokens=8000)
    report = {"model": model_key, "lint": [], "repaired": False}
    pm, err = _parse(raw)
    problems = [err] if err else lint(pm)
    if problems:
        report["lint"] = problems
        fix = msgs + [{"role": "assistant", "content": json.dumps(raw)},
                      {"role": "user", "content": REPAIR.format(problems="\n".join(f"- {p}" for p in problems))}]
        raw = llm.chat_json(f"repair ({label})", model_key, fix, meter, schema=SCHEMA, max_tokens=8000)
        pm, err = _parse(raw)
        if err:
            raise ValueError(f"{label} returned an invalid model twice: {err}")
        report["repaired"] = True
        report["lint_after"] = lint(pm)
    pm.doc, pm.source_url = doc, source_url or pm.source_url
    proven, dropped = prove_quotes(pm, text)
    report["dropped"] = dropped
    report["clauses"] = len(pm.requires) + len(pm.rules)
    report["clauses_proven"] = len(proven.requires) + len(proven.rules)
    return proven, report


def _parse(raw: dict) -> tuple[PolicyModel | None, str]:
    try:
        return PolicyModel(**raw), ""
    except Exception as exc:  # pydantic ValidationError and friends: becomes repair feedback
        return None, f"the JSON doesn't match the schema: {str(exc)[:600]}"


def formalize(text: str, meter: llm.Meter, doc: str = "policy", source_url: str = "", primary: str = "ultra",
              secondary: str = "super", rounds: int = 1) -> tuple[PolicyModel, dict]:
    """The full pipeline: two independent formalisations, Z3 equivalence, counterexample-guided repair."""
    a, rep_a = formalize_once(text, primary, meter, doc, source_url)
    report = {"primary": rep_a}
    if not secondary:
        return a, report
    b, rep_b = formalize_once(text, secondary, meter, doc, source_url)
    report["secondary"] = rep_b
    diffs = [f for f in equivalent(a, b) if f.status == "counterexample"]
    report["disagreements_before"] = [f.summary for f in diffs]
    for i in range(rounds):
        if not diffs:
            break
        cases = "\n".join(f"{n + 1}. {f.summary}" for n, f in enumerate(diffs))
        msgs = _messages(text, RECONCILE.format(cases=cases, a=a.model_dump_json(), b=b.model_dump_json()))
        raw = llm.chat_json(f"reconcile ({llm.MODELS[primary].label})", primary, msgs, meter, schema=SCHEMA,
                            max_tokens=8000, reasoning=True)
        fixed, err = _parse(raw)
        if err or lint(fixed):
            report.setdefault("reconcile_errors", []).append(err or "; ".join(lint(fixed)))
            break
        fixed.doc, fixed.source_url = doc, source_url or a.source_url
        a, dropped = prove_quotes(fixed, text)
        report.setdefault("reconcile_dropped", []).extend(dropped)
        diffs = [f for f in equivalent(a, b) if f.status == "counterexample"]
        report["rounds"] = i + 1
    report["disagreements_after"] = [f.summary for f in diffs]
    report["equivalent"] = not diffs
    return a, report
