"""Extraction completeness — the guard against silently dropping rows, checked
against the statement's own declared transaction count."""
from bsa.completeness import check_completeness, declared_from_text


HDFC_FOOTER = ("OpeningBalance DrCount CrCount Debits Credits ClosingBal\n"
               "12,310.82 75 19 1,546,406.97 1,534,787.00 690.85")
SBI_FOOTER = ("Statement Summary : 01-04-2026 To 22-07-2026\n"
              "36,564.60CR 339 44 9,62,573.18 9,41,141.00 15,132.42CR")


def test_parse_declared_counts():
    p = r'DrCount CrCount[\s\S]*?[\d,]+\.\d{2}\s+(?P<n_debits>\d+)\s+(?P<n_credits>\d+)\s'
    assert declared_from_text(HDFC_FOOTER, p) == {"n_debits": 75, "n_credits": 19}
    p2 = r'Statement Summary[\s\S]*?[\d,]+\.\d{2}\s*C?R?\s+(?P<n_debits>\d+)\s+(?P<n_credits>\d+)\s+[\d,]+\.\d{2}'
    assert declared_from_text(SBI_FOOTER, p2) == {"n_debits": 339, "n_credits": 44}


def test_matching_total_is_complete():
    # 94 extracted == 75 + 19 declared -> complete, even if the Dr/Cr split
    # differs (a reversal in the withdrawal column nets positive).
    c = check_completeness(94, 71, 23, {"n_debits": 75, "n_credits": 19})
    assert c["checked"] and c["complete"] and c["notes"] == []


def test_dropped_rows_are_caught():
    # The 598 failure: 183 extracted against 299 declared.
    c = check_completeness(183, 160, 23, {"n_txns": 299})
    assert c["checked"] and not c["complete"]
    assert "116 not extracted" in c["notes"][0]


def test_no_declared_totals_is_not_checked():
    assert check_completeness(100, 50, 50, {}) == {"checked": False}


def test_amount_totals_match_is_complete():
    # Axis prints "TRANSACTION TOTAL <dr> <cr>"; matching extracted sums pass.
    d = {"sum_debits": 124214245.60, "sum_credits": 124389503.07}
    c = check_completeness(1782, 900, 882, d, 124214245.60, 124389503.07)
    assert c["checked"] and c["complete"]


def test_amount_total_gap_is_flagged():
    # A dropped/mis-read row shifts the debit sum beyond tolerance.
    d = {"sum_debits": 4573531.88, "sum_credits": 4586656.68}
    c = check_completeness(1237, 700, 537, d, 4562893.72, 4586656.68)
    assert not c["complete"] and "10,638" in c["notes"][0]


def test_amount_totals_tolerate_rounding():
    d = {"sum_debits": 100000.00, "sum_credits": 50000.00}
    c = check_completeness(10, 5, 5, d, 100000.01, 49999.99)
    assert c["complete"]


def test_parse_axis_transaction_total():
    p = r'TRANSACTION TOTAL\s+(?P<sum_debits>[\d,]+\.\d{2})\s+(?P<sum_credits>[\d,]+\.\d{2})'
    got = declared_from_text("OPENING BALANCE .00\nTRANSACTION TOTAL 4,573,531.88 4,586,656.68\nCLOSING BALANCE 13124.80", p)
    assert got == {"sum_debits": 4573531.88, "sum_credits": 4586656.68}
