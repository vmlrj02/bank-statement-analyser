"""The generic parser's line-to-row assignment. ICICI's combined statement
centres a row's block on the amount line — narration above AND below it —
which neither 'above' nor 'below' can place: 'below' glued every block's
first line onto the previous row, shifting all 995 descriptions one row off.
'nearest' assigns each narration line to the vertically closest anchor."""
from bsa.extract.generic_layout import _flush_nearest


def anchor(top, date, bal, own=None):
    return {"top": top, "date": date, "cheque": "", "wd": None, "dep": 100.0,
            "bal": bal, "page": 1,
            "parts": [(top, own)] if own else []}


def test_centred_blocks_keep_each_narration_with_its_own_row():
    # Real geometry from the ICICI statement: rows are ~12.8pt apart, and a
    # wrapped block prints narration 3.6pt above and 3.6pt below its anchor.
    rows = []
    anchors = [
        anchor(414.94, "04-07-2025", 5024530.09, own=["BY", "CASH"]),
        anchor(431.37, "05-07-2025", 5040788.09),          # narration is all wrapped
    ]
    narrs = [
        (427.76, ["NEFT-REF-DURGA", "SWEETS"]),   # above its anchor (431.37)
        (434.97, ["CORNER-0001"]),                # below the same anchor
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
    _flush_nearest([], [(100.0, ["orphan", "words"])], rows)
    assert rows == []


def test_flush_clears_its_buffers():
    anchors, narrs, rows = [anchor(100.0, "01-01-2026", 1.0)], [(101.0, ["x"])], []
    _flush_nearest(anchors, narrs, rows)
    assert anchors == [] and narrs == []
