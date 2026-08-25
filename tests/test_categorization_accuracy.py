"""Categorisation accuracy harness — the ground-truth set that was missing.

Every case here is a real narration the reviewer labelled (from the master
taxonomy and review docs ID1-ID8), paired with the category and — where the
review called it out — the party it must resolve to. The test runs each through
the real pipeline (detect_mode -> extract_counterparty -> categorize), so it
measures the whole chain end to end, and prints a per-category scoreboard.

This is how we STOP guessing: a change either moves the number up or it doesn't,
and a regression in one category is visible immediately. Add a row here for every
new rule the reviewer gives, and (when they share a labelled CSV) point
BSA_CATEGORY_TRUTH at it to fold real statements in.
"""
import collections
import csv
import os

import pytest

from bsa.categorize import categorize
from bsa.models import Txn
from bsa.normalize import detect_mode, extract_counterparty


def _categorize_one(desc, amount):
    mode = detect_mode(desc)
    t = Txn(date="2025-07-01", cheque_no="", description=desc, amount=amount,
            balance=0.0, mode=mode, counterparty=extract_counterparty(desc, mode))
    t.compute_uid("1", 0)
    return categorize([t])[0]


# (description, amount, expected_category, expected_party_or_None)
# amount sign encodes debit/credit; party is checked only when given.
CASES = [
    # --- Interest: sign decides received vs payments (ID6/ID8) ---
    ("SB/925010000665679:Int.Pd:03-01-2025 to 31-", -7000.0, "Interest payments", None),
    ("SB/925010000665679:Int.Pd:01-04-2025 to 30-", -400.0, "Interest payments", None),
    ("Int.Pd on Savings", 120.0, "Interest received", None),
    ("DEBIT INTEREST- /", -456845.0, "Interest payments", None),
    ("CREDIT INTEREST", 500.0, "Interest received", None),

    # --- NBFC / lender names (ID8: EMI or Interest payments on debit; disbursal on credit) ---
    ("BIL/BPAY/00000018WXN7/BBPS/KinaraCapital/WC", -71007.0, "Interest payments", "Kinara Capital"),
    ("ACH/CLIXCAPITALSERVICE/ICIC0000000016310674/TXNR", -36938.0, "EMI transaction", None),
    ("ECS/UTIBDE11165163202409/Bajaj Finance Ltd_SMS OT", -128182.0, "EMI transaction", "Bajaj Finance Ltd"),
    ("INDIA/INBSGROYAL CAPITAL PRIVATE L UPI/P2A/847055627294/Bank Account", 943725.0, "Loan amount disbursal", "Royal Capital"),
    ("NEFT-HDFCN123-Aditya Birla Finance-", 500000.0, "Loan amount disbursal", None),

    # --- Charges: MAB/avg-bal penal; card/txn NOT penal (ID4/ID8) ---
    ("Avg bal Chgs Incl GST OCT-25 SB/925010000665679:Int.Pd:01-10-2025 to 31-", -504.84, "other penal charges", None),
    ("AMB Chgs Incl GST 01-06-2025", -354.0, "other penal charges", None),
    ("MIN BAL CHARGES", -300.0, "other penal charges", None),
    # NOT penal: a POS purchase, and a name that merely contains "AMB".
    ("POS/MD ENTERPRISES/BANGALORE/311025/20:35/73 1111", -1500.0, "Regular debit", None),
    ("UPI/P2A/567161905063/BODIDHAMMA JAMBAGA /UPI/State Bank Of I", -500.0, "Regular debit", None),
    ("BNA Txn Chrgs Incl GST UPI/P2A/848101872379/ASHISH GU/AXIS", -59.0, "Regular debit", None),
    ("/Paymen/AXIS BANK Dr Card Charges GST ANNUAL", -2000.0, "Regular debit", None),
    ("Dr Card Charges GST ISSUE", -14999.0, "Regular debit", None),

    # --- Cash deposit variants (ID8/ID9: CASHDEP glued; ATM-CASH credit) ---
    ("CAM/77571SRY/CASHDEP-Other/11-02-26/9931", 48500.0, "cash deposit", None),
    ("BY CASH -NEW DELHI - FATEHPURI", 350000.0, "cash deposit", None),
    ("B/Payment/ ATM-CASH-", 18000.0, "cash deposit", None),
    ("OF I/Payment/ ATM-CASH", 25000.0, "cash deposit", None),
    # ...but an ATM-CASH DEBIT is still a withdrawal, not a deposit.
    ("ATM-CASH- AXIS/DPRH515001/5685/250526/BANGALORE", -10000.0, "cash withdrawal", None),

    # --- Party names the reviewer corrected (ID9) ---
    ("Ban/Payment/ UPI/P2M/580669431573/M/S.VINAY", 40000.0, "Regular credit", "VINAY"),
    ("TRF/GEETA/TRANSFER IMPS/MRT/526827147422/9186901641146/91869", 60000.0, "Regular credit", "GEETA"),

    # --- Recharge is not penal (ID4) ---
    ("BAN/528212361969/ICI8e968/ UPI/Google Ind/gpayrecharge@i/UPI/ICICI", -100.0, "Regular debit", None),

    # --- Bounce / return (unchanged expectations) ---
    ("RTGS RETURN-ICICR42026011900518516-S N S PRODUCTSPVT LTD-OPERATIONS SUSPENDED", 772905.0, "return / refund", None),
    ("RVSL IW CTR RTN CHQNO:011541", 2900000.0, "return / refund", None),
    ("Chq Rtrn Chrgs Incl GST", -590.0, "Outward Bounced Xns", None),

    # --- ATM / cash withdrawal ---
    ("ATM-CASH/+SARJAPUR ROAD BR/BANGALORE-URB/010226", -10000.0, "cash withdrawal", None),

    # --- Salary ---
    ("NEFT SALARY JULY payroll", 55000.0, "Salary credited", None),

    # --- Plain transfers stay Regular ---
    ("UPI/P2A/557305326847/K S SHALI/YES BANK /UPI/", 2.0, "Regular credit", "K S SHALI"),
    ("NEFT/HDFCH00395013738/RHEA HEALTHCARE PVT LTD/HDFC BANK/0001", 150000.0, "Regular credit", "RHEA HEALTHCARE PVT LTD"),
]


