"""Stage 6 — Publish: render outputs.

- transactions.csv : flat file (Sl. No., Date, Cheque No., Description,
  Amount in accounting format, Category, Category Detail, Party, Balance)
- analysis.xlsx    : multi-sheet workbook per the lending template —
  Summary, EOD Balances, then one sheet per destination in the taxonomy
- statement.json   : full internal records + validation report
"""
from __future__ import annotations

import csv
import json
import os
import re
from collections import defaultdict
from dataclasses import asdict
from datetime import date, timedelta

from openpyxl import Workbook
from openpyxl.styles import Font

from .categorize import category_detail
from .credit_summary import credit_summary
from .integrity import account_integrity
from .models import JobResult, Txn

_CS_LABELS = [
    ("months", "Months covered", "int"),
    ("avg_monthly_credits", "Avg monthly credits (turnover)", "money"),
    ("avg_monthly_debits", "Avg monthly debits", "money"),
    ("total_credits", "Total credits", "money"),
    ("total_debits", "Total debits", "money"),
    ("avg_balance", "Average balance", "money"),
    ("min_balance", "Minimum balance", "money"),
    ("closing_balance", "Closing balance", "money"),
    ("cash_intensity_pct", "Cash intensity", "pct"),
    ("emi_outflow_monthly", "EMI / interest outflow (monthly)", "money"),
    ("bounce_count", "Bounce / return events", "int"),
    ("penal_charges", "Penal charges", "money"),
    ("loan_disbursals", "Loan disbursals received", "money"),
    ("related_party_credit_pct", "Related-party share of credits", "pct"),
    ("distinct_credit_parties", "Distinct credit counterparties", "int"),
    ("top_party_share_pct", "Top counterparty share of credits", "pct"),
]


def party_key(name: str) -> str:
    """A fuzzy key that groups truncated variants of one party — the bank
    prints "MARSCONSTRUCTI" on one row and "MARSCONSTRUCTION" on another, and
    both must land in the same group. The first 12 alphanumerics collapse both
    to "MARSCONSTRUC"."""
    return re.sub(r"[^A-Za-z0-9]", "", name or "").upper()[:12]

# taxonomy tag -> destination sheet (from Banking_pdf_extraction.xlsx)
DESTINATION_SHEETS = {
    "EMI transaction": "EMI Xns",
    "ECS transaction": "ECS Xns",
    "cash deposit": "Cash Deposit Xns",
    "cash withdrawal": "Cash Withdrawal Xns",
    "Salary paid": "Salary Paid Xns",
    "Salary credited": "Salary Paid Xns",
    "Loan amount disbursal": "Loan Disbursed Xns",
    "inward bounce penal charges": "Bounced-Penal Xns",
    "Outward Bounced Xns": "Outward Bounced Xns",
    "other penal charges": "Bounced-Penal Xns",
    "Regular credit": "Regular Credits",
    "Regular debit": "Regular Debits",
    "Related party credit": "Regular Credits",
    "Related party debit": "Regular Debits",
    "Interest received": "Other Xns",
    "Interest payments": "Other Xns",
    "Investment return credited": "Other Xns",
    "return / refund": "Other Xns",
}
# The "Group" sheets carry a leading party-group column; the others do not.
GROUPED_SHEETS = {"Regular Credits", "Regular Debits"}
SHEET_ORDER = ["EMI Xns", "ECS Xns",
               "Cash Deposit Xns", "Cash Withdrawal Xns", "Salary Paid Xns",
               "Loan Disbursed Xns", "Bounced-Penal Xns", "Outward Bounced Xns",
               "Regular Credits", "Regular Debits", "Other Xns"]

# Account is a column because one report may merge several banks/accounts;
# without it the Balance column is unreadable across a multi-account job.
HEADERS = ["Sl. No.", "Date", "Account", "Bank", "Cheque No.", "Description",
           "Amount", "Category", "Category Detail", "Party", "Balance"]


def _fmt_amount(a: float) -> str:
    s = f"{abs(a):,.2f}"
    return f"({s})" if a < 0 else s


def _fmt_date(iso: str) -> str:
    # dd-mm-yyyy throughout, per the review spec (ID3).
    y, m, d = iso.split("-")
    return f"{d}-{m}-{y}"


def _row(i: int, t: Txn) -> list:
    return [i, _fmt_date(t.date), t.account_no, t.bank, t.cheque_no,
            t.description, _fmt_amount(t.amount), t.category, category_detail(t),
            t.counterparty or "unknown party", f"{t.balance:,.2f}"]


def write_csv(result: JobResult, path: str) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADERS)
        for i, t in enumerate(result.txns, start=1):
            w.writerow(_row(i, t))


def write_json(result: JobResult, path: str) -> None:
    with open(path, "w") as f:
        json.dump({
            "meta": asdict(result.meta),
            "validation": result.validation.to_dict(),
            "transactions": [asdict(t) for t in result.txns],
        }, f, indent=1)


