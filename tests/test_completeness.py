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
