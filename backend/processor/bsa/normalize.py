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
                "%d %b, %Y", "%d-%B-%Y", "%d/%B/%Y", "%Y/%m/%d",
                # Full month name in the American order. The ICICI
                # OpTransactionHistory layout declares "%B %d, %Y" for its
                # header period, so that bank prints month names in full and a
                # row date in the same form would otherwise be unparseable.
                "%B %d, %Y")

# Word-boundary patterns: descriptions are usually prefixed by a bold title
# ("SATHYA PRASAD B RTGS-…"), so modes must match mid-string, never only at ^.
MODE_RULES = [
    # HDFC prints UPI with a hyphen and no slashes ("UPI-ASHOK GARG-<phone>@<vpa>",
    # "UPI-MUSHARRAF <ref>"), so match either separator. "UPISETTLEMENT-…" has no
    # separator right after UPI, so it stays out of this rule (it is a merchant
    # settlement, not a person).
    (r"\bUPI/|\bUPI-[A-Za-z]", "upi"),
    (r"\bMMT/IMPS|/IMPS/|\bIMPS[/:]", "imps"),
    (r"\bNEFT[-/:]", "neft"),
    (r"\bRTGS[-/:]", "rtgs"),
    (r"\bECSRTN|\bRTN CHG|\bRET CHG", "ecs-return"),
    # ACH mandates print as "ACH/…", "ACH-DR-…" and glued "ACHD-…" / "ACHC-…";
    # all are NACH so a lender debit reads as an EMI, not a generic payment.
    (r"\bACH[DC]?[-/]|\bNACH\b|\bECS(?!RTN)", "nach"),
    (r"\bBIL/|Bil Payment", "billpay"),
    # Axis prints "ATM-CASH/+<branch>" and "ATM-CASH- AXIS/…" — the hyphen
    # form was falling through to Regular debit.
    (r"NFS/CASH WDL|\bATM[-/ ]|ATM trxn", "atm-cash"),
    (r"\bCLG/", "clearing"),
    (r"BY CASH|CASH ?DEP|\bCDM\b|CASHDEP", "cash-deposit"),
    (r"\bCMS/", "cms"),
    (r"\bSMP/", "standing-instruction"),
    (r"Int\.Pd", "interest"),
    # ICICI cheque/branch transfers print "TRF/<NAME>/ICI" (often prefixed
    # "CHEQUE 3451"), and its internet banking prints "INF/INFT/<ref>/…".
    # Every company and individual paid by cheque was an unknown party until
    # these two forms were read.
    (r"\bINF/INFT/", "netbanking"),
    (r"\bTRFR\b|\bTRF/", "transfer"),
]

_IFSC = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
_REFNUM = re.compile(r"^\d{6,}$")
# Channel markers that appear where a name would: UPI prints the transfer TYPE
# (P2A person-to-account, P2M person-to-merchant) as its first segment on some
# banks, and the boss-facing report once showed "P2A" as a customer's party.
_CHANNEL_TOKENS = {"P2A", "P2M", "UPI", "IMPS", "NEFT", "RTGS", "MMT", "DR", "CR"}
_BANKISH = re.compile(r"\bBANKS?\b\s*$|\bBANK LTD\.?$", re.I)
# NEFT/RTGS references: a short bank prefix glued to a long number
# ("HDFCH00395013738") — never a party, however name-like the letters look.
_ALNUM_REF = re.compile(r"^[A-Z]{2,6}\d{6,}$")


def _name_segments(raw: str) -> list[str]:
    """Split a slash-delimited descriptor tail into candidate name segments,
    dropping the things that are never a party: reference numbers, IFSC codes,
    channel markers, and empty pieces."""
    out = []
    for s in raw.split("/"):
        s = _clean_segment(s)
        su = s.upper().replace(" ", "")
        if (not s or su in _CHANNEL_TOKENS or _IFSC.match(su)
                or _REFNUM.match(su) or su.isdigit() or _ALNUM_REF.match(su)):
            continue
        out.append(s)
    return out


# Words that mark a segment as a REMARK / purpose the sender typed, not the
# recipient — used to tell the two apart when their order is not fixed.
_REMARK_WORDS = {"PAY", "PAYME", "PAYMENT", "PAYMENTS", "TRANSFER", "TRF", "FUND",
                 "FUNDS", "TXN", "BILL", "PYMT", "TRF.", "SALARY", "RENT", "GST"}


