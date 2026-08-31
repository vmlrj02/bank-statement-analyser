"""Ingest measures what can be read, which is what later explains what could
not be extracted."""
import pytest

from bsa.ingest import IngestError, MIN_PAGE_CHARS, ingest


def test_a_missing_file_is_named(tmp_path):
    with pytest.raises(IngestError) as e:
        ingest(str(tmp_path / "nope.pdf"))
    assert "file not found" in str(e.value)


def test_an_empty_file_is_rejected(tmp_path):
    p = tmp_path / "empty.pdf"
    p.write_bytes(b"")
    with pytest.raises(IngestError) as e:
        ingest(str(p))
    assert "empty file" in str(e.value)


def test_the_threshold_distinguishes_a_sparse_page_from_a_scan():
    """Below this a page has no text layer at all rather than little text."""
    assert 0 < MIN_PAGE_CHARS < 200


def test_a_password_that_is_the_holders_name_plus_digits():
    """The commonest convention banks use, and the one we were missing: a few
    letters of the account holder's name glued to a few digits. Four statements
    in the August sample drop failed to open on this alone, each with its own
    password printed on the file we were handed.

    The shape has to stay narrow or every export name becomes a guess — hence
    letters IMMEDIATELY followed by digits, bounded by non-alphanumerics."""
    from bsa.ingest import password_candidates

    assert "PRAD2597" in password_candidates("HDFC BANK PRADEEP ( PRAD2597 ).pdf")
    assert "NKES0301" in password_candidates("NKES0301-ICICI-799.pdf")
    assert "NETH1112" in password_candidates(
        "NETHRA UBI 0165 06.08.2025 to 04.08.2026 NETH1112.pdf")
    assert "SRAM1006" in password_candidates(
        "Ramesh Union 0589 18-07-2025 to 18-07-2026 SRAM1006.pdf")

    # …and must not fire on ordinary export names, or a real password gets
    # crowded out of MAX_PASSWORD_GUESSES by noise. The underscore in the first
    # and the long word in the second are what keep them out.
    assert "Bankstatement10085925401" not in password_candidates(
        "IDFCFIRSTBankstatement_10085925401 (5).pdf")
    assert password_candidates("OpTransactionHistory27-08-2026.pdf") == []
    assert password_candidates("Axis CA 6464 01-08-25 to 01-08-26.pdf") == []

    # a name with nothing password-shaped in it yields nothing, rather than a
    # guess that would waste an attempt
    assert password_candidates("BOB VINAY 10-03-2026 TO 10-06-2026.pdf") == []

    # the labelled form still wins the first slot — it is a statement of intent
    assert password_candidates("SHRUTHI BS 2 PW - SHRU2705-160.pdf")[0] == "SHRU2705"
