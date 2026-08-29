"""Structured narration parser: isolate the payer's remark from the counterparty
so a lender name typed in a remark can't misclassify the transaction."""
from bsa.narration import parse_narration


def test_upi_remark_is_separated_from_counterparty():
    n = parse_narration("UPI/CR/446230491393/happylaser/HDFC BANK/parimal finance amount")
    assert n.channel == "upi"
    assert n.counterparty == "happylaser"
    assert n.remark == "parimal finance amount"
    assert "parimal finance" not in n.structured.lower()   # the fix: not matched


def test_no_remark_leaves_structured_whole():
    n = parse_narration("UPI/P2A/557573893934/Sandeep Sandeep /UPI/State Bank Of India")
    assert n.counterparty == "Sandeep Sandeep"
    assert n.remark == ""
    assert n.structured == n.raw                            # nothing removed


def test_unrecognised_format_is_a_safe_passthrough():
    n = parse_narration("SOME OPAQUE REF 12345 NARRATION")
    assert n.remark == ""
    assert n.structured == n.raw                            # never regress


def test_parimal_finance_no_longer_tags_loan(tmp_path):
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
    from test_categorization_accuracy import _categorize_one
    # lender name in the remark -> stays a regular credit
    assert _categorize_one("UPI/CR/44/happylaser/HDFC BANK/parimal finance amount",
                           50000.0).category == "Regular credit"
    # lender as the actual counterparty -> still recognised
    assert _categorize_one("NBSM/141757229/L&T FINANCE LTD/", -80513.90).category \
        in ("EMI transaction", "Interest / fee payments")


def test_channel_prefix_and_bank_codes_are_not_names():
    n = parse_narration("NEFT CR-SBINN52025070153048367 -KAMLAINDUSTRIES -SBIN0009566 - /ATTN/TRF")
    assert n.counterparty == "KAMLAINDUSTRIES"      # not "NEFT CR", not "ATTN"
    n2 = parse_narration("WDL TFR IMPS/609616554865/MAHB- xx872-Mr. Vigh/trf")
    assert n2.counterparty == "Mr. Vigh"            # not "WDL TFR IMPS", not "MAHB"


def test_ltd_company_is_a_counterparty_not_a_bank():
    """'ADITYA BIRLA FINANCE LTD' was classed as a bank segment because of the
    LTD token, so the row had no counterparty at all. A limited company is the
    party; a real bank segment says BANK or carries a bank code."""
    n = parse_narration("NEFT DR-AUBLN62025071622798999 -ADITYA BIRLA FINANCE LTD -HDFC0000060 -6")
    assert n.counterparty == "ADITYA BIRLA FINANCE LTD"


def test_single_channel_letter_is_not_a_name():
    """IndusInd prints 'N/<ref>/<bank>/<NAME>' — the leading N (NEFT) must not
    be read as the counterparty, and the real company after the bank code must
    stay in the structured part (it is the party, not a remark)."""
    n = parse_narration("N/INDBH19110369425/UTIB/AXIS FINANACE LTD")
    assert n.counterparty == "AXIS FINANACE LTD"
    assert "AXIS FINANACE" in n.structured


def test_fill_from_narration_only_fills_blanks():
    from bsa.models import RawRow, StatementMeta, StatementExtract
    from bsa.normalize import normalize
    meta = StatementMeta(bank="B", layout="x", account_no="1", account_name="",
                         period_from="", period_to="", source_file="f.pdf")
    rows = [RawRow(sl_no=None, date="2026-01-01", cheque_no="",
                   description="NEFT CR-SBINN52025070153048367 -KAMLAINDUSTRIES -SBIN0009566",
                   withdrawal=None, deposit=100.0, balance=100.0, page=1)]
    tx = normalize(StatementExtract(meta=meta, rows=rows))
    assert tx[0].counterparty == "KAMLAINDUSTRIES"
