"""Stage 4 — Normalize: canonical Txn records from any extractor's output."""
from __future__ import annotations

import re
from collections import Counter
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
    # HDFC's ATM withdrawal code is "NWD-<masked card>-<terminal>-<branch>";
    # it carries no "ATM" token at all, so it was falling through to Regular
    # debit and being read as a supplier payment — a cash withdrawal counted as
    # trade spend, which is the wrong answer twice over.
    (r"NFS/CASH WDL|\bATM[-/ ]|ATM trxn|\bNWD-", "atm-cash"),
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
        if _BANK_CODE.match(re.sub(r"(TRANSFER|TRF|IMPS|NEFT|RTGS|UPI)$", "", su)):
            continue                     # "IDFB", "KKBKTransfer" — their bank
        if not _strip_stamps(s):
            continue                     # "IMPSAB", "P2A" — how, not who
        out.append(s)
    return out


# Words that mark a segment as a REMARK / purpose the sender typed, not the
# recipient — used to tell the two apart when their order is not fixed.
# "PAYMEN" is not a typo: Axis truncates the remark at the cell edge and the
# reviewer found it twice in one hundred rows, once naming a row "Paymen" that
# should have read ASHISH and once where the row has no party at all. The other
# truncations of the same word are already here — this was the missing length.
_REMARK_WORDS = {"PAY", "PAYM", "PAYME", "PAYMEN", "PAYMENT", "PAYMENTS",
                 "TRANSFER", "TRF", "FUND",
                 "FUNDS", "TXN", "BILL", "PYMT", "TRF.", "SALARY", "RENT", "GST",
                 # UPI request/collect PURPOSE tokens. They sit in the same slot
                 # a name occupies and are mixed-case like a typed remark, so
                 # nothing else separates them: "UPI/ReqPay/MrUjjal/…" was read
                 # as ReqPay and "UPI/CollPay/ChandanaP/…" as CollPay. The party
                 # is the human next to them, which is the reviewer's point.
                 "REQPAY", "COLLPAY", "PAYREQ", "COLLECT", "REQUEST", "REQ",
                 "COLL", "MANDATE", "AUTOPAY", "QRPAY"}

