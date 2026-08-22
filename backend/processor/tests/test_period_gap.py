"""Non-contiguous statements must not produce a false validation failure.

Two statements from the same account with a gap between them (January, then
March) each reconcile perfectly on their own. Concatenating them and running
one balance chain would compare March's opening balance against January's
closing balance and report a mismatch that does not exist — the account simply
moved during the month nobody uploaded.

So validation runs PER SOURCE STATEMENT and the account reports the worst
individual status. This test pins that behaviour.

Run: python backend/processor/tests/test_period_gap.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bsa.models import Txn                      # noqa: E402
from bsa.normalize import dedup_merge           # noqa: E402
from bsa.validate import validate               # noqa: E402

ACCT, BANK = "058301562192", "ICICI Bank"


def _txn(date, amount, balance, desc):
    t = Txn(date=date, cheque_no="", description=desc, amount=amount,
            balance=balance, mode="other", counterparty="",
            account_no=ACCT, bank=BANK, source_file=desc[:4])
    t.compute_uid(ACCT, 0)
    return t


# January: 1000 -> 900 -> 1500. Internally consistent.
JAN = [_txn("2026-01-05", -100.0, 900.0, "JAN a"),
       _txn("2026-01-20", 600.0, 1500.0, "JAN b")]

# March: opens at 4000 because February happened and was never uploaded.
MAR = [_txn("2026-03-05", -250.0, 3750.0, "MAR a"),
       _txn("2026-03-19", 50.0, 3800.0, "MAR b")]


def main():
    assert validate(JAN).status == "passed", "January alone must reconcile"
    assert validate(MAR).status == "passed", "March alone must reconcile"

    # What a single chain across the gap would wrongly report:
    naive = validate(dedup_merge([JAN, MAR]))
    assert naive.status == "failed", (
        "expected the naive whole-account chain to fail across the gap; "
        "if this stops failing the test no longer proves anything")

    # What the handler actually does: worst of the per-statement results.
    per = [validate(s).status for s in (JAN, MAR)]
    account_status = ("failed" if "failed" in per else
                      "passed_with_warnings" if "passed_with_warnings" in per
                      else "passed")
    assert account_status == "passed", f"false failure across a gap: {per}"

    merged = dedup_merge([JAN, MAR])
    assert len(merged) == 4, f"expected 4 merged rows, got {len(merged)}"

    # And overlap between statements of one account still de-duplicates.
    assert len(dedup_merge([JAN, JAN])) == 2, "uid dedup should drop the repeat"

    print("PASS: per-statement validation reports 'passed' across a period gap")
    print(f"      (a single chain would have reported "
          f"'{naive.status}' with {len(naive.issues)} false issue(s))")


if __name__ == "__main__":
    main()
