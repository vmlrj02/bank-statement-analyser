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


def test_existing_keys_are_still_present():
    """handler/publish read these keys — additive changes only."""
    txns = [txn("2025-01-05", 100000, 100000)]
    m = credit_summary(txns)["metrics"]
    for key in ["months", "total_credits", "total_debits", "net_cashflow",
                "avg_monthly_credits", "avg_monthly_debits", "avg_balance",
                "min_balance", "closing_balance", "cash_deposits",
                "cash_intensity_pct", "emi_outflow", "emi_outflow_monthly",
                "bounce_count", "penal_charges", "loan_disbursals",
                "related_party_credit_pct", "distinct_credit_parties",
                "top_party_share_pct", "turnover_trend", "balance_stability_cv",
                "monthly_surplus", "servicing_coverage", "integrity",
                "balance_verified"]:
        assert key in m, key


# --- month-by-month turnover -------------------------------------------------

def test_monthly_turnover_series():
    txns = [txn("2025-01-05", 100000, 100000),
            txn("2025-01-20", -40000, 60000, cat="Regular debit"),
            txn("2025-02-05", 80000, 140000)]
    m = credit_summary(txns)["metrics"]
    assert m["monthly_turnover"] == [
        {"month": "2025-01", "credits": 100000, "turnover": 100000,
         "debits": 40000, "net": 60000},
        {"month": "2025-02", "credits": 80000, "turnover": 80000,
         "debits": 0, "net": 80000}]


def test_monthly_turnover_excludes_loans_and_salary():
    """TURNOVER is business credits. A loan disbursal and a salary credit are
    inflows, so they raise `credits`, but neither is revenue — so neither may
    move `turnover`. This is the whole definition, pinned on the series a
    lender actually reads month by month."""
    txns = [txn("2025-01-05", 100000, 100000),
            txn("2025-01-10", 500000, 600000, cat="Loan amount disbursal"),
            txn("2025-01-15", 50000, 650000, cat="Salary credited")]
    m = credit_summary(txns)["metrics"]
    jan = m["monthly_turnover"][0]
    assert jan["credits"] == 650000        # every inflow
    assert jan["turnover"] == 100000       # business credits only
    assert m["business_credits"] == 100000
    # And nothing derived from turnover may inherit the borrowed money.
    assert m["avg_monthly_business_credits"] == 100000


def test_servicing_coverage_ignores_loan_disbursals():
    """Debt-service coverage is turnover ÷ EMI outflow. Counting a loan
    disbursal as capacity to repay loans is circular — it flatters exactly the
    most leveraged borrower. One month, 100k of trade, a 500k disbursal and
    50k of EMI: coverage is 2x, not 12x."""
    txns = [txn("2025-01-05", 100000, 100000),
            txn("2025-01-10", 500000, 600000, cat="Loan amount disbursal"),
            txn("2025-01-20", -50000, 550000, cat="EMI transaction")]
    m = credit_summary(txns)["metrics"]
    assert m["servicing_coverage"] == 2.0


def test_cash_intensity_is_a_share_of_turnover():
    """Cash deposits ARE business credits, so intensity is measured against
    turnover. A loan disbursal in the denominator would dilute a cash trader's
    intensity and hide the very risk the number exists to show."""
    txns = [txn("2025-01-05", 60000, 60000, cat="cash deposit"),
            txn("2025-01-06", 40000, 100000),
            txn("2025-01-10", 400000, 500000, cat="Loan amount disbursal")]
    m = credit_summary(txns)["metrics"]
    assert m["cash_intensity_pct"] == 60.0


def test_last_quarter_decline_read():
    txns, bal = [], 0.0
    for mo, amt in [("01", 100000), ("02", 100000), ("03", 100000),
                    ("04", 30000), ("05", 30000), ("06", 30000)]:
        bal += amt
        txns.append(txn(f"2025-{mo}-05", amt, bal))
    out = credit_summary(txns)
    assert out["metrics"]["last_quarter_credit_change_pct"] == -70.0
    assert any("down 70.0%" in r for r in out["reads"])


