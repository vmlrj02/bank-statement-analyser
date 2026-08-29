"""A protected statement carries its own password in its file name.

Whoever forwards a locked statement writes the password into the name, because
that is the only place it survives the forward. Every protected file in the
sample corpus does it — and before this, every one of them failed the upload
and asked a person to retype what was already on their screen. One was still
sitting failed in production ("Nithin SBI SB 3140 … PW- 21166141185.pdf").

These pin the two halves: what the name is read as, and that a real encrypted
PDF opens from the name alone with no password supplied.
"""
import os

import pytest

from bsa.ingest import (MAX_PASSWORD_GUESSES, PasswordRequired, ingest,
                        password_candidates)

pikepdf = pytest.importorskip("pikepdf")


# Every shape seen in the corpus, with the password each one is carrying.
CORPUS_NAMES = [
    ("Acct Statement pass - 43862308.pdf", "43862308"),
    ("Acct Statement_pass - 43888983.pdf", "43888983"),
    ("HDFC 6260 _pass- 41361703.pdf", "41361703"),
    ("Acct Statement_4672_PW- 220593370.pdf", "220593370"),
    ("PSW-176284535-HDFC MANSA ENGG.pdf", "176284535"),
    ("Karnataka Bank -JAMEELA BANU- Password -JAME1982.pdf", "JAME1982"),
    ("5 HDFC Bank Statement - SYEDZAYYAN -Password-174591602.pdf", "174591602"),
    ("Nithin SBI SB 3140 01-05-25 To 20-02-26 PW- 21166141185.pdf", "21166141185"),
    # No label at all — the name simply IS the password.
    ("133591747.pdf", "133591747"),
    ("hdfc bank statement - 174591602.pdf", "174591602"),
]


@pytest.mark.parametrize("name,expected", CORPUS_NAMES)
def test_the_password_the_name_is_carrying_is_offered(name, expected):
    assert expected in password_candidates(name)


def test_a_labelled_password_is_tried_before_a_bare_number():
    """"pass - X" is a statement of intent; a loose number is a guess. A name
    holding both must try the declared one first, or an account number in the
    file name gets to burn the attempt budget ahead of the real answer."""
    cands = password_candidates("Acct_Statement_XXXX4672_12052026_pass - 43888983.pdf")
    assert cands[0] == "43888983"


def test_a_folder_can_carry_it_too():
    """Seen for real: three files named only "133591747.pdf" filed under
    "HDFC-607-PS-133591747"."""
    assert "133591747" in password_candidates(
        "/x/HDFC-607-PS-133591747/statement.pdf")


def test_a_name_with_no_number_in_it_offers_nothing():
    assert password_candidates("statement.pdf") == []
    assert password_candidates("Acct Statement June.pdf") == []
    assert password_candidates(None) == []


def test_an_underscore_does_not_hide_a_number():
    """\\b does not fire between "_" and a digit, and bank exports are full of
    "Acct_Statement_XXXX9675_12052026.pdf". A date is a wrong guess that costs
    one open on an already-refused file; missing the right one costs an upload."""
    assert "12052026" in password_candidates(
        "Acct_Statement_XXXXXXXX9675_12052026.pdf")


def test_the_guess_list_is_bounded():
    """Every guess is a pikepdf open on an encrypted file. A name stuffed with
    numbers must not turn one upload into an unbounded try-them-all."""
    name = "-".join(str(1000000 + i) for i in range(40)) + ".pdf"
    assert len(password_candidates(name)) <= MAX_PASSWORD_GUESSES


# --- and the whole way through, on a genuinely encrypted PDF -----------------

@pytest.fixture
def locked_pdf(tmp_path):
    """A real encrypted PDF whose password is 43888983."""
    src = tmp_path / "plain.pdf"
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    pdf.save(str(src))
    pdf.close()

    out = tmp_path / "in.pdf"          # the scratch name Lambda downloads to
    pdf = pikepdf.open(str(src))
    pdf.save(str(out), encryption=pikepdf.Encryption(user="43888983",
                                                     owner="43888983"))
    pdf.close()
    return out


def test_without_the_name_it_is_still_refused(locked_pdf):
    """The baseline this is measured against: a scratch path carries no name,
    so there is nothing to read and the file legitimately needs a password."""
    with pytest.raises(PasswordRequired):
        ingest(str(locked_pdf), password=None, filename=None)


def test_the_original_name_opens_it_with_no_password_supplied(locked_pdf):
    res = ingest(str(locked_pdf), password=None,
                 filename="Acct Statement_pass - 43888983.pdf")
    # It decrypted to a working copy, not the encrypted original.
    assert res.n_pages == 1
    assert os.path.exists(res.path)


def test_a_typed_password_still_wins_over_the_name(locked_pdf):
    """The person knows something we are only inferring. A name whose guess is
    wrong must not shadow a password they actually typed."""
    res = ingest(str(locked_pdf), password="43888983",
                 filename="Acct Statement_pass - 99999999.pdf")
    assert res.n_pages == 1


def test_a_wrong_typed_password_falls_through_to_the_name(locked_pdf):
    res = ingest(str(locked_pdf), password="00000000",
                 filename="Acct Statement_pass - 43888983.pdf")
    assert res.n_pages == 1


def test_a_name_that_helps_nobody_reports_the_file_needs_a_password(locked_pdf):
    with pytest.raises(PasswordRequired) as e:
        ingest(str(locked_pdf), password=None, filename="June statement.pdf")
    assert "password-protected" in str(e.value)


def test_a_wrong_typed_password_is_reported_as_wrong(locked_pdf):
    """Not "this file is protected" — they know that. They need to be told the
    one they gave was rejected."""
    with pytest.raises(PasswordRequired) as e:
        ingest(str(locked_pdf), password="00000000", filename="June.pdf")
    assert "wrong password" in str(e.value)


def test_an_unprotected_file_never_consults_the_name(tmp_path):
    """The half that matters for the "it asked for a password on a file that
    isn't protected" report: a readable PDF is opened and that is the end of
    it, whatever its name looks like."""
    p = tmp_path / "Statement pass - 43888983.pdf"
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    pdf.save(str(p))
    pdf.close()
    assert ingest(str(p)).n_pages == 1
