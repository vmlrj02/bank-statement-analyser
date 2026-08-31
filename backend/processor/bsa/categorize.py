"""Stage 4b — Categorize, using the SME/MSME lending taxonomy
(from Banking_pdf_extraction.xlsx "Category list").

Category tags:
  EMI transaction | Interest received | Interest / fee payments |
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

# ---------------------------------------------------------------------------
# Taxonomy metadata, straight from the "Banking extraction data labeling"
# master. These are properties OF the taxonomy, so they live in code beside
# the tags; the fuzzy vocabulary that decides which tag a row gets stays in
# data (category_rules.yaml).
# ---------------------------------------------------------------------------

INWARD_BOUNCE = "inward bounce penal charges"
OUTWARD_BOUNCE = "Outward Bounced Xns"

# The master's column "Number of Payments Issued - relevant for inward bounce":
# debits the account HOLDER initiated, and therefore the only payments that
# could have come back as an inward return. A bounce count means nothing on its
# own - three returns against four payments is a failing account, three against
# nine hundred is noise - so the Summary sheet reports the rate against this
# denominator, and the master names exactly which categories belong in it.
PAYMENTS_ISSUED = frozenset({
    "EMI transaction", "Salary paid", "ECS transaction",
    "Related party debit", "Regular debit",
})

# The master's column "Number of Payments Deposited - relevant for outward
# bounce": credits the holder presented for collection, the denominator for
# outward cheque returns. Cash deposits, interest, loan disbursals and refunds
# are marked "No" - nothing was presented, so they cannot bounce.
PAYMENTS_DEPOSITED = frozenset({
    "Related party credit", "Regular credit",
})

# Credits that are NOT business turnover. The SME master is explicit about
# which inflows must be stripped before a turnover figure is quoted: interest
# and treasury income are "excluded from business turnover calculations",
# own/group transfers "must be stripped out to prevent artificial turnover
# inflation", asset sales and tax refunds are "non-core". Borrowed money and
# personal salary were never revenue. Cash deposits DO count - they are sales
# receipts, and their risk is reported separately as cash intensity.
NON_TURNOVER = frozenset({
    "Loan amount disbursal", "Salary credited", "Interest received",
    "Investment return credited", "return / refund", "Related party credit",
})


def is_business_credit(t) -> bool:
    """True when a transaction counts toward business turnover (GTO)."""
    return t.amount > 0 and t.category not in NON_TURNOVER


def _squash(s: str) -> str:
    return re.sub(r"[^A-Z0-9&.]", "", (s or "").upper())


def high_risk_group(description: str) -> str | None:
    """Which speculative / high-risk group a DEBIT narration belongs to, if any.

    The SME master calls these out because an SME diverting working capital
    into betting, crypto or F&O is a credit signal an underwriter must see —
    but they get no category tag, because every tag maps to a sheet in the
    customer's template and a nineteenth would break that contract. The row
    stays a Regular debit; the assessment reports it.
    """
    groups = _RULES.get("high_risk_spend") or {}
    hay = _squash(description)
    for group, needles in groups.items():
        for n in needles:
            if _squash(n) and _squash(n) in hay:
                return group
    return None


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
    # A credit from a broker's client account ("HDFCSECURITIESLTD-CLIENTA/C",
    # "ZERODHA BROKING LTD-DSCNB A/C") is a payout of the customer's own
    # invested funds. Credit-only, like every hint in this list.
    "SECURITIES", "BROKING",
    # The word itself — a co-operative bank prints "Dividend Credit to A-…
    # for the year 2024-25" with no NACH/DIV marker anywhere near it.
    "DIVIDEND",
]
# (?<!DIRECT ) on the bare SAL: oil-company beneficiaries print truncated
# "direct sal(es)" ("INB/RTGS/…/h pcl direct sal/HDFC BANK") — a ₹24-lakh fuel
# purchase, not payroll. "HPCL DIRECT SALES" never matched (\b fails on SALES);
# only the cut-off form did.
SALARY_HINTS = [r"(?<!DIRECT )\bSAL\b", r"SALARY", r"\bSAL-", r"SAL CREDIT", r"PAYROLL"]
# \bREFUND\b, not \bREF(UND)?\b: bare "REF" is a ubiquitous reference stamp in
# NEFT/IMPS narrations ("…/INDIAN OVERSEAS BANK/REF/", "…-REF") — on real
# statements nearly half the rows it tagged as refunds were ordinary credits.
REFUND_HINTS = [r"\bREFUND\b", r"\bREV(ERSAL)?\b", r"\bRFND\b", r"RETURN OF", r"\bRET\b.*\bCR\b",
                # "RVSL IW CTR RTN CHQNO:…" — a returned instrument credited
                # back. Prefix match (no trailing \b): HDFC glues it to what
                # was reversed ("RVSLEDCRENTALAPR25", "UPI/RVSL5079…").
                r"\bRVSL",
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
# Leading non-alphanumeric boundary on every deposit key: without it "BNA/"
# fires inside a truncated bank name ("…/CARBONAN/PUNJABNA/PiramalP" — Punjab
# National), turning an ordinary IMPS credit into a cash deposit. A glued form
# still matches ("CAM/…/CASHDEP-Other" — the "/" is non-alphanumeric).
_CASH_DEP_KEYS = [r"(?<![A-Za-z0-9])" + re.escape(k)
                  for k in _RULES["cash_deposit_keywords"]]


def _is_penal(desc: str) -> bool:
    if _NON_PENAL_KEYS and _any(desc, _NON_PENAL_KEYS):
        return False
    for p in _PENAL_KEYS:
        for m in re.finditer(p, desc, re.I):
            # A keyword glued to an account/VPA fragment is a handle, not a
            # charge phrase: "UPI-…-MAB.03732201893" is a payment to a
            # merchant, and the ".0373…" tail is what says so. Likewise a
            # keyword sitting inside a VPA ("…0219389.mab@pnb") — flagged by
            # the "@" right after it or the "."/"@" right before it.
            if re.match(r"\.?\d|@", desc[m.end():]):
                continue
            if m.start() > 0 and desc[m.start() - 1] in ".@":
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
                 r"NACH\s*RET\w*.*(CHR?G|CHARGE)",
                 # PNB's bare "ACH RTN--28-01-2026": the fixed ₹295 charge for
                 # a NACH pull returned on the mandate date, printed with no
                 # charge token at all. \bACH keeps "NACH RTN CHG" (outward,
                 # below) out — its A is glued to the N.
                 r"\bACH\s*RTN\b"]
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


# Words that say the recurring payment is wages, rent or an advance, not a
# loan instalment — seen verbatim in the cadence-precision review ("house",
# "DRIVERADVANCEROH", "Rent 0098292162098").
_NOT_EMI_HINTS = re.compile(r"\bRENT\b|ADVANCE|DRIVER|SALARY|\bWAGES?\b"
                            r"|\bHOUSE\b|MAINTEN", re.I)


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

    A precision review of 30 real hit-groups across 8 banks found the original
    guards let through wages, rent, supplier payments and personal transfers
    (~1/30 was plausibly an EMI), so a hit must now also look like a mandate:
      * a NON-ROUND amount — an EMI is interest-arithmetic (21,990 / 74,322 /
        41,005.90); wages, rent and trade payments are round thousands. Any
        multiple of 500 with no paise is treated as round;
      * CONSECUTIVE months — an instalment does not skip a month;
      * a STABLE day of month (span <= 7 across the group) — the mandate date,
        with room for weekends and one late retry;
      * no wages/rent/advance wording, and not a bare bank-name counterparty
        (a group keyed on "KOTAK MAHINDRA BANK LIMITED" is a party-extraction
        failure, not a lender).
    Only consulted for rows that would otherwise fall through to Regular debit,
    so an explicit rule/dictionary/related-party tag always wins."""
    groups: dict[tuple, list[Txn]] = defaultdict(list)
    for t in txns:
        party = t.counterparty.upper().replace(" ", "")
        if t.amount <= -500 and len(party) >= 4 and party != "UNKNOWNPARTY":
            groups[(party, round(-t.amount, 2))].append(t)
    emis: set[str] = set()
    for (_, amt), ts in groups.items():
        months = sorted({t.date[:7] for t in ts})
        if len(months) < 3 or len(ts) > len(months) + 1:
            continue
        if amt % 500 == 0:
            continue                              # round amount: wages/rent/trade
        idx = [12 * int(m[:4]) + int(m[5:7]) for m in months]
        if idx[-1] - idx[0] != len(idx) - 1:
            continue                              # skipped a month: no mandate
        days = sorted(int(t.date[8:10]) for t in ts)
        if days[-1] - days[0] > 7:
            continue                              # wandering date: ad-hoc transfers
        if any(_NOT_EMI_HINTS.search(t.description) for t in ts) \
                or re.search(r"\bBANK\b", ts[0].counterparty, re.I):
            continue
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
        narr = parse_narration(d)
        lender_name = _matched_lender(narr.structured)
        # …but an ALL-CAPS "remark" is usually a bank-printed field the splitter
        # misread, not a typed note: IndusInd puts the sender's name in the
        # remark slot ("N/<ref>/<ifsc>/CHOLAMANDALAM INVES/T AND FINANC…"), and
        # dropping it hid a ₹91-lakh disbursal. A payer-typed note ("parimal
        # finance amount") is lower/mixed case, so the guard above still holds.
        if lender_name is None and narr.remark and not re.search(r"[a-z]", narr.remark):
            lender_name = _matched_lender(narr.remark)
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
        # debit is an "Interest / fee payments" (OD interest, or a non-EMI payment to
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
                r"|REDEEM\s*INTEREST|INT\s*AUTO\s*REDEEM|SHORTFALL\s*INT\b"
                # Co-operative "By-Interest Normal Cr. Int." (verb-first), and
                # SBI's OD interest swept to the loan account ("TO TRANSFER-
                # INTEREST TRANSFER TO 43256012084"). \bINT\b in CR INT keeps
                # "CR INTERIORS"-style names out.
                r"|\bCR\.?\s*INT\b|INTEREST\s*TRANSFER",
                d, re.I):
            tag = "Interest received" if credit else "Interest / fee payments"
        # The TDS guard: "TDS Debit against Cash withdrawal on 16022026" is the
        # tax ON a withdrawal (sec 194N), not cash leaving — a Regular debit.
        elif not credit and (t.mode == "atm-cash" or _matched_norm(d, _CASH_WD_KEYS)) \
                and not re.search(r"\bTDS\b", d, re.I):
            tag = "cash withdrawal"
        elif credit and (t.mode == "cash-deposit" or _any(d, _CASH_DEP_KEYS)):
            tag = "cash deposit"
        # The narration SAYS instalment: a bare word EMI ("To-Transfer SI No:
        # 950860 ML 218/EMI", "ACHD-BD-VASTUHFC-EMI-…", "camera emi" typed in a
        # UPI remark) or a loan-repayment remark ("…/Mr. Vigh/Loan Rep",
        # "LoanRepayment"). Debit-only — an EMI-marked CREDIT is someone
        # repaying money the customer lent out, which stays a Regular credit.
        # Sits after the charge/bounce/interest tiers so "EMI bounce chgs"
        # narrations keep their charge reading.
        # (?<![A-Z0-9])EMI(?![A-Z0-9]) rather than \bEMI\b: an underscore is a
        # \w character, so \b never fires in "UCR013913427589_EMI_05/11/2025".
        elif not credit and re.search(
                r"(?<![A-Z0-9])EMI(?![A-Z0-9])|\bLOAN\s*REPAY|\bLOAN\s*REP\b",
                d, re.I):
            tag = "EMI transaction"
        # A known NBFC / lender name decides both directions: a credit is a
        # loan disbursal; a debit is an EMI — however it was paid. A one-off
        # UPI/IMPS debit to a lender is overwhelmingly an EMI paid by hand
        # (typically right after the NACH pull bounced), so recurrence is not
        # required. The exception, per the reviewer (ID8), is a BBPS bill-pay
        # to a lender, which stays a non-EMI servicing payment. Rows whose
        # narration says interest were already resolved above.
        # A credit that names a LOAN ACCOUNT is a disbursal even when the payer
        # is a plain bank rather than one of the NBFCs in `lenders`. Reported
        # by the reviewer: "NEFT/KKBK…/Kotak Mahindra Bank Ltd/… Pyt Loan A c
        # CSG …" for ₹16.1 lakh was reading as trade income, which overstates
        # turnover by the size of the loan — the exact circularity gotcha 18
        # exists to prevent. Banks must NOT go in `lenders` (every NEFT from
        # one would become a disbursal); the loan-account wording is the
        # signal. "LOAN REPAY" is excluded — that is money going out — and this
        # is credit-only anyway.
        elif credit and re.search(
                r"\bLOAN\s*A\s*/?\s*C\b|\bLOAN\s*ACCOUNT\b|\bLOAN\s*DISB"
                r"|\bDISBURS", d, re.I):
            tag = "Loan amount disbursal"
        # A credit whose narration simply says "loan" — the word the payer or
        # the customer typed into the remark, with no account number and no
        # lender name to key on. "BY TRANSFER-INB loan- …", "…/ATTN/HAND LOAN",
        # "IMPS/…/Loan/MOZASUMULT". The reviewer asked of one of these "might be
        # a loan right?", and it was: every one of the sixteen such rows in the
        # corpus is a genuine borrowing, ₹47 lakh of it — a ₹40 lakh hand loan
        # among them — all of it counted as business income until now, which is
        # precisely the turnover circularity gotcha 18 forbids.
        #
        # The exclusions are what make it safe. Repayment wording means money
        # going the other way, and an EMI or interest remark on a credit is a
        # reversal or a rebate, not a drawdown.
        elif credit and re.search(r"(?<![A-Za-z])LOANS?(?![A-Za-z])", d, re.I) \
                and not re.search(r"LOAN\s*REPAY|LOAN\s*REP\b|EMI|INTEREST"
                                  r"|\bINT\b", d, re.I):
            tag = "Loan amount disbursal"
        elif credit and lender:
            tag = "Loan amount disbursal"
        elif not credit and lender:
            tag = ("Interest / fee payments" if re.search(r"BPAY|BBPS", d, re.I)
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
        # mode == "nach" too: ICICI prints "ACH/TCS2ndIntDiv04112025/…" — the
        # detector reads the mode but the text never says ACH-CR or NACH.
        elif credit and (t.mode == "nach"
                         or re.search(r"ACH.?CR|\bNACH\b", d, re.I)):
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
