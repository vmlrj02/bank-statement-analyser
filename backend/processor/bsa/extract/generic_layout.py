"""Generic layout-driven parser — one parser, many banks, described in YAML.

Most Indian bank statements share a shape: a table header row, then dated
anchor lines carrying the amounts, with the narration wrapping onto extra
lines either above or below its anchor. What differs between banks is
geometry (which x range is which column) and wording, and both of those are
data, not code. So a layout YAML supplies them and this module does the rest:

    parse:
      row_anchor:        regex the first token of an anchor line must match
      continuation:      above | below   (where wrapped narration lives)
      columns:           x cutoffs; numeric columns keyed by right edge (x1)
      skip_rows:         substrings marking non-transaction rows
      footer_markers:    substrings after which the page is footer

Numeric columns are matched on the right edge because amounts are
right-aligned, so their left edge moves with digit count while the right edge
stays put. That single detail is what makes cutoffs portable across statements.

A bank whose narration needs font-face rules (ICICI's bold-title convention)
still warrants its own module; this is for the common case. Correctness is
gated the same way either way — validate() reconciles every running balance.
"""
from __future__ import annotations

import re
from datetime import datetime

import pdfplumber

from ..models import RawRow, StatementMeta, StatementExtract

NUM_RE = re.compile(r"^-?\d{1,3}(,\d{2,3})*\.\d{2}$|^-?\d+\.\d{2}$")


def _parse_amount(tok: str) -> float:
    return float(tok.replace(",", ""))


def _lines(words: list[dict], tol: float = 3.0) -> list[dict]:
    """Group words into visual lines by vertical position."""
    out: list[dict] = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if out and abs(w["top"] - out[-1]["top"]) <= tol:
            out[-1]["words"].append(w)
        else:
            out.append({"top": w["top"], "words": [w]})
    for ln in out:
        ln["words"].sort(key=lambda w: w["x0"])
    return out


def _amount_role(x1: float, cols: dict) -> str | None:
    """Which money column a right edge belongs to.

    `amount_bands` names each role explicitly because column ORDER is not
    universal — one ICICI export prints Withdrawals then Deposits, another
    prints Deposits then Withdrawals. The older ordered *_x1_max form is kept
    for layouts that were written against it.
    """
    if bands := cols.get("amount_bands"):
        for role in ("withdrawal", "deposit", "balance"):
            b = bands.get(role)
            if b and b[0] <= x1 <= b[1]:
                return role
        return None
    if x1 <= cols["withdrawal_x1_max"]:
        return "withdrawal"
    if x1 <= cols["deposit_x1_max"]:
        return "deposit"
    if x1 <= cols["balance_x1_max"]:
        return "balance"
    return None


def _meta(page1_text: str, source_file: str, layout: dict) -> StatementMeta:
    h = layout.get("header", {})
    account_no = p_from = p_to = ""
    if pattern := h.get("account_line"):
        if m := re.search(pattern, page1_text, re.M):
            g = m.groupdict()
            account_no = (g.get("account_no") or "").strip()
            fmt = h.get("period_date_format")
            for key, target in (("period_from", "p_from"), ("period_to", "p_to")):
                raw = (g.get(key) or "").strip()
                if raw and fmt:
                    try:
                        iso = datetime.strptime(raw, fmt).date().isoformat()
                    except ValueError:
                        iso = raw
                    if target == "p_from":
                        p_from = iso
                    else:
                        p_to = iso

    name = ""
    lines = [l.strip() for l in page1_text.split("\n") if l.strip()]
    idx = h.get("account_name_line")
    if isinstance(idx, int) and 0 <= idx < len(lines):
        name = lines[idx]

    return StatementMeta(
        bank=layout["bank"], layout=layout["id"], account_no=account_no,
        account_name=name, period_from=p_from, period_to=p_to,
        source_file=source_file, is_digital_text=True,
    )


