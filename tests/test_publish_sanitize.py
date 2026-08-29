"""A control character in ANY cell must not kill the workbook.

Seen in production: a narration carrying control bytes reached publish and
openpyxl refused the cell ("cannot be used in worksheets"), failing the whole
merge. normalize scrubs at the source, but publish is the last line — every
worksheet append is routed through a sanitiser, and this pins it.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "backend", "processor"))

from bsa.models import JobResult, StatementMeta, Txn, ValidationReport  # noqa: E402
from bsa.publish import write_workbook  # noqa: E402


def _txn(desc, amount, balance):
    return Txn(date="2026-01-05", cheque_no="", description=desc,
               amount=amount, balance=balance, mode="neft",
               counterparty="ACME \x02TRADERS", category="Regular debit",
               account_no="XX1234", bank="HDFC Bank", source_file="s.pdf")


def test_control_characters_do_not_kill_the_workbook(tmp_path):
    meta = StatementMeta(bank="HDFC Bank", layout="hdfc", account_no="XX1234",
                         account_name="M/S \x01STEEL\x1f", period_from="2026-01-01",
                         period_to="2026-01-31", source_file="s.pdf")
    txns = [_txn("NEFT \x00£êÈ\x07 JUNK/NARRATION", -100.0, 900.0),
            _txn("CASH DEP \x0b OK", 500.0, 1400.0)]
    result = JobResult(meta=meta, txns=txns,
                       validation=ValidationReport(status="passed",
                                                   checked_rows=2, issues=[]))
    out = str(tmp_path / "analysis.xlsx")
    write_workbook(result, out)          # must not raise IllegalCharacterError
    assert os.path.getsize(out) > 0