def _run_truth_file(path):
    """Optional real ground truth: a CSV with Description, Amount, Category
    (and optionally Party) columns of reviewer-labelled rows."""
    out = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            desc = r.get("Description") or r.get("description")
            amt = r.get("Amount") or r.get("amount")
            cat = r.get("Category") or r.get("category")
            if not (desc and amt and cat):
                continue
            try:
                a = float(str(amt).replace(",", "").replace("(", "-").replace(")", ""))
            except ValueError:
                continue
            out.append((desc, a, cat.strip(), (r.get("Party") or "").strip() or None))
    return out


def _all_cases():
    cases = list(CASES)
    truth = os.environ.get("BSA_CATEGORY_TRUTH")
    if truth and os.path.exists(truth):
        cases += _run_truth_file(truth)
    return cases


def test_categorization_accuracy(capsys):
    cases = _all_cases()
    by_cat = collections.Counter()
    hits = collections.Counter()
    party_checked = party_hits = 0
    failures = []
    for desc, amt, want_cat, want_party in cases:
        t = _categorize_one(desc, amt)
        by_cat[want_cat] += 1
        ok = t.category == want_cat
        hits[want_cat] += int(ok)
        if not ok:
            failures.append(f"  cat  want={want_cat!r:22s} got={t.category!r:22s} | {desc[:52]}")
        if want_party is not None:
            party_checked += 1
            if t.counterparty == want_party:
                party_hits += 1
            else:
                failures.append(f"  party want={want_party!r:20s} got={t.counterparty!r:20s} | {desc[:46]}")

    total = sum(by_cat.values())
    correct = sum(hits.values())
    with capsys.disabled():
        print(f"\n=== Categorisation accuracy: {correct}/{total} "
              f"({100*correct/total:.0f}%)  party {party_hits}/{party_checked} ===")
        for cat in sorted(by_cat):
            print(f"   {hits[cat]:2d}/{by_cat[cat]:<2d}  {cat}")
        if failures:
            print("--- misses ---")
            print("\n".join(failures))

    # The bar the harness enforces. Raise it as coverage improves; today every
    # labelled case must pass, so a regression fails the build.
    assert correct == total and party_hits == party_checked, \
        f"{total-correct} category + {party_checked-party_hits} party misses (see scoreboard above)"


