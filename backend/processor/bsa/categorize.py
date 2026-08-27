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
from .narration import parse_narration

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
    d.setdefault("cash_withdrawal_keywords", ["CHQ PAID SELF", "CASH PAID"])
    d.setdefault("return_keywords", ["CHQ DEP RET", "NEFT RETURN", "RTGS RETURN"])
    d.setdefault("dividend_payers", [])
    return d


_RULES = _load_rules()


def _norm(s: str) -> str:
    """Strip ALL non-alphanumeric and uppercase — the narration form used for
    substring matching. Strip everything (not just spaces) so a key matches the
    glued/punctuated print forms too ("L&T Finance" -> "L&TFINANCELIMITED",
    "NEFT RETURN" -> "NEFT_RETURN", "CHQ DEP RET" -> "CHQDEP RET -")."""
    return re.sub(r"[^A-Za-z0-9]", "", s).upper()


# Normalised lender fragments for substring matching.
_LENDER_KEYS = [_norm(x) for x in _RULES["lenders"]]
LENDERS = _RULES["lenders"]                       # kept for callers/tests
_CASH_WD_KEYS = [_norm(x) for x in _RULES["cash_withdrawal_keywords"]]
_RETURN_KEYS = [_norm(x) for x in _RULES["return_keywords"]]
_DIV_PAYER_KEYS = [_norm(x) for x in _RULES["dividend_payers"]]


def _matched_norm(desc: str, keys: list[str]) -> bool:
    k = _norm(desc)
    return any(x in k for x in keys)


def _matched_lender(desc: str):
    """The display name of the lender named in the description, or None. Used
    both to classify and to name the party — a BBPS/UPI lender payment prints a
    channel marker ("BPAY") or a generic tail ("Bank Account") where the party
    should be, so when we recognise the lender we say so."""
    key = _norm(desc)
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
# \bREFUND\b, not \bREF(UND)?\b: bare "REF" is a ubiquitous reference stamp in
# NEFT/IMPS narrations ("…/INDIAN OVERSEAS BANK/REF/", "…-REF") — on real
# statements nearly half the rows it tagged as refunds were ordinary credits.
REFUND_HINTS = [r"\bREFUND\b", r"\bREV(ERSAL)?\b", r"\bRFND\b", r"RETURN OF", r"\bRET\b.*\bCR\b",
                # "RVSL IW CTR RTN CHQNO:…" — a returned instrument credited back
                r"\bRVSL\b",
                # "RTGS RETURN-<ref>-<name>-OPERATIONS SUSPENDED" — an outgoing
                # payment bounced back by the beneficiary's bank
                r"(RTGS|NEFT|IMPS) RETURN",
                # "TOD PENALTY CHARGEREVERSAL" — glued, so \bREV never fires
                r"REVERSAL",
                # A CREDIT stamped insufficient-funds is a failed debit pull
                # coming back ("ACH DR …:INSUFFICIENTFUNDS" at +amount)
                r"INSUFFICIENT\s*FUNDS"]
# CHG / CHRG / CHRGS / CHGS / CHARGE / FEE — the row is a fee, not the amount
# it relates to. Used to keep a bank's charge FOR a returned payment out of the
# "return / refund" tag (the fee is penal; the return is the full amount).
_CHARGE_TOKEN = r"\bCHR?GS?\b|CHARGE|\bFEE\b"
# Dividend markers on a NACH credit ("ACH-CR-TML DIV 30062026", "LICHSG
# FNLDIV"). Only consulted inside the ACH-CR/NACH credit branch, so a person
# named Divya can never trip it.
_DIVIDEND_HINTS = [r"\bDIV\b", r"DIVIDEND", r"FNLDIV", r"INTDIV"]
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
    for p in _PENAL_KEYS:
        for m in re.finditer(p, desc, re.I):
            # A keyword glued to an account/VPA fragment is a handle, not a
            # charge phrase: "UPI-…-MAB.03732201893" is a payment to a
            # merchant, and the ".0373…" tail is what says so.
            if re.match(r"\.?\d", desc[m.end():]):
                continue
            return True
    return False


BOUNCE_INWARD = [r"ECSRTN", r"I/?W.*(RTN|RETURN|BOUNCE)", r"INWARD.*(RTN|RET)", r"CHQ RETURN.*DEP",
                 # The fee for a returned deposit/pull, spelled out: "CHEQUE
                 # RETURN CHARGES", "RETURN HANDLING CHARGES", "ECS Return
                 # Chrgs Incl GST". Word RETURN only — the abbreviated "Chq
                 # Rtrn Chrgs" form is an OUTWARD bounce fee (see below) and
                 # must not land here.
                 r"\bRETURN\b.*(CHR?GS?\b|CHARGE)",
                 # "ECS/NACHRET INSFND CHARGEFOR12-OCT-25" — glued NACH-return
                 r"NACH\s*RET\w*.*(CHR?G|CHARGE)"]
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


