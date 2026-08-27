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


@pytest.mark.parametrize("desc,mode,party", [
    # Axis internet banking: the party is printed inside INB/IFT (sometimes
    # with a glued leading ref) and INB/RTGS glues the name onto the UTR.
    ("INB/IFT/PIRAMAL PETROLEUM P LTD/TPARTY TRANSFER",
     "other", "PIRAMAL PETROLEUM P LTD"),
    ("INB/IFT/47586937Shree mansa traders/TPARTY TRANSF",
     "other", "Shree mansa traders"),
    ("INB/RTGS/UTIBR62025052905920002 adhur iron and/RBL BANK LIMITED/",
     "rtgs", "adhur iron and"),
    ("INB/942446069/TIN 2.0 CBDT TAX PAYMENT/NA",
     "other", "TIN 2.0 CBDT TAX PAYMENT"),
    # YES Bank collection credits: beneficiary after the Bl reference.
    ("YESF26182604791600 108361100000014/Bl0041166/TIMEZONE REAL E/",
     "other", "TIMEZONE REAL E"),
    # PNB NEFT: the name rides between the UTR and a trailing reference.
    ("NEFT IN::YESBN12025070802302561/ONE 97 YESAP51891729831",
     "other", "ONE 97"),
    ("NEFT OUT:PUNBN62025082559509721:CHOLAMANDALAM",
     "other", "CHOLAMANDALAM"),
    # NACH forms that hid the mandate holder: slash-with-flag (Axis), IndusInd
    # inward, AU glued, Equitas comma-form.
    ("ACH/DR/HDFC BANK LIMITED/0000145372021/UTIB000000",
     "nach", "HDFC BANK LIMITED"),
    ("ACH DR INW PAY/0000151188104/HDFC BANK LIMITED",
     "other", "HDFC BANK LIMITED"),
    ("ACH DR 10AXIS BANK1074249321", "other", "AXIS BANK"),
    ("ACH DR:P5R6PPS18127558,ESFB0000000000590375,BAJAJ FINANCELIMITE~20250702 CLG",
     "other", "BAJAJ FINANCELIMITE"),
    # Cheques name their payee — and who bounced matters for lending.
    ("CHQ PAID-IC 1100-ADITYABIRLA CAPITAL LIM-ICICIBANKING CORPORATIONLTD-RPC",
     "other", "ADITYABIRLA CAPITAL LIM"),
    ("CHQ PAID-INWARD CLEA-LIFE INSURANCECORPORATION OF INDIA -A",
     "other", "LIFE INSURANCECORPORATION OF INDIA"),
    ("I/W CHEQUE RETURN-SANMUKHLEASING FINA-000092-02-EXCEEDS ARRANGEMENT",
     "other", "SANMUKHLEASING FINA"),
    ("I/W CHQ RETURN-DRAWER S SIGNATUREDIFFERS- FOR PAYEE -KODACHADRI CHITSPRIVATE LTD-AXIS BANK-SER",
     "other", "KODACHADRI CHITSPRIVATE LTD"),
    # Equitas transfer tails and long-form UPI.
    ("KMPSTEELS/PAYMENTTRANSFER CR - OM SAISTEELS", "other", "OM SAISTEELS"),
    ("FT - DR - 200001233035 -KMP STEEL TRADERS", "other", "KMP STEEL TRADERS"),
    ("UPI REF NO 549165481787P2A-GVSCONSTRUCTIONS-PAYMENT FROM-KARB0000182- HEADOFFICE",
     "other", "GVSCONSTRUCTIONS"),
    # City Union prose.
    ("BY NEFT TRF:SIGNET FOUNDATIO IN42609851322653:", "other", "SIGNET FOUNDATIO"),
    ("TO ONL VELU:: SB 500101010581506:00067", "other", "VELU"),
    # Union Bank prints the party as the trailing segment.
    ("UPIAB/549306583166 W83727662 - /CR/PRABHU", "other", "PRABHU"),
    ("IMPSAB/60031656120 T67128624 - 7/ARUNKUMAR", "other", "ARUNKUMAR"),
    # Vasavi co-operative prose forms.
    ("By-Transfer NEFT Sender : SAROJA CHANDRASHEKARAN, UTR : IN12533812066569",
     "other", "SAROJA CHANDRASHEKARAN"),
    ("By-Transfer 1001056000199 SAROJA CHANDRA SHEKARAN 1 % REBATE FOR 2025",
     "other", "SAROJA CHANDRA SHEKARAN"),
    ("By-Transfer Dividend Credit to A -4458-SAROJA CHANDRA SHEKARAN 9880241000 .",
     "other", "SAROJA CHANDRA SHEKARAN"),
    # Tax remittances: the tax head is the only party there is (GIB/GST
    # precedent).
    ("TAX/21862550/139010640001/290526/1 :16", "other", "TAX"),
    ("SGST202602246815177075", "other", "SGST"),
    # HDFC one-offs: government e-pay merchant, STP tail.
    ("9255666396235/SBIEPYEGRASRAJASTHAN", "other", "SBIEPYEGRASRAJASTHAN"),
    ("3017FA2000165280-STP-BPCL", "other", "BPCL"),
    # AU drawdown glues the name to a long reference.
    ("9001220341328885-PANDEY ANDSONS (DRAWDOWN FROM CASA)",
     "other", "PANDEY ANDSONS"),
    # A narration that leads with — or simply is — the party.
    ("ARIHANT CAPITAL/159690058", "other", "ARIHANT CAPITAL"),
    ("NIPPON INDIA LA/134367660/EARG", "other", "NIPPON INDIA LA"),
    ("G R SPONGE AND /", "other", "G R SPONGE AND"),
    # A leading branch number is not the party (was reported as "139").
    ("TRF/139/PIRAMAL PETROLEUM PR/TRANSFER", "transfer", "PIRAMAL PETROLEUM PR"),
    # A leading all-digit reference is not the biller.
    ("Bil Payment BIL/000995828480/ICICI BANK CREDIT CA/431581363320",
     "billpay", "ICICI BANK CREDIT CA"),
    # IndusInd R/N with a full/spaced IFSC in the bank slot.
    ("R/JAKA202602165000072098/JAKA0GHAZIA/PARATUS REAL/URGENT//",
     "other", "PARATUS REAL"),
    ("N/HDFCH01042304319/HDFC0000240/CHOLAMANDALAM INVES/T AND FINANC1470/",
     "other", "CHOLAMANDALAM INVES"),
    # SBI: bulk-posting office code; a phone-only UPI leg keeps its identifier.
    ("BULK POSTING- / EPAO", "other", "EPAO"),
    ("TO TRANSFER- UPI/DR/7327406342", "upi", "7327406342"),
    # A truncation remnant ("/ of-") must fall through to the ACCOUNT number,
    # not return the remnant (which the sanitiser would junk into "none").
    ("TO TRANSFER- TRANSFER TO 4698290162099 / of-", "other", "4698290162099"),
    # HDFC IBFUNDSTRANSFER with the name truncated to two letters: the account
    # is the only identifier — surface it.
    ("IBFUNDSTRANSFERDR-50200010007644 -QU", "other", "50200010007644"),
    # YES From:/To: stamps are machinery, not part of the handle.
    ("YBS6005301632717 UPI/696839383522/From:9891346233@ptyes/",
     "upi", "9891346233@ptyes"),
])
def test_counterparty_shapes_from_corpus_audit(desc, mode, party):
    """Second corpus audit (Aug 2026): nameable narration shapes that were
    resolving to none/junk across the sample corpus. Each case is a real
    printed form (identifying digits scrambled)."""
    assert detect_mode(desc) == mode
    assert extract_counterparty(desc, mode) == party


