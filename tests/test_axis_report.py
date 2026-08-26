"""The Axis 'Account Statement Report' module reads the money by VALUE, not by
fixed band, because the amount/flag/balance x-positions shift between the export's
sub-layouts and a long particulars cell overflows into the amount column."""
from bsa.extract.axis_report import _nums, _amount


def test_nums_ignores_overflow_and_refs():
    # a particulars glyph overflowed onto the amount's left edge; still read right
    assert _nums("I 29,98,970.00")[-1] == 2998970.0
    # ref numbers carry no decimal, so they are ignored; amount + balance remain
    assert _nums("AXSK250920026958 25,40,680.00 CR -,13,87,07,853.95") == [
        2540680.0, -138707853.95]
    # the two rightmost decimals are (amount, balance)
    nums = _nums("60,00,000.00 CR -5,82,59,161.43")
    assert nums[-2] == 6000000.0 and nums[-1] == -58259161.43


def test_amount_handles_indian_negative_prefix():
    assert _amount("-,14,07,52,837.95") == -140752837.95
    # the module reads amounts via _nums, which requires an integer part
    assert _nums(".90") == []
