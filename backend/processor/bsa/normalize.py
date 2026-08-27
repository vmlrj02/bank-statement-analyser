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

# Control/non-printable bytes that a font-encoding quirk can leave in extracted
# text. Excel/openpyxl rejects these outright ("cannot be used in worksheets"),
# and they are noise everywhere else (JSON, DynamoDB, the preview), so scrub them
# at the source — the exact set openpyxl forbids, plus DEL. \t\n\r are left for
# the normal whitespace collapse.
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def scrub_control(s):
    return _CTRL.sub("", s) if isinstance(s, str) else s
# Channel markers that appear where a name would: UPI prints the transfer TYPE
# (P2A person-to-account, P2M person-to-merchant) as its first segment on some
# banks, and the boss-facing report once showed "P2A" as a customer's party.
_CHANNEL_TOKENS = {"P2A", "P2M", "P2V", "P2P", "UPI", "IMPS", "NEFT", "RTGS", "MMT", "DR", "CR"}
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
    seg = re.sub(r"\s+", " ", seg).strip()
    # A beneficiary account number often rides along with the name ("CHOLAMANDALAM
    # 0000003023864727", "MANISH 000..29 SHADCOLO"), which drops an otherwise
    # good name to a machine "handle". Strip standalone 6+ digit runs WHEN a name
    # component is present; leave a pure-number counterparty (or a VPA) intact,
    # since that number is then the only identifier there is.
    toks = seg.split()
    if any(re.search(r"[A-Za-z]", t) for t in toks):
        toks = [t for t in toks if not re.fullmatch(r"\d{5,}", t)]
    return " ".join(toks).strip()


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
        # PNB prints "UPI/<ref>/P2M|P2V/<vpa>/<NAME>" — the payee NAME is the
        # LAST segment, after the VPA. Prefer it over the VPA (which the
        # fallback below would otherwise return, hiding the real name).
        m = re.search(r"UPI/\d+/P2[AMVP]/\S*@\S*/([^/]+?)\s*$", d)
        if m and sum(c.isalpha() for c in m.group(1)) >= 3:
            return _clean_segment(m.group(1))
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
        # SBI internet NEFT: "TO TRANSFER-INB NEFT … SBIN<ref>- <code> <NAME>
        # TRANSFER TO <acct>". The beneficiary sits between the bank code and the
        # "TRANSFER TO" tail — read it BEFORE the generic transfer rules below,
        # which would otherwise fall back to the counterparty account number.
        m = re.search(r"SBIN\d+-\s*\w+\s+([A-Za-z][A-Za-z .]+?)\s+TRANSFER TO", d)
        if m:
            return _clean_segment(m.group(1))
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
    # IMPS/P2A|P2M/<ref>/<NAME>/<bank>  and  IMPS-<ref>-<NAME>-<bank>. The /+
    # skips an empty segment ("…/501323167432//TIMEZONE").
    m = re.search(r"IMPS/P2[AM]/\d+/+([A-Za-z][^/]*)", d, re.I)
    if m and not _REFNUM.match(m.group(1).strip()):
        return _clean_segment(m.group(1))
    m = re.search(r"\bIMPS-\d+-([^-]+)", d, re.I)
    if m and not _REFNUM.match(m.group(1).strip()):
        return _clean_segment(m.group(1))
    # CMS/<ref>/<NAME> — cash-management collection (big on ICICI current a/cs).
    # The name may START with a digit ("3D INTERIOR"), so require letters in the
    # segment rather than a letter first.
    m = re.search(r"\bCMS/\d+/([^/]*[A-Za-z][^/]*)", d)
    if m and not _REFNUM.match(m.group(1).strip()):
        return _clean_segment(m.group(1))
    # NTS/<ref>-SFMS/<NAME>, NTS/<ref>-Commission/<NAME> — ICICI bank-guarantee /
    # commission advices name the beneficiary after the second slash.
    m = re.search(r"\bNTS/[^/]+/([A-Za-z][^/]+)", d)
    if m:
        return _clean_segment(m.group(1))
    # Axis POS/merchant collections: "IPS/<MERCHANT>/<ref>/<ref>/<location>" and
    # "VPS/<MERCHANT>/…" — the merchant (a fuel station, retailer) is the party.
    m = re.search(r"\b[IV]PS/([A-Za-z][A-Za-z0-9 &.]*)/", d)
    if m:
        return _clean_segment(m.group(1))
    # "PAYMENT TRANSFER CR -<NAME>" / "... DR -<NAME>"
    m = re.search(r"PAYMENT TRANSFER (?:CR|DR)\s*-\s*(.+?)(?:\s{2,}|$)", d, re.I)
    if m and not _REFNUM.match(m.group(1).strip()):
        return _clean_segment(m.group(1))
    # spaced IMPS: "IMP P2A <ref> - <NAME> - …" and "IMPS/<NAME>/<ref>/…" (YES).
    m = re.search(r"\bIMPS?\s+P2[APM]\s+\d+\s*-\s*([A-Za-z][A-Za-z .]+?)\s*-", d, re.I)
    if m:
        return _clean_segment(m.group(1))
    m = re.search(r"\bIMPS/([A-Za-z][A-Za-z .]+?)/", d)
    if m and not _REFNUM.match(m.group(1).strip()):
        return _clean_segment(m.group(1))
    # PNB "To:XXXX<ref>:<NAME>" / "From:XXXX<ref>:<NAME>" mapped transfers.
    m = re.search(r"\b(?:To|From):X+\d+:\s*([A-Za-z][^:]+)", d)
    if m:
        return _clean_segment(m.group(1))
    # PNB "IMPS-OUT/<ref>/<IFSC>/<NAME>", "IMPS-IN/<ref>/<ref>/<NAME>".
    m = re.search(r"\bIMPS-(?:OUT|IN|CHG)/\d+/[A-Z0-9]+/([A-Za-z][^/]+)", d)
    if m and not _REFNUM.match(m.group(1).strip()):
        return _clean_segment(m.group(1))
    # AU/Perfios "IMPS-<ref> -<NAME> -<bank>" (a space precedes the name dash)
    # and "RTGS CR-<ref> -<NAME>" / "RTGS DR-<ref> -<NAME>".
    m = re.search(r"\bIMPS-\d+\s+-\s*([A-Za-z][^-]+)", d)
    if m:
        return _clean_segment(m.group(1))
    m = re.search(r"\bRTGS\s+(?:CR|DR)-\w+\s+-\s*([A-Za-z][^-]+)", d)
    if m:
        return _clean_segment(m.group(1))
    # IndusInd "R/<ref>/<bank>/<NAME>", "N/<ref>/<bank>/<NAME>" — the counterparty
    # is the segment after the bank code.
    m = re.search(r"\b[RN]/[A-Z0-9]+/[A-Za-z]+/([A-Za-z][^/]+)", d)
    if m and not _REFNUM.match(m.group(1).strip()):
        return _clean_segment(m.group(1))
    # SBI "<ref> OF Mr./Mrs. <NAME>".
    m = re.search(r"\bOF Mr?s?\.?\s+([A-Za-z][A-Za-z .]+)", d)
    if m:
        return _clean_segment(m.group(1))
    # Last resort: a UPI VPA handle. HDFC (and others) print many UPI rows with
    # NO name, only "UPI-<ref>-<mobile>@<psp>-…" — the name is not in the
    # statement to extract. The VPA that IS there is the real payee identifier a
    # lender can act on, so surface it rather than leaving the row anonymous.
    # Prefer a human-readable handle (name@bank) over a bare mobile number.
    vpas = re.findall(r"(?:^|[\s\-/])([A-Za-z0-9._]{2,}@[A-Za-z]{2,})", d)
    if vpas:
        named = [v for v in vpas if not v.split("@")[0].isdigit()]
        return (named[0] if named else vpas[0]).lower()
    return ""