def test_junk_sequence_number_is_not_a_party():
    """'NACH/10/…' returned '10' as the counterparty; a short pure-digit party
    is a sequence counter, never an account."""
    from bsa.normalize import _sanitise_party
    assert extract_counterparty("NACH/10/9183080211 S77493130 - /TP ACH ICIC",
                                "nach") == ""
    assert _sanitise_party("10") == ""
    assert _sanitise_party("6077") == ""
    assert _sanitise_party("4698290162099") == "4698290162099"   # real account


def test_glued_utr_is_stripped_from_a_name():
    """'Ms Madhuri IDFBN52025041101368719' — a bank prefix glued straight into
    8+ digits is machinery; the name must survive as a NAME, not a handle."""
    from bsa.normalize import _sanitise_party, party_kind
    assert _sanitise_party("Ms Madhuri IDFBN52025041101368719") == "Ms Madhuri"
    assert party_kind("Ms Madhuri", "x NEFT Cr-IDFB0010201-Ms Madhuri") == "named"


def test_hyphenated_vpa_variant_is_still_a_handle():
    """HDFC prints numbered VPAs ('9950720425-2@AXL'); the plain token class
    missed them and left the rows anonymous."""
    d = "UPI-43150100017943-9950720425-2@AXL-5419 50373653-PAYMENTFROMPHONEPE"
    assert extract_counterparty(d, detect_mode(d)) == "9950720425-2@axl"


