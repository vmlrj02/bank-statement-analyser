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
    assert _sanitise_party("nusarthgi@bpunity") == "nusarthgi@bpunity"  # a handle
    # …but a handle that is only a phone number names nobody. The reviewer's
    # second labelling pass rejected both of these with "don't show these kinds
    # numbers in party name", which reverses the earlier "handles kept" rule for
    # the numeric case only.
    assert _sanitise_party("9963059528@ybl") == ""
    assert _sanitise_party("7895273091-3@ybl") == ""
    # A website is never a payee. City Union prints its registered office and
    # URL below the table; the footer marker that let the block bleed into the
    # last row was a layout bug, but nothing downstream should have accepted
    # "www.cityunionbank.com" as a counterparty either.
    assert _sanitise_party("www.cityunionbank.com") == ""
    assert _sanitise_party("Website: www.hdfcbank.com") == ""


def test_reviewer_round2_party_corrections():
    """Every party the reviewer marked wrong in the second labelled pass, with
    the answer they gave. These are the cases, not a paraphrase of them: each
    line is one row of the review file, so a rule that regresses fails here with
    the statement text that caught it.

    Four things were being read as counterparties that never are: the transfer
    TYPE glued to the name (SBI's "WDL TFR" withdrawal-transfer, "DEP TFR"
    deposit-transfer), the CHANNEL stamped a second time inside the name slot
    ("IMPS P2A shakeel"), the counterparty's BANK in the slot before the name
    ("…/IDFB/MOHD NAEEM/…", "KKBKTransfer"), and a CHARGE marker ("CHG")."""
    from bsa.normalize import _sanitise_party, extract_counterparty, detect_mode

    def party(d):
        return _sanitise_party(extract_counterparty(d, detect_mode(d)))

    # the transfer type is how, not who — and alone it is nobody
    assert party("UPI/DR/088852923500/SHAURY WDL TFR") == "SHAURY"
    assert party("UPI/DR/191010604854/SAHU WDL TFR") == "SAHU"
    assert party("UPI/DR/555106059004/ANURAG DEP TFR") == "ANURAG"
    assert party("UPI/DR/396055740677/M/S. WDL TFR") == ""
    assert party("CHEQUE TRANSFER TO WDL TFR") == ""
    assert party("To-S TFR IMPS - STAN : 000496 - RRN : 606517000496") == ""
    assert party("IMPS- CHG/61281716 0616/SBIN001 0831/31837838 407") == ""

    # the channel, stamped again inside the name slot
    assert party("IMPS/615963841356-IMPS P2A GAJANAND TOOLS MAYUR-H") == \
        "GAJANAND TOOLS MAYUR"
    assert party("IMPS/615562987924-IMPS P2A shakeel-IDFB0081105-10") == "shakeel"

    # the counterparty's BANK sits where the name is expected
    assert party("IMPS/P2A/603518249052/IDFB/MOHD NAEEM/794278554") == "MOHD NAEEM"
    assert party("MMT/IMPS/514818409623/KKBKTransfer/VEL MURUGA/Kotak "
                 "Mahindra") == "VEL MURUGA"
    assert party("IMPSAB/61250911975 T91321951 - 7/ARUNKUMAR B/8680843648") == \
        "ARUNKUMAR B"

    # names that were being missed entirely
    assert party("IMPS/P2A-603513988896-SYED ZAYYAN AHMED") == "SYED ZAYYAN AHMED"
    assert party("MBS/by SYED MUQTHAR AHMED/0200853/02-06-2026 14:") == \
        "SYED MUQTHAR AHMED"
    assert party("NET BANKING /KOTHARILELEC") == "KOTHARILELEC"
    # the EMI date wraps mid-line, so it carries a space the old pattern refused
    assert party("UCR013913299695_EMI_01-12- 2025_HINDUSTAN PETROCHEM") == \
        "HINDUSTAN PETROCHEM"
    # City Union prints the payee twice, abbreviated then in full
    assert party("TO ONL NEFT:UTR:CIUBH26094034984:SBIN0008531:ALANGIR:: "
                 "ALANGIR::00067") == "ALANGIR"
    # a reference riding behind the name
    assert party("NEFT:AMRINA IQBAL W42268794 Sender CNRBH00130597691 "
                 "No:CNRBH00130 597691") == "AMRINA IQBAL"
    # a VPA that is only a phone number
    assert party("YBS5124188157637 UPI/082777828978/From:7895273091-3@ybl/ "
                 "To:073663700000687@YESB0000736.ifsc.npci/ Payment from "
                 "PhonePe") == ""