def test_last_quarter_read_needs_a_real_decline_and_six_months():
    # flat six months: metric is 0, read does not fire
    txns, bal = [], 0.0
    for mo in ["01", "02", "03", "04", "05", "06"]:
        bal += 100000
        txns.append(txn(f"2025-{mo}-05", 100000, bal))
    out = credit_summary(txns)
    assert out["metrics"]["last_quarter_credit_change_pct"] == 0.0
    assert not any("last quarter average" in r for r in out["reads"])
    # five months: too short to call a quarter trend at all
    out5 = credit_summary(txns[:5])
    assert out5["metrics"]["last_quarter_credit_change_pct"] is None


# --- EMI obligations ---------------------------------------------------------

def test_emi_obligation_list_party_amount_months_active():
    txns = [txn("2025-01-02", 300000, 300000)]
    bal = 300000.0
    for mo in ["01", "02", "03"]:                      # Bajaj: monthly, active
        bal -= 12000
        txns.append(txn(f"2025-{mo}-05", -12000, bal,
                        cat="EMI transaction", party="Bajaj Finance"))
    bal -= 8000                                        # HDFC: Jan only, lapsed
    txns.append(txn("2025-01-10", -8000, bal,
                    cat="EMI transaction", party="HDFC Ltd"))
    txns.append(txn("2025-03-20", 50000, bal + 50000))  # period end 2025-03-20
    txns.sort(key=lambda t: t.date)
    m = credit_summary(txns)["metrics"]
    assert len(m["emi_obligations"]) == 2
    bajaj, hdfc = m["emi_obligations"]                 # sorted by monthly desc
    assert bajaj["party"] == "Bajaj Finance"
    assert bajaj["monthly_amount"] == 12000
    assert bajaj["months_seen"] == 3
    assert bajaj["last_seen"] == "2025-03-05"
    assert bajaj["total_paid"] == 36000
    assert bajaj["active"] is True
    assert hdfc["party"] == "HDFC Ltd"
    assert hdfc["active"] is False                     # last seen > 45d before end
    assert m["active_emi_obligations"] == 1
    reads = " ".join(credit_summary(txns)["reads"])
    assert "1 EMI obligation(s) active at period end (~12000/mo combined)" in reads


def test_new_emi_obligation_counted_only_when_window_observable():
    txns = [txn("2025-01-02", 500000, 500000)]
    bal = 500000.0
    for mo in ["01", "02", "03", "04", "05", "06", "07"]:   # old obligation
        bal -= 12000
        txns.append(txn(f"2025-{mo}-05", -12000, bal,
                        cat="EMI transaction", party="Bajaj Finance"))
    for mo in ["06", "07"]:                                 # appears in June
        bal -= 9000
        txns.append(txn(f"2025-{mo}-10", -9000, bal,
                        cat="EMI transaction", party="Tata Capital"))
    txns.sort(key=lambda t: t.date)
    out = credit_summary(txns)
    assert out["metrics"]["active_emi_obligations"] == 2
    assert out["metrics"]["new_emi_obligations_last_quarter"] == 1
    assert any("2 EMI obligation(s) active" in r and
               "1 added in the last quarter" in r for r in out["reads"])
    # a statement covering ONLY the last quarter cannot claim "added recently"
    short = [t for t in txns if t.date >= "2025-06-01"]
    assert credit_summary(short)["metrics"]["new_emi_obligations_last_quarter"] == 0


# --- bounce trend ------------------------------------------------------------

def test_bounce_trend_worsening_read():
    txns = [txn("2025-01-05", 200000, 200000)]
    bal = 200000.0
    for d in ["2025-05-10", "2025-06-01", "2025-06-15"]:
        bal -= 500
        txns.append(txn(d, -500, bal, cat="Outward Bounced Xns"))
    txns.append(txn("2025-06-30", 50000, bal + 50000))   # period end
    out = credit_summary(txns)
    assert out["metrics"]["bounces_last_90d"] == 3
    assert out["metrics"]["bounces_prior_90d"] == 0
    assert any("Bounces are increasing: 3 in the last 90 days vs 0" in r
               for r in out["reads"])


