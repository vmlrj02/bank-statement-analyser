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
from collections import defaultdict
from dataclasses import asdict
from datetime import date, timedelta

from openpyxl import Workbook
from openpyxl.styles import Font

from .categorize import category_detail
from .models import JobResult, Txn

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
    "Regular credit - Transfer from": "Regular credits",
    "Regular debit - Transfer to": "Regular debits",
    "Related party credit": "Regular credits",
    "Related party debit": "Regular debits",
    "Interest received": "Other Xns",
    "Investment return credited": "Other Xns",
    "return / refund": "Other Xns",
}
SHEET_ORDER = ["Summary", "EOD Balances", "EMI Xns", "ECS Xns",
               "Cash Deposit Xns", "Cash Withdrawal Xns", "Salary Paid Xns",
               "Loan Disbursed Xns", "Bounced-Penal Xns", "Outward Bounced Xns",
               "Regular credits", "Regular debits", "Other Xns"]

# Account is a column because one report may merge several banks/accounts;
# without it the Balance column is unreadable across a multi-account job.
HEADERS = ["Sl. No.", "Date", "Account", "Bank", "Cheque No.", "Description",
           "Amount", "Category", "Category Detail", "Party", "Balance"]


def _fmt_amount(a: float) -> str:
    s = f"{abs(a):,.2f}"
    return f"({s})" if a < 0 else s


def _fmt_date(iso: str) -> str:
    y, m, d = iso.split("-")
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    return f"{d}-{months[int(m)-1]}-{y[2:]}"


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
    txns = result.txns

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

    # --- category sheets ---
    sheets = {name: wb.create_sheet(name) for name in SHEET_ORDER[2:]}
    for name, ws2 in sheets.items():
        ws2.append(HEADERS)
        for c in ws2[1]:
            c.font = bold
    for i, t in enumerate(txns, start=1):
        dest = DESTINATION_SHEETS.get(t.category, "Other Xns")
        sheets[dest].append(_row(i, t))

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
