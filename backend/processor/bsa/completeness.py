"""Extraction completeness — did we capture every transaction?

Balance reconciliation proves the rows we DID extract chain correctly, but a
statement that prints its own transaction count lets us prove we did not silently
drop any — the exact failure that once read 183 rows out of 299 while still
"reconciling" around the gap. When a layout can capture the bank's declared
Dr/Cr counts (HDFC prints "DrCount CrCount", SBI's internet export prints a
count pair in its footer), we compare and say so plainly.

Best-effort and never fatal: if the totals can't be found, completeness is
simply "not checked" rather than a failure.
"""
from __future__ import annotations

import re


def declared_from_pdf(pdf_path: str, pattern: str) -> dict:
    """Read the pages where a summary total lives (last few, plus page 1) and
    parse it. Cheap and best-effort — any failure returns {}."""
    if not pattern:
        return {}
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            n = len(pdf.pages)
            idxs = sorted({0, max(0, n - 3), max(0, n - 2), n - 1})
            text = "\n".join(pdf.pages[i].extract_text() or "" for i in idxs)
        return declared_from_text(text, pattern)
    except Exception:                                       # noqa: BLE001
        return {}


def declared_from_text(text: str, pattern: str) -> dict:
    """Parse a layout's summary-totals regex (named groups n_txns / n_debits /
    n_credits) from statement text. Returns {} if it doesn't match."""
    if not pattern or not text:
        return {}
    m = re.search(pattern, text, re.I)
    if not m:
        return {}
    g = m.groupdict()
    out: dict = {}
    for k in ("n_txns", "n_debits", "n_credits"):
        v = (g.get(k) or "").replace(",", "").strip()
        if v.isdigit():
            out[k] = int(v)
    return out


def check_completeness(n_extracted: int, n_debits: int, n_credits: int,
                       declared: dict) -> dict:
    """Compare extracted counts to the statement's declared counts."""
    if not declared:
        return {"checked": False}
    expected = declared.get("n_txns")
    if expected is None and "n_debits" in declared and "n_credits" in declared:
        expected = declared["n_debits"] + declared["n_credits"]

    # The TOTAL count is the hard completeness signal — a mismatch means rows
    # were dropped or duplicated. The per-direction split is only informational:
    # a reversal posted in the withdrawal column (a negative withdrawal) nets to
    # a positive amount, so our Dr/Cr split can legitimately differ from the
    # bank's while every row is present.
    complete = True
    notes: list[str] = []
    if expected is not None and expected != n_extracted:
        complete = False
        gap = expected - n_extracted
        notes.append(
            f"extracted {n_extracted} transactions but the statement declares "
            f"{expected} — {abs(gap)} {'not extracted' if gap > 0 else 'extra'}")

    return {"checked": True, "complete": complete, "declared": declared,
            "extracted": {"n": n_extracted, "n_debits": n_debits,
                          "n_credits": n_credits},
            "notes": notes}
