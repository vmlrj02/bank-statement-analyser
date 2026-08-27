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


def _median(vals: list[float]) -> float:
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


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

    # Monthly credit series (for turnover trend) and monthly average balance
    # (for stability), oldest month first.
    monthly_cr: dict[str, float] = defaultdict(float)
    for t in credits:
        monthly_cr[t.date[:7]] += t.amount
    cr_series = [monthly_cr[k] for k in sorted(monthly_cr)]
    bal_by_month: dict[str, list[float]] = defaultdict(list)
    bal_seen: dict[str, float] = {}
    for t in txns:
        bal_seen[t.date] = t.balance
    for d, b in bal_seen.items():
        bal_by_month[d[:7]].append(b)
    monthly_avg_bal = [sum(v) / len(v) for _, v in sorted(bal_by_month.items())]

    # Turnover trend: compare the first and second halves of the credit series.
    trend = "flat"
    if len(cr_series) >= 4:
        half = len(cr_series) // 2
        first, second = sum(cr_series[:half]), sum(cr_series[half:])
        if first > 0:
            chg = (second - first) / first
            trend = "rising" if chg > 0.15 else "declining" if chg < -0.15 else "stable"

    # Balance stability: coefficient of variation of monthly average balance
    # (std / mean). Low = steady; high = swings. Undefined for an OD/negative
    # account, where "stability" of a drawn balance isn't meaningful.
    stability_cv = None
    if len(monthly_avg_bal) >= 2 and avg_bal > 0:
        mean = sum(monthly_avg_bal) / len(monthly_avg_bal)
        if mean > 0:
            var = sum((x - mean) ** 2 for x in monthly_avg_bal) / len(monthly_avg_bal)
            stability_cv = round((var ** 0.5) / mean, 2)

    cash_in = cat_sum("cash deposit")
    emi_out = cat_sum("EMI transaction") + cat_sum("Interest payments")
    bounces = (cat_n("inward bounce penal charges") + cat_n("Outward Bounced Xns"))
    penal_total = cat_sum("other penal charges") + cat_sum("inward bounce penal charges") \
        + cat_sum("Outward Bounced Xns")
    disbursals = cat_sum("Loan amount disbursal")
    related_cr = sum(abs(t.amount) for t in txns
                     if t.category == "Related party credit")

    # counterparty concentration (fuzzy-grouped); keep a display name per key
    # (the longest variant seen) so reads can name the party, not the key.
    party_cr: dict[str, float] = defaultdict(float)
    party_name: dict[str, str] = {}
    for t in txns:
        if t.counterparty:
            k = _pkey(t.counterparty)
            if len(t.counterparty) > len(party_name.get(k, "")):
                party_name[k] = t.counterparty.strip()
    for t in credits:
        if t.counterparty:
            party_cr[_pkey(t.counterparty)] += t.amount
    top_share = (round(100 * max(party_cr.values()) / total_cr, 1)
                 if party_cr and total_cr else 0.0)

    period_end = date.fromisoformat(max(t.date for t in txns))

    # --- Month-by-month turnover (credits/debits/net per calendar month) ---
    monthly_dr: dict[str, float] = defaultdict(float)
    for t in debits:
        monthly_dr[t.date[:7]] += -t.amount
    month_keys = sorted(set(monthly_cr) | set(monthly_dr))
    monthly_turnover = [{"month": k,
                         "credits": round(monthly_cr.get(k, 0.0), 2),
                         "debits": round(monthly_dr.get(k, 0.0), 2),
                         "net": round(monthly_cr.get(k, 0.0) - monthly_dr.get(k, 0.0), 2)}
                        for k in month_keys]
    # Last quarter vs everything before it: average monthly credits in the
    # final 3 calendar months against the average of the prior months. Needs
    # at least 6 months, or a seasonal dip masquerades as a trend.
    last_q_change = None
    if len(month_keys) >= 6:
        last3 = [monthly_cr.get(k, 0.0) for k in month_keys[-3:]]
        prior = [monthly_cr.get(k, 0.0) for k in month_keys[:-3]]
        prior_avg = sum(prior) / len(prior)
        if prior_avg > 0:
            last_q_change = round(100 * (sum(last3) / 3 - prior_avg) / prior_avg, 1)

    # --- EMI obligations: the LIST a lender wants, not just the sum ---
    # Group EMI debits by counterparty (fuzzy key); unnamed EMIs group by
    # amount rounded to the nearest 100 so a fixed instalment still collapses.
    emi_groups: dict[str, list[Txn]] = defaultdict(list)
    for t in txns:
        if t.category == "EMI transaction" and t.amount < 0:
            key = _pkey(t.counterparty) or f"AMT{int(round(abs(t.amount), -2))}"
            emi_groups[key].append(t)
    active_cutoff = (period_end - timedelta(days=45)).isoformat()
    new_cutoff = (period_end - timedelta(days=90)).isoformat()
    emi_obligations = []
    for key, ts in emi_groups.items():
        per_month: dict[str, float] = defaultdict(float)
        for t in ts:
            per_month[t.date[:7]] += -t.amount
        first_seen = min(t.date for t in ts)
        last_seen = max(t.date for t in ts)
        emi_obligations.append({
            "party": party_name.get(key) or "(unnamed)",
            "monthly_amount": round(_median(list(per_month.values())), 2),
            "months_seen": len(per_month),
            "first_seen": first_seen,
            "last_seen": last_seen,
            "total_paid": round(sum(-t.amount for t in ts), 2),
            "active": last_seen >= active_cutoff,
        })
    emi_obligations.sort(key=lambda o: (-o["monthly_amount"], o["party"]))
    emi_obligations = emi_obligations[:15]
    n_active_emi = sum(1 for o in emi_obligations if o["active"])
    # "Added in the last quarter" is only claimable when the statement covers
    # MORE than the last quarter — on a 2-month statement every obligation is
    # first seen recently, and that is visibility, not a new loan.
    period_start = min(t.date for t in txns)
    n_new_emi = (sum(1 for o in emi_obligations
                     if o["active"] and o["first_seen"] >= new_cutoff)
                 if period_start < new_cutoff else 0)

    # --- Bounce trend: last 90 days vs the 90 days before that ---
    bounce_cats = {"inward bounce penal charges", "Outward Bounced Xns"}
    last90_start = (period_end - timedelta(days=89)).isoformat()
    prior90_start = (period_end - timedelta(days=179)).isoformat()
    bounces_last_90d = sum(1 for t in txns
                           if t.category in bounce_cats and t.date >= last90_start)
    bounces_prior_90d = sum(1 for t in txns if t.category in bounce_cats
                            and prior90_start <= t.date < last90_start)

    # --- Balance floor: how often and how long the account sits low ---
    # Threshold is 10k or 10% of the average balance, whichever is higher, so
    # it scales with the account. Skipped for OD/negative accounts, where a
    # drawn balance is the normal operating state, not a liquidity signal.
    low_threshold = None
    low_days_pct = None
    low_streak = 0
    if eod and avg_bal > 0:
        low_threshold = round(max(10000.0, 0.10 * avg_bal), 2)
        low_days = sum(1 for b in eod if b < low_threshold)
        low_days_pct = round(100 * low_days / len(eod), 1)
        run = 0
        for b in eod:
            run = run + 1 if b < low_threshold else 0
            low_streak = max(low_streak, run)

    # --- Two-way flows: parties large on BOTH sides of the account ---
    # A counterparty that both pays in and is paid out at scale can be a
    # genuine trade relationship — or routing. The hint fires only when both
    # directions clear the large-transaction bar, and stays a question.
    large_thr = max(25000.0, 0.01 * total_cr)
    twoway_cr: dict[str, float] = defaultdict(float)
    twoway_dr: dict[str, float] = defaultdict(float)
    for t in txns:
        if t.counterparty and abs(t.amount) >= large_thr:
            if t.amount > 0:
                twoway_cr[_pkey(t.counterparty)] += t.amount
            else:
                twoway_dr[_pkey(t.counterparty)] += -t.amount
    two_way_parties = sorted(
        ({"party": party_name.get(k) or k,
          "credits": round(twoway_cr[k], 2),
          "debits": round(twoway_dr[k], 2)}
         for k in twoway_cr.keys() & twoway_dr.keys()
         if min(twoway_cr[k], twoway_dr[k]) >= large_thr),
        key=lambda p: -min(p["credits"], p["debits"]))[:5]

    # --- Inflow concentration: top-1 / top-3 credit counterparties ---
    ranked_cr = sorted(party_cr.items(), key=lambda kv: -kv[1])
    top3_share = (round(100 * sum(v for _, v in ranked_cr[:3]) / total_cr, 1)
                  if ranked_cr and total_cr else 0.0)
    top_credit_parties = [{"party": party_name.get(k) or k,
                           "amount": round(v, 2),
                           "share_pct": round(100 * v / total_cr, 1) if total_cr else 0.0}
                          for k, v in ranked_cr[:3]]

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
        "turnover_trend": trend,
        "balance_stability_cv": stability_cv,
        # Servicing capacity: monthly surplus after existing debt service, and a
        # simple coverage ratio (inflow ÷ EMI outflow). >1 means inflow covers
        # current EMIs; higher is more headroom for new debt.
        "monthly_surplus": round((total_cr - total_dr) / months, 2),
        "servicing_coverage": (round((total_cr / months) / (emi_out / months), 2)
                               if emi_out else None),
        "integrity": (integrity or {}).get("assessment", "—"),
        "balance_verified": validation_status == "passed",
        # --- additive detail blocks (see the sections above for derivation) ---
        "monthly_turnover": monthly_turnover,
        "last_quarter_credit_change_pct": last_q_change,
        "emi_obligations": emi_obligations,
        "active_emi_obligations": n_active_emi,
        "new_emi_obligations_last_quarter": n_new_emi,
        "bounces_last_90d": bounces_last_90d,
        "bounces_prior_90d": bounces_prior_90d,
        "low_balance_threshold": low_threshold,
        "low_balance_days_pct": low_days_pct,
        "longest_low_balance_streak": low_streak,
        "two_way_parties": two_way_parties,
        "top3_party_share_pct": top3_share,
        "top_credit_parties": top_credit_parties,
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
    if bounces_last_90d >= 2 and bounces_last_90d > bounces_prior_90d:
        reads.append(f"Bounces are increasing: {bounces_last_90d} in the last 90 "
                     f"days vs {bounces_prior_90d} in the 90 days before — recent "
                     f"cheque/mandate failures, not old history.")
    if m["cash_intensity_pct"] >= 40:
        reads.append(f"Cash-intensive: {m['cash_intensity_pct']}% of credits are cash "
                     f"deposits — turnover is harder to verify independently.")
    if avg_bal < 0:
        reads.append("Average balance is negative — an overdraft / CC account "
                     "operating in the drawn range.")
    if emi_out and m["emi_outflow_monthly"] > m["avg_monthly_credits"] * 0.5:
        reads.append(f"EMI/interest outflow (~{m['emi_outflow_monthly']:.0f}/mo) is a "
                     f"large share of monthly inflow — limited headroom for new EMI.")
    if n_active_emi:
        active_total = sum(o["monthly_amount"] for o in emi_obligations if o["active"])
        line = (f"{n_active_emi} EMI obligation(s) active at period end "
                f"(~{active_total:.0f}/mo combined)")
        if n_new_emi:
            line += f", {n_new_emi} added in the last quarter"
        reads.append(line + ".")
    if top_share >= 50:
        reads.append(f"Credit concentration: the top counterparty is {top_share}% of "
                     f"all credits — revenue depends heavily on one source.")
    elif top_share > 40:
        reads.append(f"Credit concentration: the top counterparty is {top_share}% of "
                     f"all credits (top 3: {top3_share}%) — revenue leans on one "
                     f"customer.")
    if two_way_parties:
        p0 = two_way_parties[0]
        reads.append(f"Funds move both ways with {len(two_way_parties)} "
                     f"counterpart(y/ies) — e.g. {p0['party']}: {p0['credits']:.0f} in / "
                     f"{p0['debits']:.0f} out — worth confirming these are "
                     f"arm's-length trade flows.")
    if m["related_party_credit_pct"] >= 25:
        reads.append(f"{m['related_party_credit_pct']}% of credits are from related "
                     f"parties — may overstate genuine third-party turnover.")
    if trend == "declining":
        reads.append("Turnover is declining over the period — credits in the "
                     "later months are materially below the earlier ones.")
    elif trend == "rising":
        reads.append("Turnover is rising over the period — a positive trend.")
    if last_q_change is not None and last_q_change <= -30:
        last3_avg = sum(monthly_cr.get(k, 0.0) for k in month_keys[-3:]) / 3
        prior_avg = (sum(monthly_cr.get(k, 0.0) for k in month_keys[:-3])
                     / len(month_keys[:-3]))
        reads.append(f"Credits in the last quarter average {last3_avg:.0f}/mo vs "
                     f"{prior_avg:.0f}/mo earlier — down {-last_q_change}%.")
    if stability_cv is not None and stability_cv > 0.6:
        reads.append("Balance swings widely month to month — low liquidity "
                     "stability, worth probing before relying on the average.")
    if low_days_pct is not None and low_days_pct >= 25:
        reads.append(f"Balance sits below {low_threshold:.0f} on {low_days_pct}% of "
                     f"days (longest continuous stretch: {low_streak} day(s)) — a "
                     f"thin liquidity buffer.")
    if m["servicing_coverage"] is not None and m["servicing_coverage"] < 1.5:
        reads.append(f"Debt-service coverage is tight (inflow is ~{m['servicing_coverage']}× "
                     f"existing EMI outflow) — little room for additional debt.")
    if not reads:
        reads.append("No adverse signals detected on the deterministic checks.")
    return {"metrics": m, "reads": reads}
