"""Stage 5 — Validate: the release gate.

Deterministic checks that hold for every correctly-extracted Indian bank
statement: running-balance reconciliation row by row, and date monotonicity.
"""
from __future__ import annotations

from datetime import date

from .models import Txn, ValidationIssue, ValidationReport

TOL = 0.011
# A balance break where the dates also jump this far is almost never a parser
# error — it is a hole in the document itself (seen for real: a 58-page ICICI
# statement assembled by hand, with November present only as a printed Gmail
# preview of the summary e-mail, so a whole month had no transaction pages).
GAP_DAYS = 20


def _days_between(a: str, b: str) -> int:
    try:
        return abs((date.fromisoformat(b) - date.fromisoformat(a)).days)
    except ValueError:
        return 0


def validate(txns: list[Txn]) -> ValidationReport:
    """Reconcile the running balance, per account.

    A job may merge statements from several banks and years, and each account
    has its own independent balance chain — checking one chain across all rows
    would fail every row after the first account ends. Grouping is a no-op for
    the single-account case.
    """
    issues: list[ValidationIssue] = []

    groups: dict[str, list[tuple[int, Txn]]] = {}
    for i, t in enumerate(txns):
        groups.setdefault(f"{t.bank}|{t.account_no}", []).append((i, t))
    multi = len(groups) > 1

    for key, items in groups.items():
        where = f" [{key.replace('|', ' ')}]" if multi else ""
        prev_balance = None
        prev_date = None
        for i, t in items:
            if getattr(t, "is_opening", False):
                # A brought-forward opening re-bases the chain: the period that
                # follows reconciles from THIS balance, not the previous period's
                # close (which may sit across a gap — gotcha 12).
                prev_balance = t.balance
                prev_date = t.date
                continue
            if prev_balance is not None:
                # A cash-credit/overdraft chain prints the balance as an amount
                # owed, so it moves opposite to the money-flow sign: subtract.
                delta = -t.amount if t.balance_inverted else t.amount
                expected = round(prev_balance + delta, 2)
                tol = max(TOL, t.balance_tolerance)
                if abs(expected - t.balance) > tol:
                    detail = (f"row {i}{where}: prev {prev_balance:.2f} + amount "
                              f"{delta:+.2f} = {expected:.2f}, statement says "
                              f"{t.balance:.2f} ({t.date} {t.description[:60]})")
                    gap = _days_between(prev_date, t.date) if prev_date else 0
                    if gap > GAP_DAYS:
                        # Two causes look identical here — a real hole in the
                        # document, or rows dropped in extraction — so name both
                        # and point at the check that tells them apart (a lesson
                        # learned the hard way: don't assert "pages missing").
                        detail += (f" — {gap} days pass since the previous row, so "
                                   f"either the pages for that period are missing "
                                   f"from the document, or transactions there were "
                                   f"not extracted; check the row count against the "
                                   f"statement's own total")
                    issues.append(ValidationIssue(
                        row_index=i, kind="balance_mismatch", detail=detail))
            prev_balance = t.balance
            if prev_date is not None and t.date < prev_date:
                issues.append(ValidationIssue(
                    row_index=i, kind="date_order",
                    detail=f"row {i}{where}: {t.date} before previous {prev_date}",
                ))
            prev_date = t.date

    issues.sort(key=lambda x: x.row_index)

    mismatches = [x for x in issues if x.kind == "balance_mismatch"]
    if mismatches:
        status = "failed"
    elif issues:
        status = "passed_with_warnings"
    else:
        status = "passed"
    return ValidationReport(status=status, checked_rows=len(txns), issues=issues)