# Banking vocabulary that disqualifies a free-standing narration from being read
# as a bare party name ("G R SPONGE AND /" is a name; "DEBIT INTEREST
# CAPITALIZED" and "NEFT CMS SALARY" are not). Used only by the two most
# generic rules at the end of extract_counterparty, where nothing structural
# anchors the name.
_BARE_NAME_STOP = _REMARK_WORDS | _CHANNEL_TOKENS | {
    "INTEREST", "CHARGE", "CHARGES", "CHRG", "CHRGS", "DEBIT", "CREDIT", "CASH",
    "RETURN", "POSTING", "RECOVERY", "WDL", "DEP", "TFR", "INT", "BULK", "CMS",
    "CHEQUE", "CHQ", "SELF", "BANK", "RET", "REVERSAL", "CLOSED", "BALANCE",
    "ACH", "NACH", "INW", "INB", "ATM", "POS", "EMI", "LIMIT", "SETTLEMENT"}


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
    rides in these descriptors ("…/RHEA HEALTHCARE PVT LTD/HDFC BANK/…"), and a
    UPI PURPOSE token sits in the same slot too ("UPI/ReqPay/MrUjjal/…"), so
    neither is accepted while a better segment remains — the party is the human
    beside them. Both are still returned if the narration holds nothing else,
    since a weak name beats no name at all."""
    for s in segs:
        if _BANKISH.search(s):
            continue
        if set(s.upper().split()) & _REMARK_WORDS:
            continue
        return s
    for s in segs:                       # nothing clean: bank or purpose will do
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
    # A direction stamp glued onto the party ("From:9891346233@ptyes",
    # "To:merchant@ybl") — YES Bank prints every UPI leg this way, and the
    # stamp is machinery, not part of the identifier.
    seg = re.sub(r"^(?:From|To)\s*:\s*", "", seg, flags=re.I)
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
    # token makes the mode look like imps (ID9: TRF/GEETA/… → GEETA). A leading
    # branch/sequence number is skipped ("TRF/139/PIRAMAL PETROLEUM PR" — the
    # party is Piramal, and "139" was reaching reports as the counterparty).
    m = re.match(r"TRF/(?:\d+/)?([^/]*[A-Za-z][^/]*)", d)
    if m and not _REFNUM.match(m.group(1).strip()):
        return _clean_segment(m.group(1))
    # --- Axis branch/card/merchant forms (ID5, ID9: "party detection are poor
    # in axis bank for all categories"). Each of these prints a perfectly good
    # name that no mode branch below claims, because detect_mode reads them as
    # "other" and the generic segment scan then picks a reference or nothing.
    #
    #   POS/MD ENTERPRISES/BANGALORE/311025/20:35/73 1111
    #   ECOM PUR/PAY*BIGTREE E/MUMBAI/300925/22:50/367451
    # The merchant is the FIRST segment after the channel word; the segments
    # after it are city, date, time and terminal. "PAY*" is the aggregator's
    # prefix (PayU/Paytm gateway), not part of the merchant's name.
    m = re.match(r"(?:POS|ECOM PUR|ECOMPUR)/([^/]+)", d, re.I)
    if m:
        name = re.sub(r"^(?:PAY\*|BBPS\*|RAZ\*)", "", m.group(1).strip(), flags=re.I)
        if sum(c.isalpha() for c in name) >= 3:
            return _clean_segment(name)
    #   MOB/TPFT/VIKAS VASANTH /925010004538960   (mobile-banking transfer)
    # search, not match: Axis prints the remark and the counterparty's bank
    # BEFORE the transfer stamp on some rows ("RATHORE/Paymen/State Bank Of
    # India MOB/TPFT/ASHISH"), so anchoring at the start missed the name.
    m = re.search(r"\bMOB/(?:TPFT|TPT|FT)/([^/]+)", d, re.I)
    if m and sum(c.isalpha() for c in m.group(1)) >= 3:
        return _clean_segment(m.group(1))
    #   BRN-CLG-CHQ PAID TO Vishwanath B/KARNATAKA BANK  → the payee, not the
    #   bank that presented the cheque.
    m = re.search(r"CHQ PAID TO\s+([^/]+)", d, re.I)
    if m and sum(c.isalpha() for c in m.group(1)) >= 3:
        return _clean_segment(m.group(1))
    #   SAK/CASH DEP/SAK472180594/4543/AYAZ MOHIDDIN — a cash deposit made at
    #   the counter BY a named person, which is exactly who a lender wants.
    m = re.match(r"SAK/[^/]*/[^/]*/[^/]*/([^/]+)", d, re.I)
    if m and sum(c.isalpha() for c in m.group(1)) >= 3:
        return _clean_segment(m.group(1))
    # --- the four shapes the reviewer labelled (round 1) ---------------------
    # 41 of 47 labelled shapes — 489 of 563 transactions — were these, and in
    # every one the payee is at a KNOWN POSITION in the string. Worth stating
    # because it is the argument against reaching for a model here: this is
    # "take the text inside the brackets", not something that needs learning.
    #
    # 1. The name printed in brackets after the VPA. The single biggest shape
    #    in the labelled set (33 shapes, 359 rows), Karnataka Bank and others:
    #      UPI:516187488189:paytmqr6er7uc@ptys(Maseeha Banu)
    #      UPI:553101391507:q939300321@ybl(MED ZONE PHARMA):UPI-
    #    The closing bracket is OPTIONAL because Karnataka truncates the
    #    particulars cell mid-name ("…@okhdfcbank(SYED"), and half a name is
    #    still better than none — it is what the reviewer labelled those as.
    #    When the bracket CLOSES, the name is complete and wins. When it does
    #    not, the cell was cut mid-name and the VPA handle is used instead —
    #    the reviewer's call, and the right one: a truncation point varies row
    #    to row, so "(SYED ARZAAN" and "(SYED" would split one payee into two
    #    parties, while the handle is identical every time. Their own labels
    #    had that exact shape both ways ("SYED ARZAAN" on one row, "syedarzaan"
    #    on another), which is the symptom. Letters only, because they stripped
    #    the serial off "syedarzaan3-1" and "zayyanzsyed16-1" as well.
    m = re.search(r"[:\-/]([A-Za-z][\w.\-]*)@[\w.\-]+\(([A-Za-z][^)]*?)(\)|$)", d)
    if m:
        handle, name, closed = m.group(1), m.group(2), m.group(3)
        if closed == ")" and sum(c.isalpha() for c in name) >= 3:
            return _clean_segment(name)
        stem = re.match(r"[A-Za-z]+", handle)
        # …but only when the handle identifies somebody. A bare payment-app
        # handle ("bharatpe.905…") is the rail, not the payee, so a truncated
        # merchant name beats it — "SILVERT" is at least who was paid.
        if (stem and len(stem.group(0)) >= 3
                and not _is_bare_psp(stem.group(0).upper())):
            return _clean_segment(stem.group(0))
        if sum(c.isalpha() for c in name) >= 3:
            return _clean_segment(name)
        if stem and len(stem.group(0)) >= 3:
            return _clean_segment(stem.group(0))
    m = re.search(r"@[\w.\-]+\(([A-Za-z][^)]*)", d)
    if m and sum(c.isalpha() for c in m.group(1)) >= 3:
        return _clean_segment(m.group(1))
    # 2. IMPS/P2A-<ref>-<Name>-<phone>. "Mr"/"Mrs" is a title, not the name.
    m = re.search(r"IMPS/P2A-\d+-(?:MR|MRS|MS)\.?\s*([A-Za-z][A-Za-z .]*?)(?:-\d|$)"
                  r"|IMPS/P2A-\d+-([A-Za-z][A-Za-z .]*?)(?:-\d|$)", d, re.I)
    if m:
        name = m.group(1) or m.group(2)
        if name and sum(c.isalpha() for c in name) >= 3:
            return _clean_segment(name)
    # 3. EBANK:<ref>/<NAME>/<ref>. The slash run is greedy because the name
    #    segment is sometimes empty ("EBANK:1475338882///KSBCL").
    m = re.match(r"EBANK:\d+/+([A-Za-z][^/]*)", d, re.I)
    if m and sum(c.isalpha() for c in m.group(1)) >= 3:
        return _clean_segment(m.group(1))
    # --- round 2: shapes found by ranking layouts on rows that COULD be named
    # (narration carries a name-like word) rather than on rows merely unnamed.
    # That re-ranking is the point — the biggest pile of unnamed rows was SBI's
    # "TRANSFER- TRANSFER <account no>", which carries no name at all and is
    # NONE by decision, not by failure.
    #
    #   Trf to EARTHCON DEVELOPERS PRIVATE LIMITED/960856     (IndusInd)
    m = re.search(r"\bTrf to ([A-Za-z][A-Za-z .&'-]+?)\s*/\s*\d", d, re.I)
    if m and sum(c.isalpha() for c in m.group(1)) >= 3:
        return _clean_segment(m.group(1))
    #   UCR013913427589_EMI_05-11-2025_PIRAMAL PETROLEUM P    (Axis cash-credit)
    m = re.search(r"_EMI_[\d\-/ ]+_([A-Za-z][A-Za-z .&'-]+)", d, re.I)
    if m and sum(c.isalpha() for c in m.group(1)) >= 3:
        return _clean_segment(m.group(1))
    #   CLG/510811/011125/Bank Of Ba/AKASH — the payee is AFTER the presenting
    #   bank, which is why the last segment wins here.
    m = re.match(r"CLG/\d+/\d+/[^/]*/([A-Za-z][^/]*)", d, re.I)
    if m and sum(c.isalpha() for c in m.group(1)) >= 3:
        return _clean_segment(m.group(1))
    #   IMPS/615963841356-IMPS P2A GAJANAND TOOLS MAYUR-H     (Karnataka)
    m = re.search(r"\bIMPS\s*P2[AMVP]\s+([A-Za-z][A-Za-z .&'-]+?)(?:\s*-|$)", d, re.I)
    if m and sum(c.isalpha() for c in m.group(1)) >= 3:
        return _clean_segment(m.group(1))
    #   Bank of Maharashtra: "UPI 707889175861/SBIN/KOLA SHEKHAR/Payment from
    #   Ph". The reference follows UPI with a SPACE rather than a slash, and the
    #   counterparty's bank code sits between the reference and the name.
    m = re.search(r"\bUPI\s+\d{9,}\s*/\s*[A-Z]{4}\s*/\s*([A-Za-z][^/]*)", d)
    if m and sum(c.isalpha() for c in m.group(1)) >= 3:
        return _clean_segment(m.group(1))
    #   Union Bank's "DETAILS OF STATEMENT" export writes the channel with a
    #   suffix and puts the direction in the middle:
    #   "UPIAR/435932534940/DR/KUMAR T /SBIN/ 9739696759@ax".
    m = re.search(r"\bUPIA[A-Z]?/\d+/(?:DR|CR)/([A-Za-z][^/]*)", d, re.I)
    if m and sum(c.isalpha() for c in m.group(1)) >= 3:
        return _clean_segment(m.group(1))
    #   "NEFT:BHOX AIRGAS X14432989 Sender PRIVATE LTD No:SBIN5250921" — the
    #   payee runs from the colon to the first reference token. Without this the
    #   ENTIRE string was the party, reference and all, which is what the
    #   reviewer keeps flagging as machinery in the party column.
    m = re.match(r"NEFT:\s*([A-Za-z][A-Za-z .&'-]+?)\s+[A-Z]?\d{6,}", d, re.I)
    if m and sum(c.isalpha() for c in m.group(1)) >= 3:
        return _clean_segment(m.group(1))
    #   Canara's branch export: "FUNDS TRANSFER DEBIT 04781010002434 - ADARSH
    #   CONSTRUCTIONS", and the same form without the account number. The payee
    #   follows the dash; the account number between is not part of the name.
    m = re.search(r"FUNDS TRANSFER\s*(?:DEBIT|CREDIT)?\s*\d*\s*-\s*"
                  r"([A-Za-z][A-Za-z .&'-]+)", d, re.I)
    if m and sum(c.isalpha() for c in m.group(1)) >= 3:
        return _clean_segment(m.group(1))
    #   "CHQ PAID-MICR INWARD CLEARING-ARJUN SOUHARDHA PATHIN-FEDERAL BANK" —
    #   the drawer sits between the clearing stamp and the presenting bank.
    #   Bandhan puts a zone between the stamp and the dash ("CHQ PAID-CTS
    #   INWARD CLEARING ZONE 8- BEHARILAL G H"), Canara does not.
    m = re.search(r"INWARD CLEARING[A-Z0-9 ]*?-\s*([A-Za-z][A-Za-z .&'-]+?)(?:-|$)",
                  d, re.I)
    if m and sum(c.isalpha() for c in m.group(1)) >= 3:
        return _clean_segment(m.group(1))
    #   Kotak prints three shapes no other bank does.
    #   "MB: SENT TO PRASEEJA ASHOKAN M"  — mobile-banking transfer, the payee
    #   is the whole tail.
    m = re.search(r"\bMB:\s*(?:SENT TO|RECEIVED FROM)\s+([A-Za-z][A-Za-z .&'-]+)",
                  d, re.I)
    if m and sum(c.isalpha() for c in m.group(1)) >= 3:
        return _clean_segment(m.group(1))
    #   "Recd:IMPS/534311801758/SREEJITH K/KKBK/X9365/IMPS" — without this the
    #   "Recd:IMPS" stem itself was reading as the counterparty.
    m = re.search(r"\bRecd:(?:IMPS|NEFT|RTGS|UPI)/\d+/([A-Za-z][^/]*)", d, re.I)
    if m and sum(c.isalpha() for c in m.group(1)) >= 3:
        return _clean_segment(m.group(1))
    #   "NEFT IN12534713810084 SREEJITH KUMAR P ICIC0SF000" — the name sits
    #   between the UTR and the beneficiary bank's IFSC.
    m = re.search(r"\bNEFT\s+[A-Z]{2}\d{8,}\s+([A-Za-z][A-Za-z .&'-]+?)"
                  r"(?=\s+[A-Z]{4}\d|\s*$)", d)
    if m and sum(c.isalpha() for c in m.group(1)) >= 3:
        return _clean_segment(m.group(1))
    #   MBS/by SYED MUQTHAR AHMED/0200853/02-06-2026          (Karnataka)
    m = re.search(r"\bMBS/by ([A-Za-z][A-Za-z .&'-]+?)\s*/", d, re.I)
    if m and sum(c.isalpha() for c in m.group(1)) >= 3:
        return _clean_segment(m.group(1))
    #   NET BANKING /KOTHARILELEC — the whole tail is the payee.
    m = re.search(r"\bNET BANKING\s*/\s*([A-Za-z][A-Za-z .&'-]+)", d, re.I)
    if m and sum(c.isalpha() for c in m.group(1)) >= 3:
        return _clean_segment(m.group(1))
    #   TO ONL NEFT:UTR:CIUBH26094034984:SBIN0008531:SARAVANA:: SARAVANAN::00067
    #   (City Union). Colon-delimited machinery with the payee printed twice —
    #   abbreviated once, then in full. The LONGEST alphabetic segment is the
    #   full one, which is why this takes the max rather than the first.
    if re.search(r"\bONL\s+(?:NEFT|RTGS|IMPS)\b", d, re.I):
        best = ""
        for seg in re.split(r"[:/]+", d):
            seg = seg.strip(" .-")
            seg = _strip_stamps(seg)
            if (sum(c.isalpha() for c in seg) >= 4 and not any(c.isdigit() for c in seg)
                    and seg.upper() not in _CHANNEL_TOKENS
                    and seg.upper() not in _REMARK_WORDS
                    and not _is_bank_name(seg.upper())
                    and len(seg) > len(best)):
                best = seg
        if best:
            return _clean_segment(best)
    #   I/W CHEQUE PAID-SAHILPOLYMERS-000000000035            (AU SFB)
    m = re.search(r"CHEQUE PAID-([A-Za-z][A-Za-z .&'-]+?)-\d", d, re.I)
    if m and sum(c.isalpha() for c in m.group(1)) >= 3:
        return _clean_segment(m.group(1))
    #   ACHInwDr-ROVER FINANCE LIMITE/05-07-2025              (Karnataka)
    #   A bank in that slot is dropped later by _is_bank_name, which is what
    #   keeps "ACHInwDr-IDFC FIRST BANK/…" from naming a rail.
    m = re.search(r"ACH\s*InwDr-([A-Za-z][A-Za-z .&'-]+?)\s*/", d, re.I)
    if m and sum(c.isalpha() for c in m.group(1)) >= 3:
        return _clean_segment(m.group(1))
    #   …UN79642505 02150625 by DNARENDR from Tally B         (ICICI combined)
    m = re.search(r"\bby ([A-Za-z][A-Za-z .&'-]{2,}?) from\b", d)
    if m and sum(c.isalpha() for c in m.group(1)) >= 3:
        return _clean_segment(m.group(1))
    # 4. A reversal keeps the original payee's VPA handle. Letters only: the
    #    reviewer labelled "ZAYYANZSYED16-1@okhdfcbank" as "ZAYYANZSYED" here
    #    and in the bracket form too, so the trailing serial is not the name.
    m = re.match(r"REV-UPI-\d+-([A-Za-z]+)", d, re.I)
    if m and sum(c.isalpha() for c in m.group(1)) >= 3:
        return _clean_segment(m.group(1))
    if mode == "upi":
        # PNB prints "UPI/<ref>/P2M|P2V/<vpa>/<NAME>" — the payee NAME is the
        # LAST segment, after the VPA. Prefer it over the VPA (which the
        # fallback below would otherwise return, hiding the real name).
        m = re.search(r"UPI/\d+/P2[AMVP]/\S*@\S*/([^/]+?)\s*$", d)
        if m and sum(c.isalpha() for c in m.group(1)) >= 3:
            return _clean_segment(m.group(1))
        # SBI "TO TRANSFER- UPI/DR/7327406342": only the payee's mobile is
        # printed. It is a real identifier (resolve_identifiers can name it
        # from a sibling row), so surface it rather than nothing. Must run
        # before the generic segment scan, which returns "" on all-ref tails.
        m = re.search(r"UPI/(?:DR|CR)/(\d{9,12})\s*$", d)
        if m:
            return m.group(1)
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
        # The mandate holder may sit after a CR/DR flag ("ACH/DR/HDFC BANK
        # LIMITED/…"), and the first segment can be a bare sequence number
        # ("NACH/10/…") — take the first segment that carries letters.
        m = re.search(r"ACH/(?:(?:CR|DR)/)?([^/]*[A-Za-z][^/]*)/", d)
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
        # A leading all-digit reference is not the biller ("Bil Payment
        # BIL/000995828480/ICICI BANK CRED…" → the bank, not the number).
        m = re.search(r"BIL/(?:ONL/\d+/)?(?:\d{6,}/)?([^/]*[A-Za-z][^/]*?)(?:/|$)", d)
        if m:
            return _clean_segment(m.group(1))
    if mode == "transfer":
        m = re.search(r"TRFR (?:TO|FROM):?\s*(.+)$", d, re.I)
        if m:
            return _clean_segment(m.group(1))
        m = re.search(r"\bTRF/(?:\d+/)?([^/]*[A-Za-z][^/]*)", d)   # TRF/<NAME>/ICI
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
            # A truncation remnant ("/ of-", "/ 6077-") is not a name: with
            # fewer than three letters, fall through so the account-number
            # rule below returns the real identifier instead.
            if name and not _REFNUM.match(name) \
                    and sum(c.isalpha() for c in name) >= 3:
                return name
        # SBI bulk salary/pension postings: "BULK POSTING- / EPAO" — the paying
        # office code is the only party printed.
        m = re.search(r"BULK POSTING-\s*/?\s*([A-Za-z]{3,})\s*$", d)
        if m:
            return m.group(1)
        # SBI's "TRANSFER- TRANSFER <acct> [<x>/<BANK>/<merchant>/UPI-]" form
        # (ID7). Prefer the merchant/VPA that sits after a 4-letter bank code
        # (L/UTIB/swiggyinst/UPI-, /HDFC/grofersind/, I/RATN/amazon@rap/ →
        # swiggyinst / grofersind / amazon); otherwise fall back to the
        # counterparty ACCOUNT NUMBER, which consolidates the name-less ones.
        # Vasavi co-op "By-Transfer <acct> <NAME> …" prints the name right after
        # the account — read it BEFORE the account-number fallback below.
        m = re.search(r"By-Transfer\s+\d{9,}\s+([A-Za-z][A-Za-z .]+)", d)
        if m:
            return _clean_segment(m.group(1))
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
    # HDFC internet-banking transfer: "IBFUNDSTRANSFERDR-<acct> -<NAME>". Some
    # exports truncate the name to two letters ("-QU"); the beneficiary ACCOUNT
    # is then the only identifier, so return it (resolve_identifiers can still
    # name it from a sibling row) rather than a stub the sanitiser rejects.
    m = re.search(r"IBFUNDSTRANSFER(?:DR|CR)-(\d+)\s*-\s*(.+)", d, re.I)
    if m:
        name = _clean_segment(m.group(2))
        if sum(c.isalpha() for c in name) >= 3:
            return name
        return m.group(1)
    # IMPS/P2A|P2M/<ref>/<NAME>/<bank>  and  IMPS-<ref>-<NAME>-<bank>. The /+
    # skips an empty segment ("…/501323167432//TIMEZONE").
    # The segment after the ref is not always the name: some banks print the
    # beneficiary's BANK there first ("IMPS/P2A/603518249052/IDFB/MOHD NAEEM/…"),
    # so walk the remaining segments rather than taking the next one on faith.
    m = re.search(r"IMPS/P2[AM]/\d+/+(.*)", d, re.I)
    if m:
        name = _first_name(_name_segments(m.group(1)))
        if name and not _REFNUM.match(name.strip()):
            return _clean_segment(name)
    # "IMPSAB/<ref>/<NAME>/<phone>" — the channel is glued to a suffix, so no
    # exact channel token matches and the whole stamp was reading as the party.
    m = re.match(r"IMPS[A-Z]{1,3}/(.*)", d, re.I)
    if m:
        # _best_name, not _first_name: the ref segment here survives cleaning
        # ("61250911975 T91321951 - 7") and would win on position alone. Scoring
        # marks it down for its digits and the payee up for being bank-cased.
        name = _best_name(_name_segments(m.group(1)))
        if name and not _REFNUM.match(name.strip()):
            return _clean_segment(name)
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
    # is the segment after the bank code, which may be a bare prefix ("ICIC"), a
    # full IFSC ("JAKA0GHAZIA", "HDFC0000240"), or carry a space ("ICIC0007 055").
    m = re.search(r"\b[RN]/[A-Z0-9]+/[A-Za-z][A-Za-z0-9 ]*/([A-Za-z][^/]+)", d)
    if m and not _REFNUM.match(m.group(1).strip()):
        return _clean_segment(m.group(1))
    # SBI "<ref> OF Mr./Mrs. <NAME>".
    m = re.search(r"\bOF Mr?s?\.?\s+([A-Za-z][A-Za-z .]+)", d)
    if m:
        return _clean_segment(m.group(1))
    # --- Second corpus audit (Aug 2026): nameable shapes the rules above still
    # missed, found by digit-masking every unresolved narration and reading the
    # top shapes per bank. Each rule below is anchored to a real printed form. ---
    # Axis internet banking. "INB/IFT/<NAME>/TPARTY TRANSFER" (the name may have
    # a glued leading ref: "INB/IFT/47586937Shree mansa traders/…"),
    # "INB/RTGS/<UTR> <name>/<bank>/", and "INB/<ref>/<NAME>/NA".
    m = re.search(r"\bINB/IFT/\d*([A-Za-z][^/]*)", d)
    if m:
        return _clean_segment(m.group(1))
    m = re.search(r"\bINB/RTGS/[A-Z]{5}\d+\s+([A-Za-z][^/]*)", d)
    if m:
        return _clean_segment(m.group(1))
    m = re.search(r"\bINB/\d{6,}/([^/]*[A-Za-z][^/]*)", d)
    if m:
        return _clean_segment(m.group(1))
    # YES Bank collection credits: "YESF<ref> <acct>/Bl<ref>/<NAME>/" — the
    # beneficiary rides after the Bl reference.
    m = re.search(r"/Bl\w*\d/([A-Za-z][^/]{2,})", d)
    if m:
        return _clean_segment(m.group(1))
    # PNB "NEFT IN::<UTR>/<NAME> <ref>" / "NEFT OUT:<UTR>:<NAME>" — strip the
    # ride-along references ("ONE 97 YESAP51891729831" → "ONE 97").
    m = re.search(r"NEFT (?:IN|OUT)::?[A-Z0-9]{10,}[/:](.+)$", d)
    if m:
        name = _clean_segment(re.sub(r"\b[A-Z]{2,6}\d{6,}\b", " ", m.group(1)))
        if sum(c.isalpha() for c in name) >= 3:
            return name
    # IndusInd inward NACH: "ACH DR INW PAY/<ref>/<NAME>"; AU's glued form
    # "ACH DR 10AXIS BANK1074249321" (the lender between the sequence and ref).
    m = re.search(r"\bACH (?:DR|CR) INW PAY/\d+/([A-Za-z].+?)\s*$", d)
    if m:
        return _clean_segment(m.group(1))
    m = re.search(r"\bACH (?:DR|CR) \d+([A-Za-z][A-Za-z ]+?)\d{6,}", d)
    if m:
        return _clean_segment(m.group(1))
    # Equitas comma-form NACH: "ACH DR:<mandate>,<code>,<NAME>~<date> CLG".
    m = re.search(r"\bACH (?:DR|CR):[^,]+,(?:[A-Z]{4}\d+,)?\s*([A-Za-z][^,~]*)", d)
    if m:
        return _clean_segment(m.group(1))
    # Cheques name their payee: Equitas "CHQ PAID-IC <no>-<NAME>-<bank>" /
    # "CHQ PAID-INWARD CLEA-<NAME> - <bank>", the return's "FOR PAYEE -<NAME>",
    # and AU's "I/W CHEQUE RETURN-<NAME>-<reason>" (who bounced matters).
    m = re.search(r"CHQ PAID-(?:IC \w+-(?:ICI-)?|INWARD CLEA-)([A-Za-z][^-]{2,})", d)
    if m:
        return _clean_segment(m.group(1))
    m = re.search(r"FOR PAYEE -\s*([A-Za-z][^-]{2,})", d)
    if m:
        return _clean_segment(m.group(1))
    m = re.search(r"I/W CHEQUE RETURN-([A-Za-z][^-]{2,})", d)
    if m:
        return _clean_segment(m.group(1))
    # HDFC cheque payments, flattened. Three shapes, all naming the PAYEE, which
    # for a cheque is the whole point — a cheque is the one instrument where the
    # bank prints who was actually paid:
    #   "PRABHUDAYALSHARMA-CHQPAID-PETBASHEERAB"   payee, then branch
    #   "CHEQPAIDTOMOHAMMADBINADBUL SATTARQ -CHQPAID-KOMPALLY"
    #   "CHQPAID-CTSS5-RKS-CHENDHINAGENDER"        payee last, after CTS codes
    # "SELF-CHQPAID-…" is the account holder drawing their own cash and is left
    # unnamed on purpose; it is a withdrawal, not a payment to someone.
    m = re.search(r"CHEQPAIDTO([A-Za-z][A-Za-z .&']{2,}?)\s*-\s*CHQPAID", d, re.I)
    if m:
        return _clean_segment(m.group(1))
    m = re.search(r"^([A-Za-z][A-Za-z .&']{2,})-\s*CHQPAID", d)
    if m and m.group(1).strip().upper() != "SELF":
        return _clean_segment(m.group(1))
    m = re.search(r"CHQPAID-(?:[A-Z0-9]+-)+([A-Za-z][A-Za-z .&']{3,})\s*$", d)
    if m:
        return _clean_segment(m.group(1))
    # HDFC collection credit: "PAYMENTS-K&NKIDSLLP" — the remitter is all the
    # narration carries.
    m = re.search(r"^PAYMENTS-([A-Za-z&][A-Za-z0-9 .&']{2,})\s*$", d, re.I)
    if m:
        return _clean_segment(m.group(1))
    # Equitas transfer tails: "…TRANSFER DR - SHREERENUKA STEELS",
    # "FT - DR - <acct> -KMP STEEL TRADERS", and its long-form UPI
    # "UPI REF NO <ref>P2A-<NAME>-…".
    m = re.search(r"TRANSFER (?:DR|CR) -\s*([A-Za-z][A-Za-z .&/]{2,})\s*$", d)
    if m:
        return _clean_segment(m.group(1))
    # Spaces optional around the hyphens: Equitas prints "FT - DR - <acct> -
    # <NAME>", HDFC prints the same shape flattened, "FT-CR-50100415695344-
    # PULIVENKATAI AH". A re-exported HDFC statement has the spaces squeezed
    # out of the whole narration, so the spaced form missed every one of them.
    m = re.search(r"\bFT\s*-\s*(?:DR|CR)\s*-\s*\d+\s*-\s*([A-Za-z].+?)\s*$", d)
    if m:
        return _clean_segment(m.group(1))
    # HDFC IMPS through the same FT prefix: "FT-<ref>-IMPSTRANSACTION-<junk>
    # <NAME>-FTIMPS<ref>" — the payee is the token just before the FTIMPS tail.
    m = re.search(r"IMPSTRANSACTION-.*?([A-Za-z]{4,})\s*-\s*FTIMPS", d, re.I)
    if m:
        return _clean_segment(m.group(1))
    m = re.search(r"UPI REF NO \d+P2[AMVP]-([A-Za-z][^-]{2,})", d)
    if m:
        return _clean_segment(m.group(1))
    # City Union: "BY NEFT TRF:<NAME> <UTR>:" and "TO ONL <NAME>:: SB <acct>".
    m = re.search(r"BY NEFT TRF:([A-Za-z][A-Za-z .]*?)\s+[A-Z]{2}\d{8,}", d)
    if m:
        return _clean_segment(m.group(1))
    m = re.search(r"\bTO ONL ([A-Za-z]{3,})::", d)
    if m:
        return m.group(1)
    # Union Bank suffix-name forms: "UPIAB/<ref> <ref> - /CR/PRABHU",
    # "IMPSAB/<ref> <ref> - 7/ARUNKUMAR" — the party is the trailing segment.
    m = re.search(r"\b(?:UPI|IMPS|NEFT|RTGS)AB/.*/([A-Za-z][A-Za-z .]{2,})\s*$", d)
    if m:
        return _clean_segment(m.group(1))
    # Vasavi co-operative prose: "NEFT Sender : <NAME>, UTR : …",
    # "Dividend Credit to A -<no>-<NAME> <phone>", "By-Transfer <acct> <NAME> …".
    m = re.search(r"NEFT Sender\s*:\s*([A-Za-z][A-Za-z .]+?)\s*(?:,|$)", d)
    if m:
        return _clean_segment(m.group(1))
    m = re.search(r"Dividend Credit to A\s*-\d+-([A-Za-z][A-Za-z .]+)", d)
    if m:
        return _clean_segment(m.group(1))
    # Tax remittances: the tax head is the only party there is (same reasoning
    # as GIB/GST above): Axis "TAX/<ref>/<acct>/<date>/…", ICICI "SGST<ref>".
    if re.match(r"TAX/\d{5,}/\d+/", d):
        return "TAX"
    m = re.match(r"([SCI]GST)\d{8,}", d)
    if m:
        return m.group(1)
    # HDFC one-off shapes: "<ref>/SBIEPYEGRASRAJASTHAN" (a government e-pay
    # merchant) and the "…-STP-BPCL" standing-transfer tail.
    m = re.search(r"^\d{9,}/([A-Za-z][A-Za-z ]{3,})$", d)
    if m:
        return _clean_segment(m.group(1))
    m = re.search(r"-STP-([A-Za-z]{3,})\s*$", d)
    if m:
        return m.group(1)
    # AU drawdown: "<longref>-PANDEY ANDSONS (DRAWDOWN FROM CASA)".
    m = re.search(r"^\d{10,}[- ]+([A-Za-z][A-Za-z &.]{3,})", d)
    if m:
        return _clean_segment(m.group(1))
    # A narration that LEADS with the party: "ARIHANT CAPITAL/159690058",
    # "NIPPON INDIA LA/134367660/EARG". Guarded by the stop list so a channel
    # prefix never reads as a name.
    m = re.match(r"([A-Za-z][A-Za-z .&']{3,})/\d{6,}\b", d)
    if m and not set(m.group(1).upper().split()) & _BARE_NAME_STOP:
        return _clean_segment(m.group(1))
    # A narration that IS the party and nothing else: "G R SPONGE AND /".
    # Only when the whole text is digit-free and no token is banking vocabulary.
    if not re.search(r"\d", d):
        m = re.match(r"([A-Za-z][A-Za-z .&']+?)\s*/?\s*$", d)
        if m and sum(c.isalpha() for c in m.group(1)) >= 4 \
                and not set(m.group(1).upper().split()) & _BARE_NAME_STOP:
            return _clean_segment(m.group(1))
    # Last resort: a UPI VPA handle. HDFC (and others) print many UPI rows with
    # NO name, only "UPI-<ref>-<mobile>@<psp>-…" — the name is not in the
    # statement to extract. The VPA that IS there is the real payee identifier a
    # lender can act on, so surface it rather than leaving the row anonymous.
    # Prefer a human-readable handle (name@bank) over a bare mobile number.
    # "(?:-\d)?" admits the numbered-VPA variant ("9950720425-2@AXL") that a
    # plain token class missed, leaving ~100 HDFC rows anonymous.
    vpas = re.findall(r"(?:^|[\s\-/])([A-Za-z0-9._]{2,}(?:-\d)?@[A-Za-z]{2,})", d)
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
    drop_useless_identifiers(txns)

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
# Corpus-audited additions: QR/ECS settlement refs, cash withdrawals, bank
# charges ("RETURN HANDLING CHARGES", "GST @18% on Chq Book Issuance Chrg"),
# the bank's own interest postings (Int.Pd / Int.Coll / INTEREST CAPITALIZED),
# and a card autopay to one's own credit card — none of these has an external
# party for a report to name.
_UNNAMEABLE = re.compile(
    r"UPISETTLEMENT|QRSETTLEM|ECSRTN|\bPOS\b|ATW-|CHRGS|\bCHRG\b|\bCHARGES?\b|"
    r"/GST/|CASH\s*DEP|BY CASH|CASH\s*W(?:DL|ITHDRAWAL)|ATM WDR|"
    r"Int\.Pd|Int\.Coll|INTEREST\s*CAPITALIZED|DEBIT INTEREST|"
    r"MONTHLY SAVINGS INTEREST|AUTOPAYSI|"
    # HDFC, with the spaces flattened out: the bank's OWN charges and interest,
    # and an ATM withdrawal, which names a machine's location and never a payee.
    # These have no counterparty to find, so they belong here rather than
    # counting against party coverage as rows we failed to name.
    r"\bNWD-|CHGSBRN|CHGSINCL|CHGSINCLTAXES|\bCHGS\b|INTERESTPAID|"
    # A cheque drawn to SELF is the holder taking their own cash out; there is
    # no payee to name, however the bank orders the words.
    r"_DAP_RENEWAL|\bSELF\s*-\s*CHQPAID|CHQPAID\s*-?\s*SELF", re.I)


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
# The third alternative is a GLUED UTR ("Ms Madhuri IDFBN52025041101368719") —
# a bank prefix run straight into 8+ digits is always machinery, never a name.
_REF_TAIL = re.compile(r"\s+(?:[A-Z]{2,6}[-/][A-Z0-9/*.-]*\d[\S]*|[A-Z]{2,6}\d{8,}\S*|\d[\d/*.]{8,}\S*)(\s.*)?$")


_PARTY_STOP = {"ATTN", "TPT", "CHG", "RETURN", "REVERSAL", "ACCOUNT CLOSED",
               "UTR NO", "UTR", "CLG", "NEFT CR", "NEFT DR", "RTGS CR", "RTGS DR"}
_BANK_PREFIX = re.compile(r"^(?:SBIN|HDFC|ICIC|UTIB|KKBK|PUNB|CNRB|BARB|IDIB|IOBA|"
                          r"UBIN|INDB|YESB|IDFB|FDRL|KVBL|MAHB|AUBL|ESFB|NTBL|BKID|"
                          r"SIBL|AIRP|YBL|PTSB)$", re.I)


# "Bank names: ICICI, HDFC, etc." — rule 4 of the master's Party naming tab,
# under "Avoid capturing these in the party names". A bank is the RAIL the
# money moved on, not the counterparty, and letting one through poisons exactly
# what the party column is for: the Top-10 lists came back led by "KARNATAKA
# BANK LIMIT" at a 122.8% share, "ICICI BANK LIMITED" and "BANK OF BARODA",
# which tells a lender nothing about who the business trades with.
#
# The test is the WORD "bank" anywhere in the name, plus the handful of brands
# that get printed without it. Word-bounded on purpose: "BANKATLAL TEXTILES" is
# a person's name and stays. The brands are matched only as the WHOLE name —
# "AXIS" alone is a bank, but "AXIS MACHINE TOOLS" is a customer.
_BANK_WORD = re.compile(r"\bBANKS?\b|\bBANKING\b", re.I)
_BANK_BRANDS = {
    "SBI", "STATE BANK", "HDFC", "ICICI", "AXIS", "KOTAK", "KOTAK MAHINDRA",
    "CANARA", "PNB", "PUNJAB NATIONAL", "IOB", "INDIAN OVERSEAS", "IDBI",
    "IDFC", "IDFC FIRST", "YES", "INDUSIND", "UNION", "UNION OF INDIA",
    "BOB", "BARODA", "EQUITAS", "AU", "AU SMALL FINANCE", "FEDERAL",
    "KARNATAKA", "CITY UNION", "DEUTSCHE", "RBL", "DBS", "HSBC", "CITI",
    "CITIBANK", "STANDARD CHARTERED", "INDIAN", "CENTRAL", "UCO", "BANDHAN",
}


def _is_bank_name(up: str) -> bool:
    """True when this "party" is really a bank."""
    if _BANK_WORD.search(up):
        return True
    stripped = re.sub(r"\b(LTD|LIMITED|LIMIT|PVT|PRIVATE|INDIA|THE)\b", " ", up)
    stripped = re.sub(r"[^A-Z ]", " ", stripped)
    return " ".join(stripped.split()) in _BANK_BRANDS


# Payment apps and PSP handles. These are the RAIL the money moved on, exactly
# like a bank name: "paytm", "phonepe" and "gpay" tell a lender nothing about
# who was paid, and a Paytm QR handle ("paytm-83541894@ptys") is the merchant's
# terminal id, not a name. But a handle that carries a real SERVICE word is
# worth keeping — "gpayrecharge" is a recharge merchant, not the rail — so the
# test strips the app name and asks whether anything meaningful is left.
_PSP = ("PAYTM", "PHONEPE", "GPAY", "GOOGLEPAY", "BHARATPE", "AMAZONPAY",
        "MOBIKWIK", "FREECHARGE", "PAYZAPP", "PAYU", "RAZORPAY", "BHIM",
        "PAYTMQR", "OKAXIS", "OKICICI", "OKSBI", "OKHDFCBANK", "YBL", "IBL",
        "AXL", "PTYS", "PTYB", "APL", "UPI")


def _is_bare_psp(up: str) -> bool:
    """True when the "party" is only a payment app / PSP handle."""
    core = re.sub(r"[^A-Z0-9]", "", up)
    for app in _PSP:
        if core.startswith(app):
            rest = core[len(app):]
            rest = re.sub(r"^(QR|ME|MERCHANT)", "", rest)
            # Anything left that is a real word (letters, not a terminal id)
            # means this is a named service: gpay + "RECHARGE" is kept.
            if len(rest) >= 4 and rest.isalpha():
                return False
            return True
    return False


def drop_useless_identifiers(txns) -> None:
    """Final pass: clear parties that identify nothing.

    Runs AFTER resolve_identifiers, which uses a bare account number or VPA as
    a JOIN KEY — an account named in one row fills the rows where only the
    number was printed. So the number has to survive until that has happened,
    and only then be cleared where it never resolved to a name.

    Two kinds go, on the reviewer's instruction:
      - a bare account / reference number ("TRANSFER- TRANSFER 4897692162094"),
        which he judged "no use". Note this OVERRIDES rule 2 of his own Party
        naming tab ("if not names, UPI ID or account number") — raised with him
        rather than applied quietly.
      - a payment-app handle with no name attached (see _is_bare_psp).
    """
    for t in txns:
        p = (t.counterparty or "").strip()
        if not p:
            continue
        core = re.sub(r"[^A-Za-z0-9]", "", p)
        if core.isdigit() or _is_bare_psp(p.upper()):
            t.counterparty = ""


# Channel and transfer STAMPS that ride at the edge of a party name. The
# reviewer flagged twelve of these in one pass: "SHAURY WDL TFR" (withdrawal
# transfer), "ANURAG DEP TFR" (deposit transfer), "IMPS P2A GAJANAND TOOLS
# MAYUR", "IMPSAB", "S TFR IMPS", a bare "CHG" for a charge. They are how the
# money moved, never who was paid, and when one is all that is left the row has
# no party at all. Stripped from EITHER END and repeatedly, because they stack
# ("S TFR IMPS" is three of them).
_STAMP_WORDS = (r"WDL|DEP|TFR|TRF|IMPS|NEFT|RTGS|UPI|ACH|NACH|ECS|P2A|P2M|"
                r"IMPSAB|CHG|CHRG|CHRGS|CHARGES|SENDER|TRANSFER|PAYMENT|PAYME|"
                r"CR|DR|INB|INF|TPT|BY|TO|FROM|SELF|CLG")
_STAMP_HEAD = re.compile(rf"^(?:{_STAMP_WORDS})\b[\s./:-]*", re.I)
_STAMP_TAIL = re.compile(rf"[\s./:-]*\b(?:{_STAMP_WORDS})$", re.I)
# A trailing reference that rode along with the name ("AMRINA IQBAL W42268794").
_REF_WORD_TAIL = re.compile(r"\s+[A-Z]?\d{5,}[A-Z0-9]*$", re.I)
# A four-letter IFSC bank prefix standing alone, or glued to a channel word
# ("IDFB", "KKBKTransfer") — the counterparty's bank, not the counterparty.
# A web address or an email domain, wherever it leaked in from. City Union
# prints its own website below the table and it reached a party column as
# "www.cityunionbank.com"; the footer marker that let it in was a layout bug,
# but nothing downstream should have accepted it as a name either.
_URLISH = re.compile(r"\bwww\.|https?://|\.(?:com|in|net|org|co\.in)\b", re.I)
_BANK_CODE = re.compile(r"^(?:SBIN|HDFC|ICIC|UTIB|KKBK|PUNB|CNRB|BARB|IDIB|"
                        r"IOBA|UBIN|INDB|YESB|IDFB|FDRL|KVBL|MAHB|AUBL|ESFB|"
                        r"CIUB|SCBL|RATN|BKID|SIBL|PYTM)$", re.I)


def _strip_stamps(p: str) -> str:
    """Peel channel/transfer stamps off both ends until the name is bare."""
    prev = None
    while p and p != prev:
        prev = p
        p = _STAMP_TAIL.sub("", _STAMP_HEAD.sub("", p)).strip(" -/.:,")
    return p


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
    p = _strip_stamps(p)
    p = _REF_WORD_TAIL.sub("", p).strip(" -/*.:")
    up = p.upper()
    # A bank's four-letter IFSC prefix, alone or glued to the channel that
    # followed it ("IDFB", "KKBKTransfer"): the counterparty's BANK, not the
    # counterparty. Same reasoning as _is_bank_name, one level more abbreviated.
    if _BANK_CODE.match(re.sub(r"(?i)(TRANSFER|TRF|IMPS|NEFT|RTGS|UPI)$", "", up)):
        return ""
    # A VPA whose local part is only a phone number ("7895273091-3@ybl",
    # "8817969839@ptyes") names nobody. The reviewer's instruction was plain:
    # "don't show these kinds numbers in party name". A handle with letters in
    # it still stands — it is at least a chosen identity.
    if "@" in p and not any(c.isalpha() for c in p.split("@", 1)[0]):
        return ""
    letters = sum(c.isalpha() for c in p)
    if letters <= 2 and not p.isdigit() and "@" not in p:
        return ""                                    # "NE", "DR", "S" — noise
    if p.isdigit() and len(p) < 6:
        return ""                                    # "10", "139" — a sequence
                                                     # counter, not an account
    if up in _CHANNEL_TOKENS or up in _REMARK_WORDS or up in _PARTY_STOP:
        return ""                                    # a channel/stamp/stop word
    if _IFSC_SHAPE.fullmatch(up.replace(" ", "")):
        return ""                                    # an IFSC is a bank, not a party
    if _is_bank_name(up):
        return ""                                    # nor is the bank itself
    if _URLISH.search(p):
        return ""                                    # a website, not a payee
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
# A name seen on this few rows may not be stamped on more than this multiple of
# them. Loose on purpose: the cost of losing one fill is a blank cell, the cost
# of a bad fill is a stranger's name on a lending report.
_FILL_MIN = 3
_FILL_RATIO = 3
_VPA_IN_TEXT = re.compile(r"\b([A-Za-z0-9._-]{2,}@[A-Za-z]{2,})\b")


def _row_ids(t: Txn, own: set) -> set:
    """Every identifier a row offers as a join key: account-shaped digit runs
    and UPI VPAs, from the narration and from the party field itself, minus the
    account's own side."""
    ids = set(_ID_IN_TEXT.findall(t.description)) - own
    ids |= {v.lower() for v in _VPA_IN_TEXT.findall(t.description)}
    if t.counterparty:
        ids |= set(_ID_IN_TEXT.findall(t.counterparty)) - own
        if "@" in t.counterparty:
            ids.add(t.counterparty.lower())
    return ids


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
        for i in set(_ID_IN_TEXT.findall(t.description)) - own | {
                v.lower() for v in _VPA_IN_TEXT.findall(t.description)}:
            id2names.setdefault(i, set()).add(t.counterparty)
    resolved = {i: next(iter(ns)) for i, ns in id2names.items() if len(ns) == 1}
    if not resolved:
        return
    # THE EVIDENCE MUST SCALE WITH THE CLAIM. Count what each identifier would
    # fill before filling anything, and drop the ones where a name learned from
    # a handful of rows would be stamped on far more.
    #
    # Why: SBI prints "TRANSFER TO 4897690162095" on every UPI row — the same
    # number on every SBI customer's statement, because it is SBI's pooled UPI
    # nodal account, not anybody's account. It is not the statement's own
    # account_no, so the `own` guard above never saw it. One row of 299 happened
    # to carry a payee inline ("…/UTIB/amazonupi@/You a") and that single
    # sighting then named 28 other rows Amazon. The reviewer found it exactly
    # as it reads: "why is party amazon upi when description doesn't have any
    # of that". Frequency alone does not separate the two cases (29 rows of 299
    # is not obviously a rail), but the RATIO does: a genuine beneficiary
    # account is named about as often as it is bare, while a rail is named once
    # and bare everywhere.
    would_fill: Counter = Counter()
    for t in txns:
        if party_kind(t.counterparty, t.description) in ("named", "na"):
            continue
        for i in _row_ids(t, own):
            if i in resolved:
                would_fill[i] += 1
    for i, n in would_fill.items():
        if n > _FILL_MIN and n > _FILL_RATIO * len(
                [1 for t in txns
                 if resolved[i] == t.counterparty
                 and party_kind(t.counterparty, t.description) == "named"]):
            del resolved[i]
    if not resolved:
        return
    for t in txns:
        kind = party_kind(t.counterparty, t.description)
        if kind in ("named", "na"):
            continue
        names = {resolved[i] for i in _row_ids(t, own) if i in resolved}
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
