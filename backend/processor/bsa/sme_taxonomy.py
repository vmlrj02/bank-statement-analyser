"""The SME sub-category — column B of the master's "Category (SME)" tab.

The customer's instruction is that this is what the categorisation should look
like: not eighteen broad tags but the ~33 sub-categories an underwriter
actually reasons about — Payroll & Wages, GST Payments, POS / Merchant
Settlements, OD / CC Drawdowns, Bank Penalties & Non-Maintenance.

It is a SECOND label, layered over the ABCL category tag rather than replacing
it. The master's own sheet-mapping column settles that: several sub-categories
roll up to one sheet (Loan EMIs, Credit Card Payments and OD/CC Interest all
land on "EMI Xns"), so the ABCL tag still decides where a row is written and
the output template's sheet-per-category contract is untouched. Telling a
lender that 118 rows are "Regular debit" says almost nothing; splitting them
into payroll, GST, suppliers and utilities is the whole point.

How a row is resolved, in order:

1. The ABCL tag narrows the candidates — a sub-category declares which tags it
   may apply to, so "GST Payments" can never claim a credit and "Loan EMIs"
   can never claim a cash withdrawal. The speculative groups declare no tags
   and so may match any debit, which is deliberate: a gambling payment is
   whatever the bank called it, and the risk is the point.
2. Narration patterns from the master's own "Identification & Narrative
   Patterns" column pick between the candidates, longest pattern first so
   "GST LATE FEE" beats "GST".
3. Failing both, the tag alone decides. For most tags that is definitional
   rather than a guess (a cash deposit IS a Direct Cash Deposit); the two
   trade lines are honest residuals, documented in the YAML.
"""
from __future__ import annotations

import os
import re

import yaml

_PATH = os.path.join(os.path.dirname(__file__), "data", "sme_subcategories.yaml")
_CACHE: dict | None = None

# At or below this squashed length a pattern must match a WHOLE token. Five
# covers the short bank shorthand that collides — F&O, PF, GST, MAB, SAL, LIC,
# RENT, LEASE — while leaving distinctive names (RAZORPAY, WAZIRX, ZERODHA) to
# match anywhere in the narration.
SHORT_PATTERN = 5


def _squash(s: str) -> str:
    """Upper-case and drop everything that is not a letter or digit, so a
    pattern matches through the punctuation banks scatter through narration
    ("ACH-D/ GST-PMT/ 123" contains "GSTPMT")."""
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


# The bank/app that ROUTED a UPI payment, never the party who was paid or what
# it was for. "OMEGAINC.PAYU@MAIRTEL" is Omega Inc paid via Airtel Payments
# Bank — the reviewer's words: "transfer from Airtel money; not recharge
# related. So it is a vendor payment" — but AIRTEL sat in the haystack and the
# row read as a telecom utility. Masking the handle also stops "@paytm" and
# "@okicici" claiming a POS-settlement or bank rule for the same wrong reason.
#
# ONLY the handle after the "@" is masked. The LOCAL part routinely names the
# merchant ("AIRTELPREDIRECT1@YBL" really is an Airtel recharge), so blanking
# it would lose the very rows this must keep.
_PSP_HANDLE = re.compile(r"@[A-Za-z][A-Za-z0-9]*")


def _mask_psp_handles(s: str) -> str:
    return _PSP_HANDLE.sub(" ", s or "")


# A token for WHOLE-TOKEN matching (see SHORT_PATTERN). A dot or an "@" BINDS:
# "MAB.03724403966" is one payment handle, not the word "MAB" followed by a
# number. Splitting on every non-alphanumeric — which is what this used to do —
# manufactured a bare "MAB" token out of an Axis merchant-acquiring VPA, so
# five UPI payments across HDFC, PNB and SBI were tagged as minimum-balance
# penalties. categorize._is_penal already refused those rows for exactly this
# reason; the sub-category had no such guard.
_TOKEN = re.compile(r"[A-Z0-9]+(?:[._@][A-Z0-9]+)*")


def _load() -> dict:
    global _CACHE
    if _CACHE is None:
        with open(_PATH) as fh:
            raw = yaml.safe_load(fh) or {}
        entries = []
        for side in ("credit", "debit"):
            for e in raw.get(side) or []:
                entries.append({
                    "name": e["name"],
                    "group": e.get("group", ""),
                    "sheet": e.get("sheet") or "",
                    "side": side,
                    "priority": int(e.get("priority", 0)),
                    "tags": set(e.get("tags") or []),
                    # Optional size ceiling in rupees (see sme_subcategory).
                    "max_abs_amount": (float(e["max_abs_amount"])
                                       if e.get("max_abs_amount") is not None
                                       else None),
                    # Longest pattern first, so a specific phrase wins over a
                    # fragment of itself.
                    "patterns": sorted(
                        (_squash(p) for p in (e.get("patterns") or []) if p),
                        key=len, reverse=True),
                })
        _CACHE = {"entries": entries, "defaults": raw.get("defaults") or {}}
    return _CACHE


