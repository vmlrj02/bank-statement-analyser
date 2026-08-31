"""The generic parser's line-to-row assignment. ICICI's combined statement
centres a row's block on the amount line — narration above AND below it —
which neither 'above' nor 'below' can place: 'below' glued every block's
first line onto the previous row, shifting all 995 descriptions one row off.
'nearest' assigns each narration line to the vertically closest anchor."""
from bsa.extract.generic_layout import _flush_nearest


def anchor(top, date, bal, own=None, own_x1=0.0):
    return {"top": top, "date": date, "cheque": "", "wd": None, "dep": 100.0,
            "bal": bal, "page": 1,
            "parts": [(top, own, own_x1)] if own else []}


def test_centred_blocks_keep_each_narration_with_its_own_row():
    # Real geometry from the ICICI statement: rows are ~12.8pt apart, and a
    # wrapped block prints narration 3.6pt above and 3.6pt below its anchor.
    rows = []
    anchors = [
        anchor(414.94, "04-07-2025", 5024530.09, own=["BY", "CASH"]),
        anchor(431.37, "05-07-2025", 5040788.09),          # narration is all wrapped
    ]
    narrs = [
        (427.76, ["NEFT-REF-DURGA", "SWEETS"], 0.0),   # above its anchor (431.37)
        (434.97, ["CORNER-0001"], 0.0),                # below the same anchor
    ]
    _flush_nearest(anchors, narrs, rows)
    assert [r.description for r in rows] == \
        ["BY CASH", "NEFT-REF-DURGA SWEETS CORNER-0001"]
    assert [r.date for r in rows] == ["04-07-2025", "05-07-2025"]


def test_rows_come_out_in_reading_order_even_if_buffered_otherwise():
    rows = []
    anchors = [anchor(200.0, "02-01-2026", 900.0, own=["second"]),
               anchor(100.0, "01-01-2026", 800.0, own=["first"])]
    _flush_nearest(anchors, [], rows)
    assert [r.description for r in rows] == ["first", "second"]


def test_narration_with_no_anchor_is_dropped_not_misassigned():
    rows = []
    _flush_nearest([], [(100.0, ["orphan", "words"], 0.0)], rows)
    assert rows == []


def test_flush_clears_its_buffers():
    anchors, narrs, rows = [anchor(100.0, "01-01-2026", 1.0)], [(101.0, ["x"], 0.0)], []
    _flush_nearest(anchors, narrs, rows)
    assert anchors == [] and narrs == []


def test_complete_year_infers_from_period():
    """SBI wraps the year of a two-digit-day date onto the next line, so the
    anchor carries only 'day month'; the year is completed from the period."""
    from bsa.extract.generic_layout import _complete_year

    class M:  # noqa
        period_from, period_to = "2025-07-01", "2025-09-30"
    assert _complete_year("17 Aug", M) == "17 Aug 2025"

    class M2:  # a period spanning a year boundary
        period_from, period_to = "2025-12-01", "2026-01-31"
    assert _complete_year("15 Jan", M2) == "15 Jan 2026"
    assert _complete_year("15 Dec", M2) == "15 Dec 2025"


def test_num_re_accepts_leading_dot_amount():
    """Axis prints a sub-rupee charge with no leading zero (".90" for GST on a
    small fee). Without matching it, the row is dropped and the balance chain
    breaks by exactly that amount — pins the NUM_RE fix."""
    from bsa.extract.generic_layout import NUM_RE, _parse_amount
    assert NUM_RE.match(".90")
    assert _parse_amount(".90") == 0.9
    assert NUM_RE.match("-.05")
    assert not NUM_RE.match(".")        # a bare dot is not an amount
    assert NUM_RE.match("1,14,95,250.00")   # wide crore amount still matches


# ---- wrapped balance (SBI Statement of Account, balance >= 10 lakh) --------

SBI_COLS = {"date_x_max": 78, "remarks_x_min": 128, "remarks_x_max": 300,
            "cheque_x_min": 300, "cheque_x_max": 375,
            "withdrawal_x1_max": 430, "deposit_x1_max": 510,
            "balance_x1_max": 580}


def w(text, x0, x1, top):
    return {"text": text, "x0": x0, "x1": x1, "top": top}


def line(top, *words):
    return {"top": top, "words": list(words)}


