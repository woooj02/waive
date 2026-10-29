# Waive: finding the households hospital charity-care policies leave behind

Colorado law promises hospital discounts to every resident at or below 250% of the poverty line, caps payment
plans at 4% of monthly income, and bars collection lawsuits for 182 days. Federal 501(r) rules add their own
windows for nonprofit hospitals. Each hospital writes its own 10–40 page policy. Nobody checks those policies
against the law, household by household.

Waive does. **NVIDIA Nemotron on Nebius Token Factory** writes each hospital's policy as logic, every clause tied
to a sentence of the policy. The **Z3 theorem prover** then checks that logic against Colorado's Hospital
Discounted Care law and 501(r), exhaustively within a stated range of households and billing timelines (1–12
people, income up to $1M, first bill by day 400). A pass is a proof about the encoded rules; a fail comes with
the exact household, or the exact day, where the written policy falls short. How faithful the encoded rules are
to each policy is measured separately (evaluation and reviews below).

Examples from the audit (all from public policies, each with the policy's and the law's own words on screen):

- Children's Hospital Colorado: if the first bill goes out on day 46, the written timeline allows extraordinary
  collection actions (lawsuits, garnishment) from day 166; Colorado bars them until day 182.
- Memorial Regional Health (Craig): with a first bill on day 46, the written policy stops taking applications on
  day 45 after care; Colorado requires at least 181 days.
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
     │      code: an amount the quote states ("a 100% Charity discount") is kept even if Super doubts it
     │
     ├─► Nemotron 3 Super: five independent formal models (same checks), one per run
     │
     └─► Z3 (app/law.py): 10 legal checks on each model + equivalence of the models
            a counterexample is "confirmed" only if the exact same household or day also fails in at least
            3 of the 5 Super models; otherwise "disputed". A Super model that lost rules in checking can't
            vote for a gap its own missing rule could explain. Every confirmed one is read against the policy.
```

- **Encoding** (`app/formal.py`): a symbolic household (size, income, assets, bill, nine yes/no facts such as
  insured, Colorado resident, in the service area, applied for Medicaid) and a symbolic first-bill day. Income as
  a percent of the 2026 poverty line stays linear: `100·income ≤ k·(15,960 + 5,680·(size−1))`.
- **Checks** (`app/law.py`): HDC coverage (one query per requirement, so each reason is its own finding);
  non-interference by self-composition (two households that differ only in savings, or only in whether they
  applied for Medicaid, must be treated the same); collection timing and application windows over every
  first-bill day; payment-plan cap; 501(r) windows; gaps between income bands; summary vs policy equivalence.
- **Check my bill**: a patient's household is checked against the same formal model. Known facts are fixed,
  unknown ones (service area, emergency, under-insured, savings left blank) stay symbolic, and Z3 answers "yes for sure", "yes if …"
  (naming the fact), or "no" (`app/prover_check.py`). The amount gets its own guarantee: Z3 finds the lowest and
  highest discount over every value of the unknowns, so "free" is only said when it is free in all of them. A
  household outside the proofs' range (over 12 people, income over $1M) is refused, not guessed. What's left to pay
  never exceeds Colorado's cap (4% of monthly income for 36 months). The apply-by date comes from the same policy's
  window; a policy with none gets the legal minimum (Colorado's 181 days after care, 501(r)'s 240 days after the
  first bill for nonprofits), and a missing date of care is asked for, not guessed. A "no" from a
  model that lost rules in checking becomes "we can't be sure". An uploaded bill and its reading are never cached
  or stored (only the fictional samples are cached).
- **Readable counterexamples**: MaxSMT (soft constraints) picks the most ordinary household that still fails,
  then money is pinned to round numbers when the failure survives.
- **Law quotes** are verbatim from HB21-1198 (C.R.S. 25.5-3-501 ff., 6-20-203), HCPF's Hospital Discounted Care
  Operations Manual (March 2026) and 26 CFR 1.501(r); a test checks each against the downloaded source.

## Which model does what, and how we know

| Job | Model (Nebius Token Factory) |
|---|---|
| Write the policy as logic | `nvidia/Nemotron-3-Ultra-550b-a55b` |
| Five independent second models; meaning check of every quote | `nvidia/nemotron-3-super-120b-a12b` |
| Pick the official document among search results | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` |
| Read a bill photo (Check my bill tab) | `nvidia/Nemotron-3.5-Super-VL` |
| Every legal decision | Z3, no model |

Evaluation (`scripts/formal_eval.py`, results in `formal-eval.json`): we wrote reference models by hand for five
documents, checking every quote, and scored each model by the audit verdicts its model leads to.

| Model | Audit verdicts agreeing with our references (5 documents) | Cost per document |
|---|---|---|
| Nemotron 3 Nano | 72% | $0.003 |
| Nemotron 3 Super | 84% | $0.007 |
| Nemotron 3 Ultra | 90% | $0.015 |
| Ultra + Super + counterexample repair (earlier meaning check) | 76% | $0.063 |
| **Checks where Ultra and Super agree** (84% of checks) | **95%** agreement (where they disagree: 62%) | $0.022 |

