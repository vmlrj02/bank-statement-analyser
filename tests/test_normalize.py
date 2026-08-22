"""Normalisation turns whatever an extractor saw into canonical rows. Its
mistakes are silent — a misparsed date or a dropped sign still reconciles or
still fails, but for the wrong reason."""
import pytest

from bsa.models import RawRow, StatementExtract, StatementMeta, Txn
from bsa.normalize import (dedup_merge, detect_mode, extract_counterparty,
                           normalize, parse_date, _repair_swapped_pairs)


def meta(acct="0001", bank="ICICI Bank", src="a.pdf"):
    return StatementMeta(bank=bank, layout="test", account_no=acct,
                         account_name="X", period_from="", period_to="",
                         source_file=src)


def raw(date, w, d, bal, desc="x", page=1):
    return RawRow(sl_no=None, date=date, cheque_no="", description=desc,
                  withdrawal=w, deposit=d, balance=bal, page=page)


@pytest.mark.parametrize("printed,iso", [
    ("03.07.2025", "2025-07-03"), ("3-Jul-25", "2025-07-03"),
    ("03/07/2025", "2025-07-03"), ("2025-07-03", "2025-07-03"),
    ("02/Jan/2026", "2026-01-02"), ("July 3, 2025", "2025-07-03"),
    ("3 Jul 2025", "2025-07-03"),
])
def test_date_formats_seen_in_real_statements(printed, iso):
    assert parse_date(printed) == iso


def test_an_unparseable_date_names_the_file_and_page():
    """A twenty-file job must say WHICH statement failed."""
    ex = StatementExtract(meta=meta(src="march.pdf"),
                          rows=[raw("32/13/2025", 10.0, None, 90.0, page=7)])
    with pytest.raises(ValueError) as e:
        normalize(ex)
    assert "march.pdf" in str(e.value) and "page 7" in str(e.value)


def test_withdrawal_becomes_a_negative_amount():
    t = normalize(StatementExtract(meta=meta(), rows=[raw("01-01-2026", 100.0, None, 900.0)]))
    assert t[0].amount == -100.0


def test_balance_only_rows_are_not_transactions():
    """Brought-forward lines carry a balance and no amount."""
    ex = StatementExtract(meta=meta(), rows=[raw("01-01-2026", None, None, 1000.0, "B/F")])
    assert normalize(ex) == []


@pytest.mark.parametrize("desc,mode", [
    ("SATHYA PRASAD B RTGS-ABC123-NAME", "rtgs"),
    ("UPI/427/somebody/xyz", "upi"),
    ("MMT/IMPS/50912/KRISHNA/HDFC", "imps"),
    ("ACH/INDIstore/12345/", "nach"),
    ("NFS/CASH WDL/1234", "atm-cash"),
    ("ATM-CASH/+SARJAPUR ROAD BR/BANGALORE-URB/010226", "atm-cash"),
    ("something ordinary", "other"),
])
def test_mode_detection_matches_mid_string(desc, mode):
    """Descriptions are prefixed by a bold title line, so a mode anchored at ^
    would miss every real row."""
    assert detect_mode(desc) == mode


def test_counterparty_skips_reference_numbers_and_ifsc():
    d = "MMT/IMPS/509123456/IMPS/HDFC0000123/KRISHNA TRADERS"
    assert extract_counterparty(d, "imps") == "KRISHNA TRADERS"