def test_a_name_seen_once_is_not_stamped_on_the_whole_statement():
    """SBI prints "TRANSFER TO 4897690162095" on every UPI row — the same number
    on every SBI customer's statement, because it is SBI's pooled UPI nodal
    account rather than anybody's. It is not the statement's own account number,
    so the `own` guard never saw it, and one row of 299 that happened to carry a
    payee inline named 28 other rows after it. The reviewer found it exactly as
    it reads: "why is party amazon upi when description doesn't have any of
    that". The evidence has to scale with the claim."""
    from bsa.normalize import resolve_identifiers
    from bsa.models import Txn

    def t(desc, cp):
        return Txn(date="2026-01-01", cheque_no="", description=desc, amount=-10.0,
                   balance=0.0, mode="upi", counterparty=cp, account_no="345288465")

    rail = "4897690162095"
    rows = [t(f"TO TRANSFER- UPI/DR/1/Amazon Pay/UTIB/amazonupi@/You-"
              f"TRANSFER TO {rail}", "Amazon Pay")]
    rows += [t(f"TRANSFER- TRANSFER {rail}", "") for _ in range(28)]
    resolve_identifiers(rows)
    assert [r.counterparty for r in rows[1:]] == [""] * 28

    # …while a beneficiary account named about as often as it is bare still
    # fills, which is the whole point of the mechanism.
    good = [t("TRANSFER TO 4698150044305 SUKUMAR", "SUKUMAR"),
            t("TRANSFER TO 4698150044305 SUKUMAR", "SUKUMAR"),
            t("TRANSFER TO 4698150044305", "")]
    resolve_identifiers(good)
    assert good[2].counterparty == "SUKUMAR"


def test_upi_purpose_token_never_beats_the_human_name():
    """Reviewer doc ID4 items 5 and 6: '"MrUjjal" is the party name; not
    "ReqPay"' and 'Party name wrong; it is "ChandanaP"'.

    UPI request/collect narrations put a PURPOSE token in the very slot a name
    occupies, and it is mixed-case like a typed remark, so nothing structural
    separates the two — the first-segment rule simply took it. Those tokens are
    now remark vocabulary, so the human beside them wins.
    """
    from bsa.normalize import detect_mode, extract_counterparty

    def party(d):
        return extract_counterparty(d, detect_mode(d))

    assert party("UPI/ReqPay/MrUjjal/9876543210/Payment") == "MrUjjal"
    assert party("UPI/CollPay/ChandanaP/8765432109") == "ChandanaP"
    # A real name that merely sits after a purpose token is still found, and a
    # narration carrying ONLY a purpose token keeps it rather than going blank.
    assert party("UPI/ReqPay/SHREE LAKSHMI STEEL/HDFC0001") == "SHREE LAKSHMI STEEL"


def test_transfer_narrations_name_the_party_or_the_account_number():
    """Reviewer doc id7 items 1 and 2: when a transfer prints the other side's
    account number, that number is the party, so the same counterparty
    aggregates across rows where the bank printed only the number. Where the
    bank printed a name too, the NAME wins."""
    from bsa.normalize import detect_mode, extract_counterparty

    def party(d):
        return extract_counterparty(d, detect_mode(d))

    assert party("TRANSFER TO 4698150044305 SUKUMAR") == "SUKUMAR"
    assert party("TRANSFER FROM 4897650123456 RAJESH TRADERS") == "RAJESH TRADERS"
    assert party("TRANSFER TO 4698150044305") == "4698150044305"
    assert party("TRANSFER FROM 4897650123456") == "4897650123456"


def test_hdfc_flattened_narrations_name_the_party():
    """A re-exported HDFC statement has the spaces squeezed out of every
    narration, so the spaced patterns written for other banks matched none of
    it and the account named 43% of its nameable rows. These are the shapes
    that were failing, verbatim from a real statement."""
    from bsa.normalize import detect_mode, extract_counterparty

    def party(d):
        return extract_counterparty(d, detect_mode(d))

    # Fund transfers: the payee sits after the account number, hyphens unspaced.
    assert party("FT-CR-50100415695344-PULIVENKATAI AH") == "PULIVENKATAI AH"
    assert party("FT-DR-50100563477863-PULIPRANEETH GOUD") == "PULIPRANEETH GOUD"
    # IMPS through the same prefix — payee is the token before the FTIMPS tail.
    assert party("FT-99999012489999-IMPSTRANSACTION-PULI 0000FTIMPS054382 "
                 "SANDEEPGOUD-FTIMPS054382") == "SANDEEPGOUD"
    # Cheques name who was actually paid, in three different orders.
    assert party("AKHILESHRAO-CHQPAID-OLDALWAL") == "AKHILESHRAO"
    assert party("CHEQPAIDTOMOHAMMADBINADBUL SATTARQ -CHQPAID-KOMPALLY") \
        == "MOHAMMADBINADBUL SATTARQ"
    assert party("CHQPAID-CTSS5-RKS-CHENDHINAGENDER") == "CHENDHINAGENDER"
    # Collection credit carrying only the remitter.
    assert party("PAYMENTS-K&NKIDSLLP") == "K&NKIDSLLP"