def _name_score(s: str) -> float:
    """How much a narration segment looks like a counterparty NAME rather than a
    typed remark. The recipient is bank-populated and usually UPPERCASE and
    digit-free ("SRIVENKATESHWAR", "QUEST RE"); the remark is sender-typed, mixed
    case and often carries digits or a purpose word ("Steel 1573", "Pay",
    "amzn-dja7p"). This is order-independent, which is the whole point: banks put
    the remark before the name in one export and after it in another."""
    su = s.upper()
    words = su.split()
    score = 0.0
    if re.search(r"\d", s):                         # digits read as a ref/remark
        score -= 3
    if _BANKISH.search(s):                          # the counterparty's own bank
        score -= 4
    if any(w in _REMARK_WORDS for w in words):      # a purpose word
        score -= 3
    letters = s.replace(" ", "")
    if letters.isalpha() and s.isupper():           # bank-populated recipient
        score += 2
    score += min(len(letters), 24) * 0.05           # mild bias to a fuller name
    return score


def _best_name(segs: list[str]) -> str:
    """Pick the segment most like a counterparty name, order-independent. Ties
    keep the earliest, preserving prior behaviour where all segments look equal."""
    if not segs:
        return ""
    best = max(range(len(segs)), key=lambda i: (_name_score(segs[i]), -i))
    return segs[best]


