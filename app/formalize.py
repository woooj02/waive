"""Policy text → formal policy model, with Z3 in the loop.

1. Nemotron 3 Ultra writes the policy as a PolicyModel (CNF rules over a fixed vocabulary).
2. Code rejects unknown variables; Z3 lints the model (rules that can never fire, requirements nobody meets)
   and the lint goes back to the model once for a repair.
3. verify.py keeps only clauses whose quote is in the policy text; then Nemotron 3 Super checks that each quote
   really says what its clause claims (a real quote about something else is the commonest error).
4. Nemotron 3 Super formalises independently. Z3 decides whether the two models are logically equivalent;
   if not, each difference comes with a concrete household where they disagree. The audit runs on both models:
   a verdict both agree on is "confirmed", otherwise "disputed" (measured: 94% vs 33% agreement with our
   hand references).
5. Optional counterexample-guided repair (rounds > 0): Ultra rewrites its model from those households. It scored
   worse than no repair on our references, so it's off by default and kept for the eval.

The policy text opens every prompt so follow-up calls on the same policy reuse Token Factory's prefix cache.
"""

from __future__ import annotations

import json
import re

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
- "requires": only conditions the policy applies to EVERY form of assistance it offers (residency, service area,
  care type...). A condition of one program only (e.g. HDC needs Colorado residency but emergency help doesn't)
  goes inside that rule's "when". If the policy only says assistance "may" be denied for something, set
  discretionary true. Procedures (documents, income verification, cooperation, the hospital's own processing
  deadlines) are not requirements; leave them out.
- "rules": each way to get help: an income tier, presumptive eligibility, catastrophic/medical hardship.
  "when" is a list of clauses that must ALL hold; each clause is a list of atoms of which ANY may hold
  (conjunctive normal form). Example: uninsured or underinsured AND at or below 250% FPL is
  [[{{"var":"insured","op":"false"}},{{"var":"underinsured","op":"true"}}],[{{"var":"fpl_pct","op":"<=","value":250}}]].
- Yes/no atoms use op "true" or "false" and no value. Number atoms use <=, <, >=, >, == with a value.
- AND vs OR: a range like "between 251% and 400%" is TWO clauses, [[>= 251]] and [[<= 400]]. Putting both atoms
  in one clause would mean OR and cover everyone.
- Negations: a sentence saying the hospital does NOT require or does NOT deny something (e.g. "does not deny
  discounted care on the basis the patient has not applied for public benefits") is not a requirement. Leave it
  out of "requires" (mention it in notes).
- Optional exceptions ("may, at its sole discretion, make an exception for emergencies") do not create a
  requirement. Keep the main requirement as written and mention the exception in notes.
- Exclusions of services (cosmetic, transplant, research) that the vocabulary can't express go in notes.
- Only use anchors "service" and "first_statement". A deadline counted from another event (a notice, an ECA,
  a determination letter) is left out of timing and described in notes.
- Write income bands exactly as worded: "250% or less" is <= 250; "between 251% and 400%" is >= 251 and <= 400;
  "in excess of 250%" is > 250. Do not "fix" gaps between bands: the prover reports them.
- "discount_pct": 100 for free care; the stated percent if one applies to all facilities; null when the amount
  is a table, a state rate, AGB, or not stated.
- Standing discounts (a self-pay or uninsured discount with a stated percent) are rules too.
- "timing.application": when the PATIENT's window to apply ends (not how long the hospital takes to decide), as days after an anchor: "service" (date of service
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
    s = z3.Solver()
    s.add(*h.axioms())
    for item in [*pm.requires, *pm.rules]:
        clauses = item.cond if hasattr(item, "cond") else item.when
        if not clauses:
            problems.append(f"{item.id}: has no conditions; remove it or give its conditions")
        for clause in clauses:
            if len(clause) > 1 and s.check(z3.Not(h.cnf([clause]))) == z3.unsat:
                problems.append(f"{item.id}: the clause {' OR '.join(f'{a.var} {a.op} {a.value}' for a in clause)} "
                                f"is true for every household. Did you mean AND? Put each condition in its own clause.")
    if problems:
        return problems
    enc = Encoded(pm, h)
    if s.check(enc.eligible) == z3.unsat:
        problems.append("the requirements together exclude every household")
    for r in pm.rules:
        if s.check(enc.eligible, enc.rule_terms[r.id]) == z3.unsat:
            problems.append(f"rule {r.id} can never apply to anyone (its clauses contradict each other or the "
                            f"requirements)")
    if not pm.rules:
        problems.append("no rules: the model grants help to nobody")
    return problems


def _atom_ok(a) -> bool:
    if a.var in BOOL_VARS:
        return a.op in ("true", "false")
    return a.var in NUM_VARS and a.op not in ("true", "false") and a.value is not None


def drop_invalid(pm: PolicyModel) -> tuple[PolicyModel, list[dict]]:
    """Whatever still breaks the vocabulary after the repair is removed (and reported), never sent to Z3."""
    out = pm.model_copy(deep=True)
    dropped = []

    def ok(item, clauses, kind):
        if clauses and all(_atom_ok(a) for c in clauses for a in c):
            return True
        dropped.append({"kind": kind, "id": item.id, "quote": item.quote, "reason": "invalid logic"})
        return False
    out.requires = [r for r in out.requires if ok(r, r.cond, "requirement")]
    out.rules = [r for r in out.rules if ok(r, r.when, "rule")]
    return out, dropped


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
                   source_url: str = "", support: bool = True) -> tuple[PolicyModel, dict]:
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
    pm, invalid = drop_invalid(pm)
    proven, dropped = prove_quotes(pm, text)
    proven, unsupported = check_support(proven, text, meter) if support else (proven, [])
    report["dropped"] = invalid + dropped + unsupported
    report["clauses"] = len(pm.requires) + len(pm.rules)
    report["clauses_proven"] = len(proven.requires) + len(proven.rules)
    report["unsupported"] = len(unsupported)
    return proven, report


SUPPORT = """TASK: A formal model of the policy above makes these claims. Each cites a quote from the policy.
For each claim, decide from the policy (not only the quote) whether the policy really says this. Watch for:
a hospital's own processing deadline presented as the patient's deadline; a "does not require" turned into a
requirement; a condition of one program presented as a condition of all assistance; a quote about something else.
Application deadlines are the most common error: deadlines to send documents, respond to screening, reapply
after approval or denial, or for the hospital's own decisions are NOT deadlines for submitting an application.
Not errors: a requirement the hospital may waive at its discretion is still a requirement; a deadline that is
one of several "the later of" options is supported if the policy lists it as one of them.
CLAIMS:
{claims}
For each claim answer "supported", "incomplete" (right, but the policy adds a further condition the claim leaves
out: name it in "why"), "amount_unsupported" (who qualifies is right, but the policy doesn't state that amount of
help) or "unsupported" (the policy contradicts it or doesn't say it).
Return JSON {{"verdicts": [{{"id": ..., "verdict": ..., "why": "one short sentence"}}]}}."""

SUPPORT_SCHEMA = {"type": "object", "properties": {"verdicts": {"type": "array", "items": {
    "type": "object", "properties": {"id": {"type": "string"},
                                     "verdict": {"type": "string",
                                                 "enum": ["supported", "incomplete", "amount_unsupported",
                                                          "unsupported"]},
                                     "why": {"type": "string"}}, "required": ["id", "verdict", "why"]}}},
    "required": ["verdicts"]}


def _atom_words(a) -> str:
    if a.op == "true":
        return a.var
    if a.op == "false":
        return "not " + a.var
    return f"{a.var} {a.op} {a.value:g}"


def _claim(clauses) -> str:
    return " AND ".join("(" + " OR ".join(_atom_words(a) for a in c) + ")" for c in clauses)


def states_amount(quote: str, pct: float) -> bool:
    """Does a rule's own quote (already found verbatim in the policy) state this percent as the help itself, as in
    "will have a 100% Charity discount"? Plain code, so a model's doubt can't remove an amount the text states."""
    n = re.escape(f"{pct:g}")
    return re.search(rf"(?<![\d.]){n}\s*(?:%|percent)\s+(?:[\w-]+\s+){{0,2}}?"
                     r"(?:discount|off|charity|reduction|write[- ]?off|adjustment|free)\b", quote, re.I) is not None


def check_support(pm: PolicyModel, text: str, meter: llm.Meter, judge: str = "super") -> tuple[PolicyModel, list]:
    """Second-model meaning check: drop clauses whose quote exists but doesn't say what the clause claims."""
    claims = {}
    for r in pm.requires:
        claims[f"req:{r.id}"] = (f"Every form of assistance requires: {_claim(r.cond)}"
                                 f"{' (the hospital MAY deny otherwise)' if r.discretionary else ''}", r.quote)
    for r in pm.rules:
        amt = "an unstated amount of help" if r.discount_pct is None else f"{r.discount_pct:g}% off"
        claims[f"rule:{r.id}"] = (f"A patient with {_claim(r.when)} gets {amt}", r.quote)
    for i, d in enumerate(pm.timing.application):
        claims[f"app:{i}"] = (f"The policy's deadline for a patient to SUBMIT a financial-assistance application is "
                              f"{d.days} days after the "
                              f"{'date of service' if d.anchor == 'service' else 'first post-discharge bill'}"
                              " (alone, or as one of several 'the later of' options)", d.quote)
    for i, d in enumerate(pm.timing.eca_not_before):
        claims[f"eca:{i}"] = (f"The policy promises no extraordinary collection action before {d.days} days after the "
                              f"{'date of service' if d.anchor == 'service' else 'first post-discharge bill'}",
                              d.quote)
    if not claims:
        return pm, []
    listing = "\n".join(f'- id {k}: {c} | quote: "{q}"' for k, (c, q) in claims.items())
    raw = llm.chat_json(f"meaning check ({llm.MODELS[judge].label})", judge,
                        _messages(text, SUPPORT.format(claims=listing)), meter, schema=SUPPORT_SCHEMA,
                        max_tokens=4000)
    verdicts = raw.get("verdicts", [])
    bad = {v["id"]: v.get("why", "") for v in verdicts if v.get("verdict") == "unsupported"}
    vague = {v["id"]: v.get("why", "") for v in verdicts if v.get("verdict") == "amount_unsupported"}
    out = pm.model_copy(deep=True)
    for r in out.rules:  # right people, unproven amount: keep the rule, drop the number
        if f"rule:{r.id}" in vague:
            if r.discount_pct is not None and states_amount(r.quote, r.discount_pct):
                why = vague.pop(f"rule:{r.id}")  # the quote says it in so many words: the doubt loses
                out.notes.append(f"{r.id}: the meaning check doubted the {r.discount_pct:g}% ({why}), but the quote "
                                 f"states it as the discount, so it is kept")
            else:
                r.discount_pct = None
    partial = {v["id"]: v.get("why", "") for v in verdicts if v.get("verdict") == "incomplete"}
    for k, why in partial.items():  # keep it: dropping a real rule would make the model claim nobody qualifies
        out.notes.append(f"{k.split(':', 1)[-1]} may leave out a condition: {why}")
    out.requires = [r for r in out.requires if f"req:{r.id}" not in bad]
    out.rules = [r for r in out.rules if f"rule:{r.id}" not in bad]
    out.timing.application = [d for i, d in enumerate(out.timing.application) if f"app:{i}" not in bad]
    out.timing.eca_not_before = [d for i, d in enumerate(out.timing.eca_not_before) if f"eca:{i}" not in bad]
    return out, [{"kind": "unsupported", "id": k, "claim": claims[k][0], "quote": claims[k][1], "why": w}
                 for k, w in bad.items() if k in claims] + \
        [{"kind": "amount removed", "id": k, "claim": claims[k][0], "quote": claims[k][1], "why": w}
         for k, w in vague.items() if k in claims]


def _parse(raw: dict) -> tuple[PolicyModel | None, str]:
    try:
        return PolicyModel(**raw), ""
    except Exception as exc:  # pydantic ValidationError and friends: becomes repair feedback
        return None, f"the JSON doesn't match the schema: {str(exc)[:600]}"


def formalize(text: str, meter: llm.Meter, doc: str = "policy", source_url: str = "", primary: str = "ultra",
              secondary: str = "super", rounds: int = 0) -> tuple[PolicyModel, dict]:
    """Two independent formalisations and their Z3 equivalence. The second model is returned in the report
    (report["secondary_model"]) so the audit can mark each verdict confirmed or disputed.

    rounds > 0 adds counterexample-guided repair (Ultra rewrites its model from the disagreement households).
    Measured on our references it did worse than leaving both models alone, so production uses rounds=0.
    """
    a, rep_a = formalize_once(text, primary, meter, doc, source_url)
    report = {"primary": rep_a}
    if not secondary:
        return a, report
    b, rep_b = formalize_once(text, secondary, meter, doc, source_url)
    report["secondary"] = rep_b
    report["secondary_model"] = json.loads(b.model_dump_json())
    names = (f"{llm.MODELS[primary].label} model", f"{llm.MODELS[secondary].label} model")
    diffs = [f for f in equivalent(a, b, names=names) if f.status == "counterexample"]
    report["disagreements_before"] = [f.summary for f in diffs]
    for i in range(rounds):
        if not diffs:
            break
        cases = "\n".join(f"{n + 1}. {f.summary}" for n, f in enumerate(diffs))
        msgs = _messages(text, RECONCILE.format(cases=cases, a=a.model_dump_json(), b=b.model_dump_json()))
        raw = llm.chat_json(f"reconcile ({llm.MODELS[primary].label})", primary, msgs, meter, schema=SCHEMA,
                            max_tokens=16000, reasoning=True)
        fixed, err = _parse(raw)
        if err or any("unknown variable" in p or "no conditions" in p for p in lint(fixed)):
            report.setdefault("reconcile_errors", []).append(err or "; ".join(lint(fixed)))
            break
        fixed.doc, fixed.source_url = doc, source_url or a.source_url
        fixed, _ = drop_invalid(fixed)
        a, dropped = prove_quotes(fixed, text)
        a, unsupported = check_support(a, text, meter)
        dropped += unsupported
        report.setdefault("reconcile_dropped", []).extend(dropped)
        diffs = [f for f in equivalent(a, b, names=names) if f.status == "counterexample"]
        report["rounds"] = i + 1
    report["disagreements_after"] = [f.summary for f in diffs]
    report["equivalent"] = not diffs
    return a, report
