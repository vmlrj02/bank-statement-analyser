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


def _one(desc, amount):
    from bsa.categorize import categorize
    from bsa.models import Txn
    from bsa.normalize import detect_mode, extract_counterparty
    m = detect_mode(desc)
    t = Txn(date="2025-07-01", cheque_no="", description=desc, amount=amount,
            balance=0.0, mode=m, counterparty=extract_counterparty(desc, m))
    t.compute_uid("1", 0)
    return categorize([t])[0]


def test_confidence_high_for_a_rule_match():
    # A lender EMI is a definite category -> high confidence.
    assert _one("ECS/UTIBDE11165163202409/Bajaj Finance Ltd_SMS OT", -128182.0).confidence == "high"
    # A penal charge, a cash deposit -> high.
    assert _one("AMB Chgs Incl GST 01-06-2025", -354.0).confidence == "high"
    assert _one("CAM/77571SRY/CASHDEP-Other/11-02-26/9931", 48500.0).confidence == "high"


def test_confidence_medium_for_a_known_party_regular_transfer():
    # A plain transfer we could not specially categorise, but the party is known.
    t = _one("UPI/P2A/557305326847/K S SHALI/YES BANK /UPI/", 2.0)
    assert t.category == "Regular credit" and t.counterparty == "K S SHALI"
    assert t.confidence == "medium"


def test_confidence_low_when_neither_category_nor_party_is_known():
    # A settlement/merchant ref with no name and no special category -> review.
    t = _one("UPISETTLEMENT-549564-07/05/25 000000000000000", 500.0)
    assert t.category == "Regular credit" and not (t.counterparty and t.counterparty != "unknown party")
    assert t.confidence == "low"


def _rec(date, amount, party, desc="IMPS/123/x", uid=""):
    t = Txn(date=date, cheque_no="", description=desc, amount=amount,
            balance=0.0, mode="imps", counterparty=party)
    t.uid = uid or f"{date}|{party}|{amount}"
    return t


def test_monthly_recurring_same_amount_is_emi():
    """The reviewer's rule: a debit of the same amount to the same party in
    three or more distinct months is an EMI, whatever the channel — seen with
    Mahindra Finance paid by IMPS, no lender keyword in the narration."""
    rows = [_rec(f"2026-0{m}-05", -21990.0, "MAHINDRA FIN SERVICES")
            for m in (1, 2, 3)]
    rows.append(_rec("2026-03-09", -777.0, "SOMEONE ELSE"))
    categorize(rows)
    assert [t.category for t in rows[:3]] == ["EMI transaction"] * 3
    assert all(t.category_source == "recurrence-cadence" for t in rows[:3])
    assert all(t.confidence == "medium" for t in rows[:3])
    assert rows[3].category == "Regular debit"


def test_recurrence_guards_hold():
    # a tiny recurring charge is not an EMI
    small = [_rec(f"2026-0{m}-01", -5.9, "SMS ALERTS") for m in (1, 2, 3)]
    # the same amount MANY times a month is trading volume, not an instalment
    busy = [_rec(f"2026-01-{d:02d}", -25000.0, "STEEL TRADER", uid=f"b{d}")
            for d in range(1, 9)] + \
           [_rec(f"2026-0{m}-01", -25000.0, "STEEL TRADER", uid=f"m{m}")
            for m in (2, 3)]
    # no counterparty -> unrelated debits must not collapse into one group
    anon = [_rec(f"2026-0{m}-02", -9000.0, "") for m in (1, 2, 3)]
    rows = small + busy + anon
    categorize(rows)
    assert all(t.category != "EMI transaction" for t in rows)


def test_recurrence_never_overrides_an_explicit_rule():
    rows = [_rec(f"2026-0{m}-04", -1500.0, "SOME SHOP",
                 desc="BY CASH DEPOSIT MACHINE") for m in (1, 2, 3)]
    for t in rows:
        t.amount = 1500.0            # cash deposits, rule-tagged credits
    categorize(rows)
    assert all(t.category == "cash deposit" for t in rows)
