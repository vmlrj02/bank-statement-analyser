"""Stage 4 — Normalize: canonical Txn records from any extractor's output."""
from __future__ import annotations

import re
from datetime import datetime

from .models import RawRow, StatementExtract, Txn

DATE_FORMATS = ("%d.%m.%Y", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%y", "%d-%b-%Y",
                "%d %b %Y", "%d/%m/%y", "%d-%m-%y", "%b %d, %Y", "%d %B %Y",
                "%Y-%m-%d")

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
        txns.append(Txn(
            date=parse_date(r.date), cheque_no=r.cheque_no, description=desc,
            amount=round(amount, 2), balance=r.balance, mode=mode,
            counterparty=extract_counterparty(desc, mode),
            page=r.page, source_file=extract.meta.source_file,
        ))

    # "New Criteria": statements ordered latest-to-oldest — detect & flip
    dates = [t.date for t in txns]
    if len(dates) > 2 and dates == sorted(dates, reverse=True) and dates[0] != dates[-1]:
        txns.reverse()

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


def dedup_merge(txn_lists: list[list[Txn]]) -> list[Txn]:
    """Merge transactions from multiple statements of the same account,
    dropping rows whose uid already appeared (overlapping periods)."""
    out: list[Txn] = []
    seen: set[str] = set()
    for txns in txn_lists:
        for t in txns:
            if t.uid in seen:
                t.is_duplicate = True
                continue
            seen.add(t.uid)
            out.append(t)
    out.sort(key=lambda t: t.date)
    return out