What we learned: Ultra is the best single reader; the agreement of two models is the most useful signal.
A second model works best as a confidence check, not as an editor. Letting Ultra rewrite its model from the households where the two models disagree
cost four times as much and scored lower, so it's off. Most errors are a real quote used for the wrong claim (a
"reapply every six months" rule read as the application window), which is why the meaning check exists and why
an audit finding that rests on a rule being *absent* is shown as inconclusive when any rule was dropped. We also
tried letting Ultra settle each disputed finding by reading the policy for that one case: it agreed with our
references on 0 of 6 decided cases and tended to dismiss findings the text supports, so legal verdicts stay with
Z3. Nemotron found three clauses our own Children's reference had missed; we corrected the reference and noted
it in the file. No model is logically equivalent to our references on any whole document yet. Five references is a small
set: treat these as directions, not precise rates.

Repeated runs (`scripts/stability_eval.py`, results in `stability-eval.json`): each model formalised the same five
documents five times with fresh calls. Ultra's wording changed from run to run, but every check got the same
verdict in all five runs (90% each time). Super was less steady: the same verdict in every run on 76% of checks
(84–92% per run). So one Super run is a noisy confirmer. We replayed each distinct counterexample Ultra found (25)
against our reference model:

| Confirmation rule | Confirmed, and the reference also fails | Disputed, but the reference also fails |
|---|---|---|
| One Super run (what we used before) | 33 of 35 pairings (94%) | 37 of 50 (74%) |
| **Most of five Super runs** (used now) | **6 of 6** | 8 of 11 (73%) |

This is exploratory, not validation: the rule was chosen after seeing these cases, several share one underlying
gap, and all five models are Super, so separate runs can share one misunderstanding. The production reviews show
the limit: 3 counterexamples confirmed by most Super models (two of them 5 of 5) were rejected on reading the
policy. It is also why disputed findings stay on the page for a person rather than being hidden. A Super run votes
"unable to confirm", never "fails too", when its failure could be its own lost rule: no rules left at all, or some
rules lost and the case turned away only because no surviving rule grants help (the guard that already makes such a
finding of Ultra's "inconclusive"). Each run's audit is computed in a fresh process, so the table is reproducible.

## The Colorado audit (October 2026)

28 policy documents found for 38 Colorado hospital systems (five sites blocked downloads); 25 formalised and
audited, covering 64 hospitals; 3 documents lost every rule in the checks and are listed as not formalised.
Z3 proved 90 requirements for the encoded rules and found 42 counterexamples. 8 are confirmed: the
exact same household or day also fails in at least three of five separate Nemotron 3 Super formalisations. Every
confirmed counterexample was read against the policy text during development (5 upheld, 3 qualified); 8 findings in all
(3 of them confirmed) were dismissed on review as translation errors, and 17 are inconclusive because a rule was
dropped in checking. The reviews (`eval/reviews.json`, 36 entries, shown on each finding) are ours, not
independent validation.
Tax status for 501(r) comes from IRS data (ProPublica Nonprofit Explorer); hospitals with no 501(c)(3) record
(mostly public districts) skip the federal checks. All Z3 work for the state, including replaying every finding
against the five Super models, takes about 17–22 s; each document is audited in a fresh process so the counterexample
Z3 reports depends on that document alone (two runs give identical output).

## Where Nebius Token Factory helped

- One OpenAI-compatible endpoint for four Nemotron sizes, so the eval could compare them with one code path.
- Structured output (`response_format` JSON schema) for the formal models.
- Prefix caching: the policy text opens every prompt, so the meaning check reuses the cached policy tokens.
- Low prices: the whole build, including every eval run, five Super runs per policy and every policy, cost about
  $3.10.

## Run it

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements-dev.txt
cp .env.example .env          # add NEBIUS_API_KEY and TAVILY_API_KEY
./run.sh                      # http://127.0.0.1:8100 (audits are served from audit/, no key needed to view)
./run.sh test                 # offline tests

./venv/bin/python scripts/build_atlas.py --fetch-only     # Tavily + Nano: collect policies into data/corpus/
./venv/bin/python scripts/audit.py --formalize all        # Ultra + Super formal models into formal/
./venv/bin/python scripts/audit.py --super-runs 5         # 4 more Super models per policy (live), then the Z3 audit
./venv/bin/python scripts/audit.py                        # Z3 audit into audit/ (no API calls)
LLM_CACHE_ONLY=1 ./venv/bin/python scripts/formal_eval.py --models nano super ultra   # rebuild from saved responses only
./venv/bin/python scripts/formal_eval.py                  # score the models against eval/formal/
./venv/bin/python scripts/stability_eval.py --runs 5      # repeated runs: stability and confirmation rules
LLM_NO_CACHE=1 ./venv/bin/python scripts/live_run.py childrens-colorado   # one policy, live, every call printed
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
