"""One check, start to finish, as a stream of events the UI renders live.

read bill → find policy (our Z3-audited formal models first; else Atlas, else Tavily + live compile) → decide
(Z3 on the formal model, or plain code on a compiled program).
Each step reports the model that did it, time and cost. A failed step says so
and the check continues with what the user can still be told.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import date

from . import atlas, bill as billmod, compiler, engine, finder, llm, prover_check
from .fetch import FetchError
from .schema import Bill, Household


def _step(sid: str, status: str, title: str, **kw) -> dict:
    return {"type": "step", "id": sid, "status": status, "title": title, **kw}


def _calls_since(meter: llm.Meter, n: int) -> list[dict]:
    return meter.as_dicts()[n:]


def run(household: Household, upload: tuple[bytes, str] | None = None, typed: dict | None = None,
        allow_live_compile: bool = True) -> Iterator[dict]:
    meter = llm.Meter()
    started = time.monotonic()
    fields: billmod.BillFields | None = None

    # 1. Read the bill.
    if upload:
        yield _step("read", "running", "Reading your bill", model="Nemotron 3.5 Super VL")
        n = len(meter.calls)
        try:
            fields = billmod.read_bill(upload[0], upload[1], meter)
        except (llm.LLMError, llm.BudgetExceeded, ValueError, OSError) as exc:
            yield _step("read", "error", "Couldn't read the bill", detail=str(exc)[:200],
                        calls=_calls_since(meter, n))
            yield {"type": "need_fields", "message": "Type the hospital name, statement date and balance instead."}
            return
        notes = billmod.check(fields)
        yield _step("read", "done", "Read your bill", calls=_calls_since(meter, n),
                    detail=f"{fields.hospital} · balance ${fields.balance_usd or 0:,.2f} · statement "
                           f"{fields.statement_date or 'date not found'}", bill=fields.model_dump(), notes=notes)
    else:
        fields = billmod.BillFields(**(typed or {}))
        yield _step("read", "skipped", "Bill typed in by you", detail=fields.hospital)

    # The patient's own answer about insurance wins; the bill can only raise a question.
    insurance_note = ""
    if upload and not household.insured and (fields.insurance_paid_usd or 0) > 0:
        insurance_note = (f"Your bill shows an insurance payment of ${fields.insurance_paid_usd:,.2f}. If you have "
                          f"insurance, choose 'Insured' and check again: the answer can change.")
    b = Bill(hospital=fields.hospital, balance_usd=fields.balance_usd or 0, statement_date=fields.statement_date,
             provider_type=fields.provider_type if fields.provider_type in ("hospital", "physician_group") else "unknown")

    # 2. Find the hospital's policy: first among the policies we've formalised and audited (Z3 decides).
    yield _step("find", "running", "Finding this hospital's own policy")
    audited = prover_check.find(b.hospital)
    if audited:
        pid, pm, doc = audited
        counts = sum(f["status"] == "counterexample" for f in doc["findings"])
        source = {"url": pm.source_url, "from_audit": True, "audit_id": pid, "audit_counterexamples": counts,
                  "fetched": ""}
        yield _step("find", "done", "Found this hospital's policy, already written as logic",
                    detail=f"{doc['hospital']} · formal model by Nemotron 3 Ultra, cross-checked by Nemotron 3 Super",
                    source=source, from_cache=True)
        verdict, proof = prover_check.decide(pm, household, b, doc.get("dropped_rules", 0))
        if insurance_note:
            verdict.uncertain.append(insurance_note)
        how = ("true for every value of what we don't know" if proof["always"] else
               "depends on: " + "; ".join(proof["depends_on"]) if proof["sometimes"] else
               "no rule of this policy covers this household")
        yield _step("decide", "done", "Checked your household against the policy with a theorem prover",
                    model=f"Z3 {proof['z3']} (no AI)", detail=f"{verdict.pct_fpl:g}% of the poverty level → {how}")
        yield {"type": "result", "verdict": verdict.model_dump(), "program": None, "source": source,
               "proof": proof, "bill": fields.model_dump(), "household": household.model_dump(),
               "receipt": receipt(meter), "seconds": round(time.monotonic() - started, 1)}
        return
    entry, score, name = atlas.match(b.hospital)
    program = None
    source = None
    if entry:
        program = atlas.program(entry)
        source = {"url": entry["source_url"], "fetched": entry.get("fetched", ""), "from_atlas": True,
                  "policy_id": entry["id"], "compiled": entry.get("compiled", ""),
                  "cross_check": entry.get("report", {})}
        yield _step("find", "done", "Found the policy in the Colorado Atlas",
                    detail=f"{entry['hospital']} (matched '{name}', {score:.0%}) · compiled {entry.get('compiled', '')}"
                           f" by Nemotron 3 Ultra, cross-checked by Nemotron 3 Super",
                    source=source, calls=entry.get("calls", []), from_cache=True)
    elif not allow_live_compile:
        yield _step("find", "error", "This hospital isn't in the Atlas yet", detail="Live compiling is off.")
    elif not finder.tavily_configured() or not llm.configured():
        yield _step("find", "error", "Live search isn't configured on this server")
    else:
        n = len(meter.calls)
        trace: list = []
        try:
            doc = finder.find_policy(b.hospital, fields.city, "Colorado" if household.state == "CO" else household.state,
                                     meter, trace)
            yield _step("find", "done", "Found the policy with Tavily", model="Tavily + Nemotron 3 Nano",
                        detail=doc["url"], calls=_calls_since(meter, n), trace=trace)
            yield _step("compile", "running", "Compiling the policy into rules",
                        model="Nemotron 3 Ultra + Nemotron 3 Super")
            n = len(meter.calls)
            program, report = compiler.compile_policy(doc["text"], meter, doc["url"])
            source = {"url": doc["url"], "fetched": date.today().isoformat(), "from_atlas": False,
                      "cross_check": report}
            yield _step("compile", "done", "Compiled and proved the rules", calls=_calls_since(meter, n),
                        detail=f"{sum(t.verified for t in program.tiers)} income tiers proven, "
                               f"{len(program.dropped)} rules dropped, "
                               f"{len(report.get('disagreements', {}))} disagreements settled")
        except (finder.NotFound, FetchError) as exc:
            yield _step("find", "error", "Couldn't find this hospital's policy online", detail=str(exc)[:200],
                        calls=_calls_since(meter, n), trace=trace)
        except llm.BudgetExceeded as exc:
            yield _step("find", "error", "Today's live-model budget is used up", detail=str(exc))
        except (llm.LLMError, ValueError) as exc:
            yield _step("compile", "error", "Compiling failed", detail=str(exc)[:200], calls=_calls_since(meter, n))

    # 3. Decide, in code.
    verdict = engine.evaluate(program, household, b)
    if insurance_note:
        verdict.uncertain.append(insurance_note)
    yield _step("decide", "done", "Checked your numbers against the rules", model="plain code (no AI)",
                detail=f"{verdict.pct_fpl:g}% of the poverty level → {verdict.status.replace('_', ' ')}")
    yield {"type": "result", "verdict": verdict.model_dump(), "program": program.model_dump() if program else None,
           "source": source, "bill": fields.model_dump(), "household": household.model_dump(),
           "receipt": receipt(meter), "seconds": round(time.monotonic() - started, 1)}


def receipt(meter: llm.Meter) -> dict:
    """What this check cost, and what the same tokens would cost if every call went to Ultra."""
    ultra = llm.MODELS["ultra"]
    live = [c for c in meter.calls if not c.cached_result and not c.error]
    baseline = sum(((c.tokens_in - c.tokens_cached) * ultra.price_in + c.tokens_cached * ultra.price_cached
                    + c.tokens_out * ultra.price_out) / 1e6 for c in live)
    return {"calls": meter.as_dicts(), "cost_usd": meter.cost_usd, "all_ultra_usd": round(baseline, 6),
            "cached_calls": sum(c.cached_result for c in meter.calls)}
