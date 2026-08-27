"""Structured narration parser — split an Indian bank narration into its parts.

A narration is not one string: it is a channel stamp, reference numbers, the
counterparty (the sender on a credit, the recipient on a debit), the
counterparty's bank, and — crucially — the payer's own free-text REMARK. We used
to pattern-match the flattened string, so a lender name a customer happened to
type in their remark ("parimal finance amount") tagged the credit as a loan
disbursal even though the real sender was "happylaser".

This module isolates the remark from the structured part, so classification and
naming read the RIGHT field. It is deliberately conservative: when a format is
not recognised the whole text is left as the structured part and the remark is
empty, so nothing regresses — the win is only ever removing a genuine remark
from the matched text.

Formats handled (the high-volume ones):
    UPI/P2A/<ref>/<NAME>/<bank>/<remark>      UPI/<DR|CR>/<ref>/<NAME>/<bank>/<remark>
    UPI-<NAME>-<vpa>-<bank>-<remark>          IMPS-<ref>-<NAME>-<bank>-<remark>
    IMPS/P2A/<ref>/<NAME>/<bank>/<remark>     NEFT/<ref>/<NAME>  (no remark)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# A segment that is a pure reference: all digits, or a bank+digits UTR/IFSC code
# (SBIN0001234, HDFCR520250101...), or an XXXX-masked account.
_REF = re.compile(r"^(?:\d[\d ]*\d|\d|[A-Z]{3,5}\d[A-Z0-9]*|X{2,}\d+|\*{2,}\d+)$")
# A channel/type stamp segment that names no party.
_STAMP = {"UPI", "IMPS", "NEFT", "RTGS", "MMT", "P2A", "P2M", "P2P", "DR", "CR",
          "ACH", "TPT", "INB", "INF", "BIL", "ONL", "NA", "UPIINTENT", "PAYMENT",
          "TRANSFER", "URGENT", "REVSWEEP", "OUT", "IN"}
# A segment that names the counterparty's BANK, not the counterparty.
_BANKISH = re.compile(r"BANK|\bLTD\b|SBIN|HDFC|ICIC|UTIB|KKBK|PUNB|CNRB|BARB|IDIB|"
                      r"IOBA|UBIN|INDB|YESB|IDFB|FDRL|KVBL|@[a-z]", re.I)


@dataclass
class Narration:
    channel: str = "other"          # upi | neft | rtgs | imps | nach | cheque | other
    counterparty: str = ""          # sender (credit) / recipient (debit) name
    counterparty_bank: str = ""     # the other side's bank, when printed
    refs: list = field(default_factory=list)
    remark: str = ""                # the payer's free-text note
    structured: str = ""            # everything EXCEPT the remark — match lenders here
    raw: str = ""


def _channel(d: str) -> str:
    if re.search(r"\bUPI[/-]", d):
        return "upi"
    if re.search(r"\bIMPS\b|MMT/IMPS", d):
        return "imps"
    if re.search(r"\bRTGS\b", d):
        return "rtgs"
    if re.search(r"\bNEFT\b|\bN/[A-Z]", d):
        return "neft"
    if re.search(r"\bACH\b|\bNACH\b|\bECS\b", d):
        return "nach"
    if re.search(r"CHQ|CHEQUE|CLG", d, re.I):
        return "cheque"
    return "other"


def _seg_kind(seg: str) -> str:
    s = seg.strip()
    if not s:
        return "empty"
    if s.upper() in _STAMP:
        return "stamp"
    if _REF.match(s):
        return "ref"
    if _BANKISH.search(s):
        return "bank"
    if re.search(r"[A-Za-z]", s):
        return "name"
    return "ref"


def parse_narration(desc: str, mode: str = "") -> Narration:
    d = re.sub(r"\s+", " ", desc or "").strip()
    n = Narration(channel=_channel(d), raw=d)
    # Only the slash/dash-delimited channel formats carry a trailing remark we can
    # isolate. Everything else keeps the whole string as the structured part.
    if n.channel not in ("upi", "imps", "neft", "rtgs"):
        n.structured = d
        return n
    parts = re.split(r"\s*/\s*|\s+-\s+|(?<=\w)-(?=[A-Za-z])", d)
    parts = [p for p in (s.strip() for s in parts) if p]
    if len(parts) < 3:
        n.structured = d
        return n
    kinds = [_seg_kind(p) for p in parts]
    # The counterparty is the FIRST name-kind segment that is not bankish.
    name_i = next((i for i, k in enumerate(kinds) if k == "name"), None)
    if name_i is None:
        n.structured = d
        return n
    n.counterparty = parts[name_i]
    n.refs = [parts[i] for i, k in enumerate(kinds) if k == "ref"]
    bank_i = next((i for i in range(name_i + 1, len(parts)) if kinds[i] == "bank"), None)
    if bank_i is not None:
        n.counterparty_bank = parts[bank_i]
    # The remark is the free text AFTER the name and its bank — the segments the
    # bank did not generate. Keep only trailing name-kind segments as the remark;
    # a trailing ref/stamp is machine noise, not a note.
    tail_start = (bank_i + 1) if bank_i is not None else (name_i + 1)
    # A payer-typed note contains words, not references: a tail segment with a
    # digit in it ("CHOLAMXVFPKUD000 IMPS-", a beneficiary handle) is
    # machine-generated, and stripping it as a "remark" would hide the real
    # counterparty from lender matching. Keep those in the structured part.
    remark_parts = [parts[i] for i in range(tail_start, len(parts))
                    if kinds[i] == "name" and not re.search(r"\d", parts[i])]
    n.remark = " ".join(remark_parts).strip()
    # The structured part is the narration with the remark removed, so a lender
    # name in the remark is NOT matched while one in the counterparty/bank still is.
    n.structured = d
    if n.remark:
        n.structured = d.replace(n.remark, " ").strip()
    return n
