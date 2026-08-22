"""Stage 4 — Normalize: canonical Txn records from any extractor's output."""
from __future__ import annotations

import re
from datetime import datetime

from .models import RawRow, StatementExtract, Txn

DATE_FORMATS = ("%d.%m.%Y", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%y", "%d-%b-%Y",
                "%d %b %Y", "%d/%m/%y", "%d-%m-%y", "%b %d, %Y", "%d %B %Y",
                "%Y-%m-%d",
                # slash/dot forms with a month name — "02/Jan/2026" appears in
                # real statements and cost a 6-file job before it was covered
                "%d/%b/%Y", "%d/%b/%y", "%d.%b.%Y", "%d.%b.%y",
                "%d %b, %Y", "%d-%B-%Y", "%d/%B/%Y", "%Y/%m/%d")

# Word-boundary patterns: descriptions are usually prefixed by a bold title
# ("SATHYA PRASAD B RTGS-…"), so modes must match mid-string, never only at ^.
MODE_RULES = [
    (r"\bUPI/", "upi"),
    (r"\bMMT/IMPS|/IMPS/|\bIMPS[/:]", "imps"),
    (r"\bNEFT[-/:]", "neft"),
    (r"\bRTGS[-/:]", "rtgs"),
    (r"\bECSRTN|\bRTN CHG|\bRET CHG", "ecs-return"),
    (r"\bACH/|\bNACH\b|\bECS(?!RTN)", "nach"),
    (r"\bBIL/|Bil Payment", "billpay"),
    (r"NFS/CASH WDL|\bATM[/ ]|ATM trxn", "atm-cash"),
    (r"\bCLG/", "clearing"),
    (r"BY CASH|CASH DEP|\bCDM\b", "cash-deposit"),
    (r"\bCMS/", "cms"),
    (r"\bSMP/", "standing-instruction"),
    (r"Int\.Pd", "interest"),
    (r"\bTRFR\b", "transfer"),
]

_IFSC = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
_REFNUM = re.compile(r"^\d{6,}$")


def parse_date(s: str) -> str:
    s = s.strip()
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"unparseable date: {s!r}")


def detect_mode(desc: str) -> str:
    for pat, mode in MODE_RULES:
        if re.search(pat, desc, re.I):
            return mode
    return "other"


def _clean_segment(seg: str) -> str:
    return re.sub(r"\s+", " ", seg).strip()


def extract_counterparty(desc: str, mode: str) -> str:
    """Best-effort counterparty display name from Indian payment descriptors."""
    d = re.sub(r"\s+", " ", desc)
    if mode == "upi":
        m = re.search(r"UPI/([^/]+)/", d)
        if m:
            return _clean_segment(m.group(1))
    if mode == "imps":
        m = re.search(r"MMT/IMPS/\d+/(.+)$", d)
        if m:
            segs = [_clean_segment(s) for s in m.group(1).split("/") if s.strip()]
            # prefer a name segment: not 'IMPS', not IFSC, not a number, not a bank suffix
            for s in segs:
                su = s.upper().replace(" ", "")
                if su == "IMPS" or _IFSC.match(su) or _REFNUM.match(su):
                    continue
                return s
    if mode == "neft":
        # NEFT-<ref>-<NAME>-… (name may be followed by empty segment: "--")
        m = re.search(r"NEFT-[A-Z0-9]+-([^-]+)", d)
        if m and not _REFNUM.match(m.group(1).strip()):
            return _clean_segment(m.group(1))
    if mode == "rtgs":
        m = re.search(r"RTGS-[A-Z0-9]+-([^-]+)", d)      # RTGS-<ref>-<NAME>-…
        if m and not _REFNUM.match(m.group(1).strip()):
            return _clean_segment(m.group(1))
        m = re.search(r"RTGS[/:][A-Z0-9]+[/:](.+)$", d)  # RTGS/<ref>/<bank>/<NAME>
        if m:
            segs = [_clean_segment(s) for s in re.split(r"[/:]", m.group(1)) if s.strip()]
            # last non-IFSC, non-numeric segment is the name
            for s in reversed(segs):
                if not _IFSC.match(s.upper().replace(" ", "")) and not _REFNUM.match(s):
                    return s
    if mode == "nach":
        m = re.search(r"ACH/([^/]+)/", d)
        if m and not _REFNUM.match(m.group(1).split("-")[0]):
            return _clean_segment(m.group(1))
    if mode == "clearing":
        m = re.search(r"CLG/([^/]+)", d)
        if m:
            return _clean_segment(m.group(1))
    if mode == "billpay":
        m = re.search(r"BIL/(?:ONL/\d+/)?(.+?)(?:/|$)", d)
        if m:
            return _clean_segment(m.group(1))
    if mode == "transfer":
        m = re.search(r"TRFR (?:TO|FROM):?\s*(.+)$", d, re.I)
        if m:
            return _clean_segment(m.group(1))
    if mode == "standing-instruction":
        m = re.search(r"SMP/\w+_(.+)$", d)               # SMP/<ref>_<NAME>
        if m:
            return _clean_segment(m.group(1))
    return ""