def normalize(extract: StatementExtract) -> list[Txn]:
    # Scrub control bytes out of the statement identity ONCE, at the single point
    # every downstream stage (workbook, JSON, DynamoDB, preview) reads it from.
    m = extract.meta
    for fld in ("account_name", "account_no", "bank", "producer", "creator",
                "pdf_created", "pdf_modified"):
        setattr(m, fld, scrub_control(getattr(m, fld, "")))
    m.account_name = re.sub(r"\s+", " ", m.account_name or "").strip()
    txns: list[Txn] = []
    for r in extract.rows:
        if r.is_opening:
            # A brought-forward / opening row: no transaction, but its balance
            # re-bases the chain for the period that follows (see validate).
            try:
                iso_date = parse_date(r.date)
            except ValueError:
                iso_date = ""
            txns.append(Txn(
                date=iso_date, cheque_no="", description="Balance brought forward",
                amount=0.0, balance=r.balance, mode="other", counterparty="",
                page=r.page, source_file=extract.meta.source_file,
                account_no=extract.meta.account_no, bank=extract.meta.bank,
                is_opening=True))
            continue
        if r.withdrawal is not None and r.deposit is not None:
            # both printed (rare OCR error) — trust the balance delta later
            amount = (r.deposit or 0) - (r.withdrawal or 0)
        elif r.withdrawal is not None:
            amount = -r.withdrawal
        elif r.deposit is not None:
            amount = r.deposit
        else:
            continue  # balance-only row (B/F etc.) — not a transaction
        desc = re.sub(r"\s+", " ", scrub_control(r.description)).strip()
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
            date=iso_date, cheque_no=scrub_control(r.cheque_no), description=desc,
            amount=round(amount, 2), balance=r.balance, mode=mode,
            counterparty=extract_counterparty(desc, mode),
            page=r.page, source_file=extract.meta.source_file,
            account_no=extract.meta.account_no, bank=extract.meta.bank,
            balance_inverted=r.balance_inverted,
            balance_tolerance=r.balance_tolerance,
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

    # Reviewer-eye gate FIRST: clear junk parties ("NE", "DR", an IFSC, a glued
    # ref tail) so the narration fill below gets a clean shot at those rows.
    sanitise_parties(txns)

    # Structured-narration fallback: where none of the per-format rules named a
    # party, take the counterparty field the narration PARSER found (it
    # decomposes UPI/IMPS/NEFT/RTGS into channel/refs/name/bank/remark). The
    # per-format rules stay primary — they carry years of pinned cases — so this
    # only ever fills a blank, never overwrites. Runs BEFORE the gazetteer so a
    # name recovered here propagates to sibling rows too.
    _fill_party_from_narration(txns)

    # Self-learning within the statement: a counterparty read cleanly in one row
    # fills the same name where another row's format hid it.
    _apply_gazetteer(txns)

    # Identifier self-reference: an account number / VPA named in one row fills
    # the rows where only the identifier was printed. Runs again on the merged
    # account in the processor, where it sees every statement at once.
    resolve_identifiers(txns)

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