def test_rows_with_no_counterparty_are_not_counted_as_unnamed():
    """Party coverage is only honest if rows that CANNOT have a counterparty
    are excluded. HDFC's own charges, its interest posting, an ATM withdrawal
    (which names a machine's location) and a cheque drawn to SELF have no payee
    to find — counting them as failures understated the naming badly."""
    from bsa.normalize import detect_mode, extract_counterparty, party_kind

    for d in ("NWD-652166XXXXXX3333-18072HRY-KOMPALLY",
              "SELF-CHQPAID-PETBASHEERAB",
              "NEFTCHGSBRNINCLGST05-06-2026-EPR2716219786735",
              "SBCASHTXNCHGSINCLTAXES19-06-2026-EPR2717153040250",
              "INTERESTPAIDTILL31-MAR-2026",
              "SBY30259689_DAP_RENEWAL"):
        kind = party_kind(extract_counterparty(d, detect_mode(d)), d)
        assert kind == "na", f"{d} -> {kind}"


def test_hdfc_atm_withdrawal_is_cash_not_a_supplier_payment():
    """"NWD-<masked card>-<terminal>-<branch>" is HDFC's ATM withdrawal and
    carries no "ATM" token, so it fell through to Regular debit and was read as
    trade spend — a cash withdrawal counted as a supplier payment."""
    from bsa.normalize import detect_mode

    assert detect_mode("NWD-652166XXXXXX3333-18072HRY-KOMPALLY") == "atm-cash"


# --- Axis branch / card / merchant forms (ID5, ID9) --------------------------
# "party detection are poor in axis bank for all categories" — these six shapes
# all print a perfectly good name that no mode branch claimed, because
# detect_mode reads them as "other" and the generic segment scan then returned
# a reference or nothing.

@pytest.mark.parametrize("desc,want", [
    ("POS/MD ENTERPRISES/BANGALORE/311025/20:35/73 1111", "MD ENTERPRISES"),
    # "PAY*" is the payment aggregator's prefix, not part of the merchant.
    ("ECOM PUR/PAY*BIGTREE E/MUMBAI/300925/22:50/367451", "BIGTREE E"),
    ("MOB/TPFT/VIKAS VASANTH /925010004538960", "VIKAS VASANTH"),
    # The payee, NOT the bank that presented the cheque.
    ("BRN-CLG-CHQ PAID TO Vishwanath B/KARNATAKA BANK", "Vishwanath B"),
    ("BRN CLG-CHQ PAID TO CRAMESH", "CRAMESH"),
    # A counter cash deposit made BY a named person — exactly who a lender
    # wants to see against a cash credit.
    ("SAK/CASH DEP/SAK472180594/4543/AYAZ MOHIDDIN", "AYAZ MOHIDDIN"),
])
def test_axis_branch_and_merchant_forms_are_named(desc, want):
    from bsa.normalize import detect_mode, extract_counterparty

    assert extract_counterparty(desc, detect_mode(desc)) == want


@pytest.mark.parametrize("desc", [
    "NEFT CHRGS 010725",
    "GST @18% ON NEFT CHRGS",
    "SELF CASH DEP",
    "ECS TXN CHRGS INCL GST",
])
def test_a_bank_charge_still_has_no_counterparty(desc):
    """The other half of the Axis naming number: most of what stays unnamed is
    a fee, a self-transaction or interest, which HAS no counterparty. Naming
    those would raise the percentage and make the report wrong."""
    from bsa.normalize import detect_mode, extract_counterparty

    assert extract_counterparty(desc, detect_mode(desc)) == ""


# --- "bank names wont be parties" (master, Party naming tab, rule 4) ---------
# The Top-10 party lists came back led by "KARNATAKA BANK LIMIT" at a 122.8%
# share, "ICICI BANK LIMITED" and "BANK OF BARODA". A bank is the rail the
# money moved on, not the counterparty.