def normalize(extract: StatementExtract) -> list[Txn]:
    txns: list[Txn] = []
    for r in extract.rows:
        if r.withdrawal is not None and r.deposit is not None:
            # both printed (rare OCR error) — trust the balance delta later
            amount = (r.deposit or 0) - (r.withdrawal or 0)
        elif r.withdrawal is not None:
            amount = -r.withdrawal
        elif r.deposit is not None:
            amount = r.deposit
        else:
            continue  # balance-only row (B/F etc.) — not a transaction
        desc = re.sub(r"\s+", " ", r.description).strip()
        mode = detect_mode(desc)
        try:
            iso_date = parse_date(r.date)
        except ValueError as e:
            # name the offending statement and row: a bulk job may hold twenty
            # files, and "unparseable date" alone says nothing about which.
            raise ValueError(
                f"{e} in {extract.meta.source_file or 'statement'} "
                f"(page {r.page}): {desc[:60]}") from None
        txns.append(Txn(
            date=iso_date, cheque_no=r.cheque_no, description=desc,
            amount=round(amount, 2), balance=r.balance, mode=mode,
            counterparty=extract_counterparty(desc, mode),
            page=r.page, source_file=extract.meta.source_file,
            account_no=extract.meta.account_no, bank=extract.meta.bank,
        ))

    # "New Criteria": statements ordered latest-to-oldest — detect & flip
    dates = [t.date for t in txns]
    if len(dates) > 2 and dates == sorted(dates, reverse=True) and dates[0] != dates[-1]:
        txns.reverse()

    # Same-timestamp pairs are sometimes numbered in the wrong order by the
    # bank itself: ICICI printed a 194,000 credit as Sl 38 and its matching
    # debit as Sl 39, but the printed balances only chain the other way round.
    # Swap adjacent same-date rows ONLY when doing so repairs the running
    # balance — never on a guess, so a genuine extraction error still fails.
    _repair_swapped_pairs(txns)

    # uid + duplicate flagging (same content key => occurrence index disambiguates
    # genuine same-day identical reversal pairs; a repeat of the SAME occurrence
    # across merged files is a duplicate)
    seen: dict[str, int] = {}
    for t in txns:
        base = f"{t.date}|{t.description}|{t.amount:.2f}|{t.balance:.2f}"
        occ = seen.get(base, 0)
        seen[base] = occ + 1
        t.compute_uid(extract.meta.account_no, occ)
    return txns


def account_key(t: Txn) -> str:
    """Identity of the account a row belongs to."""
    return f"{(t.bank or '').strip()}|{(t.account_no or '').strip()}"


def _repair_swapped_pairs(txns: list[Txn]) -> int:
    """Reorder adjacent same-date rows where the printed balances prove the
    bank listed them back to front. Returns how many pairs were swapped."""
    fixed = 0
    for i in range(1, len(txns) - 1):
        a, b = txns[i], txns[i + 1]
        if a.date != b.date:
            continue
        prev = txns[i - 1].balance
        ok_now = (abs(prev + a.amount - a.balance) <= 0.011
                  and abs(a.balance + b.amount - b.balance) <= 0.011)
        if ok_now:
            continue
        ok_swapped = (abs(prev + b.amount - b.balance) <= 0.011
                      and abs(b.balance + a.amount - a.balance) <= 0.011)
        if ok_swapped:
            txns[i], txns[i + 1] = b, a
            fixed += 1
    return fixed


def dedup_merge(txn_lists: list[list[Txn]]) -> list[Txn]:
    """Merge statements into one ordered list, dropping overlap duplicates.

    A bulk upload can span several years AND several banks, so rows are grouped
    by account first and only ordered by date within an account. Interleaving
    accounts by date would produce a running-balance column that jumps between
    unrelated ledgers, which then fails validation for every row.

    uid already embeds the account number, so an overlapping period between two
    statements of the same account de-duplicates, while an identical amount on
    the same day in a different account does not collide.
    """
    groups: dict[str, list[Txn]] = {}
    seen: set[str] = set()
    order: list[str] = []
    for txns in txn_lists:
        for t in txns:
            if t.uid in seen:
                t.is_duplicate = True
                continue
            seen.add(t.uid)
            k = account_key(t)
            if k not in groups:
                groups[k] = []
                order.append(k)
            groups[k].append(t)

    out: list[Txn] = []
    for k in order:                      # accounts in first-seen order
        out.extend(sorted(groups[k], key=lambda t: t.date))
    return out
