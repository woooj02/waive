import json
from pathlib import Path

import pytest
import z3

from app.formal import Atom, Household, PolicyModel, Rule
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
    pm, rep = fz.formalize(text, fz.llm.Meter())
    assert rep["disagreements_before"] and rep["equivalent"]
    assert any(s.startswith("reconcile") for s in calls)
    assert [f.status for f in equivalent(pm, load("uchealth"))] == ["proved"]
