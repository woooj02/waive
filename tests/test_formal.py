import json
from pathlib import Path

import pytest
import z3

from app.formal import Atom, Household, PolicyModel, Requirement, Rule
from app.formalize import SCHEMA, lint
from app.law import LAW_QUOTES, SRC, audit, equivalent
from app.verify import found, normalize

ROOT = Path(__file__).resolve().parent.parent
FORMAL = ROOT / "eval" / "formal"
CORPUS = ROOT / "data" / "corpus"
LAW = ROOT / "research" / "law"


def load(pid: str) -> PolicyModel:
    return PolicyModel(**json.loads((FORMAL / f"{pid}.json").read_text()))


def by_check(findings, check):
    return [f for f in findings if f.check == check]


@pytest.mark.parametrize("pid", [p.stem for p in FORMAL.glob("*.json")])
def test_reference_quotes_are_in_the_policy(pid):
    text = CORPUS / f"{pid}.txt"
    if not text.exists():
        pytest.skip("policy text not collected")
    src = normalize(text.read_text())
    pm = load(pid)
    quotes = [r.quote for r in pm.requires + pm.rules] + \
             [d.quote for d in pm.timing.application + pm.timing.eca_not_before]
    assert all(found(q, src) for q in quotes)


def test_law_quotes_are_in_the_sources():
    files = {"hb1198": LAW / "hb21-1198.txt", "manual": LAW / "hdc-manual.txt", "501r1": LAW / "501r-1.txt",
             "501r6": LAW / "501r-6.txt"}
    if not all(f.exists() for f in files.values()):
        pytest.skip("law sources not downloaded")
    texts = {k: normalize(v.read_text().replace("●", " ")) for k, v in files.items()}
    for key, (src, quote) in LAW_QUOTES.items():
        assert found(quote, texts[src]), key
    assert set(k for k, _ in LAW_QUOTES.values()) <= set(SRC)


def test_fpl_boundary_is_exact():
    # 2026: household of 3 → $27,320; 250% → $68,300.
    h = Household()
    at = h.atom(Atom(var="fpl_pct", op="<=", value=250))
    s = z3.Solver()
    s.add(*h.axioms(), h.size == 3)
    assert s.check(at, h.income == 68_300) == z3.sat
    assert s.check(at, h.income == 68_301) == z3.unsat


def test_childrens_collection_timing_counterexample():
    f = by_check(audit(load("childrens-colorado")), "co_eca_182")[0]
    assert f.status == "counterexample"
    assert f.household["first_statement_day"] == 46 and f.household["policy_day"] == 166


def test_childrens_federal_window_first_failing_day():
    f = by_check(audit(load("childrens-colorado")), "fed_window_240")[0]
    assert f.status == "counterexample"
    assert f.household["first_statement_day"] == 126 and f.household["fails_every_later_day"]


def test_uchealth_timing_is_proved():
    fs = audit(load("uchealth"))
    assert {f.status for f in by_check(fs, "co_eca_182") + by_check(fs, "fed_window_240")} == {"proved"}


def test_intermountain_service_area_conflicts_with_hdc():
    fs = by_check(audit(load("intermountain")), "co_hdc_coverage")
    area = [f for f in fs if "service area" in f.summary]
    assert area and area[0].household["co_resident"] and not area[0].household["in_service_area"]
    assert any(f.severity == "allows_denial" for f in fs)


def test_healthone_covers_hdc_and_skips_501r():
    fs = audit(load("healthone"))
    assert by_check(fs, "co_hdc_coverage")[0].status == "proved"
    assert by_check(fs, "fed_window_240")[0].status == "n/a"


def test_band_gap_between_250_and_251():
    f = by_check(audit(load("uchealth")), "monotone")[0]
    assert f.status == "counterexample"
    assert 250 < f.household["fpl_pct"] < 251 or 250 < f.household_b["fpl_pct"] <= 251


def test_every_model_is_equivalent_to_itself():
    for p in FORMAL.glob("*.json"):
        assert [f.status for f in equivalent(load(p.stem), load(p.stem))] == ["proved"]


