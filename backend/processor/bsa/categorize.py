"""Stage 4b — Categorize, using the SME/MSME lending taxonomy
(from Banking_pdf_extraction.xlsx "Category list").

Category tags:
  EMI transaction | Interest received | Interest payments |
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

import yaml

from .models import Txn

_RULES_PATH = os.path.join(os.path.dirname(__file__), "data", "category_rules.yaml")


def _load_rules() -> dict:
    """Load the domain-owner-editable categorisation vocabulary. Never fatal —
    a missing or broken file degrades to a small built-in lender seed, so the
    pipeline still runs."""
    try:
        with open(_RULES_PATH) as f:
            d = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        d = {}
    d.setdefault("lenders", ["BAJAJ FIN", "HDB FINANC", "TATA CAPITAL"])
    d.setdefault("penal_keywords", ["MAB", "MIN BAL", "POS", "PENAL"])
    d.setdefault("non_penal_charge_keywords", [])
    d.setdefault("cash_deposit_keywords", ["BY CASH", "CASH DEP", "CDM"])
    return d


_RULES = _load_rules()
# Normalised lender fragments for substring matching. Strip ALL non-alphanumeric
# (not just spaces) so the key matches the description, which is normalised the
# same way in _matched_lender — otherwise a lender with punctuation ("L&T
# Finance") could never match its glued narration form ("L&TFINANCELIMITED").
_LENDER_KEYS = [re.sub(r"[^A-Za-z0-9]", "", x).upper() for x in _RULES["lenders"]]
LENDERS = _RULES["lenders"]                       # kept for callers/tests


def _matched_lender(desc: str):
    """The display name of the lender named in the description, or None. Used
    both to classify and to name the party — a BBPS/UPI lender payment prints a
    channel marker ("BPAY") or a generic tail ("Bank Account") where the party
    should be, so when we recognise the lender we say so."""
    key = re.sub(r"[^A-Za-z0-9]", "", desc).upper()
    for display, lkey in zip(_RULES["lenders"], _LENDER_KEYS):
        if lkey in key:
            return display
    return None


def _has_lender(desc: str) -> bool:
    return _matched_lender(desc) is not None


# Party values too generic to keep when we have something better (a lender name).
_WEAK_PARTY = {"", "BPAY", "P2A", "P2M", "BANK ACCOUNT", "UNKNOWN PARTY",
               "BILL", "BBPS"}
INVESTMENT_HINTS = [
    "SHAREKHAN", "ZERODHA", "GROWW", "UPSTOX", "ANGEL", "ICCLR", "MUTUAL FUND",
    "MF REDEMPTION", "NSE ", "BSE ", "CDSL", "NSDL", "RD MATURITY", "FD CLOS",
]
SALARY_HINTS = [r"\bSAL\b", r"SALARY", r"\bSAL-", r"SAL CREDIT", r"PAYROLL"]
REFUND_HINTS = [r"\bREF(UND)?\b", r"\bREV(ERSAL)?\b", r"\bRFND\b", r"RETURN OF", r"\bRET\b.*\bCR\b",
                # "RVSL IW CTR RTN CHQNO:…" — a returned instrument credited back
                r"\bRVSL\b",
                # "RTGS RETURN-<ref>-<name>-OPERATIONS SUSPENDED" — an outgoing
                # payment bounced back by the beneficiary's bank
                r"(RTGS|NEFT|IMPS) RETURN"]
# Penal detection is now DATA-driven (data/category_rules.yaml). The master
# defines penal as a threshold/violation charge ("pos threshold, MAB, etc."),
# NOT an ordinary service fee — the reviewer was explicit that card and
# transaction charges are not penal. So a charge is penal only if it hits a
# penal keyword AND is not first excluded by a service-charge keyword.
# Leading word boundary only — so a phrase never fires inside an unrelated name
# ("AMB CHG" cannot appear in "jambAMBaga"), but a trailing plural still matches
# ("AMB CHG" hits "AMB CHGS"). A full \b...\b would miss the plural.
_PENAL_KEYS = [r"\b" + re.escape(k) for k in _RULES["penal_keywords"]]
_NON_PENAL_KEYS = [r"\b" + re.escape(k) for k in _RULES["non_penal_charge_keywords"]]
_CASH_DEP_KEYS = [re.escape(k) for k in _RULES["cash_deposit_keywords"]]


def _is_penal(desc: str) -> bool:
    if _NON_PENAL_KEYS and _any(desc, _NON_PENAL_KEYS):
        return False
    return _any(desc, _PENAL_KEYS)


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

        lender_name = _matched_lender(d)
        lender = lender_name is not None
        # Name the party after the lender when the extracted one is a channel
        # marker or a generic tail (BBPS "BPAY", UPI "Bank Account").
        if lender_name and t.counterparty.strip().upper() in _WEAK_PARTY:
            t.counterparty = lender_name
        # --- Tier 1: deterministic rules ---
        if not credit and re.search(r"BIL/.*(Loan|EMI)", d, re.I):
            tag = "EMI transaction"
        elif not credit and _any(d, BOUNCE_INWARD):
            tag = "inward bounce penal charges"
        elif not credit and _any(d, BOUNCE_OUTWARD):
            tag = "Outward Bounced Xns"
        # Penal charges are resolved BEFORE interest so a MAB/avg-balance charge
        # whose reference happens to contain "Int.Pd" reads as penal, not
        # interest ("Avg bal Chgs Incl GST … Int.Pd:01-10-2025").
        elif not credit and _is_penal(d):
            tag = "other penal charges"
        # Interest, split by SIGN (ID6/ID8): a credit is interest RECEIVED, a
        # debit is an "Interest payments" (OD interest, or a non-EMI payment to
        # an NBFC). The old rule tagged every "Int.Pd" as received, so interest
        # DEBITS were mislabelled as a credit category.
        elif t.mode == "interest" or re.search(
                r"\bINT\.?\s?PD\b|DEBIT INTEREST|CREDIT INTEREST"
                r"|INTEREST (PAID|CREDIT|DEBIT|DEBITED|CHARGED|COLLECTED)"
                r"|\bINT\.?\s*(DR|DEBIT|COLL)", d, re.I):
            tag = "Interest received" if credit else "Interest payments"
        elif t.mode == "atm-cash" and not credit:
            tag = "cash withdrawal"
        elif credit and (t.mode == "cash-deposit" or _any(d, _CASH_DEP_KEYS)):
            tag = "cash deposit"
        # A known NBFC / lender name decides both directions: a credit is a loan
        # disbursal; a debit is an EMI if it recurs, otherwise an "Interest
        # payments" (a non-EMI servicing payment). Data-driven lender list.
        elif credit and lender:
            tag = "Loan amount disbursal"
        elif not credit and lender:
            tag = ("EMI transaction"
                   if (t.uid in recurring_nach or t.mode == "nach")
                   else "Interest payments")
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
            tag = ("EMI transaction" if t.uid in recurring_nach else "ECS transaction")
            if t.uid in recurring_nach:
                src = "recurrence"

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
