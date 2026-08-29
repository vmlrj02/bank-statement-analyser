"""The workbook's STRUCTURE is the product — pin it.

publish.write_workbook renders the lending template: "Credit Assessment" must
be the FIRST sheet (the lender-facing conclusion a credit team reads before the
working), followed by "Summary" and "EOD Balances", then one sheet per
destination in the SME taxonomy. Only the sanitiser had a test; a rewrite could
silently drop a sheet, reorder the lead sheet, or stop writing transaction rows
and nothing would fail until a customer opened the file.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "backend", "processor"))

from openpyxl import load_workbook  # noqa: E402

from bsa.models import JobResult, StatementMeta, Txn, ValidationReport  # noqa: E402
from bsa.publish import SHEET_ORDER, write_workbook  # noqa: E402


def _txn(desc, amount, balance, category, date):
    return Txn(date=date, cheque_no="", description=desc,
               amount=amount, balance=balance, mode="neft",
               counterparty="ACME TRADERS", category=category,
               account_no="XX1234", bank="HDFC Bank", source_file="s.pdf")


def _result():
    meta = StatementMeta(bank="HDFC Bank", layout="hdfc", account_no="XX1234",
                         account_name="M/S STEEL", period_from="2026-01-01",
                         period_to="2026-01-31", source_file="s.pdf")
    txns = [
        _txn("CASH DEP BRANCH", 500.0, 1400.0, "cash deposit", "2026-01-05"),
        _txn("ACH D- BAJAJ FINANCE EMI", -200.0, 1200.0,
             "EMI transaction", "2026-01-06"),
        _txn("NEFT TO ACME TRADERS", -100.0, 1100.0,
             "Regular debit", "2026-01-07"),
    ]
    return JobResult(meta=meta, txns=txns,
                     validation=ValidationReport(status="passed",
                                                 checked_rows=3, issues=[]))


def _load(tmp_path):
    out = str(tmp_path / "analysis.xlsx")
    write_workbook(_result(), out)
    return load_workbook(out)


def test_credit_assessment_leads_then_summary_and_eod(tmp_path):
    wb = _load(tmp_path)
    # The lead sheet is the whole point of the workbook: the credit conclusion
    # first, the categorised working after it.
    assert wb.sheetnames[0] == "Credit Assessment"
    assert wb.sheetnames[1] == "Summary"
    assert wb.sheetnames[2] == "EOD Balances"


def test_every_taxonomy_destination_sheet_exists(tmp_path):
    wb = _load(tmp_path)
    for name in SHEET_ORDER:
        assert name in wb.sheetnames, f"taxonomy sheet {name!r} missing"


def test_transactions_land_on_the_all_transactions_sheet(tmp_path):
    wb = _load(tmp_path)
    ws = wb["Xns"]
    # header + one row per input transaction
    assert ws.max_row == 1 + 3
    # Description is column 6 of HEADERS; row 2 is the first data row.
    assert ws.cell(row=2, column=6).value == "CASH DEP BRANCH"


def test_rows_route_to_their_category_sheets(tmp_path):
    wb = _load(tmp_path)
    assert wb["Cash Deposit Xns"].max_row == 2      # header + the deposit
    assert wb["EMI Xns"].max_row == 2               # header + the EMI
    emi = wb["EMI Xns"]
    assert emi.cell(row=2, column=6).value == "ACH D- BAJAJ FINANCE EMI"
    # Regular Debits is a GROUPED sheet: a leading party-group column shifts
    # everything right by one, so Description sits in column 7.
    rd = wb["Regular Debits"]
    assert rd.max_row == 2
    assert rd.cell(row=1, column=1).value == "Group"
    assert rd.cell(row=2, column=1).value == "ACME TRADERS"
    assert rd.cell(row=2, column=7).value == "NEFT TO ACME TRADERS"


def test_eod_balances_carry_forward_per_day(tmp_path):
    wb = _load(tmp_path)
    ws = wb["EOD Balances"]
    rows = [(r[1].value, r[2].value)
            for r in ws.iter_rows(min_row=2) if r[1].value]
    # One row per calendar day of the account's range, last balance of the day.
    assert ("2026-01-05", 1400.0) in rows
    assert ("2026-01-07", 1100.0) in rows
