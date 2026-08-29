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
    data = _load()
    side = "credit" if getattr(t, "amount", 0) > 0 else "debit"
    tag = getattr(t, "category", "") or ""
    text = f"{getattr(t, 'description', '')} {getattr(t, 'counterparty', '') or ''}"
    hay = _squash(text)
    # A SHORT pattern must match a whole token, never a fragment. Squashing the
    # punctuation away is what makes "ACH-D/ GST-PMT" findable, but it also
    # buries short tokens inside longer words: "F&O" squashes to "FO", which
    # sits inside "EPFO", so a PF challan read as derivatives funding. This is
    # the same trap as the bare "AMB"/"POS" keywords in category_rules.yaml.
    tokens = set(re.findall(r"[A-Z0-9]+", text.upper()))

    best_name, best_score = "", None
    for e in data["entries"]:
        if e["side"] != side:
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
    if best_name:
        return best_name
    return data["defaults"].get(tag, "")
