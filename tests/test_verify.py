from app.schema import AssetTest, Residency, RuleProgram, Tier
from app.verify import found, normalize, verify

SOURCE = """Patients with family income at or below 250% of the Federal
Poverty Guidelines will receive a 100% discount (free care). Patients between 251% and
400% of the FPG receive a sliding-scale dis-
count of 60%.  The Hospital does not consider assets when determining eligibility.
• Applicants must reside in the State of Colorado."""


def test_normalize_handles_pdf_glitches():
    assert "sliding-scale discount of 60%" in normalize(SOURCE)
    assert normalize("“Free” care") == '"free" care'


def test_exact_quote_found_across_line_breaks():
    src = normalize(SOURCE)
    assert found("at or below 250% of the Federal Poverty Guidelines will receive a 100% discount", src)


def test_fabricated_quote_rejected():
    src = normalize(SOURCE)
    assert not found("Patients at or below 300% of the Federal Poverty Guidelines receive free care", src)
    assert not found("100%", src)  # too short to prove anything


def test_verify_drops_planted_rule_and_keeps_real_ones():
    prog = RuleProgram(
        hospital="H",
        tiers=[
            Tier(max_fpl=250, discount_pct=100, quote="income at or below 250% of the Federal Poverty Guidelines will receive a 100% discount"),
            Tier(min_fpl=251, max_fpl=600, discount_pct=60, quote="Patients between 251% and 600% of the FPG receive a discount"),
        ],
        asset_test=AssetTest(applies=False, quote="The Hospital does not consider assets when determining eligibility."),
        residency=Residency(kind="state", states=["CO"], quote="Applicants must reside in the State of Colorado."),
    )
    out = verify(prog, SOURCE)
    assert out.tiers[0].verified and not out.tiers[1].verified
    assert out.asset_test.verified and out.residency.verified
    assert [d["rule"] for d in out.dropped] == ["tier 251-600% FPL"]
