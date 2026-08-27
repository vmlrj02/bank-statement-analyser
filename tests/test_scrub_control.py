"""Regression for the production HDFC failure: a font-encoding quirk left control
bytes in the account name, and openpyxl rejected the whole workbook with
"…cannot be used in worksheets", failing the job at publish."""
from bsa.normalize import scrub_control, normalize
from bsa.models import RawRow, StatementMeta, StatementExtract


def test_scrub_removes_illegal_control_bytes():
    assert scrub_control("SYED\x08ZAYYAN\x1fAHMED\x7f") == "SYEDZAYYANAHMED"
    assert scrub_control("clean name") == "clean name"
    assert scrub_control(None) is None
    # tabs/newlines are left for the normal whitespace collapse
    assert scrub_control("a\tb\nc") == "a\tb\nc"


def test_openpyxl_accepts_scrubbed_rejects_raw():
    from openpyxl import Workbook
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    raw = "SYED\x08ZAYYAN\x1fAHMED"
    assert ILLEGAL_CHARACTERS_RE.search(raw)                 # this is what crashed
    ws = Workbook().active
    ws.append([scrub_control(raw)])                          # must not raise
    assert ws["A1"].value == "SYEDZAYYANAHMED"


def test_normalize_scrubs_meta_and_description():
    meta = StatementMeta(bank="HDFC\x08 Bank", layout="x", account_no="123\x1f45",
                         account_name="SYED\x08ZAYYAN\x1fAHMED", period_from="",
                         period_to="", source_file="f.pdf")
    rows = [RawRow(sl_no=None, date="2026-01-01", cheque_no="",
                   description="UPI-FOO\x08BAR payment", withdrawal=100.0,
                   deposit=None, balance=900.0, page=1)]
    txns = normalize(StatementExtract(meta=meta, rows=rows))
    assert meta.account_name == "SYEDZAYYANAHMED"
    assert meta.bank == "HDFC Bank"
    assert "\x08" not in txns[0].description and "FOOBAR" in txns[0].description