def test_summary_omits_presumptive_eligibility():
    fs = equivalent(load("uchealth"), load("uchealth-summary"))
    assert len(fs) == 1 and fs[0].household["presumptive_program"]


def test_lint_catches_dead_rules_and_unknown_vars():
    pm = PolicyModel(hospital="X", rules=[Rule(id="dead", label="", when=[
        [Atom(var="fpl_pct", op="<=", value=100)], [Atom(var="fpl_pct", op=">", value=200)]])])
    assert any("never apply" in p for p in lint(pm))
    pm.rules[0].when = [[Atom(var="zip_code", op="==", value=80203)]]
    assert any("unknown variable" in p for p in lint(pm))
    assert lint(load("uchealth")) == []


def test_schema_has_no_refs():
    assert "$ref" not in json.dumps(SCHEMA)


def test_pipeline_repairs_a_disagreement_with_a_counterexample(monkeypatch):
    """Offline run of formalize(): Super drops a rule, Z3 finds the household, the reconcile call fixes it."""
    from app import formalize as fz
    text = (CORPUS / "uchealth.txt").read_text() if (CORPUS / "uchealth.txt").exists() else None
    if text is None:
        pytest.skip("policy text not collected")
    good = json.loads((FORMAL / "uchealth.json").read_text())
    bad = json.loads(json.dumps(good))
    bad["rules"] = [r for r in bad["rules"] if r["id"] != "presumptive"]
    calls = []

    def fake(step, model_key, messages, meter=None, schema=None, **kw):
        calls.append(step)
        if step.startswith("reconcile"):
            assert "SNAP" in messages[-1]["content"]  # the counterexample household reached the model
            return good
        return bad if model_key == "ultra" else good

    monkeypatch.setattr(fz.llm, "chat_json", fake)
    pm, rep = fz.formalize(text, fz.llm.Meter(), rounds=1)
    assert rep["disagreements_before"] and rep["equivalent"]
    assert any(s.startswith("reconcile") for s in calls)
    assert [f.status for f in equivalent(pm, load("uchealth"))] == ["proved"]


def test_lint_catches_or_instead_of_and():
    pm = PolicyModel(hospital="X", rules=[Rule(id="band", label="", when=[
        [Atom(var="fpl_pct", op=">=", value=251), Atom(var="fpl_pct", op="<=", value=400)]])])
    assert any("Did you mean AND" in p for p in lint(pm))


def test_bill_check_uses_the_audited_model_and_z3():
    from app import prover_check
    from app.schema import Bill, Household as HH
    got = prover_check.find("UCHealth University of Colorado Hospital")
    if not got:
        pytest.skip("no audit built")
    _, pm, _ = got
    bill = Bill(hospital="x", balance_usd=8412, statement_date="2026-10-15")
    v, proof = prover_check.decide(pm, HH(size=4, income_usd=70000, state="CO", insured=False), bill)
    assert v.status == "free" and proof["always"]
    v, proof = prover_check.decide(pm, HH(size=4, income_usd=70000, state="CO", insured=True), bill)
    assert not proof["always"] and proof["sometimes"] and any("under-insured" in d for d in proof["depends_on"])
    assert prover_check.find("Memorial Regional Health") is not None  # every word generic: still matched


def test_reprove_returns_exactly_the_published_audit():
    """The live button must not undo reviews, confidence or inconclusive marking (it once did)."""
    from fastapi.testclient import TestClient
    from app.main import app
    audit_dir = ROOT / "audit"
    if not (audit_dir / "index.json").exists():
        pytest.skip("no audit built")
    client = TestClient(app)
    ids = [p["id"] for p in json.loads((audit_dir / "index.json").read_text())["policies"]
           if not p.get("not_formalised")]
    strip = lambda fs: [{k: v for k, v in f.items() if k not in ("seconds", "live")} for f in fs]  # noqa: E731
    for pid in ids:
        stored = json.loads((audit_dir / f"{pid}.json").read_text())["findings"]
        live = client.post(f"/api/audit/{pid}/prove").json()["findings"]
        assert strip(live) == strip(stored), pid
        assert all(f["live"] == "holds" for f in live), (pid, [f["check"] for f in live if f["live"] != "holds"])


