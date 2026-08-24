"""Credit assessment — the lender-facing conclusion, not just categorised rows.

A statement analysis is only useful to an SME lender if it answers the
questions an underwriter actually asks: how much does this account turn over,
how stable is the balance, how cash-heavy is the business, does it bounce, how
much is already going out in EMIs, and can it service more. We already compute
every input; this turns them into the summary a credit team reads first.

Everything here is deterministic and rule-based — no scoring model pretending
to precision we don't have. The "reads" are plain-language flags a human
underwriter would raise, each traceable to a number on the page.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta

from .models import Txn


def _pkey(name: str) -> str:
    """Fuzzy party key — first 12 alphanumerics, so truncated variants of one
    counterparty collapse together (matches publish.party_key)."""
    return re.sub(r"[^A-Za-z0-9]", "", name or "").upper()[:12]


def _months(txns: list[Txn]) -> int:
    return len({t.date[:7] for t in txns}) or 1


def _eod_series(txns: list[Txn]) -> list[float]:
    """Daily carried-forward closing balance across the period (one account)."""
    if not txns:
        return []
    by_day: dict[str, float] = {}
    for t in txns:                       # last balance of each day wins
        by_day[t.date] = t.balance
    d, end = date.fromisoformat(min(by_day)), date.fromisoformat(max(by_day))
    cur, out = None, []
    while d <= end:
        cur = by_day.get(d.isoformat(), cur)
        if cur is not None:
            out.append(cur)
        d += timedelta(days=1)
    return out


def credit_summary(txns: list[Txn], integrity: dict | None = None,
                   validation_status: str = "passed") -> dict:
    """Per-account credit metrics + plain-language reads."""
    if not txns:
        return {"metrics": {}, "reads": []}

    months = _months(txns)
    credits = [t for t in txns if t.amount > 0]
    debits = [t for t in txns if t.amount < 0]
    total_cr = sum(t.amount for t in credits)
    total_dr = -sum(t.amount for t in debits)

    def cat_sum(cat):
        return sum(abs(t.amount) for t in txns if t.category == cat)

    def cat_n(cat):
        return sum(1 for t in txns if t.category == cat)

    eod = _eod_series(txns)
    avg_bal = round(sum(eod) / len(eod), 2) if eod else 0.0
    min_bal = round(min(eod), 2) if eod else 0.0
    closing_bal = round(txns[-1].balance, 2)

    cash_in = cat_sum("cash deposit")
    emi_out = cat_sum("EMI transaction") + cat_sum("Interest payments")
    bounces = (cat_n("inward bounce penal charges") + cat_n("Outward Bounced Xns"))
    penal_total = cat_sum("other penal charges") + cat_sum("inward bounce penal charges") \
        + cat_sum("Outward Bounced Xns")
    disbursals = cat_sum("Loan amount disbursal")
    related_cr = sum(abs(t.amount) for t in txns
                     if t.category == "Related party credit")

    # counterparty concentration (fuzzy-grouped)
    party_cr: dict[str, float] = defaultdict(float)
    for t in credits:
        if t.counterparty:
            party_cr[_pkey(t.counterparty)] += t.amount
    top_share = (round(100 * max(party_cr.values()) / total_cr, 1)
                 if party_cr and total_cr else 0.0)

    m = {
        "months": months,
        "total_credits": round(total_cr, 2),
        "total_debits": round(total_dr, 2),
        "net_cashflow": round(total_cr - total_dr, 2),
        "avg_monthly_credits": round(total_cr / months, 2),      # ~turnover
        "avg_monthly_debits": round(total_dr / months, 2),
        "avg_balance": avg_bal,
        "min_balance": min_bal,
        "closing_balance": closing_bal,
        "cash_deposits": round(cash_in, 2),
        "cash_intensity_pct": round(100 * cash_in / total_cr, 1) if total_cr else 0.0,
        "emi_outflow": round(emi_out, 2),
        "emi_outflow_monthly": round(emi_out / months, 2),
        "bounce_count": bounces,
        "penal_charges": round(penal_total, 2),
        "loan_disbursals": round(disbursals, 2),
        "related_party_credit_pct": round(100 * related_cr / total_cr, 1) if total_cr else 0.0,
        "distinct_credit_parties": len(party_cr),
        "top_party_share_pct": top_share,
        "integrity": (integrity or {}).get("assessment", "—"),
        "balance_verified": validation_status == "passed",
    }

    reads: list[str] = []
    if not m["balance_verified"]:
        reads.append("Balance does not reconcile on every row — treat totals as "
                     "provisional until the source document is complete.")
    if integrity and integrity.get("assessment") == "review":
        reads.append("Statement integrity flagged for review — see the integrity "
                     "notes before relying on this account.")
    if bounces:
        reads.append(f"{bounces} bounce/return event(s) in {months} month(s) — a "
                     f"cheque/mandate discipline signal.")
    if m["cash_intensity_pct"] >= 40:
        reads.append(f"Cash-intensive: {m['cash_intensity_pct']}% of credits are cash "
                     f"deposits — turnover is harder to verify independently.")
    if avg_bal < 0:
        reads.append("Average balance is negative — an overdraft / CC account "
                     "operating in the drawn range.")
    if emi_out and m["emi_outflow_monthly"] > m["avg_monthly_credits"] * 0.5:
        reads.append(f"EMI/interest outflow (~{m['emi_outflow_monthly']:.0f}/mo) is a "
                     f"large share of monthly inflow — limited headroom for new EMI.")
    if top_share >= 50:
        reads.append(f"Credit concentration: the top counterparty is {top_share}% of "
                     f"all credits — revenue depends heavily on one source.")
    if m["related_party_credit_pct"] >= 25:
        reads.append(f"{m['related_party_credit_pct']}% of credits are from related "
                     f"parties — may overstate genuine third-party turnover.")
    if not reads:
        reads.append("No adverse signals detected on the deterministic checks.")
    return {"metrics": m, "reads": reads}
