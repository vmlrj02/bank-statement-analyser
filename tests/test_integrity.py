"""Statement-integrity signals. These are flags for a human underwriter, not
proof of fraud, so the tests pin the conservative behaviour: 'verified' only
when the balance reconciles AND nothing else is off."""
from bsa.integrity import account_integrity, statement_flags
from bsa.models import StatementMeta


def meta(src="a.pdf", producer="", creator="", created="", modified="",
         unreadable=None, digital=True):
    return StatementMeta(
        bank="B", layout="l", account_no="1", account_name="X",
        period_from="", period_to="", source_file=src,
        producer=producer, creator=creator, pdf_created=created,
        pdf_modified=modified, unreadable_pages=unreadable or [],
        is_digital_text=digital)


def test_a_clean_reconciled_statement_is_verified():
    intg = account_integrity([meta(producer="iText 2.1.7 by 1T3XT")], "passed")
    assert intg["assessment"] == "verified"
    assert intg["flags"] == []


def test_a_broken_balance_chain_is_flagged_for_review():
    intg = account_integrity([meta(producer="iText")], "failed")
    assert intg["assessment"] == "review"
    assert any("does not reconcile" in f for f in intg["flags"])


def test_an_editing_tool_producer_is_flagged():
    """The hand-assembled ICICI 'manual' file: Quartz producer, pdf-lib creator."""
    m = meta(producer="macOS Version 26.6.1 Quartz PDFContext", creator="pdf-lib")
    flags = statement_flags(m)
    assert any("editing tool" in f and "pdf-lib" in f for f in flags)
    # even with a reconciled balance, an editing tool forces review
    assert account_integrity([m], "passed")["assessment"] == "review"


def test_a_scanned_page_spliced_in_is_flagged():
    m = meta(producer="iText", unreadable=[3])
    assert any("no text layer" in f for f in statement_flags(m))


def test_modified_after_creation_is_informational_not_a_review_trigger():
    """A genuine statement re-saved days later (password removal, re-download)
    must NOT be forced to review — only carried as information."""
    from bsa.integrity import modified_gap_days
    m = meta(producer="iText", created="D:20260101090000Z",
             modified="D:20260220090000Z")
    assert statement_flags(m) == []
    assert account_integrity([m], "passed")["assessment"] == "verified"
    assert modified_gap_days(m) == 50


def test_optxn_nearest_assigns_narration_to_the_closest_anchor():
    """Font-flattened re-export: a title line above a row and a descriptor below
    it must each attach to their own (nearest) anchor, not shift by one."""
    from bsa.extract.icici_optransactionhistory import _extract_nearest

    def w(text, x0, top):
        return {"text": text, "x0": x0, "x1": x0 + 20, "top": top}

    def line(words):
        return {"words": words, "top": words[0]["top"]}

    cols = {"sl_no_x_max": 40, "cheque_x_min": 300, "cheque_x_max": 340,
            "remarks_x_min": 100, "withdrawal_x1_max": 470,
            "deposit_x1_max": 500}
    collected = [(1, [
        line([w("TITLE-OF-A", 100, 10)]),                     # above anchor A
        line([w("1", 10, 20), w("01.07.2025", 45, 20), w("100.00", 445, 20),
              w("900.00", 520, 20)]),                          # anchor A (wd 100)
        line([w("DESC-OF-A", 100, 30)]),                       # below A
        line([w("TITLE-OF-B", 100, 40)]),                      # above anchor B
        line([w("2", 10, 50), w("02.07.2025", 45, 50), w("50.00", 445, 50),
              w("850.00", 520, 50)]),                          # anchor B
        line([w("DESC-OF-B", 100, 60)]),                       # below B
    ])]
    ex = _extract_nearest(collected, cols, "x.pdf", None)
    assert [r.description for r in ex.rows] == \
        ["TITLE-OF-A DESC-OF-A", "TITLE-OF-B DESC-OF-B"]
