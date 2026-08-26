"""The release gate. If these stop holding, a report can claim "passed" while
being wrong, which is the one failure this product cannot have."""
from bsa.models import Txn
from bsa.validate import TOL, validate


def txn(date, amount, balance, acct="1", bank="B", desc="x"):
    return Txn(date=date, cheque_no="", description=desc, amount=amount,
               balance=balance, mode="other", counterparty="",
               account_no=acct, bank=bank)


def test_clean_chain_passes():
    r = validate([txn("2026-01-01", -100, 900), txn("2026-01-02", 50, 950)])
    assert r.status == "passed"
    assert r.checked_rows == 2
    assert r.issues == []


def test_first_row_is_never_checked():
    """There is no previous balance to chain from, so an opening row alone
    cannot fail — the chain only asserts anything from the second row on."""
    assert validate([txn("2026-01-01", -100, 12345.67)]).status == "passed"


def test_broken_chain_fails():
    r = validate([txn("2026-01-01", -100, 900), txn("2026-01-02", 50, 999)])
    assert r.status == "failed"
    assert [i.kind for i in r.issues] == ["balance_mismatch"]
    assert "999.00" in r.issues[0].detail


def test_cash_credit_balance_reconciles_inverted():
    """A cash-credit / OD account prints the balance as an amount OWED, so a
    credit REDUCES it and a debit INCREASES it — the chain moves opposite to the
    money-flow sign. With balance_inverted the reconciliation subtracts, and the
    amount keeps its money-flow sign so categorisation still sees a credit as a
    credit. Pins the Axis cash-credit layout (axis_cc_statement)."""
    def cc(date, amount, balance):
        t = txn(date, amount, balance)
        t.balance_inverted = True
        return t
    # opening owed 1000; a +200 credit drops it to 800; a -50 debit lifts it to 850
    r = validate([cc("2026-01-01", 200, 1000), cc("2026-01-02", 200, 800),
                  cc("2026-01-03", -50, 850)])
    assert r.status == "passed"
    # the SAME numbers with normal (additive) reconciliation must fail, proving
    # the flag is what makes the chain close
    assert validate([txn("2026-01-01", 200, 1000),
                     txn("2026-01-02", 200, 800)]).status == "failed"


def test_a_run_of_dropped_rows_shows_as_one_mismatch():
    """Documented in CLAUDE.md gotcha 7 and worth pinning: issue COUNT is not
    a count of wrong rows. Here five rows are missing and exactly one issue is
    reported, at the point the chain resumes."""
    rows = [txn("2026-01-01", -100, 900), txn("2026-01-08", -100, 300),
            txn("2026-01-09", -100, 200)]
    r = validate(rows)
    assert r.status == "failed"
    assert len(r.issues) == 1


def test_tolerance_absorbs_rounding_but_not_a_real_error():
    assert validate([txn("2026-01-01", -100, 900),
                     txn("2026-01-02", 50, 950 + TOL / 2)]).status == "passed"
    assert validate([txn("2026-01-01", -100, 900),
                     txn("2026-01-02", 50, 950 + 0.02)]).status == "failed"


def test_out_of_order_dates_warn_but_do_not_fail():
    r = validate([txn("2026-01-05", -100, 900), txn("2026-01-02", 50, 950)])
    assert r.status == "passed_with_warnings"
    assert [i.kind for i in r.issues] == ["date_order"]


def test_each_account_is_its_own_chain():
    """Two accounts concatenated must not be compared against each other —
    otherwise every row after the first account ends reports a false failure."""
    a = [txn("2026-01-01", -100, 900, acct="A"), txn("2026-01-02", 50, 950, acct="A")]
    b = [txn("2026-01-01", -20, 4980, acct="B"), txn("2026-01-02", 10, 4990, acct="B")]
    assert validate(a + b).status == "passed"


def test_multi_account_issues_name_the_account():
    a = [txn("2026-01-01", -100, 900, acct="A"), txn("2026-01-02", 50, 111, acct="A")]
    b = [txn("2026-01-01", -20, 4980, acct="B")]
    r = validate(a + b)
    assert r.status == "failed"
    assert "[B A]" in r.issues[0].detail


def test_empty_input_is_a_pass_over_nothing():
    r = validate([])
    assert r.status == "passed" and r.checked_rows == 0


def test_a_break_across_a_long_date_gap_names_the_likely_cause():
    """Seen for real: a hand-assembled 58-page statement where November was a
    printed Gmail preview, so a whole month had no transaction pages. The
    mismatch is genuine, but "balance_mismatch" alone sends someone hunting a
    parser bug; the date gap is the tell that pages are missing."""
    r = validate([txn("2025-10-31", -100, 900),
                  txn("2025-12-01", -50, 5000)])
    assert r.status == "failed"
    assert "missing from the document" in r.issues[0].detail


def test_a_same_week_break_is_not_blamed_on_missing_pages():
    r = validate([txn("2026-01-01", -100, 900), txn("2026-01-03", 50, 5000)])
    assert r.status == "failed"
    assert "missing from the document" not in r.issues[0].detail