def test_confirmation_is_per_finding():
    """A counterexample is confirmed only if the second model fails the same check for the same case."""
    from app.auditing import replay
    ref = load("uchealth")
    weaker = ref.model_copy(deep=True)
    weaker.rules = [r for r in weaker.rules if r.id != "presumptive"]
    f = next(x for x in audit(ref) if x.check == "co_hdc_coverage" and x.status == "counterexample").as_dict()
    assert replay(f, ref) is True          # same model: the household is still not covered
    covering = ref.model_copy(deep=True)
    covering.rules.append(Rule(id="all", label="everyone", when=[[Atom(var="fpl_pct", op=">=", value=0)]]))
    assert replay(f, covering) is False    # a model that covers this household doesn't confirm it


def test_confirmation_needs_most_super_models(monkeypatch):
    """One Super run is noisy: a counterexample is confirmed only when most of the Super models fail the same case."""
    import app.auditing as au
    ref = load("uchealth")
    covering = ref.model_copy(deep=True)
    covering.rules.append(Rule(id="all", label="everyone", when=[[Atom(var="fpl_pct", op=">=", value=0)]]))
    monkeypatch.setattr(au, "dropped_rules", lambda pid: 0)
    monkeypatch.setattr(au, "REVIEWS", [])
    for agree, want in ((2, "disputed"), (3, "confirmed")):
        others = [(ref, {})] * agree + [(covering, {})] * (5 - agree)
        monkeypatch.setattr(au, "second_models", lambda pid, o=others: o)
        f = next(x for x in au.audit_document("uchealth", ref, "nemotron")
                 if x["check"] == "co_hdc_coverage" and x["status"] == "counterexample")
        assert f["votes"] == {"agree": agree, "of": 5, "unable": 0} and f["confidence"] == want


def test_a_super_model_with_no_rules_cannot_confirm(monkeypatch):
    """With no rule granting help, a model denies everyone: that is not agreement with a coverage gap."""
    import app.auditing as au
    ref = load("uchealth")
    empty = ref.model_copy(deep=True)
    empty.rules = []
    monkeypatch.setattr(au, "dropped_rules", lambda pid: 0)
    monkeypatch.setattr(au, "REVIEWS", [])
    monkeypatch.setattr(au, "second_models", lambda pid: [(ref, {}), (ref, {}), (empty, {}), (empty, {}), (empty, {})])
    f = next(x for x in au.audit_document("uchealth", ref, "nemotron")
             if x["check"] == "co_hdc_coverage" and x["status"] == "counterexample")
    assert f["votes"] == {"agree": 2, "of": 5, "unable": 3} and f["confidence"] == "disputed"


def test_a_super_model_that_lost_rules_cannot_confirm_a_gap_by_absence(monkeypatch):
    """A Super model that lost rules and turns the case away only because none of its rules grants help can't
    confirm: the missing rule may be the one it lost (the guard Ultra's own findings get). Round-4 audit."""
    import app.auditing as au
    pm = PolicyModel(hospital="X", rules=[Rule(id="low", label="free to 200%", quote="q", discount_pct=100,
                                               when=[[Atom(var="fpl_pct", op="<=", value=200)]])])
    lost = {"dropped": [{"kind": "rule", "id": "rule-3"}]}
    monkeypatch.setattr(au, "dropped_rules", lambda pid: 0)
    monkeypatch.setattr(au, "REVIEWS", [])
    monkeypatch.setattr(au, "second_models", lambda pid: [(pm, {}), (pm, {}), (pm, lost), (pm, lost), (pm, lost)])
    f = next(x for x in au.audit_document("x", pm, "nemotron")
             if x["check"] == "co_hdc_coverage" and x["status"] == "counterexample")
    assert f["basis"] == "absence" and f["votes"] == {"agree": 2, "of": 5, "unable": 3}
    assert f["confidence"] == "disputed"
    # turned away by a stated requirement: a lost rule doesn't explain that, so the vote stands
    strict = pm.model_copy(deep=True)
    strict.rules[0].when = [[Atom(var="fpl_pct", op="<=", value=300)]]
    strict.requires = [Requirement(id="ins", label="uninsured only", quote="q", cond=[[Atom(var="insured", op="false")]])]
    g = next(x for x in au.audit(strict) if x.check == "co_hdc_coverage" and x.status == "counterexample").as_dict()
    assert au.vote(g, strict, lost) is True and au.vote(f, pm, lost) is None and au.vote(f, pm, {}) is True