def _find_recurring_emi(txns: list[Txn]) -> set[str]:
    """uids of debits — ANY channel — that recur at the same amount to the same
    counterparty in >= 3 distinct months, at a monthly cadence: an EMI paid by
    IMPS/NEFT/transfer instead of a NACH mandate (the reviewer's rule: "monthly
    recurring txns of same amount = EMI"; seen with Mahindra Finance).

    Deliberately conservative, because this reclassifies rows with no lender
    keyword at all:
      * a REAL counterparty is required — with no name, unrelated payments of
        the same amount would collapse into one group;
      * amount >= 500 — a recurring 5.90 SMS charge is not an EMI;
      * count <= months + 1 — the same amount many times a month is trading
        volume, not an instalment (the +1 allows one bounce-and-retry).
    Only consulted for rows that would otherwise fall through to Regular debit,
    so an explicit rule/dictionary/related-party tag always wins."""
    groups: dict[tuple, list[Txn]] = defaultdict(list)
    for t in txns:
        party = t.counterparty.upper().replace(" ", "")
        if t.amount <= -500 and len(party) >= 4 and party != "UNKNOWNPARTY":
            groups[(party, round(-t.amount, 2))].append(t)
    emis: set[str] = set()
    for ts in groups.values():
        months = {t.date[:7] for t in ts}
        if len(months) >= 3 and len(ts) <= len(months) + 1:
            emis.update(t.uid for t in ts)
    return emis