def test_single_recent_bounce_does_not_claim_a_trend():
    txns = [txn("2025-01-05", 200000, 200000),
            txn("2025-06-15", -500, 199500, cat="Outward Bounced Xns"),
            txn("2025-06-30", 50000, 249500)]
    out = credit_summary(txns)
    assert out["metrics"]["bounces_last_90d"] == 1
    assert any("bounce/return event" in r for r in out["reads"])
    assert not any("Bounces are increasing" in r for r in out["reads"])


# --- balance floor -----------------------------------------------------------

def test_low_balance_floor_metrics_and_read():
    txns = [txn("2025-01-01", 30000, 30000),
            txn("2025-01-11", -25000, 5000, cat="Regular debit"),
            txn("2025-01-16", 25000, 30000),
            txn("2025-01-20", 10000, 40000)]
    # EOD: 10d @30k, 5d @5k, 4d @30k, 1d @40k -> avg 24250, threshold 10000
    m = credit_summary(txns)["metrics"]
    assert m["low_balance_threshold"] == 10000.0
    assert m["low_balance_days_pct"] == 25.0
    assert m["longest_low_balance_streak"] == 5
    assert any("thin liquidity buffer" in r for r in credit_summary(txns)["reads"])


def test_healthy_balance_has_no_floor_read():
    txns = [txn("2025-01-01", 50000, 50000),
            txn("2025-01-20", 20000, 70000)]
    m = credit_summary(txns)["metrics"]
    assert m["low_balance_days_pct"] == 0.0
    assert m["longest_low_balance_streak"] == 0
    assert not any("liquidity buffer" in r for r in credit_summary(txns)["reads"])


def test_od_account_skips_floor_check():
    txns = [txn("2025-01-05", 100000, -400000),
            txn("2025-01-20", -50000, -450000, cat="Regular debit")]
    m = credit_summary(txns)["metrics"]
    assert m["low_balance_threshold"] is None
    assert m["low_balance_days_pct"] is None
    assert not any("liquidity buffer" in r for r in credit_summary(txns)["reads"])


# --- two-way flows -----------------------------------------------------------

def test_two_way_party_hint():
    txns = [txn("2025-01-05", 500000, 500000, party="Sharma Traders"),
            txn("2025-01-08", 100000, 600000, party="Verma Exports"),
            txn("2025-01-20", -450000, 150000, cat="Regular debit",
                party="Sharma Traders")]
    out = credit_summary(txns)
    assert out["metrics"]["two_way_parties"] == [
        {"party": "Sharma Traders", "credits": 500000, "debits": 450000}]
    assert any("Funds move both ways" in r for r in out["reads"])


def test_one_way_party_is_not_flagged():
    txns = [txn("2025-01-05", 500000, 500000, party="Sharma Traders"),
            txn("2025-01-20", -450000, 50000, cat="Regular debit",
                party="Landlord Realty")]
    out = credit_summary(txns)
    assert out["metrics"]["two_way_parties"] == []
    assert not any("Funds move both ways" in r for r in out["reads"])


def test_small_two_way_amounts_do_not_qualify():
    # both directions exist but neither clears the large-transaction bar
    txns = [txn("2025-01-05", 5000000, 5000000, party="Anchor Customer"),
            txn("2025-01-10", 20000, 5020000, party="Sharma Traders"),
            txn("2025-01-20", -20000, 5000000, cat="Regular debit",
                party="Sharma Traders")]
    assert credit_summary(txns)["metrics"]["two_way_parties"] == []


# --- inflow concentration ----------------------------------------------------