def party_kind(counterparty: str, description: str) -> str:
    """How identifying the resolved counterparty is — the honest read on party
    quality, which raw party-fill hides because it counts a beneficiary account
    number the same as a real name.

    - "na":     the row carries no counterparty by nature (cash, ATM, charges,
                settlement) — excluded from the quality denominator.
    - "none":   a party could have been named but was not resolved.
    - "handle": a machine identifier — an account/reference number, or a UPI VPA
                — real, but not a name an underwriter recognises.
    - "named":  an actual person or business name.
    """
    if _UNNAMEABLE.search(description or ""):
        return "na"
    cp = (counterparty or "").strip()
    if not cp or cp.lower() == "unknown party":
        return "none"
    if "@" in cp:
        return "handle"                       # a UPI VPA / email address
    letters = sum(c.isalpha() for c in cp)
    digits = sum(c.isdigit() for c in cp)
    if letters < 2 or digits >= letters:
        return "handle"                       # an account / reference number
    return "named"


# The reviewer-eye party gate: junk a human would instantly reject. A party
# failing this is cleared to "" — which lets the narration-parser fill and the
# identifier map take another, better shot at the row.
_IFSC_SHAPE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
_REF_TAIL = re.compile(r"\s+(?:[A-Z]{2,6}[-/][A-Z0-9/*.-]*\d[\S]*|\d[\d/*.]{8,}\S*)(\s.*)?$")


_PARTY_STOP = {"ATTN", "TPT", "CHG", "RETURN", "REVERSAL", "ACCOUNT CLOSED",
               "UTR NO", "UTR", "CLG", "NEFT CR", "NEFT DR", "RTGS CR", "RTGS DR"}
_BANK_PREFIX = re.compile(r"^(?:SBIN|HDFC|ICIC|UTIB|KKBK|PUNB|CNRB|BARB|IDIB|IOBA|"
                          r"UBIN|INDB|YESB|IDFB|FDRL|KVBL|MAHB|AUBL|ESFB|NTBL|BKID|"
                          r"SIBL|AIRP|YBL|PTSB)$", re.I)