def categorize(txns: list[Txn], related_parties: list[str] | None = None,
               use_llm: bool = False) -> list[Txn]:
    related = [re.sub(r"\s+", "", p).upper() for p in (related_parties or [])]
    dictionary = _load_dictionary()
    recurring_nach = _find_recurring_nach(txns)
    recurring_emi = _find_recurring_emi(txns)

    for t in txns:
        d, credit = t.description, t.amount > 0
        tag, src = "", "rule"

        # Match a lender against the STRUCTURED part of the narration, not the
        # payer's free-text remark — so a customer who types "parimal finance
        # amount" in the note of a credit from "happylaser" no longer flips it to
        # a loan disbursal. The remark is separated by narration.parse_narration.
        lender_name = _matched_lender(parse_narration(d).structured)
        # A row that IS a fee ("CHG/<ref>/<bank>/CHOLAMXVFPKUD000" — the ₹11.80
        # IMPS charge printed beside the actual EMI) names the lender but is
        # not a payment to them.
        if lender_name and re.search(r"(?:^|[\s/-])CHG[/\s-]", d):
            lender_name = None
        lender = lender_name is not None
        # Name the party after the lender when the extracted one is a channel
        # marker or a generic tail (BBPS "BPAY", UPI "Bank Account").
        if lender_name and t.counterparty.strip().upper() in _WEAK_PARTY:
            t.counterparty = lender_name
        # --- Tier 1: deterministic rules ---
        if not credit and re.search(r"BIL/.*(Loan|EMI)", d, re.I):
            tag = "EMI transaction"
        # A returned payment, either direction — the credit that comes back
        # when an outgoing transfer bounces, or the debit that reverses a
        # deposited cheque. Resolved BEFORE the bounce tiers because "I/W
        # CHEQUE RETURN-<name>" is the returned AMOUNT; only a row that also
        # carries a charge token is the bank's fee for it, and that one falls
        # through to the bounce/penal tiers instead.
        elif (_matched_norm(d, _RETURN_KEYS)
              and not (not credit and re.search(_CHARGE_TOKEN, d, re.I))):
            tag = "return / refund"
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
        # DEBITS were mislabelled as a credit category. \s* between INTEREST
        # and its verb because HDFC glues them ("INTERESTPAIDTILL30-JUN-2025",
        # "MONTHLYINTERESTCREDIT…").
        elif t.mode == "interest" or re.search(
                r"\bINT\.?\s?PD\b|DEBIT INTEREST|CREDIT INTEREST"
                r"|INTEREST\s*(PAID|CREDIT|DEBIT|DEBITED|CHARGED|COLLECTED)"
                r"|\bINT\.?\s*(DR|DEBIT|COLL)"
                # FD/sweep interest paid out ("FD REDEEM INTEREST", "INT AUTO
                # REDEEM") and OD shortfall interest recovered ("RCVRY
                # TOD OLSHORTFALL INT" — glued, hence \s*).
                r"|REDEEM\s*INTEREST|INT\s*AUTO\s*REDEEM|SHORTFALL\s*INT\b",
                d, re.I):
            tag = "Interest received" if credit else "Interest payments"
        elif not credit and (t.mode == "atm-cash" or _matched_norm(d, _CASH_WD_KEYS)):
            tag = "cash withdrawal"
        elif credit and (t.mode == "cash-deposit" or _any(d, _CASH_DEP_KEYS)):
            tag = "cash deposit"
        # A known NBFC / lender name decides both directions: a credit is a
        # loan disbursal; a debit is an EMI — however it was paid. A one-off
        # UPI/IMPS debit to a lender is overwhelmingly an EMI paid by hand
        # (typically right after the NACH pull bounced), so recurrence is not
        # required. The exception, per the reviewer (ID8), is a BBPS bill-pay
        # to a lender, which stays a non-EMI servicing payment. Rows whose
        # narration says interest were already resolved above.
        elif credit and lender:
            tag = "Loan amount disbursal"
        elif not credit and lender:
            tag = ("Interest payments" if re.search(r"BPAY|BBPS", d, re.I)
                   else "EMI transaction")
        elif credit and _any(d, [re.escape(x) for x in INVESTMENT_HINTS]):
            tag = "Investment return credited"
        elif credit and _any(d, SALARY_HINTS):
            tag = "Salary credited"
        elif not credit and _any(d, SALARY_HINTS):
            tag = "Salary paid"
        elif credit and _any(d, REFUND_HINTS):
            tag = "return / refund"
        # A NACH credit ("ACH-CR-<payer>-NACH-<mandate>") is a company paying
        # the account holder under mandate: a dividend if the narration or the
        # payer says so, interest on a company deposit if it says INT,
        # otherwise a generic ECS receipt.
        elif credit and re.search(r"ACH.?CR|\bNACH\b", d, re.I):
            if _any(d, _DIVIDEND_HINTS) or _matched_norm(d, _DIV_PAYER_KEYS):
                tag = "Investment return credited"
            elif re.search(r"\bINT\b", d, re.I):
                tag = "Interest received"
            else:
                tag = "ECS transaction"
        elif not credit and t.mode in ("ecs-return",):
            tag = "inward bounce penal charges"
        elif not credit and (t.mode == "nach"
                             # Glued ACH-debit prints the mode detector misses
                             # ("ACH DR 10INDUSIND BANK…") and UPI autopay
                             # mandates ("UPI/P2M/…/Mandate//P2V/") — both are
                             # mandate pulls, i.e. ECS in the taxonomy.
                             or re.search(r"\bACH\s*DR\b|\bMANDATE", d, re.I)) \
                and not re.search(_CHARGE_TOKEN, d, re.I):
            # The charge guard keeps the FEE for a mandate out of this branch:
            # a monthly "ECS Txn Chrgs Incl GST" of 30 was recurring at a fixed
            # amount and getting tagged an EMI — it is a service charge, and
            # falls through to the fee handling / Regular debit instead.
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

        # --- Tier 2b: general recurrence — monthly fixed-amount debit to the
        # same party = EMI, whatever the channel (IMPS/NEFT/transfer). Applied
        # only where nothing above tagged the row, so it upgrades would-be
        # Regular debits and never overrides an explicit signal.
        if not tag and not credit and t.uid in recurring_emi:
            # "recurrence-cadence" (not "recurrence"): a NACH-mandate recurrence
            # is a definitive signal, but this one is purely behavioural — a
            # fixed monthly payment to an individual can also be rent or wages —
            # so it gets MEDIUM confidence below, keeping it visible for review.
            tag, src = "EMI transaction", "recurrence-cadence"

        # --- Fallback: regular transfers ---
        if not tag:
            tag = "Regular credit" if credit else "Regular debit"
            src = "fallback"

        t.category, t.category_source = tag, src
        # Confidence: a definitive signal (a rule, recurrence, the merchant
        # dictionary, a related-party match) is HIGH. A row that fell through to
        # the "Regular debit/credit" default is only as good as its party — with
        # a real counterparty it is a known-party transfer (MEDIUM); with none it
        # is genuinely "we don't know what or who" (LOW), and those are what a
        # reviewer should eyeball rather than trust. The report never presents a
        # low row as certain.
        if src == "recurrence-cadence":
            t.confidence = "medium"      # behavioural inference, not a keyword
        elif src != "fallback":
            t.confidence = "high"
        elif t.counterparty and t.counterparty.strip().upper() not in _WEAK_PARTY:
            t.confidence = "medium"
        else:
            t.confidence = "low"
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
