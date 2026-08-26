"""Canonical internal data model.

Every extractor (template or LLM) must emit StatementExtract; every later
stage consumes/enriches it. The target CSV is a *rendering* of this model.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class RawRow:
    """One transaction as the extractor saw it, pre-normalization."""
    sl_no: Optional[str]
    date: str                     # as printed, e.g. "03.07.2025"
    cheque_no: str
    description: str              # title + descriptor lines joined
    withdrawal: Optional[float]   # None if blank
    deposit: Optional[float]
    balance: float
    page: int
    # A "balance brought forward" / opening row: it carries a balance but no
    # transaction. It RE-BASES the running chain (a multi-period statement prints
    # each month's own B/F, and the periods need not be contiguous — gotcha 12),
    # so validate anchors to it instead of checking against the previous period.
    is_opening: bool = False
    # Cash-credit / overdraft accounts print the balance as an amount OWED, so a
    # credit REDUCES it and a debit INCREASES it — the running balance moves
    # opposite to the money-flow sign. Set from the layout so reconciliation
    # subtracts instead of adds; the money-flow sign of the amount is unchanged,
    # so categorisation still sees a credit as a credit.
    balance_inverted: bool = False
    # Some exports print WIDE numbers with the last decimal clipped by the column
    # (IndusInd shows a true 15,339,837.84 as "15339837.8"), so the printed chain
    # is imprecise by up to ~0.1 and no parser can recover the lost digit. A
    # layout may raise the per-row reconciliation tolerance to absorb exactly
    # that display artifact — never enough to hide a dropped row.
    balance_tolerance: float = 0.0


@dataclass
class StatementMeta:
    bank: str
    layout: str
    account_no: str
    account_name: str
    period_from: str              # ISO date
    period_to: str
    source_file: str
    currency: str = "INR"
    is_digital_text: bool = True
    n_pages: int = 0              # captured at ingest, reported per account
    # 1-based pages with no text layer. A template reads nothing from these,
    # so any transactions printed on them are absent from the extract — which
    # is why a balance chain can break with no other sign of a problem.
    unreadable_pages: list = field(default_factory=list)
    # Populated only on the LLM path: {provider, model, tokens_in, tokens_out,
    # calls, cost_usd}. None means this statement was parsed by a template and
    # cost nothing, which is the distinction the admin view reports.
    llm_usage: Optional[dict] = None
    # PDF /Info metadata, carried for the integrity check (see integrity.py).
    producer: str = ""
    creator: str = ""
    pdf_created: str = ""
    pdf_modified: str = ""
    # The statement's own declared transaction counts, when it prints them
    # (e.g. HDFC's Dr/Cr count). Used to prove no rows were silently dropped.
    declared_totals: dict = None


@dataclass
class StatementExtract:
    meta: StatementMeta
    rows: list[RawRow]


@dataclass
class Txn:
    """Normalized, enriched transaction."""
    date: str                     # ISO yyyy-mm-dd
    cheque_no: str
    description: str
    amount: float                 # signed: negative = debit
    balance: float
    mode: str                     # upi | neft | rtgs | imps | nach | billpay | atm-cash | ...
    counterparty: str             # extracted display name, may be ""
    category: str = ""            # filled by categorize stage
    category_source: str = ""     # rule | counterparty | dictionary | llm | fallback
    confidence: str = "high"      # high | medium | low — how sure the tag is; a
                                  # low row is surfaced for human review, never
                                  # presented as a certain answer
    page: int = 0
    # Account identity travels with the row: one job may merge statements from
    # several banks/accounts, and a balance chain is only meaningful per account.
    account_no: str = ""
    bank: str = ""
    source_file: str = ""
    uid: str = ""
    is_duplicate: bool = False
    # See RawRow.balance_inverted — carried through so validate reconciles a
    # cash-credit/overdraft chain by subtracting the amount instead of adding it.
    balance_inverted: bool = False
    balance_tolerance: float = 0.0   # see RawRow.balance_tolerance
    is_opening: bool = False         # see RawRow.is_opening — rebases the chain

    def compute_uid(self, account_no: str, occurrence: int) -> None:
        key = f"{account_no}|{self.date}|{self.description}|{self.amount:.2f}|{self.balance:.2f}|{occurrence}"
        self.uid = hashlib.sha256(key.encode()).hexdigest()[:16]


@dataclass
class ValidationIssue:
    row_index: int
    kind: str                     # balance_mismatch | date_order | ...
    detail: str


@dataclass
class ValidationReport:
    status: str                   # passed | passed_with_warnings | failed
    checked_rows: int = 0
    issues: list[ValidationIssue] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


@dataclass
class JobResult:
    meta: StatementMeta
    txns: list[Txn]
    validation: ValidationReport