def test_unnameable_covers_settlements_charges_and_interest():
    """QR settlements, bank charges and the bank's own interest postings have
    no external party — party_kind must class them 'na', and the gazetteer must
    not force a name onto them."""
    from bsa.normalize import party_kind
    for desc in [
        "RTS2502 642219001289 1531044CR - 2402201158745230 - AUSMALL FINANCE "
        "BANK LIMITED QRSETTLEM - AU BANK",
        "ECSRTN1_0606250000000016310674",
        "RETURN HANDLING CHARGES 08-07-25_099908",
        "GST @18% on Chq Book Issuance Chrg",
        "921030006813067:Int.Coll:06/05/2026 to 05/06/2026",
        "DEBIT INTEREST CAPITALIZED",
        "NFS/CASHWDL/502818002845/CN144401/CHENNAI /28-01-251843",
        "Cash Withdrawal At Br : KURUD",
        "CC000457262XXXXXX6456AUTOPAYSI-TAD",
    ]:
        assert party_kind("", desc) == "na", desc
    # …but a plain narration is still nameable.
    assert party_kind("", "NEFT/x") == "none"


def test_bare_name_rule_never_reads_banking_vocabulary_as_a_party():
    """The whole-narration-is-a-name rule must refuse channel words."""
    for desc in ["TRANSFER", "NEFT CMS SALARY XYZ LTD", "ACH DR INW LIMITED",
                 "CHQDEP RET - FUNDSINSUFFICIENT", "BY TRANSFER- TRANSFER FROM"]:
        assert extract_counterparty(desc, detect_mode(desc)) == "", desc


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


def test_party_sanitiser_kills_reviewer_visible_junk():
    """The reviewer-eye gate: junk a human instantly rejects is cleared (which
    lets the narration parser take a better shot), a glued ref-tail is trimmed,
    and slash-junk recovers the real inner name."""
    from bsa.normalize import _sanitise_party
    assert _sanitise_party("NE") == ""
    assert _sanitise_party("DR") == ""
    assert _sanitise_party("ATTN") == ""
    assert _sanitise_party("UBIN0900621") == ""                 # an IFSC
    assert _sanitise_party(
        "SAHU CONSTRUCTION AND BORWELLS IMPS-OUT/516610541142/BARB0VJKRUD/8733"
    ) == "SAHU CONSTRUCTION AND BORWELLS"
    assert _sanitise_party("KKBK/chitrarama/UPI") == "chitrarama"
    assert _sanitise_party("RAMESH KUMAR") == "RAMESH KUMAR"    # real names pass
    assert _sanitise_party("9963059528@ybl") == "9963059528@ybl"  # handles kept
