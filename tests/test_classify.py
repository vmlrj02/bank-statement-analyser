"""Classification reads page-1 fingerprints. Some exports lead with a blank
cover page, so the fingerprint sits on a later page — the classifier must
widen its search rather than fail the whole statement as 'no layout'."""
import contextlib

import bsa.classify as classify_mod
from bsa.classify import classify


class _Page:
    def __init__(self, text):
        self._text = text

    def extract_text(self):
        return self._text


class _PDF:
    def __init__(self, pages):
        self.pages = pages


def _fake_open(pages):
    @contextlib.contextmanager
    def _cm(path):
        yield _PDF([_Page(t) for t in pages])
    return _cm


def test_fingerprint_on_page_one_still_matches(monkeypatch):
    monkeypatch.setattr(classify_mod.pdfplumber, "open",
                        _fake_open(["Statementof account HDFC0000696 AccountNo : 5010"]))
    c = classify("x.pdf")
    assert c.layout_id == "hdfc_account_statement"


def test_blank_cover_page_does_not_defeat_classification(monkeypatch):
    # page 1 is an empty cover; the real header is on page 2.
    monkeypatch.setattr(classify_mod.pdfplumber, "open",
                        _fake_open(["", "Statementof account HDFC0000696 AccountNo : 5010 " * 4]))
    c = classify("x.pdf")
    assert c.layout_id == "hdfc_account_statement"


def test_a_genuinely_unknown_bank_is_still_unknown(monkeypatch):
    monkeypatch.setattr(classify_mod.pdfplumber, "open",
                        _fake_open(["", "Some Bank We Have Never Seen, statement of things " * 4]))
    c = classify("x.pdf")
    assert c.layout_id is None


def test_the_three_sbi_exports_do_not_collide(monkeypatch):
    """SBI has three distinct exports; each must resolve to its own layout and
    not to one of the others."""
    cases = {
        "sbi_internet_statement":
            "STATEMENT OF ACCOUNT Account Summary Date of Statement : 22-07-2026 "
            "IFSC Code : SBIN0015035 Account Number : 34528846598",
        "sbi_savings_statement":
            "Account Name : Mrs X Account Description : REGULAR SB CHQ-INDIVIDUALS "
            "IFS Code : SBIN0015035 Account Number : 00000034528846598",
        "sbi_account_statement":
            "Account Number : 43475634459 Account Statement from 1 Jan 2025 to 2 "
            "Feb 2025 IFSC SBIN0009678 Debit Credit Balance",
    }
    for expected, text in cases.items():
        monkeypatch.setattr(classify_mod.pdfplumber, "open", _fake_open([text]))
        assert classify("x.pdf").layout_id == expected