def test_top3_share_and_moderate_concentration_read():
    txns = [txn("2025-01-05", 45000, 45000, party="Alpha Co"),
            txn("2025-01-06", 30000, 75000, party="Beta Co"),
            txn("2025-01-07", 15000, 90000, party="Gamma Co"),
            txn("2025-01-08", 10000, 100000, party="Delta Co")]
    out = credit_summary(txns)
    m = out["metrics"]
    assert m["top_party_share_pct"] == 45.0
    assert m["top3_party_share_pct"] == 90.0
    assert m["top_credit_parties"][0] == {"party": "Alpha Co", "amount": 45000,
                                          "share_pct": 45.0}
    assert any("revenue leans on one" in r for r in out["reads"])
    assert not any("depends heavily on one source" in r for r in out["reads"])


def test_heavy_concentration_keeps_the_existing_read_only():
    txns = [txn("2025-01-05", 60000, 60000, party="Alpha Co"),
            txn("2025-01-06", 40000, 100000, party="Beta Co")]
    reads = credit_summary(txns)["reads"]
    assert any("depends heavily on one source" in r for r in reads)
    assert not any("revenue leans on one" in r for r in reads)


# --- smoke over real statements (structure only, no golden numbers) ----------

import os                                                          # noqa: E402
from pathlib import Path                                           # noqa: E402

import pytest                                                      # noqa: E402

SAMPLE_DIR = os.environ.get("BSA_SAMPLE_DIR", "/Users/vee/Downloads/Samples-2")

_NEW_KEYS = ["monthly_turnover", "last_quarter_credit_change_pct",
             "emi_obligations", "active_emi_obligations",
             "new_emi_obligations_last_quarter", "bounces_last_90d",
             "bounces_prior_90d", "low_balance_threshold",
             "low_balance_days_pct", "longest_low_balance_streak",
             "two_way_parties", "top3_party_share_pct", "top_credit_parties"]


@pytest.mark.skipif(not Path(SAMPLE_DIR).is_dir(),
                    reason="no sample statements on this machine")
def test_real_statements_smoke():
    """Every metric must come out with the right SHAPE on real statements —
    values are the statement's business, so no golden numbers here."""
    from bsa.categorize import categorize
    from bsa.normalize import normalize
    from bsa.pipeline import extract_one

    checked = 0
    # smallest files first: quick to parse, and passworded/unknown ones skip
    for pdf in sorted(Path(SAMPLE_DIR).rglob("*.pdf"),
                      key=lambda p: p.stat().st_size):
        try:
            extract = extract_one(str(pdf))
            txns = normalize(extract)
            if not txns:
                continue
            categorize(txns)
        except Exception:      # password, no layout, scan — not this test's job
            continue
        out = credit_summary(txns)
        m, reads = out["metrics"], out["reads"]
        for key in _NEW_KEYS:
            assert key in m, f"{pdf.name}: missing {key}"
        # the monthly series must tie back to the totals exactly
        assert round(sum(x["credits"] for x in m["monthly_turnover"]), 2) == \
            m["total_credits"], pdf.name
        assert round(sum(x["debits"] for x in m["monthly_turnover"]), 2) == \
            m["total_debits"], pdf.name
        assert [x["month"] for x in m["monthly_turnover"]] == \
            sorted(x["month"] for x in m["monthly_turnover"]), pdf.name
        for o in m["emi_obligations"]:
            assert set(o) == {"party", "monthly_amount", "months_seen",
                              "first_seen", "last_seen", "total_paid", "active"}
            assert o["monthly_amount"] > 0 and o["months_seen"] >= 1, pdf.name
        assert m["active_emi_obligations"] <= len(m["emi_obligations"]), pdf.name
        assert 0 <= m["top3_party_share_pct"] <= 100.0, pdf.name
        assert m["top_party_share_pct"] <= m["top3_party_share_pct"] or \
            not m["top_credit_parties"], pdf.name
        if m["low_balance_days_pct"] is not None:
            assert 0 <= m["low_balance_days_pct"] <= 100.0, pdf.name
        assert reads and all(isinstance(r, str) and r for r in reads), pdf.name
        checked += 1
        if checked == 3:
            break
    if checked < 2:
        pytest.skip("fewer than 2 passwordless parseable samples found")