def test_wrapped_balance_recovered_from_neighbour_lines():
    """Real geometry from a 71-page SBI passbook: once the balance crosses
    ten lakh, "13,57,623.70CR" no longer fits the balance column — the number
    prints on the narration line ABOVE the dated anchor and the bare CR on the
    line BELOW. The anchor line then has no balance token, and every such row
    was silently dropped (335 of 1538 rows; two whole weeks above 10 lakh
    vanished, surfacing as 9 balance breaks where the drops resumed)."""
    from bsa.extract.generic_layout import _wrapped_balance
    body = [
        line(596.77, w("DEP", 143.0, 159.5, 596.77), w("TFR", 161.7, 177.2, 596.77),
             w("13,57,623.70", 512.15, 558.85, 596.77)),
        line(601.43, w("04-09-2025", 27.0, 68.0, 601.43),
             w("04-09-2025", 84.5, 125.5, 601.43),
             w("11,25,000.00", 439.7, 486.4, 601.43)),
        line(606.09, w("RTGS", 143.0, 165.2, 606.09), w("UTR", 167.5, 183.9, 606.09),
             w("NO:", 186.1, 200.3, 606.09), w("CR", 529.7, 541.3, 606.09)),
    ]
    bal, dr = _wrapped_balance(body, 1, SBI_COLS, 601.43)
    assert bal == 1357623.70
    assert dr is False


def test_wrapped_balance_dr_suffix_marks_overdrawn():
    from bsa.extract.generic_layout import _wrapped_balance
    body = [
        line(96.8, w("WDL", 143.0, 160.8, 96.8),
             w("12,00,000.00", 512.2, 558.9, 96.8)),
        line(101.4, w("04-09-2025", 27.0, 68.0, 101.4),
             w("500.00", 380.0, 404.2, 101.4)),
        line(106.1, w("NARR", 143.0, 165.2, 106.1), w("DR", 529.7, 541.3, 106.1)),
    ]
    bal, dr = _wrapped_balance(body, 1, SBI_COLS, 101.4)
    assert bal == 1200000.00
    assert dr is True


def test_wrapped_balance_ignores_lines_beyond_the_block():
    """The next block's wrapped number sits ~25pt away; only the row's own
    narration lines (~5pt) may donate a balance."""
    from bsa.extract.generic_layout import _wrapped_balance
    body = [
        line(576.0, w("OTHER", 143.0, 165.0, 576.0),
             w("99,99,999.99", 512.2, 558.9, 576.0)),
        line(601.43, w("04-09-2025", 27.0, 68.0, 601.43),
             w("100.00", 380.0, 404.2, 601.43)),
    ]
    bal, dr = _wrapped_balance(body, 1, SBI_COLS, 601.43)
    assert bal is None


def test_wrapped_balance_ignores_narration_numbers():
    """A number inside the narration band (a cheque ref, a UTR) must never be
    read as the balance — only the balance band's right edge qualifies."""
    from bsa.extract.generic_layout import _wrapped_balance
    body = [
        line(96.8, w("CHQ", 143.0, 160.0, 96.8), w("12,345.00", 200.0, 240.0, 96.8)),
        line(101.4, w("04-09-2025", 27.0, 68.0, 101.4),
             w("500.00", 380.0, 404.2, 101.4)),
    ]
    bal, dr = _wrapped_balance(body, 1, SBI_COLS, 101.4)
    assert bal is None


# ---- nearest mode: page-break narration spill (PNB Statement of Account) ---

def test_nearest_max_gap_returns_page_spill_to_previous_row():
    """PNB prints a row's narration ABOVE its anchor; when the anchor is the
    LAST line of a page the narration lands at the TOP of the next page —
    vertically nearest to that page's first anchor. Pure-nearest merged two
    UPI refs into one transaction and left the real owner empty. Over
    max_gap, and above every anchor of the segment, the line belongs to the
    previously emitted row."""
    rows = []
    # page 1: last row's anchor; nothing above it spilled yet
    _flush_nearest([anchor(700.0, "29-06-2025", 3392.50, own=["WDL"])], [], rows)
    assert rows[-1].description == "WDL"
    # page 2: spilled narration at top (16.5pt above the first anchor), then
    # that anchor's own narration 3.5pt above it
    anchors = [anchor(63.82, "01-07-2025", 3192.50)]
    narrs = [(47.32, ["UPI/DR/554641847252/DURGESH"], 0.0),
             (60.32, ["UPI/DR/518220883533/Amar"], 0.0)]
    _flush_nearest(anchors, narrs, rows, max_gap=10.0)
    assert rows[0].description == "WDL UPI/DR/554641847252/DURGESH"
    assert rows[1].description == "UPI/DR/518220883533/Amar"


