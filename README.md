# Waive: a theorem-proved audit of hospital charity-care policies

Colorado law promises hospital discounts to every resident at or below 250% of the poverty line, caps payment
plans at 4% of monthly income, and bars collection lawsuits for 182 days. Federal 501(r) rules add their own
windows for nonprofit hospitals. Each hospital writes its own 10–40 page policy. Nobody checks those policies
against the law, household by household.

Waive does. **NVIDIA Nemotron on Nebius Token Factory** writes each hospital's policy as logic, every clause tied
to a sentence of the policy. The **Z3 theorem prover** then checks that logic against Colorado's Hospital
Discounted Care law and 501(r) for every possible household and billing timeline. A pass is a proof. A fail
comes with the exact household, or the exact day, where the written policy falls short.

Examples from the audit (all from public policies, each with the policy's and the law's own words on screen):

- Children's Hospital Colorado: if the first bill goes out on day 46, the written timeline allows collection
  actions from day 166; Colorado bars them until day 182.
- Intermountain Health: a Colorado family at 99% of the poverty line living outside Intermountain's service area
  is turned away for non-emergency care; Colorado's law covers every resident.
- UCHealth: the plain-language summary (July 2024) leaves out the policy's 100% presumptive eligibility for
  people on SNAP or WIC.

Live demo: https://waive.91.99.162.43.sslip.io · Video: _(coming)_

## How it works

```
Tavily search ─► Nemotron 3 Nano picks the hospital's own document ─► fetch text
     │
     ├─► Nemotron 3 Ultra: policy → formal model (rules in CNF over household facts, deadlines, plan terms)
     │      code: every clause's quote must be in the policy text
     │      Z3:   rejects rules that can never fire and OR-vs-AND slips
     │      Nemotron 3 Super: does each quote really say what its clause claims?
     │
     ├─► Nemotron 3 Super: independent formal model (same checks)
     │
     └─► Z3 (app/law.py): 10 legal checks on each model + equivalence of the two models
            verdict both models agree on → "confirmed"; otherwise "disputed" (needs a person)
```

- **Encoding** (`app/formal.py`): a symbolic household (size, income, assets, bill, nine yes/no facts such as
  insured, Colorado resident, in the service area, applied for Medicaid) and a symbolic first-bill day. Income as
  a percent of the 2026 poverty line stays linear: `100·income ≤ k·(15,960 + 5,680·(size−1))`.
- **Checks** (`app/law.py`): HDC coverage (one query per requirement, so each reason is its own finding);
  non-interference by self-composition (two households that differ only in savings, or only in whether they
  applied for Medicaid, must be treated the same); collection timing and application windows over every
  first-bill day; payment-plan cap; 501(r) windows; gaps between income bands; summary vs policy equivalence.
- **Readable counterexamples**: MaxSMT (soft constraints) picks the most ordinary household that still fails,
  then money is pinned to round numbers when the failure survives.
- **Law quotes** are verbatim from HB21-1198 (C.R.S. 25.5-3-501 ff., 6-20-203), HCPF's Hospital Discounted Care
  Operations Manual (March 2026) and 26 CFR 1.501(r); a test checks each against the downloaded source.

## Which model does what, and how we know

| Job | Model (Nebius Token Factory) |
|---|---|
| Write the policy as logic | `nvidia/Nemotron-3-Ultra-550b-a55b` |
| Independent second model; meaning check of every quote | `nvidia/nemotron-3-super-120b-a12b` |
| Pick the official document among search results | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` |
| Read a bill photo (Check my bill tab) | `nvidia/Nemotron-3.5-Super-VL` |
| Every legal decision | Z3, no model |

Evaluation (`scripts/formal_eval.py`, results in `formal-eval.json`): we wrote reference models by hand for five
documents, checking every quote, and scored each model by the audit verdicts its model leads to.

| Model | Audit verdicts matching our references | Cost per document |
|---|---|---|
| Nemotron 3 Nano | 70% | $0.004 |
| Nemotron 3 Super | 86% | $0.007 |
| Nemotron 3 Ultra | 86% | $0.015 |
| Ultra + Super + counterexample repair (earlier meaning check) | 76% | $0.063 |
| **Ultra + Super, verdicts both agree on** (86% of verdicts) | **93%** (disputed ones: 43%) | $0.022 |

What we learned: Ultra and Super read about equally well alone; together, their agreement is the useful signal.
A second model works best as a confidence check, not as an editor. Letting Ultra rewrite its model from the households where the two models disagree
cost four times as much and scored lower, so it's off. Most errors are a real quote used for the wrong claim (a
"reapply every six months" rule read as the application window), which is why the meaning check exists and why
an audit finding that rests on a rule being *absent* is shown as inconclusive when any rule was dropped. We also
tried letting Ultra settle each disputed finding by reading the policy for that one case: it agreed with our
references on 0 of 6 decided cases and tended to dismiss findings the text supports, so legal verdicts stay with
Z3. Nemotron found three clauses our own Children's reference had missed; we corrected the reference and noted
it in the file. No model is logically equivalent to our references on any whole document yet. Five references is a small
set: treat these as directions, not precise rates.

## The Colorado audit (October 2026)

28 policy documents found for 38 Colorado hospital systems (five sites blocked downloads); 23 formalised and
audited, covering 49 hospitals; 5 documents lost every rule in the checks and are listed as not formalised.
Z3 proved 89 requirements and found 31 counterexamples. 17 were confirmed by both models,
and we read each against the policy text (`eval/reviews.json`): 12 upheld, 4 qualified, 1 dismissed because both
models missed a clause. 18 more are inconclusive. Tax status for 501(r) comes from IRS data (ProPublica Nonprofit Explorer); hospitals with no
501(c)(3) record (mostly public districts) skip the federal checks. All Z3 work for the state takes about 3.0 s.

## Where Nebius Token Factory helped

- One OpenAI-compatible endpoint for four Nemotron sizes, so the eval could compare them with one code path.
- Structured output (`response_format` JSON schema) for the formal models.
- Prefix caching: the policy text opens every prompt, so the meaning check reuses the cached policy tokens.
- Low prices: the whole build, including every eval run and every policy, cost $1.80.

## Run it

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements-dev.txt
cp .env.example .env          # add NEBIUS_API_KEY and TAVILY_API_KEY
./run.sh                      # http://127.0.0.1:8100 (audits are served from audit/, no key needed to view)
./run.sh test                 # offline tests

./venv/bin/python scripts/build_atlas.py --fetch-only     # Tavily + Nano: collect policies into data/corpus/
./venv/bin/python scripts/audit.py --formalize all        # Ultra + Super formal models into formal/
./venv/bin/python scripts/audit.py                        # Z3 audit into audit/
./venv/bin/python scripts/formal_eval.py                  # score the models against eval/formal/
```

`DAILY_BUDGET_USD` and `TOTAL_BUDGET_USD` in `.env` stop live model calls before they cost more than you allow.

## Limitations

- It audits the **written** policy. A hospital may apply Colorado's law through a process its policy doesn't
  describe, so a counterexample means "the policy as written doesn't guarantee this", not "the hospital breaks
  the law". Not legal advice.
- The translation can be wrong (see the eval). Every finding shows the policy's own words; disputed findings
  need a person.
- Facts the vocabulary can't express (specific services, documents, procedures) are left out and noted.
- Only Colorado law and federal 501(r). Some hospital sites block downloads; those hospitals are missing, not
  passed. Policies change; each audit links the document it used.

## Data

Policy texts are public documents; the repo keeps short quotes and links, not the documents. The reference
models in `eval/formal/` are ours. Licence: MIT.
