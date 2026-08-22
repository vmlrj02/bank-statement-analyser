"""Columnar layout parser — for statements whose cells wrap across lines.

The generic_layout parser assumes a row's amounts arrive intact on its dated
anchor line. Some exports do not work that way: ICICI's "Detailed Statement"
prints narrow columns, so a single cell wraps over two or three physical lines.
A balance of -23,98,82,053.45 is emitted as three fragments, the minus sign
alone on the first line:

    3 S5116 02/Jan/20 02/Jan/2026 ... 1,90,000.  -
      3459     26                          00    23,98,82,
                                                 053.45

Reading that line-by-line is hopeless. Instead this parser treats a row as a
BLOCK — everything from one anchor line up to the next — and reassembles each
cell by x band:

  * words are bucketed into columns by their x position
  * within one physical line a column's words join with a space
  * across lines they join with NO separator, because cells wrap mid-token

Applied to the block above that yields "1,90,000."+"00" -> 1,90,000.00 and
"-"+"23,98,82,"+"053.45" -> -23,98,82,053.45, which is exactly right.

Columns are declared in the layout YAML as [x_min, x_max) bands, so a new bank
in this shape is still a descriptor rather than new code.
"""
from __future__ import annotations

import re
from datetime import datetime

import pdfplumber

from ..models import RawRow, StatementMeta, StatementExtract

_AMOUNT = re.compile(r"^-?[\d,]*\.?\d+$")


def _amount(raw: str):
    """Parse a reassembled amount cell; None when blank or a '-' placeholder."""
    s = (raw or "").strip().replace(",", "").replace("₹", "")
    if not s or s in {"-", "--"}:
        return None
    neg = s.startswith("-")
    s = s.lstrip("-").rstrip(".")
    if not s or not _AMOUNT.match(s):
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def _lines(words: list[dict], tol: float = 2.2) -> list[dict]:
    out: list[dict] = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if out and abs(w["top"] - out[-1]["top"]) <= tol:
            out[-1]["words"].append(w)
        else:
            out.append({"top": w["top"], "words": [w]})
    for ln in out:
        ln["words"].sort(key=lambda w: w["x0"])
    return out


def _cells(block: list[dict], bands: dict[str, list]) -> dict[str, str]:
    """Reassemble every column of a row block.

    Space-join inside a physical line, empty-join across lines — the two rules
    that put a wrapped "1,90,000." / "00" back together as one number.
    """
    parts: dict[str, list[str]] = {name: [] for name in bands}
    for ln in block:
        for name, (lo, hi) in bands.items():
            got = [w["text"] for w in ln["words"] if lo <= w["x0"] < hi]
            if got:
                parts[name].append(" ".join(got))
    return {name: "".join(chunks) for name, chunks in parts.items()}


def _meta(page1_text: str, source_file: str, layout: dict) -> StatementMeta:
    h = layout.get("header", {})
    account_no = period_from = period_to = ""
    if pat := h.get("account_line"):
        if m := re.search(pat, page1_text, re.M):
            account_no = (m.groupdict().get("account_no") or "").strip()
    if pat := h.get("period_line"):
        if m := re.search(pat, page1_text, re.M):
            g, fmt = m.groupdict(), h.get("period_date_format")
            for key in ("period_from", "period_to"):
                raw = (g.get(key) or "").strip()
                if raw and fmt:
                    try:
                        raw = datetime.strptime(raw, fmt).date().isoformat()
                    except ValueError:
                        pass
                if key == "period_from":
                    period_from = raw
                else:
                    period_to = raw
    name = ""
    if pat := h.get("account_name"):
        if m := re.search(pat, page1_text, re.M):
            name = (m.group(1) or "").strip()
    return StatementMeta(
        bank=layout["bank"], layout=layout["id"], account_no=account_no,
        account_name=name, period_from=period_from, period_to=period_to,
        source_file=source_file, is_digital_text=True,
    )


