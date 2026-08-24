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


def test_od_interest_debit_is_interest_payments_not_regular():
    """SBI prints "DEBIT INTEREST- /" on an OD account every month-end; a
    lender reads that as cost of borrowing, not an ordinary transfer. The master
    renamed this category "Interest payments" (ID8)."""
    t = categorize([txn("DEBIT INTEREST- /", -456845.00)])[0]
    assert t.category == "Interest payments"


def test_interest_received_is_untouched_by_the_debit_rule():
    t = categorize([txn("INTEREST PAID ON SAVINGS", 120.0, mode="interest")])[0]
    assert t.category == "Interest received"


def test_by_cash_credit_is_a_cash_deposit():
    """"Cash credit came as regular credit" — a BY CASH deposit must never
    fall through to Regular credit."""
    t = txn("BY CASH -NEW DELHI - FATEHPURI", 350000.0)
    t.mode = "cash-deposit"
    assert categorize([t])[0].category == "cash deposit"


def test_interest_payments_detail_reads_as_itself():
    t = categorize([txn("DEBIT INTEREST- /", -1000.0)])[0]
    assert category_detail(t) == "Interest payments"


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


@pytest.mark.parametrize("desc", ["SMS Chrgs Incl GST", "Keeping Chgs-- / 38976288",
                                  "BNA Txn Chrgs Incl GST", "Dr Card Charges GST ANNUAL"])
def test_service_charges_are_not_penal(desc):
    """The master defines penal as a threshold/violation charge (MAB, POS); an
    ordinary service fee — SMS, card, transaction, folio — is NOT penal (ID8),
    so it falls through to Regular debit rather than being flagged penal."""
    assert categorize([txn(desc, -649.0)])[0].category != "other penal charges"


@pytest.mark.parametrize("desc", [
    "BAN/528212361969/ICI8e968/ UPI/Google Ind/gpayrecharge@i/UPI/ICICI",
    "M/603142889032/ICIf7fbe/ UPI/Google Ind/gpayrecharge@i/UPI/AXIS",
])
def test_a_gpay_recharge_is_not_a_penal_charge(desc):
    """"reCHARGE" inside a gpayrecharge VPA was matching the penal CHARGES
    rule, tagging every recharge as a penal charge."""
    t = categorize([txn(desc, -100.0, mode="upi")])[0]
    assert t.category != "other penal charges"


def test_a_genuine_mab_charge_is_penal():
    """A minimum/average-balance charge is the canonical penal charge (master:
    "pos threshold, MAB, etc."), and must still be caught."""
    t = categorize([txn("AMB Chgs Incl GST 01-06-2025", -354.0)])[0]
    assert t.category == "other penal charges"


def test_a_reversed_returned_instrument_credit_is_a_refund():
    t = categorize([txn("RVSL IW CTR RTN CHQNO:011541_DT:01122025/FEDERAL",
                        2900000.0)])[0]
    assert t.category == "return / refund"


def test_a_returned_outgoing_rtgs_credit_is_a_refund():
    t = categorize([txn("RTGS RETURN-ICICR42026011900518516-S N S PRODUCTSPVT "
                        "LTD-OPERATIONS SUSPENDED/R09", 772905.0)])[0]
    assert t.category == "return / refund"


def test_an_ecs_debit_to_a_known_lender_is_an_emi():
    """ID5: the Bajaj Finance ECS debit must read as EMI, not generic ECS,
    even when a single statement cannot see it recur."""
    from bsa.normalize import extract_counterparty
    d = "ECS/UTIBDE11165163202409/Bajaj Finance Ltd_SMS OT"
    t = txn(d, -128182.0, mode="nach")
    t.counterparty = extract_counterparty(d, "nach")
    out = categorize([t])[0]
    assert out.category == "EMI transaction"
    assert out.counterparty == "Bajaj Finance Ltd"   # _SMS OT suffix stripped