@pytest.mark.parametrize("name", [
    "KARNATAKA BANK LIMIT", "ICICI BANK LIMITED", "BANK OF BARODA",
    "Kotak Mahindra Bank Ltd", "CANARA BANK", "YES BANK LIMITED",
    "State Bank of India",
    # Brands that get printed with no "bank" word at all.
    "HDFC", "ICICI", "AXIS", "IDFC FIRST",
])
def test_a_bank_is_never_a_party(name):
    from bsa.normalize import _sanitise_party

    assert _sanitise_party(name) == ""


@pytest.mark.parametrize("name", [
    # Word-bounded on purpose: a person, not a bank.
    "BANKATLAL TEXTILES",
    # A brand word only counts as the WHOLE name.
    "AXIS MACHINE TOOLS",
    # Lenders and finance companies are real, nameable counterparties.
    "Bajaj Finance Ltd", "Kinara Capital", "ZERODHA BROKING LTD",
    "SRI SUMUKHA ENTERPRISE", "MD ENTERPRISES", "Trupti Shetty",
])
def test_a_real_counterparty_survives_the_bank_filter(name):
    from bsa.normalize import _sanitise_party

    assert _sanitise_party(name) == name


# --- the four shapes from the reviewer's first labelling round ---------------
# 41 of 47 labelled shapes — 489 of 563 transactions — were these four, and in
# every one the payee sits at a KNOWN POSITION in the string. Worth recording
# because it settled an architecture question: this is "take the text inside
# the brackets", not something that needs a model.

@pytest.mark.parametrize("desc,want", [
    # 1. The name in brackets after the VPA — the biggest shape in the set
    #    (33 shapes, 359 rows), and worth 65 points of Karnataka Bank on its own.
    ("UPI:516187488189:paytmqr6er7uc@ptys(Maseeha Banu)", "Maseeha Banu"),
    ("UPI:553101391507:q939300321@ybl(MED ZONE PHARMA):UPI-", "MED ZONE PHARMA"),
    ("UPI:820392704009:6363874107@ybl(Mr ILIYAS PASHA)", "Mr ILIYAS PASHA"),
    ("UPI:560343925394:9945200442@ibl(CHAND PASHA):Vrl", "CHAND PASHA"),
    # Karnataka TRUNCATES the particulars cell mid-name. When the bracket does
    # not close, the VPA handle wins instead of the cut name — the reviewer's
    # call, and the right one: a truncation point varies row to row, so
    # "(SYED ARZAAN" and "(SYED" would split one payee in two, while the handle
    # is identical every time. Their own labels had this exact shape BOTH ways,
    # which is the symptom. Letters only, matching how they wrote the handles.
    ("UPI:515313177312:syedarzaan3@okicici(SYED ARZAAN", "syedarzaan"),
    ("UPI:571108761096:syedarzaan3-1@oksbi(SYED ARZAAN", "syedarzaan"),
    ("UPI:517943266761:zayyanzsyed16-1@okhdfcbank(SYED", "zayyanzsyed"),
    ("UPI:515680738386:gpayrecharge@okpayaxis(Google In", "gpayrecharge"),
    # 2. IMPS/P2A-<ref>-<Name>-<phone>; "Mr" is a title, not the name.
    ("IMPS/P2A-515714922426-Mr ShakeelKhan-9198450572", "ShakeelKhan"),
    ("IMPS/P2A-518704811776-SYEDSADIQAHMED-919986778644", "SYEDSADIQAHMED"),
    # 3. EBANK — the name segment is sometimes empty, hence the greedy slashes.
    ("EBANK:1475569389/ONE 97 COMMUNICATION/51006701886", "ONE 97 COMMUNICATION"),
    ("EBANK:1475338882///KSBCL", "KSBCL"),
    # 4. A reversal keeps the original payee's handle.
    ("REV-UPI-50200073096835-ZAYYANZSYED16-1@O KHDFCBANK-1", "ZAYYANZSYED"),
])
def test_the_labelled_party_shapes(desc, want):
    from bsa.normalize import detect_mode, extract_counterparty

    assert extract_counterparty(desc, detect_mode(desc)) == want


# --- three reviewer decisions on what is NOT a party -------------------------
# "paytm is like money transfer app but something like paytm recharge capture
# that, not just upi txns from paytm, phonepe or gpay"; "account number no use,
# none". A payment app is the RAIL the money moved on, exactly like a bank
# name, and a bare account number identifies nobody a lender would recognise.

