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
        in ("EMI transaction", "Interest payments")