def _first_name(segs: list[str]) -> str:
    """First segment that is a plausible party. A counterparty's BANK also
    rides in these descriptors ("…/RHEA HEALTHCARE PVT LTD/HDFC BANK/…"), so a
    bank-looking segment is only accepted when nothing better exists."""
    for s in segs:
        if not _BANKISH.search(s):
            return s
    return segs[0] if segs else ""


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
    # "M/S." (Messrs) is a company prefix, not a name segment — without this the
    # slash splits "M/S.VINAY" into "M" and the wrong half wins (ID9).
    d = re.sub(r"\bM/S[./ ]*", "", d, flags=re.I)
    # "TRF/<NAME>/…" names the party in the prefix even when a later "IMPS/"
    # token makes the mode look like imps (ID9: TRF/GEETA/… → GEETA).
    m = re.match(r"TRF/([^/]+)", d)
    if m and not _REFNUM.match(m.group(1).strip()):
        return _clean_segment(m.group(1))
    if mode == "upi":
        # UPI/<NAME>/… on some banks; Axis prints UPI/P2A/<ref>/<NAME>/<bank>/…
        # so the first PLAUSIBLE segment is the party, never blindly the first.
        m = re.search(r"UPI/(.+)$", d)
        if m:
            return _first_name(_name_segments(m.group(1)))
        # HDFC hyphen form + merchant UPIs: "UPI-ASHOK GARG-<phone>@<vpa>",
        # "UPI-BLINKIT-<vpa>@<bank>", "UPI-MUSHARRAF <ref>". The party is the
        # text right after "UPI-" up to the first delimiter (hyphen, @, or a
        # digit run). A purely numeric first segment (PhonePe merchant refs like
        # "UPI-3154000…-<phone>@…") has no name and is left unresolved.
        m = re.search(r"UPI-([A-Za-z][A-Za-z0-9 .&']*?)(?=[-@]|\s+\d|\s*$)", d)
        if m:
            name = m.group(1).strip()
            if name and not name.isdigit():
                return _clean_segment(name)
    if mode == "imps":
        m = re.search(r"MMT/IMPS/\d+/(.+)$", d)
        if m:
            segs = _name_segments(m.group(1))
            # The remark rides ahead of the name ("…/bill 2876/SONI BAKER/…"),
            # so prefer the first digit-free segment; fall back to the first.
            for s in segs:
                if not re.search(r"\d", s) and not _BANKISH.search(s):
                    return s
            if segs:
                return segs[0]
    if mode == "neft":
        # NEFT-<ref>-<NAME>-… (name may be followed by empty segment: "--")
        m = re.search(r"NEFT-[A-Z0-9]+-([^-]+)", d)
        if m and not _REFNUM.match(m.group(1).strip()):
            return _clean_segment(m.group(1))
        # NEFT/<ref>/<NAME>/<bank>/… (Axis slash form)
        m = re.search(r"NEFT[/:](.+)$", d)
        if m:
            return _best_name(_name_segments(m.group(1)))
    if mode == "rtgs":
        m = re.search(r"RTGS-[A-Z0-9]+-([^-]+)", d)      # RTGS-<ref>-<NAME>-…
        if m and not _REFNUM.match(m.group(1).strip()):
            return _clean_segment(m.group(1))
        # RTGS/<ref>/<NAME>/<bank> (Axis) and RTGS/<ref>/<IFSC>/<NAME> (ICICI):
        # segment ORDER differs between banks, so the party is the first
        # segment that is not a reference, an IFSC, or a bank name — taking
        # the last one reported "HDFC BANK" as a customer's counterparty.
        m = re.search(r"RTGS[/:][A-Z0-9]+[/:](.+)$", d)
        if m:
            return _best_name(_name_segments(re.sub(r":", "/", m.group(1))))
    if mode == "nach":
        m = re.search(r"ACH/([^/]+)/", d)
        if m and not _REFNUM.match(m.group(1).split("-")[0]):
            return _clean_segment(m.group(1))
        # SBI/Axis dash form: ACH-CR-<NAME>-NACH-<mandate>… / ACH-DR-<NAME>-…
        m = re.search(r"ACH-(?:CR|DR)-(.+?)-NACH\b", d, re.I) or \
            re.search(r"ACH-(?:CR|DR)-([^-]+)", d, re.I)
        if m and not _REFNUM.match(m.group(1).strip()):
            return _clean_segment(m.group(1))
        # ECS/<ref>/<NAME>… ("ECS/UTIBDE…/Bajaj Finance Ltd_SMS OT"); the
        # "_SMS OT" tail is a channel suffix, not part of the name.
        m = re.search(r"\bECS/(.+)$", d)
        if m:
            name = _first_name(_name_segments(m.group(1)))
            return name.split("_")[0].strip() if "_" in name else name
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
        m = re.search(r"\bTRF/([^/]+)", d)               # TRF/<NAME>/ICI
        if m and not _REFNUM.match(m.group(1).strip()):
            return _clean_segment(m.group(1))
    if mode == "netbanking":
        # INF/INFT/<ref>/<remark>/<NAME> — the NAME is the LAST segment
        # ("…/Amit payment /AMIT"); the remark rides ahead of it.
        m = re.search(r"INF/INFT/\d+/(.+)$", d)
        if m:
            segs = _name_segments(m.group(1))
            if segs:
                return segs[-1]
    if mode == "other":
        # GIB/<ref>/GST /<ref> — government internet banking; the tax head
        # is the only party there is.
        m = re.search(r"\bGIB/\d+/([A-Z]+)\b", d)
        if m:
            return _clean_segment(m.group(1))
        # RTGS RETURN-<ref>-<NAME>-<reason> — a returned RTGS names its party.
        m = re.search(r"(?:RTGS|NEFT) RETURN-[A-Z0-9]+-([^-]+)", d, re.I)
        if m and not _REFNUM.match(m.group(1).strip()):
            return _clean_segment(m.group(1))
        # SBI's prose form: "… TRANSFER TO 43465553898 TREE OF LIFE DWELLINGS /"
        # or "TRANSFER FROM 10448586579 Mr. CHANDRASHEKAR AN O /". The account
        # number and stray FRM/PENAL markers ride along with the name.
        m = re.search(r"TRANSFER[- ]+(?:FROM|TO)[- ]+(.+?)\s*/*\s*$", d, re.I)
        if m:
            name = re.sub(r"\b(?:FRM|PENAL)\b|\b\d{5,}\b", " ", m.group(1))
            name = _clean_segment(name)
            if name and not _REFNUM.match(name):
                return name
        # SBI's "TRANSFER- TRANSFER <acct> [<x>/<BANK>/<merchant>/UPI-]" form
        # (ID7). Prefer the merchant/VPA that sits after a 4-letter bank code
        # (L/UTIB/swiggyinst/UPI-, /HDFC/grofersind/, I/RATN/amazon@rap/ →
        # swiggyinst / grofersind / amazon); otherwise fall back to the
        # counterparty ACCOUNT NUMBER, which consolidates the name-less ones.
        if re.search(r"\bTRANSFER\b", d, re.I):
            mm = re.search(r"/[A-Z]{4}/([^/@\s]+)", d)
            if mm and not _REFNUM.match(mm.group(1)):
                return _clean_segment(mm.group(1))
            mn = re.search(r"\bTRANSFER[- ]+(?:TRANSFER\s+)?(?:N\s+|FROM\s+|TO\s+)?(\d{6,})", d, re.I)
            if mn:
                return mn.group(1)
    if mode == "standing-instruction":
        m = re.search(r"SMP/\w+_(.+)$", d)               # SMP/<ref>_<NAME>
        if m:
            return _clean_segment(m.group(1))
    # --- General bank-independent forms, reached when no mode rule resolved a
    # name. Found by auditing the party-less transfer rows across every bank. ---
    # NEFT/RTGS/IMPS with a Cr/Dr flag, then an IFSC, then the party name:
    # "NEFT Cr-ICIC0SF0002-VIVISH TECHNOLOGIES-…" (YES), "RTGSDR-ICIC0000610-…",
    # "NEFTCR-CBIN0280410-SHRI… CO.-…" (HDFC), "RTGS DR-UTIB0000129-SHRI LAKSHMI…"
    m = re.search(r"(?:NEFT|RTGS|IMPS)\s*(?:CR|DR)-?\s*[A-Z]{4}0[A-Z0-9]{6}-([^-]+)", d, re.I)
    if m and not _REFNUM.match(m.group(1).strip()):
        return _clean_segment(m.group(1))
    # Ref-first variant: "NEFT DR-<ref>-<NAME>-<IFSC>-…" (name between the
    # reference and the IFSC), e.g. "NEFT DR-ESFBN…-SHRI LAKSHMI STEEL S-UTIB…".
    m = re.search(r"(?:NEFT|RTGS)\s*(?:CR|DR)-[A-Z0-9]{8,}-([^-]+)-[A-Z]{4}0[A-Z0-9]{6}", d, re.I)
    if m and not _REFNUM.match(m.group(1).strip()):
        return _clean_segment(m.group(1))
    # NACH debit naming the mandate holder: "ACHD-HDFCBANKLTD-<ref>",
    # "ACHD-L&TFINANCELIMITED-<ref>", and the hyphen "ACH-DR-Indian Overseas
    # Bank- <ref>" form that mode detection routes to "other".
    m = re.search(r"\bACHD-([A-Za-z][^-]*)", d) \
        or re.search(r"\bACH-(?:CR|DR)-([^-]+)", d, re.I)
    if m and not _REFNUM.match(m.group(1).strip()):
        return _clean_segment(m.group(1))
    # "<ref>-TPT-<remark>-<NAME>" fund transfer — the party is the last segment.
    m = re.search(r"\bTPT-.+-([A-Za-z][A-Za-z0-9& .]+)$", d)
    if m:
        return _clean_segment(m.group(1))
    # HDFC internet-banking transfer: "IBFUNDSTRANSFERDR-<acct> -<NAME>"
    m = re.search(r"IBFUNDSTRANSFER(?:DR|CR)-\d+\s*-\s*(.+)", d, re.I)
    if m:
        return _clean_segment(m.group(1))
    # IMPS/P2A|P2M/<ref>/<NAME>/<bank>  and  IMPS-<ref>-<NAME>-<bank>
    m = re.search(r"IMPS/P2[AM]/\d+/([^/]+)", d, re.I)
    if m and not _REFNUM.match(m.group(1).strip()):
        return _clean_segment(m.group(1))
    m = re.search(r"\bIMPS-\d+-([^-]+)", d, re.I)
    if m and not _REFNUM.match(m.group(1).strip()):
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

    # Self-learning within the statement: a counterparty read cleanly in one row
    # fills the same name where another row's format hid it.
    _apply_gazetteer(txns)

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