def test_nearest_without_max_gap_keeps_pure_nearest():
    rows = []
    _flush_nearest([anchor(700.0, "29-06-2025", 1.0, own=["WDL"])], [], rows)
    anchors = [anchor(63.82, "01-07-2025", 2.0)]
    narrs = [(47.32, ["SPILL"], 0.0), (60.32, ["OWN"], 0.0)]
    _flush_nearest(anchors, narrs, rows)
    assert rows[1].description == "SPILL OWN"      # the old behaviour, unchanged


# ---- continuation pages: the table header prints on page 1 ONLY -------------
# Gotcha 10: a statement's column header is often printed once, on page 1, and
# every later page starts straight into rows. A parser that requires the header
# before parsing silently drops every continuation page — this cost 143 of 163
# rows on the first Axis run. Pin end-to-end that a headerless page 2 is parsed
# whole, through extract() itself with pdfplumber faked out.

class _FakePage:
    def __init__(self, words):
        self._words = words

    def extract_words(self):
        return [dict(w) for w in self._words]

    def extract_text(self):
        return ""


class _FakePdf:
    def __init__(self, pages):
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


CONT_LAYOUT = {
    "bank": "Test Bank", "id": "test_generic",
    "parse": {
        "row_anchor": r"\d{2}-\d{2}-\d{4}",
        "continuation": "below",
        "table_header_words": ["Date", "Particulars", "Debit", "Credit",
                               "Balance"],
        "columns": {"date_x_max": 100,
                    "remarks_x_min": 110, "remarks_x_max": 300,
                    "cheque_x_min": 300, "cheque_x_max": 360,
                    "withdrawal_x1_max": 430, "deposit_x1_max": 510,
                    "balance_x1_max": 580},
    },
}

# Page 1 carries the one and only table header line; two rows follow it.
PAGE1 = [
    w("Date", 30, 55, 50), w("Particulars", 115, 170, 50),
    w("Debit", 400, 425, 50), w("Credit", 480, 505, 50),
    w("Balance", 545, 575, 50),
    w("01-01-2026", 30, 90, 100), w("OPENING", 115, 160, 100),
    w("CREDIT", 165, 200, 100),
    w("1,000.00", 460, 505, 100), w("1,000.00", 530, 575, 100),
    w("NEFT", 115, 140, 112), w("REF-AAA", 145, 190, 112),   # wrapped narration
    w("02-01-2026", 30, 90, 130), w("CHEQUE", 115, 160, 130),
    w("PAID", 165, 190, 130),
    w("200.00", 395, 425, 130), w("800.00", 545, 575, 130),
]
# Page 2 has NO header tokens at all — only dated anchors and narration.
PAGE2 = [
    w("03-01-2026", 30, 90, 80), w("NEFT", 115, 140, 80), w("IN", 145, 155, 80),
    w("500.00", 475, 505, 80), w("1,300.00", 530, 575, 80),
    w("FROM", 115, 140, 92), w("DURGA-TRD", 145, 200, 92),
    w("04-01-2026", 30, 90, 110), w("ATM", 115, 135, 110),
    w("WDL", 140, 160, 110),
    w("300.00", 395, 425, 110), w("1,000.00", 530, 575, 110),
    w("05-01-2026", 30, 90, 140), w("INTEREST", 115, 160, 140),
    w("250.00", 475, 505, 140), w("1,250.00", 530, 575, 140),
]


def test_headerless_continuation_page_is_parsed_whole(monkeypatch):
    from bsa.extract import generic_layout

    monkeypatch.setattr(
        generic_layout.pdfplumber, "open",
        lambda path: _FakePdf([_FakePage(PAGE1), _FakePage(PAGE2)]))
    ex = generic_layout.extract("fake.pdf", "s.pdf", CONT_LAYOUT)

    # Every page-2 row is emitted — none silently dropped for lack of a header.
    assert [(r.date, r.page) for r in ex.rows] == [
        ("01-01-2026", 1), ("02-01-2026", 1),
        ("03-01-2026", 2), ("04-01-2026", 2), ("05-01-2026", 2)]
    assert [r.balance for r in ex.rows] == \
        [1000.0, 800.0, 1300.0, 1000.0, 1250.0]
    assert [(r.withdrawal, r.deposit) for r in ex.rows] == [
        (None, 1000.0), (200.0, None),
        (None, 500.0), (300.0, None), (None, 250.0)]
    # Narration still assembles on both pages (wrapped line included).
    assert ex.rows[0].description == "OPENING CREDIT NEFT REF-AAA"
    assert ex.rows[2].description == "NEFT IN FROM DURGA-TRD"
    # And the header line itself never leaks into a row.
    assert all("Particulars" not in r.description for r in ex.rows)