# ID7 (SBI v2): merchant after a 4-letter bank code, else the account number.
ID7_PARTY = [
    ("TRANSFER- TRANSFER 4897696162090 L/UTIB/swiggyinst/UPI-", -696.0, "Regular debit", "swiggyinst"),
    ("TRANSFER- TRANSFER 4897696162090 /HDFC/grofersind/Payvi-", -433.0, "Regular debit", "grofersind"),
    ("TRANSFER 4897691162095 I/RATN/amazon@rap/You a-", -222.0, "Regular debit", "amazon"),
    ("TRANSFER- TRANSFER 4897695162091", -712.0, "Regular debit", "4897695162091"),
]
CASES += ID7_PARTY


# HDFC UPI hyphen form: "UPI-<NAME>-<phone>@<vpa>" / "UPI-<NAME> <ref>" (party flag).
HDFC_UPI_PARTY = [
    ("UPI-ASHOK GARG-9811361461@AXL-UTIB00019 0000460950652103", -500.0, "Regular debit", "ASHOK GARG"),
    ("UPI-MUSHARRAF 0000737849845725 0999800-737849845725-PAYM", 500.0, "Regular credit", "MUSHARRAF"),
    ("UPISETTLEMENT-549564-07/05/25 000000000000000", 500.0, "Regular credit", None),
]
CASES += HDFC_UPI_PARTY


# Cross-bank transfer party formats found by the party-detection audit.
AUDIT_PARTY = [
    ("YES0N6001560075400 NEFT Cr-ICIC0SF0002-VIVISH TECHNOLOGIES PRIVA-TIMEZ", 5000.0, "Regular credit", "VIVISH TECHNOLOGIES PRIVA"),
    ("NEFTCR-CBIN0280410-SHRIMORVINANDANAND CO.-QUALITYEARTHMINERALSPVTLTD-C", 5000.0, "Regular credit", "SHRIMORVINANDANAND CO."),
    ("RTGS DR-UTIB0000129-SHRI LAKSHMI STEELSUPPLIERS-HUBLIKARNATAKA-ESFBR62", -5000.0, "Regular debit", "SHRI LAKSHMI STEELSUPPLIERS"),
    ("IBFUNDSTRANSFERDR-50200010007644 -QU ALITYEARTHMINERALSPVTLTD", -5000.0, "Regular debit", "QU ALITYEARTHMINERALSPVTLTD"),
    ("UPI-BLINKIT-BLINKIT.PAYU@HDFCBANK-HDFC0M 0000121212507269", -300.0, "Regular debit", "BLINKIT"),
    ("IMPS/P2A/516622239796/RAJEEVIN/AUSMAL LF/3/9186014504729765000", -5000.0, "Regular debit", "RAJEEVIN"),
    ("IMPS-530221623959-SAVITRIANDSONSENT-IDFB-XXXXXXX3137-IMPSTXN", -5000.0, "Regular debit", "SAVITRIANDSONSENT"),
]
CASES += AUDIT_PARTY


# NACH-mandate / TPT / ref-first NEFT party formats (second audit round).
AUDIT_PARTY_2 = [
    ("ACHD-L&TFINANCELIMITED-BL2501258261493", -12000.0, "EMI transaction", "L&TFINANCELIMITED"),
    ("ACH-DR-Indian Overseas Bank- 20250414000000000380-", -5000.0, "Regular debit", "Indian Overseas Bank"),
    ("59209813398001-TPT-MATERIALPAYMENT-SKY DREAMINFRA", -50000.0, "Regular debit", "SKY DREAMINFRA"),
    ("NEFT DR-ESFBN52026031602720350-SHRI LAKSHMI STEEL S-UTIB0000129-HEADOF", -50000.0, "Regular debit", "SHRI LAKSHMI STEEL S"),
]
CASES += AUDIT_PARTY_2
