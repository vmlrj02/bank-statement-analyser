"""Bank-specific module: Axis Bank "Account Statement Report" export.

This is the one Axis shape YAML could not express. Two things defeat the generic
and columnar parsers:

  * The MONEY is on the FIRST physical line of a row (particulars-start, amount,
    Debit/Credit flag, balance) and the S.NO + dates sit on the line BELOW it,
    with more particulars wrapping under that. So the row is anchored on the
    balance-bearing line, not a dated line.
  * The export glues columns together with no spaces, so a long particulars cell
    overflows into the amount column and pdfplumber's WORD grouping merges the
    two into one band-spanning token that swallows the amount — read line by
    line the text scrambles ("N E F T /S K / A XS K ..."). Read line by line the
    text scrambles; read GLYPH by glyph it does not, because each character sits
    cleanly in its column. So this module bands per character, not per word.

A row is every line from one balance-bearing (money) line up to the next. The
balance is printed NEGATIVE (a cash-credit shown as overdrawn) and moves
normally, so no inversion. Correctness is gated the same way as every other
parser: validate() reconciles the running balance.
"""
from __future__ import annotations

import re
from datetime import datetime

import pdfplumber

from ..models import RawRow, StatementMeta, StatementExtract

_NUM = re.compile(r"^-?[\d,]*\.?\d+$")


def _amount(raw: str):
    s = (raw or "").strip().replace(",", "").replace(" ", "")
    if not s or s in {"-", "--"}:
        return None
    neg = s.startswith("-")
    s = s.lstrip("-").rstrip(".")
    if not s or not _NUM.match(s):
        return None
    try:
        return -float(s) if neg else float(s)
    except ValueError:
        return None


_MONEY = re.compile(r"-?[\d,]+\.\d{1,2}")


def _nums(cell: str) -> list[float]:
    """Every amount in a cell, left to right. Amounts always carry a decimal, so
    this ignores particulars glyphs and ref numbers (no decimal) that overflow
    into the money columns, and is immune to the column shift between the two
    "Account Statement Report" sub-layouts."""
    return [v for m in _MONEY.finditer(cell or "") if (v := _amount(m.group())) is not None]


def _lines(chars: list[dict], tol: float = 3.0) -> list[list[dict]]:
    out: list[list[dict]] = []
    for c in sorted(chars, key=lambda c: (c["top"], c["x0"])):
        if out and abs(c["top"] - out[-1][0]["top"]) <= tol:
            out[-1].append(c)
        else:
            out.append([c])
    for ln in out:
        ln.sort(key=lambda c: c["x0"])
    return out


def _band(chars: list[dict], lo: float, hi: float, gap: float = 2.5) -> str:
    """Concatenate the glyphs in [lo, hi), re-inserting a space only where a real
    x-gap sat between two of them, so a wrapped amount reassembles as one number
    and narration keeps its word breaks."""
    s, prev = "", None
    for c in chars:
        if not (lo <= c["x0"] < hi):
            continue
        if prev is not None and c["x0"] - prev > gap:
            s += " "
        s += c["text"]
        prev = c["x1"]
    return s.strip()


def _meta(page1_text: str, source_file: str, layout: dict) -> StatementMeta:
    h = layout.get("header", {})
    account_no = p_from = p_to = ""
    if m := re.search(h.get("account_line", ""), page1_text):
        account_no = (m.groupdict().get("account_no") or "").strip()
    if pat := h.get("period_line"):
        if m := re.search(pat, page1_text):
            fmt = h.get("period_date_format")
            for key, tgt in (("period_from", "p_from"), ("period_to", "p_to")):
                raw = (m.groupdict().get(key) or "").strip()
                if raw and fmt:
                    try:
                        raw = datetime.strptime(raw, fmt).date().isoformat()
                    except ValueError:
                        pass
                if tgt == "p_from":
                    p_from = raw
                else:
                    p_to = raw
    name = ""
    if pat := h.get("account_name"):
        if m := re.search(pat, page1_text):
            name = (m.group(1) or "").strip()
    return StatementMeta(
        bank=layout["bank"], layout=layout["id"], account_no=account_no,
        account_name=name, period_from=p_from, period_to=p_to,
        source_file=source_file, is_digital_text=True)


def extract(pdf_path: str, source_file: str, layout: dict) -> StatementExtract:
    p = layout["parse"]
    b = p["columns"]
    debit_flags = tuple(f.upper() for f in p.get("debit_flags", ("DR",)))
    ends = tuple(p.get("end_markers", []))
    skips = tuple(p.get("skip_rows", []))
    ignores = tuple(p.get("ignore_lines", []))

    rows: list[RawRow] = []
    meta: StatementMeta | None = None
    block: list[dict] = []          # cell dicts, first is the money line
    block_page = 1

    def flush():
        nonlocal block
        if not block:
            return
        # The money line's amount, flag and balance sit between the particulars
        # and the branch, and their exact x shifts between the two sub-layouts —
        # so read them by VALUE, not by fixed band: the two rightmost decimal
        # numbers are (amount, balance), and the flag is the CR/DR between them.
        money = block[0].get("money", "")
        nums = _nums(money)
        if not nums:
            block = []
            return
        bal = nums[-1]
        flag = (re.search(r"\b(CR|DR)\b", money) or [None, ""])[1].upper()
        wd = dep = None
        if flag:                    # single Amount column + Debit/Credit flag
            amt = nums[-2] if len(nums) >= 2 else None
            if amt is not None:
                if flag in debit_flags:
                    wd = amt
                else:
                    dep = amt
        else:                       # separate Debit and Credit columns (no flag)
            deb, cred = _nums(block[0].get("debit", "")), _nums(block[0].get("credit", ""))
            wd = deb[-1] if deb else None
            dep = cred[-1] if cred else None
        dm = next((re.search(r"\d{2}/\d{2}/\d{4}", c.get("date", "")) for c in block
                   if re.search(r"\d{2}/\d{2}/\d{4}", c.get("date", ""))), None)
        date = dm.group() if dm else ""
        desc = " ".join(c.get("remarks", "") for c in block if c.get("remarks")).strip()
        if date:
            rows.append(RawRow(
                sl_no="".join(c.get("sl_no", "") for c in block).strip() or None,
                date=date, cheque_no="", description=re.sub(r"\s+", " ", desc),
                withdrawal=wd, deposit=dep, balance=bal, page=block_page))
        block = []

    with pdfplumber.open(pdf_path) as pdf:
        stop = False
        for pageno, page in enumerate(pdf.pages, start=1):
            if stop:
                break
            if pageno == 1:
                meta = _meta(page.extract_text() or "", source_file, layout)
            for ln in _lines(page.chars):
                text = _band(ln, -1e9, 1e9, gap=2.5)
                if any(m in text for m in ends):
                    stop = True
                    break
                if any(x in text for x in ignores) or any(s in text for s in skips):
                    continue
                cell = {"sl_no": _band(ln, *b["sl_no"]), "date": _band(ln, *b["date"]),
                        "remarks": _band(ln, *b["remarks"]), "money": _band(ln, *b["money"]),
                        "debit": _band(ln, *b["debit"]), "credit": _band(ln, *b["credit"])}
                # A money line carries the balance — a decimal number in the
                # balance region (right of the flag, left of the branch).
                if _MONEY.search(_band(ln, *b["balance_probe"])):
                    flush()
                    block = [cell]
                    block_page = pageno
                elif block:
                    block.append(cell)
        flush()

    if meta is None:
        raise ValueError("could not parse statement header")
    return StatementExtract(meta=meta, rows=rows)
