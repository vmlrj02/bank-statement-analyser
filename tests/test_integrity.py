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
