"""Rejoining a narration that wrapped mid-token.

Most banks wrap the narration cell at a space, so the lines rejoin with one.
Some wrap at the CELL EDGE, mid-token: BoB prints
"IMPS/P2A/.../Miscell" then "aneou", and Karnataka Bank prints
"UPI:...:aqib.macho@okhd" then "fcbank(CITY GUN H". Space-joining those splits
a payee across two words, so the party never consolidates with the same payee
spelled whole on another row.

The rule is per LINE, not per bank, because the same statement does both.
"""
from bsa.extract.generic_layout import _join_narration

EDGE = 319.0          # the cell's measured right edge


def test_with_no_edge_every_join_is_a_space():
    """The default, and every layout that has not opted in. This is what keeps
    the change invisible to the other thirty descriptors: joining line strings
    with a space is the same string as joining all their words with a space."""
    lines = [("CHARGES FOR", 199.0), ("IMPS/P2A/509218514532", 270.0)]
    assert _join_narration(lines) == "CHARGES FOR IMPS/P2A/509218514532"


def test_a_line_that_reaches_the_edge_was_cut_mid_token():
    lines = [("IMPS/P2A/518208007942/XXXXXXXXXX1495/Miscell", 316.0),
             ("aneou", 167.0)]
    assert _join_narration(lines, EDGE) == \
        "IMPS/P2A/518208007942/XXXXXXXXXX1495/Miscellaneou"


def test_a_line_that_stops_short_broke_at_a_space():
    """Both happen in one statement, which is why the test is per line."""
    lines = [("CHARGES FOR", 199.0), (":IMPS/P2A/50", 270.0)]
    assert _join_narration(lines, EDGE) == "CHARGES FOR :IMPS/P2A/50"


def test_a_trailing_hyphen_is_a_continuation_marker():
    """A token can also be cut without reaching the edge, when the remainder
    was too wide to fit at all: BoB breaks "paytm-53817591@ptyb" after the
    hyphen at 274 in a cell running to 319."""
    lines = [("UPI/612459393592/08:38:55/UPI/paytm-", 274.5),
             ("53817591@ptyb", 199.0)]
    assert _join_narration(lines, EDGE) == \
        "UPI/612459393592/08:38:55/UPI/paytm-53817591@ptyb"


def test_a_trailing_slash_joins_too():
    lines = [("RTGS-SEEMA MUSHEER-HDFC/", 238.0), ("KARBH26155843224", 211.0)]
    assert _join_narration(lines, EDGE) == \
        "RTGS-SEEMA MUSHEER-HDFC/KARBH26155843224"


def test_empty_lines_are_skipped_not_joined_as_gaps():
    lines = [("", 0.0), ("REAL TEXT", 150.0), ("", 0.0)]
    assert _join_narration(lines, EDGE) == "REAL TEXT"


def test_a_single_line_is_returned_whole():
    assert _join_narration([("ONLY LINE", 300.0)], EDGE) == "ONLY LINE"
    assert _join_narration([]) == ""
