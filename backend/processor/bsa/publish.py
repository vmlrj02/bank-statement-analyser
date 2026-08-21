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

HEADERS = ["Sl. No.", "Date", "Cheque No.", "Description", "Amount",
           "Category", "Category Detail", "Party", "Balance"]


def _fmt_amount(a: float) -> str:
    s = f"{abs(a):,.2f}"
    return f"({s})" if a < 0 else s


def _fmt_date(iso: str) -> str:
    y, m, d = iso.split("-")
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    return f"{d}-{months[int(m)-1]}-{y[2:]}"


def _row(i: int, t: Txn) -> list:
    return [i, _fmt_date(t.date), t.cheque_no, t.description,
            _fmt_amount(t.amount), t.category, category_detail(t),
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


def _eod_balances(txns: list[Txn]) -> list[tuple[str, float]]:
    if not txns:
        return []
    by_day: dict[str, float] = {}
    for t in txns:                       # last balance of each day wins
        by_day[t.date] = t.balance
    d0 = date.fromisoformat(min(by_day))
    d1 = date.fromisoformat(max(by_day))
    out, cur = [], None
    d = d0
    while d <= d1:
        cur = by_day.get(d.isoformat(), cur)
        out.append((d.isoformat(), cur))
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
    ws.append(["Account", result.meta.account_no, result.meta.account_name])
    ws.append(["Bank", result.meta.bank, result.meta.layout])
    ws.append(["Period", result.meta.period_from, result.meta.period_to])
    ws.append(["Validation", result.validation.status,
               f"{result.validation.checked_rows} rows checked"])
    ws.append([])
    ws.append(["Category", "Count", "Net Amount"])
    for c in ws[6]:
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
    ws.append(["Date", "EOD Balance"])
    for c in ws[1]:
        c.font = bold
    for d, b in _eod_balances(txns):
        ws.append([d, b])

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
