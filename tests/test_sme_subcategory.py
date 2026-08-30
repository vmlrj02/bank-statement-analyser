"""The SME sub-category — column B of the master's "Category (SME)" tab.

The customer asked for the categorisation to look like this list: not eighteen
broad tags but the ~33 sub-categories an underwriter reasons about. Telling a
lender that 118 rows are "Regular debit" says nothing; splitting them into
payroll, GST, suppliers and utilities is the point.

The sub-category is a SECOND label over the ABCL tag, never a replacement — the
master's own sheet-mapping column has several sub-categories rolling up to one
sheet, so the tag still decides where a row is written and the output
template's contract is untouched.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "backend", "processor"))

from bsa.models import Txn  # noqa: E402
from bsa.sme_taxonomy import (group_of, sme_subcategory,  # noqa: E402
                              subcategories)


def _txn(desc, amount, category, party=""):
    return Txn(date="2026-01-01", cheque_no="", description=desc,
               amount=amount, balance=0.0, mode="neft", counterparty=party,
               category=category, account_no="X", bank="B",
               source_file="s.pdf")


# (narration, amount, ABCL tag) -> expected sub-category
CASES = [
    # Statutory & Compliance — the split that makes a "Regular debit" useful.
    ("ACH-D/ GST-PMT/CPIN123456", -50000, "Regular debit", "GST Payments"),
    ("OLTAS CHALLAN 281 TDS Q3", -18000, "Regular debit",
     "Direct Tax (TDS / Advance Tax)"),
    ("EPFO ECR CHALLAN PF", -22000, "Regular debit",
     "Labour Welfare (PF / ESIC)"),
    # Operating expenses
    ("NEFT-CMS SAL JAN WAGES", -450000, "Salary paid", "Payroll & Wages"),
    ("BESCOM ELECTRICITY BBPS", -9400, "Regular debit",
     "Commercial Rent & Utilities"),
    # Operational revenue
    ("RAZORPAY SETTLEMENT MRL", 128000, "Regular credit",
     "POS / Merchant / QR Settlements"),
    ("CLG/ CTS INWARD CLEARING 8891", 250000, "Regular credit",
     "Cheque Clearances (Inward)"),
    # Capital & financing
    ("CAPITAL CONTRIB BY DIRECTOR", 500000, "Regular credit",
     "Promoter / Equity Infusion"),
    ("OD DRAWDOWN CASH CREDIT SWEEP", 300000, "Regular credit",
     "OD / CC Drawdowns"),
    ("GST REFUND CBIC", 42000, "return / refund", "Tax & Duty Refunds"),
    # Debt obligations
    ("ACH-D BAJAJ FINANCE EMI", -25000, "EMI transaction", "Loan EMIs"),
    ("CRED CC BILL PAYMENT", -31000, "Regular debit", "Credit Card Payments"),
    ("INT DR OD ACCOUNT", -12000, "Interest / fee payments",
     "OD / CC Interest & Renewal Fees"),
    # Banking friction — a bank penalty and a statutory one are different reads.
    ("MIN BAL NON-MAINT CHG", -236, "other penal charges",
     "Bank Penalties & Non-Maintenance"),
    ("GST LATE FEE PENALTY", -1200, "other penal charges",
     "Statutory Penalty / Late Fees"),
]


def test_narration_patterns_pick_the_masters_subcategory():
    for desc, amt, tag, want in CASES:
        got = sme_subcategory(_txn(desc, amt, tag))
        assert got == want, f"{desc!r} -> {got!r}, wanted {want!r}"


def test_high_risk_spending_outranks_a_generic_word():
    """"WAZIRX CRYPTO PURCHASE" must read as a crypto exchange. "PURCHASE" is
    the LONGER match (against Supplier / Vendor Settlements), so longest-wins
    alone gets this wrong — the risk groups carry an explicit priority."""
    assert sme_subcategory(_txn("WAZIRX CRYPTO PURCHASE", -25000,
                                "Regular debit")) == \
        "Virtual Digital Assets / Crypto Exchanges"
    assert sme_subcategory(_txn("ZERODHA BROKING F&O FUNDING", -100000,
                                "Regular debit")) == \
        "Speculative Stock & Derivatives Funding"
    assert sme_subcategory(_txn("DREAM11 GAMES24X7", -5000,
                                "Regular debit")) == \
        "Gambling / Betting / Real-Money Gaming"


def test_a_short_pattern_must_match_a_whole_token():
    """Squashing punctuation is what makes "ACH-D/ GST-PMT" findable, but it
    buries short tokens inside longer words: "F&O" squashes to "FO", which sits
    inside "EPFO". A PF challan is not derivatives funding."""
    assert sme_subcategory(_txn("EPFO ECR CHALLAN PF", -22000,
                                "Regular debit")) == \
        "Labour Welfare (PF / ESIC)"
    # "RENT" must not fire on "CURRENT", nor "LIC" on "POLICE".
    assert sme_subcategory(_txn("CURRENT ACCOUNT TRANSFER", -1000,
                                "Regular debit")) != \
        "Commercial Rent & Utilities"
    assert sme_subcategory(_txn("POLICE DEPT CHALLAN", -500,
                                "Regular debit")) != "Investments Outflows"


def test_the_tag_alone_decides_when_nothing_matches():
    """For most tags the fallback is definitional, not a guess."""
    assert sme_subcategory(_txn("XYZ", 5000, "cash deposit")) == \
        "Direct Cash Deposits"
    assert sme_subcategory(_txn("XYZ", -5000, "cash withdrawal")) == \
        "Cash Withdrawals"
    assert sme_subcategory(_txn("XYZ", -500, "inward bounce penal charges")) == \
        "Inward Cheque / Mandate Bounces"
    # An unknown tag yields nothing rather than an invented label.
    assert sme_subcategory(_txn("XYZ", -500, "not a real tag")) == ""


def test_a_subcategory_cannot_cross_the_credit_debit_line():
    """GST Payments is a debit line; a credit must never be labelled with it,
    however the narration reads."""
    assert sme_subcategory(_txn("GST PMT CPIN", 50000,
                                "Regular credit")) != "GST Payments"


def test_every_subcategory_declares_a_group_from_the_masters_column_a():
    subs = subcategories()
    assert len(subs) == 35        # the master's 33, plus Misc. credit and Misc. debit
    for s in subs:
        assert s["group"], s["name"]
        assert group_of(s["name"]) == s["group"]
    # The master's two blocks: credit sub-categories and debit sub-categories.
    assert {s["side"] for s in subs} == {"credit", "debit"}


# --- the two "Misc." lines, which the master identifies by SIZE -------------
# "Credits such 1 for account verification; credits less than 10" and
# "test transactions; deducting a token 1 or 2 while saving a card in a
# merchant PG; debits less than 10". There is no narration to match on — the
# payer is a real company and the words read like any other payment — so the
# amount IS the rule.

def test_a_penny_drop_credit_is_a_misc_credit_not_trade_income():
    """A real case: UBER INDIA SYSTEMS and OpenAI LLC each crediting 1.00 were
    landing in the trade line, which inflates the count of genuine receipts."""
    assert sme_subcategory(
        _txn("UPI/P2M/530718094408/UBER INDIA SYSTEMS PRIVATE L/", 1.00,
             "Regular credit")) == "Misc. credit"


def test_a_token_card_debit_is_a_misc_debit():
    assert sme_subcategory(
        _txn("UPI/P2M/1234/MERCHANT PG CARD SAVE/", -2.00,
             "Regular debit")) == "Misc. debit"


def test_the_ceiling_is_exclusive():
    """AT the ceiling a row stays where it was; only BELOW it is "misc".

    The number itself is expected to move — it began at ₹10 and the founder
    raised it to ₹50 once he remembered an airport lounge takes ₹25 as a
    refundable deduction — so this pins the boundary BEHAVIOUR, and the value
    lives in data where he can change it without a code edit."""
    assert sme_subcategory(_txn("UPI/P2A/395106793615/HEMALATHA/UTIB/", 50.00,
                                "Regular credit")) == "Business income"
    assert sme_subcategory(_txn("UPI/P2A/395106793615/HEMALATHA/UTIB/", 49.99,
                                "Regular credit")) == "Misc. credit"


def test_the_lounge_deduction_that_prompted_the_higher_ceiling():
    """₹25, which the old ₹10 ceiling missed."""
    assert sme_subcategory(_txn("POS/AIRPORT LOUNGE ACCESS/", -25.00,
                                "Regular debit")) == "Misc. debit"


def test_an_ordinary_payment_is_untouched_by_the_ceiling():
    assert sme_subcategory(_txn("NEFT INVOICE 8891", 250000.0,
                                "Regular credit")) == "Business income"
