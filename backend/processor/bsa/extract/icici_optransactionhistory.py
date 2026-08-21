"""Template parser: ICICI internet-banking "OpTransactionHistory" layout.

Font-driven grouping (deterministic):
  - anchor line   : S No. (digit, left edge) + date (dd.mm.yyyy)
  - title line    : remark-zone words in the *Black* face -> belongs to the
                    NEXT anchor (it is printed just above its row)
  - descriptor    : remark-zone words in the *Regular* face -> belongs to the
                    CURRENT (previous) anchor, including across page breaks
Amounts are assigned withdrawal/deposit/balance by right-edge x cutoffs.
"""
from __future__ import annotations

import re
from datetime import datetime

import pdfplumber

from ..models import RawRow, StatementMeta, StatementExtract

NUM_RE = re.compile(r"^\d{1,3}(,\d{2,3})*\.\d{2}$|^\d+\.\d{2}$")
DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
FOOTER_MARKERS = (
    "www.icici.bank.in", "Dial your Bank", "Never share your OTP",
    "Please call from your registered",
)


def _parse_amount(tok: str) -> float:
    return float(tok.replace(",", ""))


def _lines(words: list[dict], tol: float = 3.0) -> list[dict]:
    out: list[dict] = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if out and abs(w["top"] - out[-1]["top"]) <= tol:
            out[-1]["words"].append(w)
        else:
            out.append({"top": w["top"], "words": [w]})
    for ln in out:
        ln["words"].sort(key=lambda w: w["x0"])
    return out


def extract(pdf_path: str, source_file: str, layout: dict) -> StatementExtract:
    cols = layout["parse"]["columns"]
    rows: list[RawRow] = []
    meta = None
    # buffered black (title-face) lines: {page, top, text}
    title_buffer: list[dict] = []
    current: dict | None = None   # {sl,date,cheque,wd,dep,bal,title,desc,page}

    def finalize(cur: dict) -> None:
        rows.append(RawRow(
            sl_no=cur["sl"], date=cur["date"], cheque_no=cur["cheque"].strip(),
            description=" ".join(cur["title"] + cur["desc"]).strip(),
            withdrawal=cur["wd"], deposit=cur["dep"],
            balance=cur["bal"], page=cur["page"],
        ))

    def split_title(anchor_page: int, anchor_top: float) -> list[str]:
        """Take the trailing black lines that sit directly above the anchor
        (stacked ~10pt apart) as the new row's title; earlier buffered black
        lines are tail-of-title of the *previous* row."""
        kept: list[dict] = []
        for ln in reversed(title_buffer):
            k = len(kept)
            if ln["page"] == anchor_page and anchor_top - ln["top"] <= 9 + 10.5 * k:
                kept.append(ln)
            elif ln["page"] == anchor_page - 1 and k == 0:
                kept.append(ln)   # title just before a page break
            else:
                break
        kept.reverse()
        leftover = title_buffer[: len(title_buffer) - len(kept)]
        if leftover and current is not None:
            for ln in leftover:
                current["title"].append(ln["text"])
        title_buffer.clear()
        return [w for ln in kept for w in ln["text"].split(" ")]

    with pdfplumber.open(pdf_path) as pdf:
        for pageno, page in enumerate(pdf.pages, start=1):
            words = page.extract_words(extra_attrs=["fontname"])
            if pageno == 1:
                meta = _parse_meta(page.extract_text() or "", source_file, layout)

            header_tops = [w["top"] for w in words
                           if w["text"] in ("Withdrawal", "Deposit", "Balance")]
            if not header_tops:
                continue
            body_top = max(header_tops) + 14

            for ln in _lines([w for w in words if w["top"] > body_top]):
                ws = ln["words"]
                text = " ".join(w["text"] for w in ws)
                if any(m in text for m in FOOTER_MARKERS):
                    break  # rest of this page is footer

                is_anchor = (len(ws) >= 2 and ws[0]["x0"] < cols["sl_no_x_max"]
                             and ws[0]["text"].isdigit()
                             and DATE_RE.match(ws[1]["text"]))
                if is_anchor:
                    title = split_title(pageno, ln["top"])
                    if current is not None:
                        finalize(current)
                    sl_no, date = ws[0]["text"], ws[1]["text"]
                    cheque, wd, dep, bal = "", None, None, None
                    inline_desc: list[str] = []
                    for w in ws[2:]:
                        if NUM_RE.match(w["text"]) and w["x0"] > cols["cheque_x_max"]:
                            amt = _parse_amount(w["text"])
                            if w["x1"] <= cols["withdrawal_x1_max"]:
                                wd = amt
                            elif w["x1"] <= cols["deposit_x1_max"]:
                                dep = amt
                            else:
                                bal = amt
                        elif cols["cheque_x_min"] <= w["x0"] < cols["cheque_x_max"]:
                            cheque += w["text"]
                        elif w["x0"] >= cols["remarks_x_min"]:
                            inline_desc.append(w["text"])
                    if bal is None:
                        current = None  # not a real transaction row
                    else:
                        current = {"sl": sl_no, "date": date, "cheque": cheque,
                                   "wd": wd, "dep": dep, "bal": bal,
                                   "title": title, "desc": inline_desc,
                                   "page": pageno}
                    continue

                # remark-zone-only lines
                if ws[0]["x0"] >= cols["remarks_x_min"]:
                    is_title = all("Black" in w.get("fontname", "") for w in ws)
                    if is_title:
                        title_buffer.append({"page": pageno, "top": ln["top"],
                                             "text": " ".join(w["text"] for w in ws)})
                    elif current is not None:
                        current["desc"].extend(w["text"] for w in ws)
                # anything else (page numbers, stray header fragments): ignore

        if current is not None:
            for ln in title_buffer:            # trailing black lines
                current["title"].append(ln["text"])
            finalize(current)

    if meta is None:
        raise ValueError("could not parse statement header")
    for r in rows:
        r.description = r.description.strip()
    return StatementExtract(meta=meta, rows=rows)


def _parse_meta(page1_text: str, source_file: str, layout: dict) -> StatementMeta:
    m = re.search(layout["header"]["account_line"], page1_text, re.M)
    account_no, currency, p_from, p_to = "", "INR", "", ""
    if m:
        account_no, currency = m.group(1), m.group(2)
        fmt = layout["header"]["period_date_format"]
        p_from = datetime.strptime(m.group(3).strip(), fmt).date().isoformat()
        p_to = datetime.strptime(m.group(4).strip(), fmt).date().isoformat()
    name = ""
    lines = [l.strip() for l in page1_text.split("\n") if l.strip()]
    for i, l in enumerate(lines):
        if "Statement of Transactions" in l and i + 1 < len(lines):
            name = lines[i + 1].split("Your Base Branch")[0].strip()
            break
    return StatementMeta(
        bank=layout["bank"], layout=layout["id"], account_no=account_no,
        account_name=name, period_from=p_from, period_to=p_to,
        source_file=source_file, currency=currency,
    )
