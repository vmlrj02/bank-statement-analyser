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


def _wrapped_balance(body: list[dict], li: int, cols: dict,
                     anchor_top: float, tol: float = 9.0):
    """Recover a balance whose cell WRAPPED off the anchor line.

    SBI's "STATEMENT OF ACCOUNT" export fits "9,99,935.00CR" in the balance
    column, but once the account crosses ten lakh the value no longer fits and
    the cell wraps: the NUMBER prints on the narration line above the dated
    anchor and the bare CR/DR suffix on the line below. The anchor line then
    carries no balance token at all, and every such row used to be dropped
    silently — 335 of 1538 rows on a 71-page passbook, every stretch where the
    balance stayed above 10,00,000 (the running-balance chain broke only where
    the drops resumed, so 9 issues hid two whole missing weeks).

    Look one visual line either side of the anchor (same row block: narration
    sits ~5pt away, the next block ~25pt) for a numeric token whose RIGHT edge
    lands in the balance band, and a bare CR/DR token in that band for the
    sign. Returns (balance | None, is_dr).
    """
    num, dr = None, False
    for j in (li - 1, li + 1):
        if not (0 <= j < len(body)) or abs(body[j]["top"] - anchor_top) > tol:
            continue
        for w in body[j]["words"]:
            if _amount_role(w["x1"], cols) != "balance":
                continue
            t = w["text"]
            if NUM_RE.match(t):
                if num is None:
                    num = _parse_amount(t)
                    if re.search(r"(?i)DR$", t):
                        dr = True
            elif t.rstrip(".").upper() in ("CR", "DR"):
                dr = dr or t.rstrip(".").upper() == "DR"
    return num, dr


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


# Slack on "this line reached the cell's right edge". The last glyph's width
# varies, so full lines end within a few points of each other.
HARD_WRAP_TOL = 6.0


def _join_narration(lines: list[tuple], edge: float = 0.0,
                    tol: float = HARD_WRAP_TOL) -> str:
    """Rejoin a row's narration lines, deciding each join by where the PREVIOUS
    line ended.

    `lines` is [(text, right_edge), ...] in reading order.

    With edge=0 — the default, and every layout that has not opted into
    `narration_wrap: hard` — nothing can reach the edge, so every join is a
    space and the result is character-for-character what joining all the words
    with spaces produced before. That is what keeps this change invisible to
    the other thirty layouts.

    With a real edge: a line that runs out to it was cut mid-token by the cell
    and rejoins with NOTHING ("…/Miscell" + "aneou" -> "…/Miscellaneous"); a
    line that stops short of it broke at a space and rejoins with one
    ("CHARGES FOR" + ":IMPS/…"). Both happen in the same statement, which is
    why this is per line rather than per layout.

    The test is only trustworthy when the cell is WIDE. In a narrow cell a word
    that merely fills the line lands within the tolerance too — measured on
    PNB, whose 64pt cell makes "INCIDENTAL" (ending at 225) indistinguishable
    from a genuine mid-token cut at 226.2. So `narration_wrap: hard` is opt-in
    per layout, and belongs only on a bank whose statement has been read.
    """
    out = ""
    for i, (text, _x1) in enumerate(lines):
        if not text:
            continue
        if not out:
            out = text
            continue
        prev_x1 = lines[i - 1][1]
        # A line can also be cut mid-token WITHOUT reaching the edge, when the
        # remainder of the token was too wide to fit at all: BoB breaks
        # "paytm-53817591@ptyb" after the hyphen at x1 274 in a cell that runs
        # to 319. A trailing hyphen or slash at a wrap point is a continuation
        # marker, not punctuation the bank meant to print at line end.
        glued = edge and (prev_x1 >= edge - tol
                          or lines[i - 1][0].endswith(("-", "/")))
        out += ("" if glued else " ") + text
    return out.strip()