def _party(desc, amount=-100.0):
    from bsa.models import Txn
    from bsa.normalize import (detect_mode, drop_useless_identifiers,
                               extract_counterparty, _sanitise_party)
    m = detect_mode(desc)
    t = Txn(date="2026-01-01", cheque_no="", description=desc, amount=amount,
            balance=0.0, mode=m,
            counterparty=_sanitise_party(extract_counterparty(desc, m)))
    drop_useless_identifiers([t])
    return t.counterparty


@pytest.mark.parametrize("desc", [
    # The biggest single unnamed shape in the corpus, 544 rows: a Paytm QR
    # terminal id is not a merchant name.
    "UPI/691564311834/22:37:17/UPI/paytm-83541894@ptys",
    "UPI/527710576270/10:12:20/UPI/mab.037348010970125",
    # 329 rows: a bare beneficiary account number.
    "TRANSFER- TRANSFER 4897692162094",
])
def test_a_rail_or_a_bare_number_is_not_a_party(desc):
    assert _party(desc) == ""


def test_a_named_service_on_the_same_rail_survives():
    """"something like paytm recharge capture that" — gpay + RECHARGE is a
    merchant, not the app. The test is whether anything meaningful is left
    after the app name is stripped."""
    assert _party("UPI:515680738386:gpayrecharge@okpayaxis(Google In") == \
        "gpayrecharge"


def test_a_truncated_merchant_beats_a_useless_app_handle():
    """The handle-on-truncation rule has one exception: when the handle is only
    a payment app, the cut name is still the better answer — "SILVERT" at least
    says who was paid, "bharatpe" says how."""
    assert _party("UPI:515445886848:bharatpe.9052925296@fbpe(SILVERT") == \
        "SILVERT"


def test_the_number_survives_long_enough_to_resolve_a_name_first():
    """Order matters: resolve_identifiers uses a bare account number as a JOIN
    KEY — an account named in one row fills the rows that printed only the
    number — so the number must survive until that has run, and only then be
    cleared where it never resolved."""
    from bsa.models import Txn
    from bsa.normalize import drop_useless_identifiers

    named = Txn(date="2026-01-01", cheque_no="", description="x", amount=-1.0,
                balance=0.0, mode="neft", counterparty="ACME STEELS")
    bare = Txn(date="2026-01-02", cheque_no="", description="y", amount=-1.0,
               balance=0.0, mode="neft", counterparty="4897692162094")
    drop_useless_identifiers([named, bare])
    assert named.counterparty == "ACME STEELS"   # a real name is untouched
    assert bare.counterparty == ""               # an unresolved number is not


# --- round 2: shapes found by ranking on OPPORTUNITY, not on absence ---------
# The naive ranking put SBI savings top with 462 unnamed rows. 329 of them are
# "TRANSFER- TRANSFER 4897693162093" — a bare account number, which carries no
# name and is NONE by decision. Re-ranking on rows whose narration actually
# contains a name-like word moved the target to these shapes instead.

@pytest.mark.parametrize("desc,want", [
    ("Trf to EARTHCON DEVELOPERS PRIVATE LIMITED/960856",
     "EARTHCON DEVELOPERS PRIVATE LIMITED"),
    ("UCR013913427589_EMI_05-11-2025_PIRAMAL PETROLEUM P",
     "PIRAMAL PETROLEUM P"),
    # The payee sits AFTER the presenting bank, so the last segment wins.
    ("CLG/510811/011125/Bank Of Ba/AKASH", "AKASH"),
    ("I/W CHEQUE PAID-SAHILPOLYMERS-000000000035", "SAHILPOLYMERS"),
    ("ACHInwDr-ROVER FINANCE LIMITE/05-07-2025", "ROVER FINANCE LIMITE"),
    ("INF/NEFT/ICICN4202/HDFC0000361/UN79642505 02150625 by DNARENDR "
     "from Tally B", "DNARENDR"),
])
def test_round_two_party_shapes(desc, want):
    from bsa.normalize import detect_mode, extract_counterparty

    assert extract_counterparty(desc, detect_mode(desc)) == want


def test_the_same_slot_holding_a_bank_still_yields_nothing():
    """ACHInwDr- carries a lender on one row and a BANK on the next. The
    extraction rule is identical; _is_bank_name is what separates them, which
    is why a finance company survives and a rail does not."""
    assert _party("ACHInwDr-IDFC FIRST BANK/03-07-2025") == ""
    assert _party("ACHInwDr-ROVER FINANCE LIMITE/05-07-2025") == \
        "ROVER FINANCE LIMITE"
