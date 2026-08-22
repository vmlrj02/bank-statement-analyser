"""Categorisation against the SME lending taxonomy. These pin the tags the
review sheet corrected by hand — each case here was once wrong in a report."""
from bsa.categorize import categorize, category_detail
from bsa.models import Txn


def txn(desc, amount, mode="other"):
    t = Txn(date="2025-07-01", cheque_no="", description=desc, amount=amount,
            balance=0.0, mode=mode, counterparty="")
    t.compute_uid("1", 0)
    return t


def test_od_interest_debit_is_interest_debited_not_regular():
    """SBI prints "DEBIT INTEREST- /" on an OD account every month-end; a
    lender reads that as cost of borrowing, not an ordinary transfer."""
    t = categorize([txn("DEBIT INTEREST- /", -456845.00)])[0]
    assert t.category == "Interest debited"


def test_interest_received_is_untouched_by_the_debit_rule():
    t = categorize([txn("INTEREST PAID ON SAVINGS", 120.0, mode="interest")])[0]
    assert t.category == "Interest received"


def test_by_cash_credit_is_a_cash_deposit():
    """"Cash credit came as regular credit" — a BY CASH deposit must never
    fall through to Regular credit."""
    t = txn("BY CASH -NEW DELHI - FATEHPURI", 350000.0)
    t.mode = "cash-deposit"
    assert categorize([t])[0].category == "cash deposit"


def test_interest_debited_detail_reads_as_itself():
    t = categorize([txn("DEBIT INTEREST- /", -1000.0)])[0]
    assert category_detail(t) == "Interest debited"
