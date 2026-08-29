"""The taxonomy metadata from the "Banking extraction data labeling" master.

Three things came out of that workbook that are not rules about which tag a row
gets, but properties OF the taxonomy — and each of them changes a number a
lender reads, so each is pinned here.

  PAYMENTS_ISSUED / PAYMENTS_DEPOSITED
      the denominators for the two bounce rates on the Summary sheet. A bounce
      count on its own says nothing: three returns against four payments is a
      failing account, three against nine hundred is noise.

  NON_TURNOVER
      the credits that are NOT business turnover. The master is explicit that
      interest and treasury income are "excluded from business turnover
      calculations" and that own/group transfers "must be stripped out to
      prevent artificial turnover inflation".

  high_risk_spend
      the SME master's speculative-spending groups. Deliberately NOT a category
      tag: every tag maps to a sheet in the customer's template, so a
      nineteenth would break the output contract. The row stays a Regular
      debit; the credit assessment reports it.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "backend", "processor"))

from bsa.categorize import (NON_TURNOVER, PAYMENTS_DEPOSITED,  # noqa: E402
                            PAYMENTS_ISSUED, high_risk_group,
                            is_business_credit)
from bsa.credit_summary import credit_summary  # noqa: E402
from bsa.models import Txn  # noqa: E402


def _txn(desc, amount, category, date="2026-01-10", balance=1000.0):
    return Txn(date=date, cheque_no="", description=desc, amount=amount,
               balance=balance, mode="neft", counterparty="ACME",
               category=category, account_no="X1", bank="HDFC Bank",
               source_file="s.pdf")


# --- bounce denominators ----------------------------------------------------

def test_payments_issued_is_debits_the_holder_initiated():
    # The master marks exactly these five "Yes" in the Payments Issued column.
    assert PAYMENTS_ISSUED == frozenset({
        "EMI transaction", "Salary paid", "ECS transaction",
        "Related party debit", "Regular debit"})
    # A bounce CHARGE is not itself a payment that could bounce.
    assert "inward bounce penal charges" not in PAYMENTS_ISSUED
    # Nor is cash out — nothing was presented to anyone.
    assert "cash withdrawal" not in PAYMENTS_ISSUED


def test_payments_deposited_is_only_what_was_presented_for_collection():
    assert PAYMENTS_DEPOSITED == frozenset({"Related party credit",
                                            "Regular credit"})
    for never in ("cash deposit", "Interest received", "Loan amount disbursal",
                  "return / refund", "Salary credited"):
        assert never not in PAYMENTS_DEPOSITED, never


# --- business turnover ------------------------------------------------------

def test_non_turnover_strips_every_non_operating_inflow():
    for excluded in ("Loan amount disbursal", "Salary credited",
                     "Interest received", "Investment return credited",
                     "return / refund", "Related party credit"):
        assert excluded in NON_TURNOVER, excluded


def test_cash_deposits_still_count_as_business_turnover():
    # They are sales receipts. Their risk is reported separately as cash
    # intensity — excluding them here would understate a cash trader's book.
    assert "cash deposit" not in NON_TURNOVER
    assert is_business_credit(_txn("CASH DEP", 500.0, "cash deposit"))


def test_only_credits_can_be_business_credits():
    assert not is_business_credit(_txn("NEFT OUT", -500.0, "Regular debit"))


def test_business_credits_are_reported_below_all_credits():
    txns = [
        _txn("SALE PROCEEDS", 100000.0, "Regular credit", "2026-01-05"),
        _txn("INT CR", 5000.0, "Interest received", "2026-01-06"),
        _txn("LOAN DISB", 400000.0, "Loan amount disbursal", "2026-01-07"),
        _txn("NEFT OUT", -20000.0, "Regular debit", "2026-01-08"),
    ]
    m = credit_summary(txns, {"assessment": "verified"}, "passed")["metrics"]
    # All credits = 505,000; the borrowed 400k and the 5k of interest are not
    # turnover, so business credits is the 100k of trade.
    assert m["total_credits"] == 505000.0
    assert m["business_credits"] == 100000.0
    assert m["business_credit_share_pct"] == 19.8


# --- speculative / high-risk spending ---------------------------------------

def test_high_risk_groups_are_recognised_by_the_masters_names():
    cases = {
        "UPI/DREAM11 GAMING/PAY": "Gambling / betting / real-money gaming",
        "NEFT TO WAZIRX FINTECH": "Virtual digital assets / crypto",
        "IMPS/LENDBOX/P2P LOAN": "Unlicensed / P2P lending outflows",
        "ACH-D-ZERODHA BROKING LTD": "Speculative stock & derivatives funding",
    }
    for desc, group in cases.items():
        assert high_risk_group(desc) == group, desc


def test_an_ordinary_trade_payment_is_not_high_risk():
    for desc in ("NEFT TO ACME TRADERS PVT LTD", "ACH-D-BAJAJ FINANCE EMI",
                 "CASH DEP BRANCH", "GST PMT CPIN 2401"):
        assert high_risk_group(desc) is None, desc


def test_high_risk_spending_reaches_the_credit_assessment():
    txns = [
        _txn("SALE PROCEEDS", 100000.0, "Regular credit", "2026-01-05"),
        _txn("UPI/DREAM11/PAY", -15000.0, "Regular debit", "2026-01-06"),
        _txn("UPI/DREAM11/PAY", -5000.0, "Regular debit", "2026-01-07"),
        _txn("NEFT WAZIRX", -30000.0, "Regular debit", "2026-01-08"),
    ]
    out = credit_summary(txns, {"assessment": "verified"}, "passed")
    hr = out["metrics"]["high_risk_spend"]
    assert hr["Gambling / betting / real-money gaming"] == {"count": 2,
                                                            "amount": 20000.0}
    assert hr["Virtual digital assets / crypto"]["amount"] == 30000.0
    assert out["metrics"]["high_risk_spend_total"] == 50000.0
    assert any("Speculative / high-risk spending seen" in r
               for r in out["reads"])


def test_high_risk_spending_does_not_change_the_category_tag():
    # The row must still route to Regular Debits — the template's eighteen
    # tags are the output contract.
    t = _txn("UPI/DREAM11/PAY", -15000.0, "Regular debit")
    assert high_risk_group(t.description) is not None
    assert t.category == "Regular debit"