def subcategories() -> list[dict]:
    """Every sub-category, in the master's order. Used by the tests and by
    anything that needs to render the taxonomy itself."""
    return list(_load()["entries"])


def group_of(name: str) -> str:
    """The column-A group a sub-category belongs to ("" if unknown)."""
    for e in _load()["entries"]:
        if e["name"] == name:
            return e["group"]
    return ""


def sme_subcategory(t) -> str:
    """The SME sub-category for one transaction. Never raises; returns "" only
    when the tag is unknown to the master."""
    override = getattr(t, "sub_category", "") or ""
    if override:
        return override
    data = _load()
    # The printed column, not the sign — a reversal sits in the withdrawal
    # column and must be sub-categorised as the debit it is (a reversed ATM
    # withdrawal was reading as Business income).
    from .models import is_credit_side
    side = "credit" if is_credit_side(t) else "debit"
    tag = getattr(t, "category", "") or ""
    text = _mask_psp_handles(
        f"{getattr(t, 'description', '')} {getattr(t, 'counterparty', '') or ''}")
    hay = _squash(text)
    # A SHORT pattern must match a whole token, never a fragment. Squashing the
    # punctuation away is what makes "ACH-D/ GST-PMT" findable, but it also
    # buries short tokens inside longer words: "F&O" squashes to "FO", which
    # sits inside "EPFO", so a PF challan read as derivatives funding. This is
    # the same trap as the bare "AMB"/"POS" keywords in category_rules.yaml.
    tokens = set(_TOKEN.findall(text.upper()))

    amount = abs(float(getattr(t, "amount", 0) or 0))

    # The two "Misc." lines are a RESIDUAL, not an override. The master
    # identifies them by size rather than wording — a ₹1 penny-drop verifying
    # an account, the ₹1-2 a gateway takes to save a card, a ₹25 lounge
    # deduction — because the payer is a real company and the narration reads
    # like any other payment. But size alone cannot be allowed to WIN: at the
    # ₹50 ceiling that would relabel a ₹23 NEFT charge, a small MAB penalty or
    # a token EMI as "misc", destroying the signal a lender actually needs.
    # So a named match always beats them, and they only outrank the generic
    # trade default. That is also what makes them rare, which is what was
    # asked for.
    misc_name = ""

    best_name, best_score = "", None
    for e in data["entries"]:
        if e["side"] != side:
            continue
        if e["max_abs_amount"] is not None:
            # A ceiling line is normally chosen by SIZE alone, and is held back
            # as a residual (see below). But it may also carry patterns, and
            # then a pattern match selects it OUTRIGHT, ceiling or not: Gopi's
            # curated file puts a ₹364.28 LPG subsidy in Misc. credit, well
            # over the ₹50 ceiling. The old test — "and not e['patterns']" —
            # meant adding a single pattern silently switched the size rule
            # off, which would have quietly undone gotcha 23.
            if amount < e["max_abs_amount"]:
                misc_name = e["name"]
            for p in e["patterns"]:
                if not p:
                    continue
                if len(p) <= SHORT_PATTERN:
                    if p not in tokens:
                        continue
                elif p not in hay:
                    continue
                return e["name"]
            continue
        # An empty tag set means "any row on this side" — the speculative and
        # high-risk groups, which are defined by who was paid, not by the tag.
        if e["tags"] and tag not in e["tags"]:
            continue
        for p in e["patterns"]:
            if not p:
                continue
            if len(p) <= SHORT_PATTERN:
                if p not in tokens:
                    continue
            elif p not in hay:
                continue
            # Priority first, then pattern length. Priority is what stops a
            # generic word beating a specific one: "WAZIRX CRYPTO PURCHASE"
            # must read as a crypto exchange, but "PURCHASE" is the longer
            # match against Supplier / Vendor Settlements.
            score = (e["priority"], len(p))
            if best_score is None or score > best_score:
                best_name, best_score = e["name"], score
            break
    # "A named match always beats them, and they only outrank the generic trade
    # default" — that was always the intent above, but the code made the Misc.
    # lines a pure RESIDUAL, reachable only when nothing matched at all. The
    # generic lines carry patterns too (Business income lists NEFT/RTGS/IMPS),
    # so a bare channel word always beat the ceiling: the reviewer's ₹1
    # "IMPS-…-CFD-CV LOANLINKACCOUNT" penny-drop read as Business income.
    #
    # Priority is what separates the two. The generic trade lines are the only
    # entries below zero (Business income and Supplier / Vendor Settlements are
    # both -10); every specifically-named line sits at zero or above. So the
    # ceiling now outranks a negative-priority match and still loses to a real
    # one — a ₹23 NEFT charge, a small penalty or a token EMI keeps its name.
    if misc_name and (not best_name or best_score[0] < 0):
        return misc_name
    if best_name:
        return best_name
    return data["defaults"].get(tag, "")
