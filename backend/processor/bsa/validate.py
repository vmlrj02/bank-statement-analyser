"""Stage 5 — Validate: the release gate.

Deterministic checks that hold for every correctly-extracted Indian bank
statement: running-balance reconciliation row by row, and date monotonicity.
"""
from __future__ import annotations

from .models import Txn, ValidationIssue, ValidationReport

TOL = 0.011


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
            if prev_balance is not None:
                expected = round(prev_balance + t.amount, 2)
                if abs(expected - t.balance) > TOL:
                    issues.append(ValidationIssue(
                        row_index=i, kind="balance_mismatch",
                        detail=(f"row {i}{where}: prev {prev_balance:.2f} + amount "
                                f"{t.amount:+.2f} = {expected:.2f}, statement says "
                                f"{t.balance:.2f} ({t.date} {t.description[:60]})"),
                    ))
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
