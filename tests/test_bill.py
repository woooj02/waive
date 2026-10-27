from app.bill import BillFields, LineItem, check


def test_clean_bill_has_no_notes():
    b = BillFields(total_charges_usd=300, balance_usd=300,
                   line_items=[LineItem(date="2026-09-01", code="99284", amount_usd=200),
                               LineItem(date="2026-09-01", code="71046", amount_usd=100)])
    assert check(b) == []


def test_totals_mismatch_and_duplicate_flagged():
    b = BillFields(total_charges_usd=500, insurance_paid_usd=0, balance_usd=450,
                   line_items=[LineItem(date="2026-09-01", code="85025", amount_usd=150),
                               LineItem(date="2026-09-01", code="85025", amount_usd=150),
                               LineItem(date="2026-09-01", code="99284", amount_usd=150)])
    notes = check(b)
    assert any("add up to $450.00" in n for n in notes)
    assert any("not the $450.00 balance" in n for n in notes)
    assert any("duplicate" in n and "85025" in n for n in notes)
