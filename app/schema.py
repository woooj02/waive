"""The rule program: one hospital's financial-assistance policy as data.

Nemotron writes it; code checks every quote against the policy text (verify.py)
and evaluates it (engine.py). A field whose quote can't be proven is set back to
unknown, so it can never change a verdict.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Quoted(BaseModel):
    """Any rule that came from the policy carries the sentence it came from."""

    quote: str = Field("", description="Verbatim sentence from the policy that states this rule")
    verified: bool = False


class Tier(Quoted):
    min_fpl: float = Field(0, description="Lower bound, % of the federal poverty level (exclusive, 0 = none)")
    max_fpl: float = Field(description="Upper bound, % of the federal poverty level (inclusive)")
    discount_pct: float | None = Field(description="Percent off the patient's balance; 100 means free care; null if the policy gives no percent (state rates, AGB, a table we can't read)")
    label: str = ""
    facility: str = Field("", description="Facility name if this discount applies only at one facility; empty if it applies at all of them")


class AssetTest(Quoted):
    applies: bool | None = None
    limit_usd: float | None = Field(None, description="Asset limit in dollars if the policy states one")
    exclusions: list[str] = Field(default_factory=list, description="Assets not counted, e.g. primary home")


class Residency(Quoted):
    kind: Literal["none", "state", "service_area", "county_list", "unknown"] = "unknown"
    states: list[str] = Field(default_factory=list, description="Two-letter state codes if kind is state")
    areas: list[str] = Field(default_factory=list, description="Counties or ZIP areas if listed")


class Presumptive(Quoted):
    program: str = Field(description="Program that qualifies a patient automatically, e.g. Medicaid, SNAP, WIC")
    discount_pct: float = 100


class Hardship(Quoted):
    applies: bool | None = None
    bill_pct_of_income: float | None = Field(None, description="Bills above this % of yearly income qualify")


class Insured(Quoted):
    eligible: bool | None = Field(None, description="Can insured patients get help with their remaining balance?")


class Providers(Quoted):
    scope: Literal["facility_only", "listed", "all", "unknown"] = "unknown"
    not_covered: list[str] = Field(default_factory=list, description="Provider groups that bill separately and are not covered")


class Application(Quoted):
    window_days: int | None = Field(None, description="Days after the first bill that applications are accepted")
    documents: list[str] = Field(default_factory=list)
    how_to_apply: str = ""


class RuleProgram(BaseModel):
    hospital: str
    system: str = ""
    state: str = ""
    source_url: str = ""
    effective_date: str = ""
    tiers: list[Tier] = Field(default_factory=list)
    asset_test: AssetTest = Field(default_factory=AssetTest)
    residency: Residency = Field(default_factory=Residency)
    presumptive: list[Presumptive] = Field(default_factory=list)
    hardship: Hardship = Field(default_factory=Hardship)
    insured: Insured = Field(default_factory=Insured)
    providers: Providers = Field(default_factory=Providers)
    application: Application = Field(default_factory=Application)
    unknowns: list[str] = Field(default_factory=list, description="Questions the policy doesn't answer")
    dropped: list[dict] = Field(default_factory=list, description="Rules removed because their quote wasn't found")


class Household(BaseModel):
    size: int = Field(ge=1, le=12, description="The proofs cover households of 1-12 people")
    income_usd: float = Field(ge=0, description="Gross yearly household income")
    state: str = Field("CO", min_length=2, max_length=2)
    county: str = ""
    insured: bool = False
    assets_usd: float | None = Field(None, ge=0)
    programs: list[str] = Field(default_factory=list, description="Programs the household is enrolled in")


class Bill(BaseModel):
    hospital: str = ""
    balance_usd: float = Field(ge=0)
    statement_date: str = Field("", description="First statement date, YYYY-MM-DD")
    provider_type: Literal["hospital", "physician_group", "unknown"] = "hospital"