def test_matrix_cell_status_and_confidence_come_from_one_finding():
    from app.auditing import matrix_cells
    fs = [{"check": "c", "status": "counterexample", "confidence": "disputed"},
          {"check": "c", "status": "inconclusive", "confidence": "confirmed"}]
    assert matrix_cells(fs) == ({"c": "counterexample"}, {"c": "disputed"})
    fs.append({"check": "c", "status": "counterexample", "confidence": "confirmed"})
    assert matrix_cells(fs) == ({"c": "counterexample"}, {"c": "confirmed"})


def _bill_check(pm, size=3, income=27_000, insured=False, assets=0, balance=1000, statement="", service=""):
    from app.prover_check import decide
    from app.schema import Bill, Household as Hh
    hh = Hh.model_construct(size=size, income_usd=income, state="CO", county="", insured=insured, assets_usd=assets,
                            programs=[])
    return decide(pm, hh, Bill(balance_usd=balance, statement_date=statement, service_date=service))


def _simple(**kw) -> PolicyModel:
    pm = PolicyModel(hospital="X", source_url="u", rules=[Rule(id="r", label="help", quote="q", discount_pct=100,
                                                              when=[[Atom(var="fpl_pct", op="<=", value=400)]])])
    return pm.model_copy(update=kw)


def test_bill_blank_savings_are_unknown_not_zero():
    """An asset test with savings left blank: "may qualify, depends on savings", never free care for sure."""
    pm = _simple(requires=[Requirement(id="a", label="assets under $10,000", quote="q",
                                       cond=[[Atom(var="assets_usd", op="<", value=10_000)]])])
    v, proof = _bill_check(pm, assets=None)
    assert not proof["always"] and proof["sometimes"] and v.status != "free"
    assert any("savings" in m for m in v.missing)
    assert _bill_check(pm, assets=0)[0].status == "free"
    assert not _bill_check(pm, assets=50_000)[1]["sometimes"]
    v, _ = _bill_check(_simple(), assets=None)  # no asset test: blank savings change nothing
    assert v.status == "free" and not any("savings" in m for m in v.missing)


def test_bill_expected_amount_never_exceeds_colorado_cap():
    """20% off a $10,000 bill is $8,000, but Colorado caps a $12,000-a-year household at $1,440."""
    pm = _simple()
    pm.rules[0].discount_pct = 20
    v, _ = _bill_check(pm, size=1, income=12_000, balance=10_000)
    assert v.colorado["total_max_usd"] == 1440 and v.expected_usd == 1440 and "Colorado law caps" in v.headline


def test_bill_deadline_comes_from_the_audited_policy():
    """UCHealth's window is 365 days after the first bill; a for-profit with no window gets Colorado's 181 days
    after care (or a request for the date of care), never a default 240 (round-4 audit)."""
    uch = load("uchealth")
    assert {(d.anchor, d.days) for d in uch.timing.application} >= {("first_statement", 365)}
    v, _ = _bill_check(uch, statement="2026-01-01")
    assert v.deadline == "2027-01-01" and "365 days after the first bill" in v.deadline_basis
    forprofit = _simple(nonprofit=False)
    v, _ = _bill_check(forprofit, statement="2026-01-01")
    assert v.deadline == "" and any("date of care" in m for m in v.missing)
    v, _ = _bill_check(forprofit, statement="2026-01-01", service="12/01/2025 - 12/03/2025")
    assert v.deadline == "2026-05-31" and "Colorado" in v.deadline_basis
    v, _ = _bill_check(_simple(nonprofit=True), statement="2026-01-01", service="2025-12-01")
    assert v.deadline == "2026-08-29" and "501(r)" in v.deadline_basis  # the later of the two legal minimums


