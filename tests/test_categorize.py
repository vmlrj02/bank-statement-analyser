"""Categorisation against the SME lending taxonomy. These pin the tags the
review sheet corrected by hand — each case here was once wrong in a report."""
import pytest

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


def test_atm_withdrawal_with_hyphen_form():
    """Axis prints "ATM-CASH/+<branch>" — seven of these read Regular debit."""
    t = txn("ATM-CASH/+SARJAPUR ROAD BR/BANGALORE-URB/010226", -10000.0,
            mode="atm-cash")
    assert categorize([t])[0].category == "cash withdrawal"


def test_cheque_return_charges_are_a_bounce_not_a_transfer():
    """The bank's inward clearing is a cheque drawn ON the account, so its
    return charge is the customer's own payment bouncing — outward."""
    t = categorize([txn("Chq Rtrn Chrgs Incl GST", -590.0)])[0]
    assert t.category == "Outward Bounced Xns"


@pytest.mark.parametrize("desc", ["SMS Chrgs Incl GST", "Keeping Chgs-- / 38976288"])
def test_chrgs_and_chgs_spellings_are_penal(desc):
    assert categorize([txn(desc, -649.0)])[0].category == "other penal charges"


def test_a_reversed_returned_instrument_credit_is_a_refund():
    t = categorize([txn("RVSL IW CTR RTN CHQNO:011541_DT:01122025/FEDERAL",
                        2900000.0)])[0]
    assert t.category == "return / refund"