def _flush_nearest(anchors: list[dict], narrs: list[tuple], rows: list,
                   invert: bool = False, max_gap: float | None = None,
                   edge: float = 0.0) -> None:
    """Assign buffered narration lines to the vertically NEAREST anchor, then
    emit the anchors in reading order.

    Some exports centre a row's block on the amount line: a two-line narration
    prints as narration / dated-amount-line / narration, with ~3.5pt inside a
    block and ~13pt between blocks. Neither 'above' nor 'below' can place both
    halves — 'below' glued every block's first line onto the PREVIOUS row,
    which shifted every description one row off (seen on ICICI's combined
    statement, 995 rows all mislabelled). Distance to the anchor line is the
    only signal that is right for both halves.

    `max_gap` (from the layout's nearest_max_gap) handles the page-break spill:
    when a block's anchor is the LAST line of a page, PNB prints its narration
    at the TOP of the next page — nearer to that page's first anchor than to
    its own, which both merged two UPI refs into one row and left the real
    owner with an empty description. A narration line sitting ABOVE every
    anchor of its segment and farther than max_gap from the nearest one
    belongs to the previously emitted row.
    """
    for ntop, zone, zx1 in narrs:
        if not anchors:
            continue
        a = min(anchors, key=lambda a: abs(a["top"] - ntop))
        if (max_gap is not None and abs(a["top"] - ntop) > max_gap
                and rows and all(ntop < x["top"] for x in anchors)):
            rows[-1].description = \
                f"{rows[-1].description} {' '.join(zone)}".strip()
            continue
        a["parts"].append((ntop, zone, zx1))
    for a in sorted(anchors, key=lambda a: a["top"]):
        # Words within one physical line always join with a space; the LINES
        # join by the wrap rule (see _join_narration).
        lines = [(" ".join(zone).strip(), zx1)
                 for _, zone, zx1 in sorted(a["parts"], key=lambda x: x[0])]
        rows.append(RawRow(
            sl_no=None, date=a["date"], cheque_no=a["cheque"].strip(),
            description=_join_narration(lines, edge),
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
    # Opt-in: this bank wraps the narration cell mid-token (see
    # _join_narration). Off means every join is a space, exactly as before.
    hard_wrap = p.get("narration_wrap") == "hard"
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
    # Opt-in: the balance cell can WRAP off the anchor line once the value
    # outgrows its column (see _wrapped_balance). Only a layout that declares
    # it pays the neighbour-line scan.
    wrap_bal = bool(p.get("wrapped_balance"))
    # nearest mode: a narration line farther than this from every anchor of its
    # segment, and above them all, is a page-break spill belonging to the
    # previous emitted row (see _flush_nearest). None keeps pure-nearest.
    nearest_gap = p.get("nearest_max_gap")
    nearest_gap = float(nearest_gap) if nearest_gap is not None else None
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
            # The anchor's own narration line, then each continuation line as
            # its own unit, joined by the wrap rule (see _join_narration).
            description=_join_narration(
                [(" ".join(current["desc"]).strip(), current["desc_x1"])]
                + current["cont"], narr_edge),
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
        # A dated anchor line is NEVER furniture, even when its exact text
        # repeats on another page: two genuine transactions can print
        # identically (same date, same amount, and a running balance that
        # returns to the same value — seen on a real SBI passbook, where two
        # 20,000 debits five rows apart matched byte for byte and both were
        # dropped as a "repeated footer"). Test against the widest date column
        # any section declares, so a section profile can't hide an anchor.
        date_x_lim = max([cols.get("date_x_max", 0)] +
                         [sec.get("columns", {}).get("date_x_max", 0)
                          for sec in p.get("sections", [])])

        def _anchorish(ln) -> bool:
            ws = ln["words"]
            if not ws or ws[0]["x0"] >= date_x_lim:
                return False
            if anchor_re.match(ws[0]["text"]):
                return True
            if date_parts > 1:
                toks = [w["text"] for w in ws[:date_parts]
                        if w["x0"] < date_x_lim]
                return bool(anchor_re.match(" ".join(toks)))
            return False

        # A line lying WHOLLY inside the narration column is content, never
        # page furniture. Page furniture is laid out against the PAGE — a
        # title, a footer, a disclaimer all begin at the margin and run across
        # the column boundaries — whereas a wrapped narration fragment is
        # bounded by its cell.
        #
        # Without this, a bank that wraps mid-token loses text: BoB splits a
        # payee VPA across lines, so "53817591@ptys" is its own line and the
        # same merchant recurs on many pages, which made a REAL fragment look
        # like a repeated footer and deleted it. Nothing catches that
        # afterwards — the amounts sit on the dated line, so the balance chain
        # still reconciles and the row simply carries a truncated narration.
        nlo = cols.get("remarks_x_min")
        nhi = cols.get("remarks_x_max")

        def _narration_only(ln) -> bool:
            if nlo is None or nhi is None:
                return False
            return all(nlo <= w["x0"] and w["x1"] <= nhi for w in ln["words"])

        for pageno, page in enumerate(pdf.pages, start=1):
            ws = page.extract_words()
            pages_words.append(ws)
            if pageno == 1:
                page1_text = page.extract_text() or ""
            if pageno <= 3:
                head_texts.append(page.extract_text() or "")
            for ln in _lines(ws):
                t = " ".join(w["text"] for w in ln["words"])
                if len(t) > 8 and not _anchorish(ln) and not _narration_only(ln):
                    line_pages.setdefault(t, set()).add(pageno)
        furniture = {t for t, pgs in line_pages.items() if len(pgs) >= 2}
        # Where the narration cell actually ends, MEASURED rather than
        # declared: the furthest right a narration word reaches anywhere in the
        # document. The layout's remarks_x_max is only a cutoff — it has to sit
        # somewhere between this column and the next — while the wrap test
        # needs the true edge. Words are counted only if they lie WHOLLY inside
        # the band: page furniture printed across a column boundary starts
        # inside it and runs far past, and one such word would put the edge out
        # of reach so every join silently fell back to a space.
        narr_edge = 0.0
        if hard_wrap:
            lo, hi = cols["remarks_x_min"], cols.get("remarks_x_max", 1e9)
            narr_edge = max([w["x1"] for ws_ in pages_words for w in ws_
                             if lo <= w["x0"] and w["x1"] <= hi] or [0.0])
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

            body_lines = _lines([w for w in words if w["top"] > body_top])
            # Narration HUGS its anchor (a few pt), while real page furniture
            # sits well clear of the table rows — so a furniture match is only
            # honoured away from every anchor line. Without this, a recurring
            # transaction's narration ("ACHDr HDFC02165..." on a monthly ACH
            # debit, printed identically month after month) matched the
            # repeated-line rule and left the row with an EMPTY description.
            anchor_tops = [l["top"] for l in body_lines if _anchorish(l)]
            for li, ln in enumerate(body_lines):
                ws = ln["words"]
                text = " ".join(w["text"] for w in ws)
                if any(m in text for m in footers):
                    break
                if text in furniture and not any(
                        abs(ln["top"] - t) <= 10.0 for t in anchor_tops):
                    continue                 # repeated footer/header — not a row
                switched = False
                for rx, scols in sections:
                    if rx.search(text):
                        finalize()
                        pending.clear()
                        _flush_nearest(seg_anchors, seg_narrs, rows, invert, nearest_gap,
                                       narr_edge)
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
                    desc_x1 = 0.0        # how far right the anchor's narration ran
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
                            desc_x1 = max(desc_x1, w["x1"])
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
                    if bal is None and wrap_bal and (
                            wd is not None or dep is not None
                            or amount_val is not None):
                        bal, wrapped_dr = _wrapped_balance(
                            body_lines, li, active, ln["top"])
                        if bal is not None and wrapped_dr:
                            bal_dr = True
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
                            "parts": [(ln["top"], desc, desc_x1)] if desc else []})
                        continue
                    current = {"date": date_str, "cheque": cheque,
                               "wd": wd, "dep": dep, "bal": bal,
                               "desc": desc, "desc_x1": desc_x1, "cont": [],
                               "page": pageno, "opening": opening}
                    continue

                # narration-only line
                zw = [w for w in ws
                      if active["remarks_x_min"] <= w["x0"] < active.get(
                          "remarks_x_max", 1e9)]
                zone = [w["text"] for w in zw]
                if not zone:
                    continue
                if nearest:
                    seg_narrs.append((ln["top"], zone, max(w["x1"] for w in zw)))
                elif above:
                    pending.extend(zone)
                elif current is not None:
                    # Kept as its own line, not flattened into the word list,
                    # because the JOIN depends on where the previous line ended.
                    current["cont"].append(
                        (" ".join(zone).strip(), max(w["x1"] for w in zw)))

            # Blocks do not span pages in a centred layout, so a page is a
            # complete segment: assign its narration lines and emit its rows.
            _flush_nearest(seg_anchors, seg_narrs, rows, invert, nearest_gap,
                           narr_edge)

        finalize()

    if meta is None:
        raise ValueError("could not parse statement header")
    # Some banks print newest-first (PNB); reverse to oldest-first so the
    # running-balance chain reconciles forward like every other layout.
    if p.get("reverse"):
        rows.reverse()
    return StatementExtract(meta=meta, rows=rows)
