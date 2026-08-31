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
    renamed this category "Interest / fee payments" (ID8)."""
    t = categorize([txn("DEBIT INTEREST- /", -456845.00)])[0]
    assert t.category == "Interest / fee payments"


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
    assert category_detail(t) == "Interest / fee payments"


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


def test_cadence_guards_from_the_precision_review():
    """A review of 30 real recurrence-cadence hit-groups found ~1 plausible EMI
    among wages, rent, supplier payments and personal transfers, so the
    heuristic now also demands a mandate-shaped pattern: a NON-ROUND amount,
    CONSECUTIVE months, a STABLE day of month, and no wages/rent wording."""
    # round monthly 20,000 to an individual — rent-shaped, not an EMI
    rent = [_rec(f"2026-0{m}-05", -20000.0, "UMA THIA") for m in (1, 2, 3)]
    # EMI-shaped amount but a skipped month — no mandate pulls like that
    gappy = [_rec(d, -21990.0, "SOME TRADER")
             for d in ("2026-01-05", "2026-02-05", "2026-04-05")]
    # consecutive months but a wandering day — ad-hoc transfers
    wander = [_rec(d, -14322.0, "WHOEVER")
              for d in ("2026-01-03", "2026-02-18", "2026-03-27")]
    # perfect cadence at an odd amount, but the narration says rent
    rentw = [_rec(f"2026-0{m}-05", -20999.0, "UMA THIA",
                  desc="IMPS/609210958047/Uma Thia/Rent 0098292162098")
             for m in (1, 2, 3)]
    # perfect cadence but the "party" is a bank name — extraction junk
    banky = [_rec(f"2026-0{m}-04", -10999.0, "KOTAK MAHINDRA BANK LIMITED")
             for m in (1, 2, 3)]
    rows = rent + gappy + wander + rentw + banky
    categorize(rows)
    assert all(t.category != "EMI transaction" for t in rows)


def test_underscore_glued_emi_word_is_an_emi():
    """An underscore is a \\w character, so \\bEMI\\b never fired on the
    collection print "UCR…_EMI_05/11/2025"."""
    t = categorize([txn("UCR013913427589_EMI_05/11/2025_PI RAMAL PETROLEUM P",
                        -106235.0)])[0]
    assert t.category == "EMI transaction"


def test_piramal_petroleum_is_not_the_lender():
    """The bare "Piramal" lender key turned a fuel trader's whole statement
    into EMIs and disbursals; only the lending entities may match."""
    t = categorize([txn("INB/IFT/PIRAMAL PETROLEUM P LTD/TPARTY TRANSFER",
                        -2500000.0)])[0]
    assert t.category == "Regular debit"
    t2 = categorize([txn("TRF/PIRAMAL PETROLEUM PRIVATE LIMITED/TRANSFER",
                         2000000.0)])[0]
    assert t2.category == "Regular credit"
    # ...while the genuine NACH pull still reads as the lender's EMI.
    t3 = categorize([txn("ACHD-PIRAMALFINANCELIMI-HLSA000CEA16 0000003788749791",
                         -47670.0)])[0]
    assert t3.category == "EMI transaction"


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


def test_a_loan_account_credit_is_a_disbursal_not_trade_income():
    """Reported by the reviewer: "NEFT/KKBK…/Kotak Mahindra Bank Ltd/… Pyt Loan
    A c CSG …" for 16.1 lakh was reading as trade income. Banks must not go in
    the `lenders` list — every NEFT from one would become a disbursal — so the
    loan-ACCOUNT wording is the signal. It matters beyond the label: a disbursal
    is excluded from turnover, and counting borrowed money as capacity to repay
    is exactly the circularity gotcha 18 exists to prevent."""
    from bsa.categorize import categorize, is_business_credit
    from bsa.models import Txn

    t = Txn(date="2026-01-29", cheque_no="",
            description="NEFT/KKBK260293146221/ Kotak Mahindra Bank Ltd/"
                        "KOTAK MAHINDRA BANK /Pyt Loan A c CSG 156067819 dt",
            amount=1614019.0, balance=0.0, mode="neft", counterparty="")
    categorize([t])
    assert t.category == "Loan amount disbursal"
    assert is_business_credit(t) is False


def test_a_plain_trade_credit_is_not_swept_up_by_the_loan_rule():
    from bsa.categorize import categorize
    from bsa.models import Txn

    t = Txn(date="2026-01-29", cheque_no="",
            description="NEFT/ACME STEELS/INVOICE 8891", amount=250000.0,
            balance=0.0, mode="neft", counterparty="ACME STEELS")
    categorize([t])
    assert t.category == "Regular credit"


def test_a_credit_that_says_loan_is_not_business_income():
    """The reviewer asked of "BY TRANSFER-INB loan- … TRANSFER FROM …": "might
    be a loan right?" — and it was. Sixteen rows across the corpus carry a bare
    "loan" in a credit narration with no loan-account number and no lender name
    to key on, ₹47 lakh of it including a ₹40 lakh hand loan, and every one was
    being counted as business income. Turnover is business credits (gotcha 18),
    so a borrowing counted as trading receipts flatters the most leveraged
    borrower by exactly the size of the loan."""
    from bsa.models import Txn
    from bsa.categorize import categorize

    def tag(desc, amount):
        t = Txn(date="2026-01-01", cheque_no="", description=desc, amount=amount,
                balance=0.0, mode="other", counterparty="X", account_no="1")
        categorize([t])
        return t.category

    assert tag("BY TRANSFER-INB loan- CIAAMVCKJ5 TRANSFER FROM 43256010643 "
               "SPAZEOMERCHANDI SE PRIV", 150000.0) == "Loan amount disbursal"
    assert tag("RTGS/IDFBR52025122900440961/Ms Ramyashree S/IDFC FIRST BANK "
               "LTD//ATTN/HAND LOAN", 4000000.0) == "Loan amount disbursal"
    assert tag("MOZASUMULT MMT/IMPS/600316791698/Loan/MOZASUMULT/Uni on Bank "
               "Of I", 10000.0) == "Loan amount disbursal"

    # The exclusions are what make it safe: repayment wording is money going the
    # other way, an EMI or interest remark on a credit is a reversal or rebate,
    # a debit is never a disbursal, and an ordinary trade receipt is untouched.
    assert tag("UPI/CR/123/RAMESH/Loan Repayment received", 50000.0) \
        != "Loan amount disbursal"
    assert tag("NEFT/ABC/Loan EMI refund", 5000.0) != "Loan amount disbursal"
    assert tag("ACH DR BAJAJ FINANCE LOAN EMI", -15000.0) != "Loan amount disbursal"
    assert tag("UPI/CR/123/RAMESH/Payment for steel", 150000.0) == "Regular credit"
    # "LOANS" as part of a longer word must not match ("SLOANE", "LOANEE")
    assert tag("NEFT/XYZ/SLOANE SQUARE TRADING", 90000.0) == "Regular credit"