def extract(pdf_path: str, source_file: str, layout: dict) -> StatementExtract:
    p = layout["parse"]
    bands = {k: v for k, v in p["columns"].items()}
    anchor_re = re.compile(p["row_anchor"])
    anchor_x_max = float(p.get("anchor_x_max", 110))
    fm = p.get("field_map", {})
    # An anchor line must also carry content in these columns. Without it a
    # wrapped fragment of the row number is mistaken for a new row: once the
    # Sl No reaches 1000 it no longer fits its column and breaks as "100"/"0",
    # and that lone "0" sits exactly where an anchor is expected.
    need = [bands[c] for c in p.get("anchor_requires", []) if c in bands]
    skips = tuple(p.get("skip_rows", []))
    footers = tuple(p.get("footer_markers", []))
    # Page furniture often sits inside a column band — "Page 1 of 85" starts at
    # the same x as the balance, so without this it is concatenated onto the
    # balance of the last row on every page, making it unparseable and dropping
    # that row silently. Regexes because the page count varies per statement.
    footer_res = [re.compile(r) for r in p.get("footer_patterns", [])]
    # Page furniture ends a PAGE; these end the DOCUMENT. A trailing glossary
    # spans several pages, so breaking out of one page only lets the next page
    # reopen it and append to the final row.
    end_markers = tuple(p.get("end_markers", []))
    # A repeated page header must be INVISIBLE, not row-terminating: blocks are
    # carried across page breaks, so without this the last row of every page
    # swallows the next page's header and its balance becomes unparseable
    # ("-14,807,971.91Balance"), silently dropping one row per page.
    ignore_lines = tuple(p.get("ignore_lines", []))

    rows: list[RawRow] = []
    meta: StatementMeta | None = None

    def flush(block: list[dict], page: int) -> None:
        if not block:
            return
        c = _cells(block, bands)
        balance = _amount(c.get(fm.get("balance", "balance"), ""))
        if balance is None:
            return                       # header/summary block, not a txn
        rows.append(RawRow(
            sl_no=(c.get(fm.get("sl_no", "sl_no")) or None),
            date=(c.get(fm.get("date", "date"), "") or "").strip(),
            cheque_no=(c.get(fm.get("cheque_no", "cheque_no"), "") or "").strip(),
            description=re.sub(r"\s+", " ",
                               c.get(fm.get("description", "remarks"), "")).strip(),
            withdrawal=_amount(c.get(fm.get("withdrawal", "withdrawal"), "")),
            deposit=_amount(c.get(fm.get("deposit", "deposit"), "")),
            balance=balance,
            page=page,
        ))

    # The block is carried ACROSS pages: a row's cells can straddle a page
    # break, and flushing at each page end stranded the tail of its balance —
    # which silently dropped one transaction per page, 83 of them here.
    block: list[dict] = []
    block_page = 1
    stop = False
    with pdfplumber.open(pdf_path) as pdf:
        for pageno, page in enumerate(pdf.pages, start=1):
            if stop:
                break
            words = page.extract_words()
            if pageno == 1:
                meta = _meta(page.extract_text() or "", source_file, layout)

            for ln in _lines(words):
                text = " ".join(w["text"] for w in ln["words"])
                if any(m in text for m in end_markers):
                    stop = True
                    break
                if any(x in text for x in ignore_lines):
                    continue                  # skip the line, keep the block
                if any(f in text for f in footers) or \
                        any(r.search(text) for r in footer_res):
                    break
                first = ln["words"][0]
                is_anchor = (first["x0"] < anchor_x_max
                             and anchor_re.match(first["text"])
                             and all(any(lo <= w["x0"] < hi for w in ln["words"])
                                     for lo, hi in need))
                if is_anchor:
                    flush(block, block_page)
                    block, block_page = [ln], pageno
                elif block:
                    block.append(ln)
                if any(s in text for s in skips):
                    block = []
        flush(block, block_page)

    if meta is None:
        raise ValueError("could not parse statement header")
    return StatementExtract(meta=meta, rows=rows)