def extract(pdf_path: str, source_file: str, layout: dict) -> StatementExtract:
    p = layout["parse"]
    cols = p["columns"]
    anchor_re = re.compile(p["row_anchor"])
    skip_rows = tuple(p.get("skip_rows", []))
    footers = tuple(p.get("footer_markers", []))
    header_words = set(p.get("table_header_words", []))
    header_offset = float(p.get("header_offset", 14))
    above = p.get("continuation", "below") == "above"
    # Some exports run the value date straight into the narration with no
    # separator ("01-07-2025BIL/Auto"), so it arrives as one token.
    strip_date = re.compile(p["strip_leading_date"]) if p.get("strip_leading_date") else None
    # A single PDF can concatenate two different exports of the same account —
    # customers download a range, the bank changes its format, and the parts are
    # merged. Each section declares a header regex and its own column geometry.
    sections = [(re.compile(sec["match"]), sec.get("columns") or {})
                for sec in p.get("sections", [])]

    rows: list[RawRow] = []
    meta: StatementMeta | None = None
    pending: list[str] = []          # narration seen before its anchor
    current: dict | None = None
    active = cols                    # column profile currently in force

    def finalize() -> None:
        nonlocal current
        if current is None:
            return
        rows.append(RawRow(
            sl_no=None, date=current["date"], cheque_no=current["cheque"].strip(),
            description=" ".join(current["desc"]).strip(),
            withdrawal=current["wd"], deposit=current["dep"],
            balance=current["bal"], page=current["page"],
        ))
        current = None

    with pdfplumber.open(pdf_path) as pdf:
        for pageno, page in enumerate(pdf.pages, start=1):
            words = page.extract_words()
            if pageno == 1:
                meta = _meta(page.extract_text() or "", source_file, layout)

            # The table header is typically printed on page 1 only, with
            # continuation pages starting straight into rows — so a page
            # without one is parsed whole rather than skipped. Require the
            # full header on a single line before trusting it as a cut point,
            # otherwise a stray "Debit" in a summary block swallows a page of
            # transactions.
            body_top = 0.0
            for ln in _lines(words):
                if header_words and header_words <= {w["text"] for w in ln["words"]}:
                    body_top = ln["top"] + header_offset
                    break

            for ln in _lines([w for w in words if w["top"] > body_top]):
                ws = ln["words"]
                text = " ".join(w["text"] for w in ws)
                if any(m in text for m in footers):
                    break
                switched = False
                for rx, scols in sections:
                    if rx.search(text):
                        finalize()
                        pending.clear()
                        active = scols or cols
                        switched = True
                        break
                if switched:
                    continue
                if any(m in text for m in skip_rows):
                    pending.clear()
                    continue

                first = ws[0]
                is_anchor = (first["x0"] < active["date_x_max"]
                             and anchor_re.match(first["text"]))

                if is_anchor:
                    # 'above' banks wrap narration before the dated line, so the
                    # buffer belongs to this row; 'below' banks already appended
                    # theirs to the previous row.
                    finalize()
                    cheque, wd, dep, bal = "", None, None, None
                    desc = list(pending) if above else []
                    pending.clear()
                    for w in ws[1:]:
                        if w["x0"] >= active.get("tail_x_min", 1e9):
                            continue                  # trailing branch/init code
                        if NUM_RE.match(w["text"]) and w["x0"] > active["remarks_x_min"]:
                            role = _amount_role(w["x1"], active)
                            if role == "withdrawal":
                                wd = _parse_amount(w["text"])
                            elif role == "deposit":
                                dep = _parse_amount(w["text"])
                            elif role == "balance":
                                bal = _parse_amount(w["text"])
                        elif active["cheque_x_min"] <= w["x0"] < active["cheque_x_max"]:
                            cheque += w["text"]
                        elif w["x0"] >= active["remarks_x_min"]:
                            desc.append(w["text"])
                    if strip_date and desc:
                        desc[0] = strip_date.sub("", desc[0], count=1)
                        if not desc[0]:
                            desc.pop(0)
                    if bal is None:
                        current = None       # not a transaction row
                        continue
                    current = {"date": first["text"], "cheque": cheque,
                               "wd": wd, "dep": dep, "bal": bal,
                               "desc": desc, "page": pageno}
                    continue

                # narration-only line
                zone = [w["text"] for w in ws
                        if active["remarks_x_min"] <= w["x0"] < active.get(
                            "remarks_x_max", 1e9)]
                if not zone:
                    continue
                if above:
                    pending.extend(zone)
                elif current is not None:
                    current["desc"].extend(zone)

        finalize()

    if meta is None:
        raise ValueError("could not parse statement header")
    return StatementExtract(meta=meta, rows=rows)