# Narrations that carry no personal/company name, so the gazetteer must not try
# to force one onto them (they are un-nameable merchant/settlement refs).
_UNNAMEABLE = re.compile(r"UPISETTLEMENT|\bPOS\b|ATW-|CHRGS|/GST/|CASH\s*DEP|BY CASH", re.I)


def _apply_gazetteer(txns: list[Txn]) -> None:
    """In-statement entity gazetteer. A counterparty resolved cleanly in one row
    ("SHREE LAKSHMI STEEL" via NEFT) fills the SAME name where another row's
    format left it blank (the same party paid by a shape we don't parse as well).
    Conservative on purpose: only distinctive, alphabetic names of six or more
    letters, matched on a word boundary, and only into rows that resolved to
    nothing — never overriding a name we already have, and never onto an
    un-nameable ref. It learns from the data itself, so it needs no model or
    training set, and runs entirely in-account."""
    names: dict[str, str] = {}
    for t in txns:
        n = (t.counterparty or "").strip()
        key = re.sub(r"[ .&]", "", n).upper()
        if len(key) >= 6 and key.isalpha() and n.upper() not in _CHANNEL_TOKENS:
            names[n.upper()] = n
    if not names:
        return
    # Longest first so "SHREE LAKSHMI STEEL" wins over a bare "STEEL".
    ordered = sorted(names, key=len, reverse=True)
    for t in txns:
        if t.counterparty and t.counterparty != "unknown party":
            continue
        du = re.sub(r"\s+", " ", t.description).upper()
        if _UNNAMEABLE.search(du):
            continue
        for nu in ordered:
            if re.search(r"\b" + re.escape(nu) + r"\b", du):
                t.counterparty = names[nu]
                break


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