def test_bill_amount_is_guaranteed_not_one_reading():
    """100% inside the service area, 20% outside, area unknown: help is certain, free care is not."""
    pm = load("uchealth").model_copy(deep=True)
    pm.requires = []
    pm.rules = [Rule(id="in", label="in area", quote="q", discount_pct=100,
                     when=[[Atom(var="in_service_area", op="true")]]),
                Rule(id="out", label="out of area", quote="q", discount_pct=20,
                     when=[[Atom(var="in_service_area", op="false")]])]
    v, proof = _bill_check(pm)
    assert proof["always"] and proof["discount_range"] == [20.0, 100.0]
    assert v.status == "discount" and v.discount_pct == 20 and v.expected_usd == 800
    assert "at least 20%" in v.headline and any("service area" in m for m in v.missing)


def test_bill_outside_the_proof_domain_is_refused():
    """13 people is outside the encoded domain: no "always" or "never" claim, a plain refusal instead."""
    v, proof = _bill_check(load("uchealth"), size=13)
    assert proof["in_domain"] is False and v.status == "needs_info" and "outside the range" in v.headline


def test_patient_data_never_touches_the_cache(monkeypatch, tmp_path):
    """private=True: no read from and no write to the disk cache (bill uploads, packet summaries)."""
    from types import SimpleNamespace
    from app import llm
    monkeypatch.setattr(llm, "CACHE", tmp_path)
    monkeypatch.setattr(llm, "_record_spend", lambda c: None)
    monkeypatch.setattr(llm, "spent_today", lambda: 0.0)
    monkeypatch.setattr(llm, "spent_total", lambda: 0.0)
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, prompt_tokens_details=None)
    resp = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"balance": 1}'))], usage=usage)
    monkeypatch.setattr(llm, "_create", lambda kw: resp)
    llm.chat("read bill", "vl", [{"role": "user", "content": "fictional bill"}], private=True)
    assert list(tmp_path.iterdir()) == []
    llm.chat("read bill", "vl", [{"role": "user", "content": "fictional sample"}])
    assert len(list(tmp_path.iterdir())) == 1  # samples still cache


def test_meaning_check_cannot_remove_an_amount_the_quote_states(monkeypatch):
    """Super once marked HealthONE's "will have a 100% Charity discount" as an unsupported amount while its own reason
    said the policy states 100%: plain code keeps an amount the verified quote states as the discount."""
    from app import formalize as fz
    from app.formalize import check_support, states_amount
    pm = PolicyModel(hospital="X", rules=[
        Rule(id="full", label="free", discount_pct=100, quote="patients at or below 250% FPL will have a 100% Charity "
                                                                "discount processed", when=[[Atom(var="fpl_pct", op="<=", value=250)]]),
        Rule(id="band", label="80%", discount_pct=80, quote="households at 100% of the poverty level qualify",
             when=[[Atom(var="fpl_pct", op="<=", value=100)]])])
    doubt = {"verdicts": [{"id": "rule:full", "verdict": "amount_unsupported", "why": "policy states 100%"},
                          {"id": "rule:band", "verdict": "amount_unsupported", "why": "no amount stated"}]}
    monkeypatch.setattr(fz.llm, "chat_json", lambda *a, **k: doubt)
    out, dropped = check_support(pm, "text", None)
    assert [r.discount_pct for r in out.rules] == [100, None]
    assert [d["id"] for d in dropped] == ["rule:band"] and any("kept" in n for n in out.notes)
    assert not states_amount("FPL Discount\n</= 250%  100%\n251% – 350%  80%", 80)


def test_cache_only_never_calls_the_model(monkeypatch, tmp_path):
    from app import llm
    monkeypatch.setattr(llm, "CACHE", tmp_path)
    monkeypatch.setenv("LLM_CACHE_ONLY", "1")
    monkeypatch.setattr(llm, "_create", lambda kw: (_ for _ in ()).throw(AssertionError("called the model")))
    with pytest.raises(llm.LLMError):
        llm.chat("x", "super", [{"role": "user", "content": "not cached"}])