def _eod_balances(txns: list[Txn]) -> list[tuple[str, str, float]]:
    """(account, date, balance) carried forward — one series per account.

    A single series across several accounts would be meaningless, so each
    account gets its own run over its own date range.
    """
    if not txns:
        return []
    by_account: dict[str, dict[str, float]] = defaultdict(dict)
    labels: dict[str, str] = {}
    for t in txns:                       # last balance of each day wins
        k = f"{t.bank}|{t.account_no}"
        by_account[k][t.date] = t.balance
        labels[k] = t.account_no or t.bank or "—"

    out: list[tuple[str, str, float]] = []
    for k, by_day in by_account.items():
        d = date.fromisoformat(min(by_day))
        d1 = date.fromisoformat(max(by_day))
        cur = None
        while d <= d1:
            cur = by_day.get(d.isoformat(), cur)
            out.append((labels[k], d.isoformat(), cur))
            d += timedelta(days=1)
    return out


def write_workbook(result: JobResult, path: str) -> None:
    wb = Workbook()
    wb.remove(wb.active)
    bold = Font(bold=True)
    big = Font(bold=True, size=13)
    txns = result.txns
    integ = account_integrity([result.meta], result.validation.status)

    # --- Credit Assessment (the lender-facing lead sheet) ---
    cs = credit_summary(txns, integ, result.validation.status)
    ws = wb.create_sheet("Credit Assessment")
    ws.append(["Credit Assessment"])
    ws["A1"].font = big
    ws.append([result.meta.account_name or "", result.meta.bank or "",
               result.meta.account_no or ""])
    ws.append(["Balance reconciliation", result.validation.status,
               "Integrity", integ["assessment"]])
    from .completeness import check_completeness
    nd = sum(1 for t in txns if t.amount < 0)
    nc = sum(1 for t in txns if t.amount > 0)
    comp = check_completeness(len(txns), nd, nc,
                              getattr(result.meta, "declared_totals", None) or {})
    if comp.get("checked"):
        ws.append(["Completeness",
                   "complete" if comp["complete"] else "INCOMPLETE",
                   "; ".join(comp.get("notes", [])) or
                   f"{len(txns)} of {comp['declared']} declared"])
    ws.append([])
    ws.append(["Metric", "Value"])
    for c in ws[ws.max_row]:
        c.font = bold
    m = cs["metrics"]
    for key, label, kind in _CS_LABELS:
        v = m.get(key)
        if kind == "pct":
            v = f"{v}%"
        elif kind == "money" and isinstance(v, (int, float)):
            v = _fmt_amount(v)
        ws.append([label, v])
    ws.append([])
    ws.append(["Underwriting reads"])
    ws[ws.max_row][0].font = bold
    for r in cs["reads"]:
        ws.append(["", r])

    # --- Summary ---
    ws = wb.create_sheet("Summary")
    agg: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
    monthly: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for t in txns:
        agg[t.category][0] += 1
        agg[t.category][1] += t.amount
        mk = t.date[:7]
        if t.amount > 0:
            monthly[mk][0] += t.amount
        else:
            monthly[mk][1] += -t.amount
    # One row per account in the job, so a multi-bank merge is legible.
    accounts: dict[str, list] = {}
    for t in txns:
        k = f"{t.bank}|{t.account_no}"
        a = accounts.setdefault(k, [t.bank, t.account_no, t.date, t.date, 0])
        a[2] = min(a[2], t.date)
        a[3] = max(a[3], t.date)
        a[4] += 1
    ws.append(["Bank", "Account", "From", "To", "Transactions"])
    for c in ws[1]:
        c.font = bold
    for a in accounts.values():
        ws.append(a)
    ws.append([])
    ws.append(["Validation", result.validation.status,
               f"{result.validation.checked_rows} rows checked",
               f"{len(result.validation.issues)} issues"])
    # --- Integrity (statement authenticity signals) ---
    integ = account_integrity([result.meta], result.validation.status)
    ws.append([])
    ws.append(["Integrity", integ["assessment"].upper()])
    ws[ws.max_row][0].font = bold
    ws.append(["PDF producer", result.meta.producer or "—",
               "Created", result.meta.pdf_created or "—",
               "Modified", result.meta.pdf_modified or "—"])
    for flag in integ["flags"]:
        ws.append(["", flag])
    ws.append([])
    hdr_row = ws.max_row + 1
    ws.append(["Category", "Count", "Net Amount"])
    for c in ws[hdr_row]:
        c.font = bold
    for tag in sorted(agg, key=lambda k: -abs(agg[k][1])):
        ws.append([tag, agg[tag][0], round(agg[tag][1], 2)])
    ws.append([])
    ws.append(["Month", "Total Credits", "Total Debits", "Net"])
    for c in ws[ws.max_row]:
        c.font = bold
    for mk in sorted(monthly):
        cr, dr = monthly[mk]
        ws.append([mk, round(cr, 2), round(dr, 2), round(cr - dr, 2)])

    # --- EOD Balances ---
    ws = wb.create_sheet("EOD Balances")
    ws.append(["Account", "Date", "EOD Balance"])
    for c in ws[1]:
        c.font = bold
    for acct, d, b in _eod_balances(txns):
        ws.append([acct, d, b])

    # --- category sheets (some carry a leading party Group column) ---
    sheets = {name: wb.create_sheet(name) for name in SHEET_ORDER}
    for name, ws2 in sheets.items():
        ws2.append((["Group"] if name in GROUPED_SHEETS else []) + HEADERS)
        for c in ws2[1]:
            c.font = bold
    # Group rows in the grouped sheets by fuzzy party, keeping the fullest
    # party name seen as the group's label, so truncated variants collapse.
    group_label: dict[str, str] = {}
    for t in txns:
        k = party_key(t.counterparty)
        if k and len(t.counterparty or "") > len(group_label.get(k, "")):
            group_label[k] = t.counterparty
    for i, t in enumerate(txns, start=1):
        dest = DESTINATION_SHEETS.get(t.category, "Other Xns")
        row = _row(i, t)
        if dest in GROUPED_SHEETS:
            row = [group_label.get(party_key(t.counterparty), t.counterparty
                                   or "unknown party")] + row
        sheets[dest].append(row)

    # --- all transactions, and the Sunday subset ---
    for name, keep in (("Xns", lambda t: True),
                       ("SundayXns", lambda t: date.fromisoformat(t.date).weekday() == 6)):
        ws = wb.create_sheet(name)
        ws.append(HEADERS)
        for c in ws[1]:
            c.font = bold
        for i, t in enumerate(txns, start=1):
            if keep(t):
                ws.append(_row(i, t))

    # --- Top 10 parties, by direction, aggregated with the fuzzy key ---
    for name, want_credit in (("Top 10 Party Credits", True),
                              ("Top 10 Party Debits", False)):
        by_party: dict[str, float] = defaultdict(float)
        for t in txns:
            if (t.amount > 0) == want_credit and t.counterparty:
                by_party[group_label.get(party_key(t.counterparty),
                                         t.counterparty)] += abs(t.amount)
        ws = wb.create_sheet(name)
        ws.append(["Party", "Total Amount", "Count"])
        for c in ws[1]:
            c.font = bold
        counts: dict[str, int] = defaultdict(int)
        for t in txns:
            if (t.amount > 0) == want_credit and t.counterparty:
                counts[group_label.get(party_key(t.counterparty),
                                       t.counterparty)] += 1
        for party in sorted(by_party, key=lambda p: -by_party[p])[:10]:
            ws.append([party, round(by_party[party], 2), counts[party]])

    # --- Top 10 single transactions, by direction ---
    for name, want_credit in (("Top 10 Credits(Consolidated)", True),
                              ("Top 10 Debits(Consolidated)", False)):
        ws = wb.create_sheet(name)
        ws.append(["Date", "Description", "Party", "Amount"])
        for c in ws[1]:
            c.font = bold
        ranked = sorted((t for t in txns if (t.amount > 0) == want_credit),
                        key=lambda t: -abs(t.amount))[:10]
        for t in ranked:
            ws.append([_fmt_date(t.date), t.description,
                       t.counterparty or "unknown party", _fmt_amount(t.amount)])

    # --- Average balances + monthly flow ---
    ws = wb.create_sheet("Avg Balances")
    ws.append(["Month", "Average Balance", "Inflow", "Outflow", "Net Flow"])
    for c in ws[1]:
        c.font = bold
    eod = _eod_balances(txns)
    bal_by_month: dict[str, list[float]] = defaultdict(list)
    for _acct, d, b in eod:
        if b is not None:
            bal_by_month[d[:7]].append(b)
    for mk in sorted(monthly):
        bals = bal_by_month.get(mk, [])
        avg = round(sum(bals) / len(bals), 2) if bals else ""
        cr, dr = monthly[mk]
        ws.append([mk, avg, round(cr, 2), round(dr, 2), round(cr - dr, 2)])

    wb.save(path)


def publish(result: JobResult, out_dir: str, basename: str = "statement") -> dict[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    paths = {
        "csv": os.path.join(out_dir, f"{basename}_transactions.csv"),
        "xlsx": os.path.join(out_dir, f"{basename}_analysis.xlsx"),
        "json": os.path.join(out_dir, f"{basename}.json"),
    }
    write_csv(result, paths["csv"])
    write_workbook(result, paths["xlsx"])
    write_json(result, paths["json"])
    return paths
