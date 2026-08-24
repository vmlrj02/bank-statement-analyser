"""The credit-assessment metrics + underwriting reads. These are what a lender
decides on, so they must be arithmetic we can defend and flags that only fire
when the number warrants."""
from bsa.credit_summary import credit_summary
from bsa.models import Txn


def txn(date, amount, balance, cat="Regular credit", party="", acct="1"):
    t = Txn(date=date, cheque_no="", description="d", amount=amount, balance=balance,
            mode="other", counterparty=party, account_no=acct, bank="B")
    t.category = cat
    return t


def test_turnover_and_balances():
    txns = [txn("2025-01-05", 100000, 100000),
            txn("2025-01-20", -40000, 60000, cat="Regular debit"),
            txn("2025-02-05", 80000, 140000)]
    m = credit_summary(txns)["metrics"]
    assert m["months"] == 2
    assert m["total_credits"] == 180000
    assert m["avg_monthly_credits"] == 90000
    assert m["closing_balance"] == 140000


def test_bounce_and_emi_reads_fire_only_when_warranted():
    quiet = [txn("2025-01-05", 100000, 100000)]
    assert any("No adverse" in r for r in credit_summary(quiet)["reads"])

    risky = [txn("2025-01-05", 100000, 100000),
             txn("2025-01-10", -1000, 99000, cat="Outward Bounced Xns"),
             txn("2025-01-15", -60000, 39000, cat="EMI transaction")]
    reads = " ".join(credit_summary(risky)["reads"])
    assert "bounce" in reads.lower()
    assert "EMI" in reads


def test_cash_intensity_flag():
    txns = [txn("2025-01-05", 60000, 60000, cat="cash deposit"),
            txn("2025-01-06", 40000, 100000, cat="Regular credit")]
    m = credit_summary(txns)["metrics"]
    assert m["cash_intensity_pct"] == 60.0
    assert any("Cash-intensive" in r for r in credit_summary(txns)["reads"])


def test_a_failed_balance_is_called_out():
    txns = [txn("2025-01-05", 100000, 100000)]
    reads = credit_summary(txns, validation_status="failed")["reads"]
    assert any("does not reconcile" in r for r in reads)


def test_empty_is_safe():
    assert credit_summary([]) == {"metrics": {}, "reads": []}
