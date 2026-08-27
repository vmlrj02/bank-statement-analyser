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
    # ECS slash form: mandate reference is a bank prefix glued to digits, and
    # the "_SMS OT" channel suffix is stripped from the name.
    ("ECS/UTIBDE11165163202409/Bajaj Finance Ltd_SMS OT",
     "nach", "Bajaj Finance Ltd"),
    # ICICI cheque payments: every company paid by cheque was unknown before
    # the TRF/ form was read — the review called out exactly this.
    ("CHEQUE 3463 TRF/GO DIGIT GENERAL INSURANCE LTD/ICI",
     "transfer", "GO DIGIT GENERAL INSURANCE LTD"),
    # ICICI internet banking: the NAME is the LAST segment, after the remark.
    ("NET BANKING INF/INFT/044420726211/Amit payment /AMIT",
     "netbanking", "AMIT"),
    # Government internet banking: the tax head is the only party there is.
    ("GIB/002046036625/GST /25070700147871", "other", "GST"),
    ("RTGS RETURN-ICICR42026011900518516-S N S PRODUCTSPVT LTD-OPERATIONS SUSPENDED/R09",
     "other", "S N S PRODUCTSPVT LTD"),
    # SBI's prose narration — the account number rides along with the name.
    ("TO TRANSFER- CIAAKPTHB4 trf- TRANSFER TO 43465553898 TREE OF LIFE DWELLINGS /",
     "other", "TREE OF LIFE DWELLINGS"),
    ("BY TRANSFER- IID8620002 trf-TRANSFER FROM 10448586579 Mr. CHANDRASHEKAR AN O /",
     "other", "Mr. CHANDRASHEKAR AN O"),
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


def test_parse_amount_handles_the_sbi_cr_dr_balance_suffix():
    """Some SBI exports glue a CR/DR flag onto the balance ("2,47,946.81CR").
    The amount reader must strip it (negating a DR balance) and NUM_RE must still
    match it — otherwise the row's balance reads as None and the row is dropped."""
    from bsa.extract.generic_layout import NUM_RE, _parse_amount
    assert NUM_RE.match("2,47,946.81CR") and NUM_RE.match("500.00DR")
    assert _parse_amount("2,47,946.81CR") == 247946.81
    assert _parse_amount("500.00DR") == -500.00
    assert _parse_amount("1,820.00") == 1820.00          # plain amount unchanged
    assert _parse_amount("157.7") == 157.7               # one-decimal (PNB)
    # a bare integer or ref must NOT look like an amount
    assert not NUM_RE.match("990640") and not NUM_RE.match("SBIN0001626")


def test_repeated_footer_lines_are_dropped_structurally():
    """A line whose exact text repeats across pages is page furniture (a footer
    or repeated header) and must never become a transaction — caught structurally
    so a new bank's footer doesn't need to be hand-listed."""
    import bsa.extract.generic_layout as g
    # two "pages" of _lines-style groups sharing a footer line, unique txns.
    def line(top, *toks, x0=40):
        return {"top": top, "words": [{"text": t, "x0": x0 + 20*i, "x1": x0 + 20*i + 15}
                                      for i, t in enumerate(toks)]}
    # A furniture set built the way extract() builds it: text on >=2 pages.
    from collections import defaultdict
    line_pages = defaultdict(set)
    p1 = ["01/01/2026 UPI ALICE 100.00 900.00", "This is a system generated statement"]
    p2 = ["02/01/2026 UPI BOB 50.00 850.00", "This is a system generated statement"]
    for pg, lines in ((1, p1), (2, p2)):
        for t in lines:
            if len(t) > 8:
                line_pages[t].add(pg)
    furniture = {t for t, pgs in line_pages.items() if len(pgs) >= 2}
    assert "This is a system generated statement" in furniture
    assert "01/01/2026 UPI ALICE 100.00 900.00" not in furniture  # unique txn kept


def test_party_kind_splits_names_from_handles():
    from bsa.normalize import party_kind
    assert party_kind("SUKUMAR", "NEFT/..") == "named"
    assert party_kind("SAHU CONSTRUCTION & BORWELLS", "To:..") == "named"
    assert party_kind("4698150044305", "TRANSFER TO ..") == "handle"   # account no.
    assert party_kind("9963059528@ybl", "UPI-..") == "handle"          # a VPA
    assert party_kind("", "NEFT/..") == "none"
    assert party_kind("unknown party", "x") == "none"
    assert party_kind("", "BRN BY CASH self") == "na"                  # un-nameable


def test_identifier_resolution_names_the_bare_rows():
    """The same beneficiary account named in one row fills the rows where only
    the number was printed — and never smears via the customer's own account."""
    from bsa.normalize import resolve_identifiers
    from bsa.models import Txn
    def t(desc, cp, own="111000111000"):
        x = Txn(date="2026-01-01", cheque_no="", description=desc, amount=-10.0,
                balance=0.0, mode="other", counterparty=cp, account_no=own)
        return x
    rows = [
        t("TRANSFER TO 4698150044305 SUKUMAR /", "SUKUMAR"),          # names it
        t("TRANSFER TO 4698150044305 /", "4698150044305"),            # bare
        t("TRANSFER TO 4698150044305", ""),                           # empty
        # own account printed alongside a name must NOT map
        t("FROM 111000111000 TO 9999888877 RAVI KUMAR", "RAVI KUMAR"),
        t("TRANSFER 111000111000", ""),
        # ambiguous id (two names) must not fill
        t("PAY 5555666677 ALPHA TRADERS", "ALPHA TRADERS"),
        t("PAY 5555666677 BETA STORES", "BETA STORES"),
        t("PAY 5555666677", ""),
    ]
    resolve_identifiers(rows)
    assert rows[1].counterparty == "SUKUMAR"
    assert rows[2].counterparty == "SUKUMAR"
    assert rows[4].counterparty == ""            # own account never maps
    assert rows[7].counterparty == ""            # ambiguous stays empty


def test_identifier_resolution_vpa():
    from bsa.normalize import resolve_identifiers
    from bsa.models import Txn
    def t(desc, cp):
        return Txn(date="2026-01-01", cheque_no="", description=desc, amount=-10.0,
                   balance=0.0, mode="upi", counterparty=cp, account_no="1")
    rows = [t("UPI-ASHOK GARG-9963059528@ybl-SBIN0-PAY", "ASHOK GARG"),
            t("UPI-9963059528-9963059528@YBL-SBIN0-517620", "9963059528@ybl")]
    resolve_identifiers(rows)
    assert rows[1].counterparty == "ASHOK GARG"
