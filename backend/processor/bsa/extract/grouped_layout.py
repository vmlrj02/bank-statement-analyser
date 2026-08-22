"""Grouped layout parser — amount-anchored rows, sparse dates and balances.

Some co-operative bank exports do not print one self-contained line per
transaction. Instead:

  * the DATE appears only when it changes, so later rows in the same day have
    an empty date column;
  * the BALANCE appears only at the end of a group of same-day transactions,
    so intermediate rows have an empty balance column;
  * the narration sits on the lines FOLLOWING the amounts, not beside them.

So the anchor cannot be a date — it is the presence of an amount in the debit
or credit column. The date is carried forward, the narration is collected from
the lines that follow, and a missing balance is derived from the previous row.

That derivation is worth being explicit about: a derived balance reconciles by
construction, so it proves nothing on its own. What still holds is the group —
the next PRINTED balance has to match the running total of every derived row
before it, so a misread amount inside a group is caught at the group's end.
Rows whose balance was derived are marked so this is visible downstream.
"""
from __future__ import annotations

import re
from datetime import datetime

import pdfplumber

from ..models import RawRow, StatementMeta, StatementExtract

_NUM = re.compile(r"^-?[\d,]*\.?\d+$")


def _amount(tok: str):
    s = (tok or "").replace(",", "").strip()
    if not s or not _NUM.match(s):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _lines(words: list[dict], tol: float = 2.5) -> list[dict]:
    out: list[dict] = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if out and abs(w["top"] - out[-1]["top"]) <= tol:
            out[-1]["words"].append(w)
        else:
            out.append({"top": w["top"], "words": [w]})
    for ln in out:
        ln["words"].sort(key=lambda w: w["x0"])
    return out


def _meta(page1_text: str, source_file: str, layout: dict) -> StatementMeta:
    h = layout.get("header", {})
    acct = name = pf = pt = ""
    if pat := h.get("account_line"):
        if m := re.search(pat, page1_text, re.M):
            acct = (m.groupdict().get("account_no") or "").strip()
    if pat := h.get("period_line"):
        if m := re.search(pat, page1_text, re.M):
            g, fmt = m.groupdict(), h.get("period_date_format")
            for k in ("period_from", "period_to"):
                raw = (g.get(k) or "").strip()
                if raw and fmt:
                    try:
                        raw = datetime.strptime(raw, fmt).date().isoformat()
                    except ValueError:
                        pass
                if k == "period_from":
                    pf = raw
                else:
                    pt = raw
    if pat := h.get("account_name"):
        if m := re.search(pat, page1_text, re.M):
            name = (m.group(1) or "").strip()
    return StatementMeta(bank=layout["bank"], layout=layout["id"], account_no=acct,
                         account_name=name, period_from=pf, period_to=pt,
                         source_file=source_file, is_digital_text=True)


def extract(pdf_path: str, source_file: str, layout: dict) -> StatementExtract:
    p = layout["parse"]
    c = p["columns"]
    date_re = re.compile(p.get("date_pattern", r"^\d{2}/\d{2}/\d{4}$"))
    ignores = tuple(p.get("ignore_lines", []))
    stops = tuple(p.get("end_markers", []))
    opening_re = re.compile(p["opening_balance"]) if p.get("opening_balance") else None
    # Everything above the column header is address and summary text; a customer
    # id or account number there parses as an "amount" and invents rows. Capture
    # only after the header line, re-armed on every page since it repeats.
    start_after = p.get("start_after")

    def band(w, key):
        lo, hi = c[key]
        return lo <= w["x0"] < hi

    rows: list[RawRow] = []
    derived: list[bool] = []
    meta: StatementMeta | None = None
    carried_date = ""
    opening = None
    cur: dict | None = None

    def flush():
        nonlocal cur
        if cur is None:
            return
        rows.append(RawRow(
            sl_no=None, date=cur["date"], cheque_no=cur["chq"],
            description=re.sub(r"\s+", " ", " ".join(cur["desc"])).strip(),
            withdrawal=cur["wd"], deposit=cur["dep"],
            balance=cur["bal"] if cur["bal"] is not None else 0.0,
            page=cur["page"]))
        derived.append(cur["bal"] is None)
        cur = None

    with pdfplumber.open(pdf_path) as pdf:
        stop = False
        for pageno, page in enumerate(pdf.pages, start=1):
            if stop:
                break
            words = page.extract_words()
            if pageno == 1:
                text = page.extract_text() or ""
                meta = _meta(text, source_file, layout)
                if opening_re and (m := opening_re.search(text)):
                    opening = _amount(m.group(1))
            armed = start_after is None
            for ln in _lines(words):
                ws = ln["words"]
                text = " ".join(w["text"] for w in ws)
                if any(s in text for s in stops):
                    stop = True
                    break
                if not armed:
                    if start_after in text:
                        armed = True
                    continue
                if any(x in text for x in ignores):
                    continue

                for w in ws:                       # a date anywhere updates the carry
                    if band(w, "date") and date_re.match(w["text"]):
                        carried_date = w["text"]

                wd = dep = bal = None
                for w in ws:
                    v = _amount(w["text"])
                    if v is None:
                        continue
                    if band(w, "debit"):
                        wd = v
                    elif band(w, "credit"):
                        dep = v
                    elif band(w, "balance"):
                        bal = v
                narration = [w["text"] for w in ws if band(w, "particulars")]

                if wd is not None or dep is not None:
                    flush()                        # an amount line starts a new row
                    cur = {"date": carried_date, "chq": "", "wd": wd, "dep": dep,
                           "bal": bal, "desc": list(narration), "page": pageno}
                elif cur is not None:
                    if bal is not None and cur["bal"] is None:
                        cur["bal"] = bal           # balance printed on a later line
                    cur["desc"].extend(narration)
        flush()

    # Fill the balances the statement left out, forward from the opening figure.
    prev = opening
    for r, was_derived in zip(rows, derived):
        amount = (r.deposit or 0.0) - (r.withdrawal or 0.0)
        if was_derived:
            r.balance = round((prev if prev is not None else 0.0) + amount, 2)
        prev = r.balance

    if meta is None:
        raise ValueError("could not parse statement header")
    return StatementExtract(meta=meta, rows=rows)
