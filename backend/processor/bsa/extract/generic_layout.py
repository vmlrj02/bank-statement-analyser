"""Generic layout-driven parser — one parser, many banks, described in YAML.

Most Indian bank statements share a shape: a table header row, then dated
anchor lines carrying the amounts, with the narration wrapping onto extra
lines either above or below its anchor. What differs between banks is
geometry (which x range is which column) and wording, and both of those are
data, not code. So a layout YAML supplies them and this module does the rest:

    parse:
      row_anchor:        regex the first token of an anchor line must match
      continuation:      above | below | nearest  (where wrapped narration lives)
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

# Amounts carry a decimal part: two places on almost every bank, but PNB prints
# one ("157.7"), so accept one or two. Requiring a decimal point still keeps
# bare integers (serial numbers, ref codes) out of the amount columns. Some SBI
# exports glue a CR/DR flag onto the BALANCE ("2,47,946.81CR"), so allow an
# optional trailing CR/DR — _parse_amount strips it and negates a DR balance.
# The final alternative accepts a sub-rupee amount printed with no leading zero
# ("GST @18% ... .90"), which Axis does — without it those rows are dropped and
# the balance chain breaks by exactly that amount (seen on an Axis cash-credit
# statement: 51 breaks, all 0.90 GST-on-charge rows).
NUM_RE = re.compile(
    r"^-?\d{1,3}(,\d{2,3})*\.\d{1,2}(CR|DR)?$|^-?\d+\.\d{1,2}(CR|DR)?$"
    r"|^-?\.\d{1,2}(CR|DR)?$", re.I)


def _parse_amount(tok: str) -> float:
    neg = bool(re.search(r"DR$", tok, re.I))
    v = float(re.sub(r"(?i)(CR|DR)$", "", tok).replace(",", ""))
    return -v if neg else v


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
    # Single-amount-column exports: one Amount column whose sign comes from a
    # separate DR/CR flag, not from which column it sits in (Axis cash-credit
    # "Report" export, PNB). The flag is read separately; here we only place the
    # number as "amount" or "balance".
    if (amt := cols.get("amount_x1_max")) is not None:
        if x1 <= amt:
            return "amount"
        if x1 <= cols["balance_x1_max"]:
            return "balance"
        return None
    if x1 <= cols["withdrawal_x1_max"]:
        return "withdrawal"
    if x1 <= cols["deposit_x1_max"]:
        return "deposit"
    if x1 <= cols["balance_x1_max"]:
        return "balance"
    return None


def _complete_year(day_month: str, meta) -> str:
    """Append the year to a "17 Aug" date whose year wrapped to the next line.

    Chooses the year (from the statement period) that places the date inside the
    period — so a Dec/Jan boundary resolves correctly — and falls back to the
    period's start year. If no period is known, returns the input unchanged and
    the row fails to parse loudly rather than being silently misdated.
    """
    pf = getattr(meta, "period_from", "") or ""
    pt = getattr(meta, "period_to", "") or ""
    years = [y for y in (pf[:4], pt[:4]) if y.isdigit()]
    for y in dict.fromkeys(years):                       # de-dup, keep order
        for fmt in ("%d %b %Y", "%d %B %Y"):
            try:
                iso = datetime.strptime(f"{day_month} {y}", fmt).date().isoformat()
            except ValueError:
                continue
            if (not pf or iso >= pf) and (not pt or iso <= pt):
                return f"{day_month} {y}"
    return f"{day_month} {years[0]}" if years else day_month


def _flush_nearest(anchors: list[dict], narrs: list[tuple], rows: list,
                   invert: bool = False) -> None:
    """Assign buffered narration lines to the vertically NEAREST anchor, then
    emit the anchors in reading order.

    Some exports centre a row's block on the amount line: a two-line narration
    prints as narration / dated-amount-line / narration, with ~3.5pt inside a
    block and ~13pt between blocks. Neither 'above' nor 'below' can place both
    halves — 'below' glued every block's first line onto the PREVIOUS row,
    which shifted every description one row off (seen on ICICI's combined
    statement, 995 rows all mislabelled). Distance to the anchor line is the
    only signal that is right for both halves.
    """
    for ntop, zone in narrs:
        if not anchors:
            continue
        a = min(anchors, key=lambda a: abs(a["top"] - ntop))
        a["parts"].append((ntop, zone))
    for a in sorted(anchors, key=lambda a: a["top"]):
        desc = [w for _, zone in sorted(a["parts"], key=lambda x: x[0])
                for w in zone]
        rows.append(RawRow(
            sl_no=None, date=a["date"], cheque_no=a["cheque"].strip(),
            description=" ".join(desc).strip(),
            withdrawal=a["wd"], deposit=a["dep"],
            balance=a["bal"], page=a["page"],
            balance_inverted=invert, is_opening=a.get("opening", False),
        ))
    anchors.clear()
    narrs.clear()


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
    # Some statements print the holder inline with the account number rather
    # than on a predictable line, so allow a regex as well.
    if pat := h.get("account_name"):
        if m := re.search(pat, page1_text, re.M):
            name = (m.group(1) or "").strip()

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
    continuation = p.get("continuation", "below")
    above = continuation == "above"
    nearest = continuation == "nearest"
    invert = bool(p.get("invert_balance"))     # cash-credit / overdraft chain
    # Most layouts print the narration first and the money columns to its right,
    # so a number is only an amount if it sits right of remarks_x_min (this keeps
    # a ref number inside the narration from being read as an amount). A few
    # (PNB "Statement of Account") invert that — Withdrawal/Deposit/Balance sit
    # to the LEFT of the narration — so the guard flips.
    amounts_left = bool(p.get("amounts_left"))
    bal_tol = float(p.get("balance_tolerance", 0.0))   # display-truncation slack
    cf_re = re.compile(p["carry_forward"]) if p.get("carry_forward") else None
    date_parts = int(p.get("date_parts", 1))   # tokens forming a multi-word date
    infer_year = bool(p.get("infer_year_from_period", False))
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
    seg_anchors: list[dict] = []     # nearest mode: anchors of this segment
    seg_narrs: list[tuple] = []      # nearest mode: (top, words) narration

    def finalize() -> None:
        nonlocal current
        if current is None:
            return
        rows.append(RawRow(
            sl_no=None, date=current["date"], cheque_no=current["cheque"].strip(),
            description=" ".join(current["desc"]).strip(),
            withdrawal=current["wd"], deposit=current["dep"],
            balance=current["bal"], page=current["page"],
            balance_inverted=invert, balance_tolerance=bal_tol,
            is_opening=current.get("opening", False),
        ))
        current = None

    with pdfplumber.open(pdf_path) as pdf:
        # Structural page furniture: a line whose exact text repeats on two or
        # more pages is a footer/header disclaimer, never a transaction (a real
        # row is unique within a statement). Dropping these catches the "footer
        # getting added to the description" case on ANY bank, without each layout
        # having to hand-list every footer phrase. Built in one pre-pass; the
        # per-page words are cached so pages are read only once.
        pages_words, page1_text = [], ""
        head_texts: list[str] = []
        line_pages: dict[str, set] = {}
        for pageno, page in enumerate(pdf.pages, start=1):
            ws = page.extract_words()
            pages_words.append(ws)
            if pageno == 1:
                page1_text = page.extract_text() or ""
            if pageno <= 3:
                head_texts.append(page.extract_text() or "")
            for ln in _lines(ws):
                t = " ".join(w["text"] for w in ln["words"])
                if len(t) > 8:
                    line_pages.setdefault(t, set()).add(pageno)
        furniture = {t for t, pgs in line_pages.items() if len(pgs) >= 2}
        # Most statements carry the account line on page 1, but some print the
        # per-account transaction header on the transaction page instead (ICICI's
        # monthly export opens with a cover summary and only names the account
        # above the table on page 2). Feed the first few pages to _meta so its
        # account_line still resolves; page 1 stays first, so any page-1 match
        # still wins and account_name_line: 0 is unaffected.
        meta = _meta("\n".join(head_texts) if head_texts else page1_text,
                     source_file, layout)

        for pageno, words in enumerate(pages_words, start=1):
            # A section header naming this page's format is often printed in the
            # page furniture ABOVE the table header, so it never reaches the
            # body-line section switch below (a single ICICI PDF can splice the
            # old "in Currency" combined format and the new "in INR" monthly
            # format, each page carrying its own header). Pick the profile from
            # the whole page's text so the right column geometry is in force for
            # the first row. A page with no section header keeps the current one.
            if sections:
                page_text = " ".join(w["text"] for w in words)
                for rx, scols in sections:
                    if rx.search(page_text):
                        active = scols or cols
                        break
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
                if text in furniture:
                    continue                 # repeated footer/header — not a row
                switched = False
                for rx, scols in sections:
                    if rx.search(text):
                        finalize()
                        pending.clear()
                        _flush_nearest(seg_anchors, seg_narrs, rows, invert)
                        active = scols or cols
                        switched = True
                        break
                if switched:
                    continue
                if any(m in text for m in skip_rows):
                    pending.clear()
                    continue

                # Some exports lead each row with a serial number before the
                # date column (Axis cash-credit "Report"). Drop tokens left of
                # the date column so the date is still ws[0] for the scan below.
                if (slx := active.get("sl_no_x_max")) is not None:
                    ws = [w for w in ws if w["x0"] >= slx]
                    if not ws:
                        continue
                first = ws[0]
                # Most banks print the date as one token ("01/07/2025"); a few
                # print it as several ("1 Jul 2025"), so date_parts gathers the
                # leading date-column tokens (by x, up to date_parts) and
                # excludes them from the column scan. Gathering by x — not a
                # fixed count — matters because SBI wraps the YEAR of a
                # two-digit-day date onto the next line ("17 Aug" on the anchor,
                # "2025" below it), so the anchor carries only two tokens there.
                # Default 1 keeps every existing layout unchanged.
                if date_parts > 1:
                    dtoks = []
                    for w in ws:
                        if w["x0"] < active["date_x_max"]:
                            dtoks.append(w)
                        else:
                            break
                    dtoks = dtoks[:date_parts]
                    date_str = " ".join(w["text"] for w in dtoks)
                    rest = ws[len(dtoks):]
                else:
                    date_str = first["text"]
                    rest = ws[1:]
                is_anchor = (first["x0"] < active["date_x_max"]
                             and anchor_re.match(date_str))
                # A year-less date (SBI's wrapped year) is completed from the
                # statement period, choosing the year that puts it in range.
                if is_anchor and infer_year and not re.search(r"\d{4}", date_str):
                    date_str = _complete_year(date_str, meta)

                if is_anchor:
                    # 'above' banks wrap narration before the dated line, so the
                    # buffer belongs to this row; 'below' banks already appended
                    # theirs to the previous row.
                    finalize()
                    cheque, wd, dep, bal = "", None, None, None
                    amount_val, dir_flag = None, ""
                    bal_dr = False
                    tb = active.get("type_band")   # [x_min, x_max] of DR/CR flag
                    cb = active.get("balance_crdr_band")   # Cr./Dr. after balance
                    debit_flags = active.get("debit_flags", ("DR", "Dr", "D"))
                    desc = list(pending) if above else []
                    pending.clear()
                    for w in rest:
                        if w["x0"] >= active.get("tail_x_min", 1e9):
                            continue                  # trailing branch/init code
                        if tb and tb[0] <= w["x0"] < tb[1] and w["text"] in (
                                "DR", "CR", "Dr", "Cr", "D", "C"):
                            dir_flag = w["text"]
                            continue
                        # A Cr./Dr. token in its own column marks the balance's
                        # SIGN (PNB prints the balance as a magnitude, so a CA
                        # that goes overdrawn shows "116646.77 Dr."). Negate on Dr.
                        if cb and cb[0] <= w["x0"] < cb[1] and \
                                w["text"].rstrip(".").upper() in ("CR", "DR"):
                            bal_dr = w["text"].rstrip(".").upper() == "DR"
                            continue
                        in_money = (w["x0"] < active["remarks_x_min"] if amounts_left
                                    else w["x0"] > active["remarks_x_min"])
                        if NUM_RE.match(w["text"]) and in_money:
                            role = _amount_role(w["x1"], active)
                            if role == "withdrawal":
                                wd = _parse_amount(w["text"])
                            elif role == "deposit":
                                dep = _parse_amount(w["text"])
                            elif role == "amount":
                                amount_val = _parse_amount(w["text"])
                            elif role == "balance":
                                bal = _parse_amount(w["text"])
                        elif active["cheque_x_min"] <= w["x0"] < active["cheque_x_max"]:
                            cheque += w["text"]
                        elif (active["remarks_x_min"] <= w["x0"]
                              < active.get("remarks_x_max", 1e9)):
                            # honour the right edge on the anchor line too, so a
                            # trailing column (SBI prints a Branch Code between
                            # narration and amounts) stays out of the narration
                            desc.append(w["text"])
                    # Single-amount-column export: resolve the sign from the flag.
                    if amount_val is not None:
                        if dir_flag in debit_flags:
                            wd = amount_val
                        else:
                            dep = amount_val
                    if strip_date and desc:
                        desc[0] = strip_date.sub("", desc[0], count=1)
                        if not desc[0]:
                            desc.pop(0)
                    if bal is None:
                        current = None       # not a transaction row
                        continue
                    if bal_dr:               # balance printed as an overdrawn magnitude
                        bal = -abs(bal)
                    opening = bool(cf_re and cf_re.search(text))
                    if nearest:
                        seg_anchors.append({
                            "top": ln["top"], "date": date_str,
                            "cheque": cheque, "wd": wd, "dep": dep, "bal": bal,
                            "page": pageno, "opening": opening,
                            "parts": [(ln["top"], desc)] if desc else []})
                        continue
                    current = {"date": date_str, "cheque": cheque,
                               "wd": wd, "dep": dep, "bal": bal,
                               "desc": desc, "page": pageno, "opening": opening}
                    continue

                # narration-only line
                zone = [w["text"] for w in ws
                        if active["remarks_x_min"] <= w["x0"] < active.get(
                            "remarks_x_max", 1e9)]
                if not zone:
                    continue
                if nearest:
                    seg_narrs.append((ln["top"], zone))
                elif above:
                    pending.extend(zone)
                elif current is not None:
                    current["desc"].extend(zone)

            # Blocks do not span pages in a centred layout, so a page is a
            # complete segment: assign its narration lines and emit its rows.
            _flush_nearest(seg_anchors, seg_narrs, rows, invert)

        finalize()

    if meta is None:
        raise ValueError("could not parse statement header")
    # Some banks print newest-first (PNB); reverse to oldest-first so the
    # running-balance chain reconciles forward like every other layout.
    if p.get("reverse"):
        rows.reverse()
    return StatementExtract(meta=meta, rows=rows)