def _sanitise_party(p: str) -> str:
    p = re.sub(r"\s+", " ", p or "").strip(" -/*.:")
    if not p:
        return ""
    # a glued reference tail after a real name ("SAHU CONSTRUCTION AND BORWELLS
    # IMPS-OUT/5166.../BARB0...") — keep the name, drop the machinery
    p = _REF_TAIL.sub("", p).strip(" -/*.:")
    # slash-junk ("KKBK/chitrarama/UPI", "UTR NO: / TREE OF LIFE …"): recover the
    # best inner segment — the letters-heavy one that is not a bank code, stamp,
    # or stop word — instead of showing the whole machinery as the party.
    if p.count("/") >= 2:
        best = ""
        for seg in p.split("/"):
            seg = seg.strip(" -*.:")
            su = seg.upper()
            if (su in _PARTY_STOP or su in _CHANNEL_TOKENS or su in _REMARK_WORDS
                    or _BANK_PREFIX.match(su) or _IFSC_SHAPE.fullmatch(su.replace(" ", ""))):
                continue
            if sum(c.isalpha() for c in seg) > sum(c.isalpha() for c in best):
                best = seg
        p = best.strip(" -*.:")
    up = p.upper()
    letters = sum(c.isalpha() for c in p)
    if letters <= 2 and not p.isdigit() and "@" not in p:
        return ""                                    # "NE", "DR", "S" — noise
    if up in _CHANNEL_TOKENS or up in _REMARK_WORDS or up in _PARTY_STOP:
        return ""                                    # a channel/stamp/stop word
    if _IFSC_SHAPE.fullmatch(up.replace(" ", "")):
        return ""                                    # an IFSC is a bank, not a party
    return p


def sanitise_parties(txns: list[Txn]) -> None:
    for t in txns:
        t.counterparty = _sanitise_party(t.counterparty)


def _fill_party_from_narration(txns: list[Txn]) -> None:
    """Fill an EMPTY counterparty from the structured narration parser.

    Guards, because a wrong name is worse than none: the candidate must carry at
    least three letters, must not be mostly digits (a reference the parser
    misread as a name), must not be a channel/remark word, and must not be
    un-nameable by nature (cash/ATM/settlement rows)."""
    from .narration import parse_narration
    for t in txns:
        if t.counterparty or _UNNAMEABLE.search(t.description):
            continue
        cand = _clean_segment(parse_narration(t.description).counterparty)
        letters = sum(c.isalpha() for c in cand)
        digits = sum(c.isdigit() for c in cand)
        if letters < 3 or digits > letters:
            continue
        if cand.upper() in _REMARK_WORDS or cand.upper() in _CHANNEL_TOKENS:
            continue
        t.counterparty = cand


_ID_IN_TEXT = re.compile(r"\b\d{9,18}\b")           # account-number-shaped runs
_VPA_IN_TEXT = re.compile(r"\b([A-Za-z0-9._-]{2,}@[A-Za-z]{2,})\b")


def resolve_identifiers(txns: list[Txn]) -> None:
    """Corpus self-reference: the same beneficiary account number or UPI VPA
    appears across many rows — NAMED in some ("TRANSFER TO 4698150044305
    SUKUMAR"), bare in others ("TRANSFER TO 4698150044305"). Harvest
    identifier→name from the named rows, then fill the rows where only the
    identifier was printed. Called per statement in normalize and again on the
    merged account in the processor, where it sees every statement at once.

    Correctness over coverage:
      * the statement's OWN account number never enters the map (a narration
        often prints both sides, and mapping our side to the other side's name
        would smear one counterparty across everything);
      * an identifier that different rows attribute to DIFFERENT names is
        ambiguous and skipped (no majority guessing);
      * a row is filled only when ALL its known identifiers agree on one name.
    A transaction-unique reference (UTR) sails through harmlessly: it exists in
    one row only, so it can never fill another."""
    own = {t.account_no.strip() for t in txns if t.account_no} - {""}
    id2names: dict[str, set] = {}
    for t in txns:
        if party_kind(t.counterparty, t.description) != "named":
            continue
        ids = set(_ID_IN_TEXT.findall(t.description)) - own
        ids |= {v.lower() for v in _VPA_IN_TEXT.findall(t.description)}
        for i in ids:
            id2names.setdefault(i, set()).add(t.counterparty)
    resolved = {i: next(iter(ns)) for i, ns in id2names.items() if len(ns) == 1}
    if not resolved:
        return
    for t in txns:
        kind = party_kind(t.counterparty, t.description)
        if kind in ("named", "na"):
            continue
        ids = set(_ID_IN_TEXT.findall(t.description)) - own
        ids |= {v.lower() for v in _VPA_IN_TEXT.findall(t.description)}
        if t.counterparty:
            ids |= set(_ID_IN_TEXT.findall(t.counterparty))
            if "@" in t.counterparty:
                ids.add(t.counterparty.lower())
        names = {resolved[i] for i in ids if i in resolved}
        if len(names) == 1:
            t.counterparty = names.pop()


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