# --- narration_strip: boilerplate only the BANK can identify -----------------

def test_narration_strip_removes_the_banks_own_boilerplate():
    """SBI stamps the ACCOUNT HOLDER'S OWN BRANCH on every row — "AT 15035
    PREMIER BANKING BRANCH, BENGALURU" — which says where the customer banks,
    never who they paid.

    It cannot be found generically, and that is the whole point of putting it
    in the descriptor. Structurally it is identical to BoB's "COMMUNICATIONS
    LIMITE", a real payee that wraps onto its own line; measured on the corpus
    the PAYEE repeats more often than the boilerplate (180x vs 131x), so
    neither position nor frequency separates them.
    """
    import re
    from bsa.extract.generic_layout import _join_narration

    strips = [re.compile(x, re.I) for x in (
        r'\bAT \d{4,6}\b',
        r'\b(?:[A-Z]+\s){0,2}BRANCH,?(?:\s*[A-Z]+)?\b',
        r'^(?:BRANCH,\s*)?[A-Z]{4,}\s+(?=WDL |SWEEP |DEP |TFR |BY |TO )',
    )]

    def strip(s):
        for rx in strips:
            s = rx.sub(" ", s)
        return re.sub(r"\s{2,}", " ", s).strip(" -/,")

    # The stamp goes, the transaction stays.
    assert strip("WDL TFR IMPS/609210928400/KKBK- xx686-SRES/trf AT 15035 "
                 "PREMIER BANKING BRANCH, BENGALURU") == \
        "WDL TFR IMPS/609210928400/KKBK- xx686-SRES/trf"
    # The stamp straddles a line break, so its tail arrives LEADING on the
    # next row — anchored to SBI's opening verbs, not to a city name.
    assert strip("BENGALURU WDL TFR LOCKER CHARGES 0098324150359") == \
        "WDL TFR LOCKER CHARGES 0098324150359"
    # At most two uppercase words before BRANCH, or the match runs backwards
    # over the payee and deletes the name with the stamp.
    assert strip("SWEEP TRF CREDT 0045051155199 OF Mr. CHANDRASHEKARAN O "
                 "PREMIER BANKING BRANCH") == \
        "SWEEP TRF CREDT 0045051155199 OF Mr. CHANDRASHEKARAN O"


def test_a_layout_without_narration_strip_is_untouched():
    """Opt-in: every other descriptor keeps its narration verbatim."""
    from bsa.extract.generic_layout import _join_narration

    assert _join_narration([("NEFT-YESAP52740277381-ONE 97", 0.0),
                            ("COMMUNICATIONS LIMITE", 0.0)]) == \
        "NEFT-YESAP52740277381-ONE 97 COMMUNICATIONS LIMITE"


def test_a_wrapped_year_rejoins_with_the_separator_the_bank_used():
    """Two banks wrap the YEAR of a date onto the next line, and they do not
    agree on how the date is punctuated. SBI prints "17 Aug" and needs a space
    before the year; Bandhan prints "05-JUN-" where the separator is already
    there, so a space would build "05-JUN- 2025" and the row would fail to
    parse. The separator the bank used decides the join."""
    from bsa.extract.generic_layout import _complete_year

    class M:
        period_from = "2025-06-05"
        period_to = "2026-06-04"

    assert _complete_year("05-JUN-", M()) == "05-JUN-2025"
    assert _complete_year("04-JUN-", M()) == "04-JUN-2026"   # picks the year
                                                             # that lands in range
    assert _complete_year("17 Aug", M()) == "17 Aug 2025"    # SBI is unchanged

    class NoPeriod:
        period_from = ""
        period_to = ""

    # No period to reason from: return the input and let the row fail loudly
    # rather than silently misdate it.
    assert _complete_year("05-JUN-", NoPeriod()) == "05-JUN-"
