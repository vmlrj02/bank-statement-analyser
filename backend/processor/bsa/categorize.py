"""Stage 4b — Categorize, using the SME/MSME lending taxonomy
(from Banking_pdf_extraction.xlsx "Category list").

Category tags:
  EMI transaction | Interest received | Interest debited |
  Investment return credited |
  Loan amount disbursal | Salary paid | Salary credited | ECS transaction |
  cash deposit | cash withdrawal | inward bounce penal charges |
  Outward Bounced Xns | other penal charges | return / refund |
  Related party credit | Related party debit |
  Regular credit | Regular debit

Tiers (cheapest first):
  1. deterministic descriptor rules
  2. recurrence analysis (NACH debits with a monthly cadence at a fixed
     amount are EMIs, not generic ECS)
  3. merchant dictionary (persisted; DynamoDB in production, JSON locally)
  4. LLM batch classification for still-unknown merchants (Bedrock; stubbed
     locally) — resolved names are written back to the dictionary
  Fallback: Regular credit/debit (detail keeps "transfer from/to <party>")
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict

from .models import Txn

# Known lender/NBFC name fragments (seed list; grows via dictionary)
LENDERS = [
    "SMFGINDIACREDIT", "SMFG INDIA CREDIT", "BAJAJ FIN", "HDB FINANC",
    "TATA CAPITAL", "ADITYA BIRLA FIN", "FULLERTON", "CHOLAMANDALAM",
    "SHRIRAM FIN", "MUTHOOT", "MANAPPURAM", "L&T FINANCE", "PIRAMAL",
    "POONAWALLA", "INDIABULLS", "LIC HOUSING", "PNB HOUSING", "CANFIN",
]
INVESTMENT_HINTS = [
    "SHAREKHAN", "ZERODHA", "GROWW", "UPSTOX", "ANGEL", "ICCLR", "MUTUAL FUND",
    "MF REDEMPTION", "NSE ", "BSE ", "CDSL", "NSDL", "RD MATURITY", "FD CLOS",
]
SALARY_HINTS = [r"\bSAL\b", r"SALARY", r"\bSAL-", r"SAL CREDIT", r"PAYROLL"]
REFUND_HINTS = [r"\bREF(UND)?\b", r"\bREV(ERSAL)?\b", r"\bRFND\b", r"RETURN OF", r"\bRET\b.*\bCR\b",
                # "RVSL IW CTR RTN CHQNO:…" — a returned instrument credited back
                r"\bRVSL\b"]
# "Chrgs"/"Chgs" spellings ("SMS Chrgs Incl GST", "Keeping Chgs--") fell
# through to Regular debit; \bCHRG\b alone stops before the S.
PENAL_HINTS = [r"\bCHRG\b", r"\bCHR?GS?\b", r"CHARGES?", r"\bMAB\b", r"PENAL", r"\bFEE\b", r"OVERDUE", r"MIN BAL"]
BOUNCE_INWARD = [r"ECSRTN", r"I/?W.*(RTN|RETURN|BOUNCE)", r"INWARD.*(RTN|RET)", r"CHQ RETURN.*DEP"]
# A bank's "inward clearing" is a cheque drawn ON the account — so the charge
# for its return is the customer's own payment bouncing, an OUTWARD bounce in
# the taxonomy's terms ("Chq Rtrn Chrgs Incl GST" followed cheque 011541's
# return in the Axis sample).
BOUNCE_OUTWARD = [r"O/?W.*(RTN|RETURN|BOUNCE)", r"OUTWARD.*(RTN|RET)", r"CHQ.*BOUNCE.*ISSUED", r"SI FAIL", r"NACH RTN CHG",
                  r"CHQ\s*RTR?N.*CHR?GS?"]

_DICT_PATH = os.path.join(os.path.dirname(__file__), "data", "merchant_dictionary.json")


def _load_dictionary() -> dict:
    if os.path.exists(_DICT_PATH):
        with open(_DICT_PATH) as f:
            return json.load(f)
    return {}


def _save_dictionary(d: dict) -> None:
    os.makedirs(os.path.dirname(_DICT_PATH), exist_ok=True)
    with open(_DICT_PATH, "w") as f:
        json.dump(d, f, indent=1, sort_keys=True)


def _any(desc: str, patterns: list[str]) -> bool:
    return any(re.search(p, desc, re.I) for p in patterns)


def _party(t: Txn) -> str:
    return t.counterparty if t.counterparty else "unknown party"


def _emi_ref(desc: str) -> str:
    m = re.search(r"BIL/((?:Home|Auto|Consumer|Personal|Gold|Two Wheeler)?\s*Loans?\s+\S+)", desc, re.I)
    return m.group(1).strip() if m else ""


def _find_recurring_nach(txns: list[Txn]) -> set[str]:
    """uids of NACH debits that recur at the same amount in >= 3 distinct
    months — treated as EMIs pulled via NACH mandate."""
    groups: dict[tuple, list[Txn]] = defaultdict(list)
    for t in txns:
        if t.mode == "nach" and t.amount < 0:
            groups[(t.counterparty.upper().replace(" ", ""), round(-t.amount, 2))].append(t)
    emis: set[str] = set()
    for (_, _), ts in groups.items():
        months = {t.date[:7] for t in ts}
        if len(months) >= 3:
            emis.update(t.uid for t in ts)
    return emis


def categorize(txns: list[Txn], related_parties: list[str] | None = None,
               use_llm: bool = False) -> list[Txn]:
    related = [re.sub(r"\s+", "", p).upper() for p in (related_parties or [])]
    dictionary = _load_dictionary()
    recurring_nach = _find_recurring_nach(txns)

    for t in txns:
        d, credit = t.description, t.amount > 0
        tag, src = "", "rule"

        # --- Tier 1: deterministic rules ---
        if not credit and re.search(r"BIL/.*(Loan|EMI)", d, re.I):
            tag = "EMI transaction"
        elif not credit and _any(d, BOUNCE_INWARD):
            tag = "inward bounce penal charges"
        elif not credit and _any(d, BOUNCE_OUTWARD):
            tag = "Outward Bounced Xns"
        elif t.mode == "interest" or (credit and re.search(r"Int\.Pd|INTEREST (PAID|CREDIT)", d, re.I)):
            tag = "Interest received"
        elif not credit and re.search(
                r"DEBIT INTEREST|INTEREST (DEBIT|DEBITED|CHARGED|COLLECTED)"
                r"|\bINT\.?\s*(DR|DEBIT|COLL)", d, re.I):
            # OD/CC accounts are charged interest ("DEBIT INTEREST- /" on SBI);
            # a lender reads these as cost of borrowing, not a regular transfer.
            tag = "Interest debited"
        elif t.mode == "atm-cash" and not credit:
            tag = "cash withdrawal"
        elif t.mode == "cash-deposit" and credit:
            tag = "cash deposit"
        elif credit and _any(d, [re.escape(x) for x in LENDERS]) and t.mode in ("neft", "imps", "nach", "other"):
            tag = "Loan amount disbursal"
        elif credit and _any(d, [re.escape(x) for x in INVESTMENT_HINTS]):
            tag = "Investment return credited"
        elif credit and _any(d, SALARY_HINTS):
            tag = "Salary credited"
        elif not credit and _any(d, SALARY_HINTS):
            tag = "Salary paid"
        elif credit and _any(d, REFUND_HINTS):
            tag = "return / refund"
        elif not credit and t.mode in ("ecs-return",):
            tag = "inward bounce penal charges"
        elif not credit and t.mode == "nach":
            # --- Tier 2: recurrence — monthly fixed-amount NACH = EMI ---
            if t.uid in recurring_nach:
                tag, src = "EMI transaction", "recurrence"
            else:
                tag = "ECS transaction"
        elif not credit and _any(d, PENAL_HINTS) and abs(t.amount) < 5000:
            tag = "other penal charges"

        # --- Tier 3: merchant dictionary ---
        if not tag:
            key = t.counterparty.upper().replace(" ", "")
            if key and key in dictionary:
                tag, src = dictionary[key].get("credit" if credit else "debit", ""), "dictionary"

        # --- related-party override (loan-application context) ---
        party_key = t.counterparty.upper().replace(" ", "")
        if related and party_key and any(party_key == r or r in party_key or party_key in r
                                          for r in related):
            tag = "Related party credit" if credit else "Related party debit"
            src = "related-party"

        # --- Tier 4: LLM batch classification (production) ---
        # In Phase 1 the unresolved merchants of a statement are classified in
        # ONE Bedrock call and written back into the dictionary. Locally: skip.

        # --- Fallback: regular transfers ---
        if not tag:
            tag = "Regular credit" if credit else "Regular debit"
            src = "fallback"

        t.category, t.category_source = tag, src
    return txns


def category_detail(t: Txn) -> str:
    """Human-readable rendering, per the taxonomy's example column."""
    p = _party(t)
    tag = t.category
    if tag == "EMI transaction":
        ref = _emi_ref(t.description) or p
        return f"EMI paid to {ref}"
    if tag == "ECS transaction":
        return f"ECS transfer to {p}"
    if tag == "Loan amount disbursal":
        return f"Loan amount disbursed from {p}"
    if tag == "cash deposit":
        return f"cash deposit by {p}"
    if tag == "cash withdrawal":
        return f"cash withdrawal by {p}"
    if tag == "Salary paid":
        return f"Salary paid to {p}"
    if tag.startswith("Regular credit"):
        return f"transfer from {p}"
    if tag.startswith("Regular debit"):
        return f"transfer to {p}"
    if tag.startswith("Related party"):
        return f"{'Related party credit from' if 'credit' in tag else 'Related party debit to'} {p}"
    return tag