@pytest.mark.parametrize("desc,mode,party", [
    # Axis UPI prints the transfer TYPE first — "P2A" is a channel marker,
    # and it reached a customer-facing report as the party name.
    ("UPI/P2A/557305326847/K S SHALI/YES BANK /UPI/", "upi", "K S SHALI"),
    # Slash-form NEFT: the reference is bank-prefix + digits, name follows,
    # then the counterparty's BANK (which is not the party).
    ("NEFT/HDFCH00395013738/RHEA HEALTHCARE PVT LTD/HDFC BANK/0001",
     "neft", "RHEA HEALTHCARE PVT LTD"),
    # RTGS segment order differs by bank: Axis puts the name before the bank,
    # ICICI puts the IFSC before the name. Both must yield the name, and
    # taking the LAST segment reported "HDFC BANK" as a customer's party.
    ("RTGS/HDFCR52025081199521039/RHEAHEALTHCAREPVTLTD/HDFC BANK/",
     "rtgs", "RHEAHEALTHCAREPVTLTD"),
    ("RTGS/ICICR42025070700523916/KVBL0004109/B L ENTERPRISES",
     "rtgs", "B L ENTERPRISES"),
    # SBI/Axis dash-form NACH credit.
    ("ACH-CR-JSW STEEL LIMITED-NACH-22932110-22932110",
     "nach", "JSW STEEL LIMITED"),
    # The IMPS remark rides ahead of the name — prefer the digit-free segment.
    ("MMT/IMPS/518614164794/bill 2876/SONI BAKER/HDFC Bank",
     "imps", "SONI BAKER"),
    # ECS slash form: the mandate reference is a bank prefix glued to digits.
    ("ECS/UTIBDE11165163202409/Bajaj Finance Ltd_SMS OT",
     "nach", "Bajaj Finance Ltd_SMS OT"),
])
def test_counterparty_from_real_statement_descriptors(desc, mode, party):
    assert detect_mode(desc) == mode
    assert extract_counterparty(desc, mode) == party


def test_latest_first_statements_are_flipped():
    ex = StatementExtract(meta=meta(), rows=[
        raw("03-01-2026", 10.0, None, 80.0), raw("02-01-2026", 10.0, None, 90.0),
        raw("01-01-2026", 10.0, None, 100.0)])
    assert [t.date for t in normalize(ex)] == \
        ["2026-01-01", "2026-01-02", "2026-01-03"]


def test_bank_misordered_pair_is_repaired_only_when_it_reconciles():
    """ICICI printed a reversal pair back to front; the balances proved the
    true order. A swap that does NOT repair the chain must be left alone, or a
    genuine extraction error would be silently 'corrected'."""
    def t(amount, balance):
        return Txn(date="2026-01-02", cheque_no="", description="d",
                   amount=amount, balance=balance, mode="other", counterparty="")
    # Opening 1000. The true order is credit then debit, back to 1000; the
    # bank printed the debit first. Only the swapped order reconciles.
    rows = [t(0, 1000), t(-194000, 1000), t(194000, 195000)]
    rows[0].date = "2026-01-01"
    assert _repair_swapped_pairs(rows) == 1
    assert rows[1].amount == 194000 and rows[2].amount == -194000

    broken = [t(0, 1000), t(-500, 400), t(-200, 100)]
    broken[0].date = "2026-01-01"
    assert _repair_swapped_pairs(broken) == 0


def test_dedup_merge_drops_overlap_and_keeps_accounts_apart():
    def mk(acct, date, amount, balance):
        x = Txn(date=date, cheque_no="", description="d", amount=amount,
                balance=balance, mode="other", counterparty="",
                account_no=acct, bank="B")
        x.compute_uid(acct, 0)
        return x
    a1 = [mk("A", "2026-01-01", -10, 90), mk("A", "2026-01-02", -10, 80)]
    a2 = [mk("A", "2026-01-02", -10, 80), mk("A", "2026-01-03", -10, 70)]
    b1 = [mk("B", "2026-01-02", -10, 500)]
    out = dedup_merge([a1, a2, b1])
    assert len(out) == 4                       # the repeated 02-Jan row is dropped
    assert [t.account_no for t in out] == ["A", "A", "A", "B"]


def test_identical_same_day_rows_are_not_a_duplicate_within_one_statement():
    """A genuine same-day reversal pair looks identical; the occurrence index
    is what keeps both."""
    ex = StatementExtract(meta=meta(), rows=[
        raw("01-01-2026", 100.0, None, 900.0, "SAME"),
        raw("01-01-2026", None, 100.0, 1000.0, "SAME")])
    assert len({t.uid for t in normalize(ex)}) == 2
