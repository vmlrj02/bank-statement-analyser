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
    for k in ("sum_debits", "sum_credits"):
        v = (g.get(k) or "").replace(",", "").strip()
        try:
            if v:
                out[k] = float(v)
        except ValueError:
            pass
    return out


def check_completeness(n_extracted: int, n_debits: int, n_credits: int,
                       declared: dict, sum_debits: float = 0.0,
                       sum_credits: float = 0.0) -> dict:
    """Compare what we extracted to the statement's own declared totals — either
    a transaction COUNT (HDFC, SBI internet) or the debit/credit amount TOTALS
    (Axis prints "TRANSACTION TOTAL"). Both prove no rows were dropped."""
    if not declared:
        return {"checked": False}

    complete = True
    notes: list[str] = []

    # (a) Count check — the total count is the hard signal.
    #
    # The per-direction split USED to be excused here, on the grounds that "a
    # reversal posted in the withdrawal column nets to a positive amount, so
    # our Dr/Cr split can legitimately differ". That excuse was the bug: the
    # split differed because we counted by SIGN while the bank counts by
    # COLUMN. Txn.side carries the column now and publish counts with it, so a
    # remaining difference is a real one and worth reporting.
    expected = declared.get("n_txns")
    if expected is None and "n_debits" in declared and "n_credits" in declared:
        expected = declared["n_debits"] + declared["n_credits"]
    if expected is not None and expected != n_extracted:
        complete = False
        gap = expected - n_extracted
        notes.append(
            f"extracted {n_extracted} transactions but the statement declares "
            f"{expected} — {abs(gap)} {'not extracted' if gap > 0 else 'extra'}")

    # (b) Amount check — the extracted debit/credit sums must match the bank's
    # printed totals. A relative tolerance absorbs rounding; a real dropped row
    # moves the sum well beyond it.
    def _off(got, want):
        return abs(got - want) > max(5.0, abs(want) * 0.0002)

    if "sum_debits" in declared and _off(sum_debits, declared["sum_debits"]):
        complete = False
        notes.append(f"debit total {sum_debits:,.2f} vs declared "
                     f"{declared['sum_debits']:,.2f} — a "
                     f"{abs(sum_debits - declared['sum_debits']):,.2f} gap "
                     f"(a row is likely missing or mis-read)")
    if "sum_credits" in declared and _off(sum_credits, declared["sum_credits"]):
        complete = False
        notes.append(f"credit total {sum_credits:,.2f} vs declared "
                     f"{declared['sum_credits']:,.2f} — a "
                     f"{abs(sum_credits - declared['sum_credits']):,.2f} gap")

    return {"checked": True, "complete": complete, "declared": declared,
            "extracted": {"n": n_extracted, "n_debits": n_debits,
                          "n_credits": n_credits,
                          "sum_debits": round(sum_debits, 2),
                          "sum_credits": round(sum_credits, 2)},
            "notes": notes}
