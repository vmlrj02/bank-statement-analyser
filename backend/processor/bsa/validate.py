"""Stage 5 — Validate: the release gate.

Deterministic checks that hold for every correctly-extracted Indian bank
statement: running-balance reconciliation row by row, and date monotonicity.
"""
from __future__ import annotations

from .models import Txn, ValidationIssue, ValidationReport

TOL = 0.011


def validate(txns: list[Txn]) -> ValidationReport:
    issues: list[ValidationIssue] = []
    prev_balance = None
    prev_date = None

    for i, t in enumerate(txns):
        if prev_balance is not None:
            expected = round(prev_balance + t.amount, 2)
            if abs(expected - t.balance) > TOL:
                issues.append(ValidationIssue(
                    row_index=i, kind="balance_mismatch",
                    detail=(f"row {i}: prev {prev_balance:.2f} + amount "
                            f"{t.amount:+.2f} = {expected:.2f}, statement says "
                            f"{t.balance:.2f} ({t.date} {t.description[:60]})"),
                ))
        prev_balance = t.balance
        if prev_date is not None and t.date < prev_date:
            issues.append(ValidationIssue(
                row_index=i, kind="date_order",
                detail=f"row {i}: {t.date} before previous {prev_date}",
            ))
        prev_date = t.date

    mismatches = [x for x in issues if x.kind == "balance_mismatch"]
    if mismatches:
        status = "failed"
    elif issues:
        status = "passed_with_warnings"
    else:
        status = "passed"
    return ValidationReport(status=status, checked_rows=len(txns), issues=issues)
