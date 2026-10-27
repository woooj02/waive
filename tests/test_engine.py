from app.engine import colorado_cap, deadline, evaluate, fpl, pct_fpl
from app.schema import AssetTest, Bill, Household, Insured, Presumptive, Providers, Residency, RuleProgram, Tier


def program(**kw) -> RuleProgram:
    tiers = [
        Tier(min_fpl=0, max_fpl=250, discount_pct=100, quote="free care up to 250%", verified=True),
        Tier(min_fpl=251, max_fpl=400, discount_pct=60, quote="60% discount 251-400%", verified=True),
    ]
    return RuleProgram(hospital="Test Hospital", source_url="https://example.org/fap.pdf", tiers=tiers, **kw)


def test_fpl_table_2026():
    assert fpl(1) == 15_960
    assert fpl(4) == 33_000
    assert fpl(8) == 55_720
    assert fpl(1, "AK") == 19_950
    assert fpl(1, "HI") == 18_360


def test_pct_fpl():
    assert pct_fpl(66_000, 4) == 200.0
    assert pct_fpl(0, 3) == 0


def test_free_tier_boundary_inclusive():
    v = evaluate(program(), Household(size=4, income_usd=82_500, state="TX"), Bill(balance_usd=8412))
    assert v.pct_fpl == 250.0 and v.status == "free" and v.expected_usd == 0


def test_gap_between_whole_number_tiers_goes_to_discount():
    v = evaluate(program(), Household(size=4, income_usd=82_650, state="TX"), Bill(balance_usd=1000))
    assert 250 < v.pct_fpl < 251 and v.status == "discount" and v.expected_usd == 400


def test_above_limit_not_eligible_outside_colorado():
    v = evaluate(program(), Household(size=1, income_usd=90_000, state="TX"), Bill(balance_usd=1000))
    assert v.status == "not_eligible" and v.colorado is None


def test_unverified_tier_never_counts():
    p = program()
    p.tiers[0].verified = False
    v = evaluate(p, Household(size=4, income_usd=50_000, state="TX"), Bill(balance_usd=1000))
    assert v.status == "not_eligible"  # only the 251-400% tier is proven


def test_asset_test_missing_and_over_limit():
    p = program(asset_test=AssetTest(applies=True, limit_usd=10_000, quote="assets", verified=True))
    v = evaluate(p, Household(size=2, income_usd=30_000, state="TX"), Bill(balance_usd=500))
    assert v.status == "free" and v.missing and "likely" in v.headline
    v = evaluate(p, Household(size=2, income_usd=30_000, state="TX", assets_usd=50_000), Bill(balance_usd=500))
    assert v.status == "not_eligible"


def test_residency_state_rule():
    p = program(residency=Residency(kind="state", states=["CO"], quote="Colorado residents", verified=True))
    v = evaluate(p, Household(size=1, income_usd=20_000, state="WY"), Bill(balance_usd=500))
    assert v.status == "not_eligible"


def test_insured_excluded():
    p = program(insured=Insured(eligible=False, quote="uninsured only", verified=True))
    v = evaluate(p, Household(size=1, income_usd=20_000, state="TX", insured=True), Bill(balance_usd=500))
    assert v.status == "not_eligible"


def test_physician_group_not_covered():
    p = program(providers=Providers(scope="facility_only", quote="facility charges only", verified=True))
    v = evaluate(p, Household(size=1, income_usd=20_000, state="TX"), Bill(balance_usd=500, provider_type="physician_group"))
    assert v.status == "not_eligible"


def test_presumptive_medicaid():
    p = program(presumptive=[Presumptive(program="Medicaid", quote="Medicaid enrollees qualify", verified=True)])
    v = evaluate(p, Household(size=1, income_usd=200_000, state="TX", programs=["medicaid"]), Bill(balance_usd=500))
    assert v.status == "free"


def test_colorado_cap_when_hospital_policy_says_no():
    # Household at 240% FPL; the hospital only helps to 200%, but Colorado caps payments at 4% x 36.
    p = RuleProgram(hospital="H", tiers=[Tier(max_fpl=200, discount_pct=100, quote="q", verified=True)])
    hh = Household(size=1, income_usd=38_304, state="CO")
    v = evaluate(p, hh, Bill(balance_usd=20_000))
    cap = colorado_cap(hh, Bill(balance_usd=20_000))
    assert v.status == "colorado_cap"
    assert cap["monthly_max_usd"] == round(38_304 / 12 * 0.04, 2)
    assert v.expected_usd == round(38_304 / 12 * 0.04 * 36, 2)


def test_colorado_cap_lowers_a_discount():
    p = RuleProgram(hospital="H", tiers=[Tier(max_fpl=250, discount_pct=50, quote="q", verified=True)])
    v = evaluate(p, Household(size=1, income_usd=30_000, state="CO"), Bill(balance_usd=50_000))
    assert v.expected_usd == round(30_000 / 12 * 0.04 * 36, 2) and "Colorado" in v.headline


def test_no_policy_in_colorado_still_gets_cap():
    v = evaluate(None, Household(size=3, income_usd=40_000, state="CO"), Bill(balance_usd=30_000))
    assert v.status == "colorado_cap"


def test_deadline_240_days():
    assert deadline(Bill(balance_usd=1, statement_date="2026-10-01"), None) == "2027-05-29"
    assert deadline(Bill(balance_usd=1, statement_date="bad"), None) == ""
