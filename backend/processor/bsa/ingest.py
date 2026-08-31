"""Stage 1 — Ingest & prepare.

Opens the PDF (decrypting if a password is supplied), measures text density
per page, and exposes a pdfplumber handle for downstream stages.
"""
from __future__ import annotations

import io
import os
import re
import tempfile
from dataclasses import dataclass

import pdfplumber

try:
    import pikepdf
    HAVE_PIKEPDF = True
except ImportError:          # pragma: no cover
    HAVE_PIKEPDF = False


class IngestError(Exception):
    pass


class PasswordRequired(IngestError):
    """Raised when the PDF is encrypted and no/wrong password was given."""


# A page below this is not a sparse page, it is a page with no text layer —
# a scan, or an image pasted into an otherwise digital PDF.
MIN_PAGE_CHARS = 40


@dataclass
class IngestResult:
    path: str                 # path to a readable (decrypted) PDF
    n_pages: int
    is_digital_text: bool     # True if a usable text layer exists
    text_density: float       # mean chars per page
    # 1-based page numbers with no extractable text. A template parser reads
    # nothing from these, so the transactions on them are silently absent and
    # the balance chain breaks where they were. Recording them turns an
    # unexplained mismatch into a sentence: "3 pages had no readable text".
    # Seen for real: a statement PDF assembled by hand, with one month
    # scanned in among digital exports.
    empty_pages: list[int] = None
    # PDF document metadata, for the integrity check. A genuine bank export is
    # produced by a server PDF library (iText, OpenPDF); a hand-assembled or
    # edited statement shows an editing tool (pdf-lib, Photoshop, Quartz) and
    # often a ModDate after its CreationDate.
    producer: str = ""
    creator: str = ""
    created: str = ""
    modified: str = ""


# --- passwords that the file is carrying in its own name ---------------------
#
# Whoever sends a protected statement almost always writes its password into
# the file name, because that is the only place it survives being forwarded:
#   "Acct Statement pass - 43888983.pdf"        "HDFC 6260 _pass- 41361703.pdf"
#   "Acct Statement_4672_PW- 220593370.pdf"     "PSW-176284535-HDFC MANSA.pdf"
#   "Karnataka Bank -JAMEELA BANU- Password -JAME1982.pdf"
# and sometimes the name IS the password ("133591747.pdf", filed under a
# folder called "HDFC-607-PS-133591747"). Asking a person to retype what is
# already on the screen is the kind of friction that makes an upload fail for
# no reason, so the pipeline reads it instead.
#
# Every candidate is only ever TRIED: a wrong guess costs one cheap pikepdf
# open and falls through to the next, and a file that opens with no password
# never gets here at all. So this can never lock someone out — it can only
# save them a step.
_PW_LABELLED = re.compile(
    r"(?:pass(?:word)?|pwd|psw|pw|ps)\s*[-:_ ]{0,3}\s*([A-Za-z0-9@#]{4,})", re.I)
# A bare run of digits long enough to be a password rather than a page number.
# Deliberately loose — account numbers and dates match too and simply fail,
# which costs nothing: candidates are only ever tried on a file that has
# ALREADY refused to open, so an unprotected statement never pays for them.
# The boundary is "not a digit" rather than \b, because \b does not fire
# between "_" and a digit and bank exports are full of
# "Acct_Statement_XXXX9675_12052026.pdf".
_PW_BARE = re.compile(r"(?<!\d)(\d{6,12})(?!\d)")
# The commonest convention of all, and the one we were missing: the first few
# letters of the account holder's name glued to a few digits — "PRAD2597" for a
# PRADEEP, "NETH1112" for a NETHRA, "SRAM1006", "NKES0301". Banks mail it that
# way and the customer forwards the file under the name it arrived with, so the
# password is sitting on the file we were handed. Four statements in the August
# sample drop failed on this alone.
#
# The token must be letters IMMEDIATELY followed by digits with a non-alphanumeric
# boundary either side, which is what keeps it from firing on ordinary export
# names: "Bankstatement_10085925401" separates them with an underscore, and
# "OpTransactionHistory27" has too many letters to match.
_PW_NAME_DIGITS = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{3,6}\d{3,6})(?![A-Za-z0-9])")
# Trying every guess on a big encrypted file is the only way this could get
# slow, so the list is bounded. Six covers every shape seen in the corpus.
MAX_PASSWORD_GUESSES = 6


def password_candidates(name: str | None) -> list[str]:
    """Passwords implied by a file's own name (and its folder, when we have a
    real path). Labelled forms first — "pass - 12345678" is a statement of
    intent — then bare numeric tokens, which are a guess."""
    if not name:
        return []
    base = os.path.basename(name)
    parent = os.path.basename(os.path.dirname(name))
    stem = os.path.splitext(base)[0]

    out: list[str] = []

    def add(v):
        if v and v not in out:
            out.append(v)

    for text in (base, parent):
        for m in _PW_LABELLED.finditer(text or ""):
            add(m.group(1))
    # Name+digits before bare digits: it is the more specific shape, and a
    # filename that carries one usually carries several digit runs too (an
    # account number, a date) that would otherwise crowd it out of the cap.
    for text in (stem, parent):
        for m in _PW_NAME_DIGITS.finditer(text or ""):
            add(m.group(1))
            add(m.group(1).upper())          # banks print these in caps
    for text in (stem, parent):
        for m in _PW_BARE.finditer(text or ""):
            add(m.group(1))
    return out[:MAX_PASSWORD_GUESSES]


def ingest(path: str, password: str | None = None,
           filename: str | None = None) -> IngestResult:
    if not os.path.exists(path):
        raise IngestError(f"file not found: {path}")
    if os.path.getsize(path) == 0:
        raise IngestError("empty file")

    work_path = path
    docinfo: dict = {}
    # Decrypt if needed
    if HAVE_PIKEPDF:
        try:
            pdf = pikepdf.open(path)                      # unencrypted
            docinfo = _read_docinfo(pdf)
            pdf.close()
        except pikepdf.PasswordError:
            # The one the person typed is tried first — they know something we
            # are only inferring — then whatever the file name is carrying.
            attempts = ([password] if password else []) + [
                c for c in password_candidates(filename or path) if c != password]
            pdf = None
            for candidate in attempts:
                try:
                    pdf = pikepdf.open(path, password=candidate)
                    break
                except pikepdf.PasswordError:
                    continue
            if pdf is None:
                # "Wrong password" only when one was actually offered and
                # rejected; otherwise this file simply needs one, and saying so
                # is what the upload screen asks the person to act on.
                raise PasswordRequired(
                    "wrong password" if password else "PDF is password-protected")
            docinfo = _read_docinfo(pdf)
            tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
            pdf.save(tmp.name)
            pdf.close()
            work_path = tmp.name
        except pikepdf.PdfError as e:
            raise IngestError(f"not a valid PDF: {e}")

    with pdfplumber.open(work_path) as pdf:
        n_pages = len(pdf.pages)
        # Sample up to 3 pages for text density
        sample = pdf.pages[: min(3, n_pages)]
        chars = [len(p.extract_text() or "") for p in sample]
        density = sum(chars) / max(len(chars), 1)

    return IngestResult(
        path=work_path,
        n_pages=n_pages,
        is_digital_text=density > 200,   # scanned pages yield ~0
        text_density=density,
        empty_pages=unreadable_pages(work_path),
        producer=docinfo.get("producer", ""),
        creator=docinfo.get("creator", ""),
        created=docinfo.get("created", ""),
        modified=docinfo.get("modified", ""),
    )


def _read_docinfo(pdf) -> dict:
    """PDF /Info metadata as plain strings — diagnostic only, never fatal."""
    try:
        di = pdf.docinfo
        g = lambda k: str(di.get(k, "")) if di.get(k) is not None else ""
        return {"producer": g("/Producer"), "creator": g("/Creator"),
                "created": g("/CreationDate"), "modified": g("/ModDate")}
    except Exception:                                       # noqa: BLE001
        return {}


def unreadable_pages(path: str) -> list[int]:
    """1-based pages with no text layer, via pypdfium2 rather than pdfminer.

    Every page has to be looked at — a single scanned page spliced into an
    otherwise digital statement is exactly what a first-three-pages sample
    misses, and is precisely what breaks a balance chain for no visible reason.

    But pdfminer is not the tool for that question. Asking pdfplumber for every
    page's text costs about as long again as the whole extraction (4s on a
    58-page statement, which doubled a run), because it does full layout
    analysis to answer a question that only needs a character count.
    pypdfium2 — already a dependency, since pdfplumber renders through it —
    answers it in 0.13s and agrees on the result.

    Diagnostic only: any failure here returns "nothing to report" rather than
    taking down an extraction that would otherwise have succeeded.
    """
    try:
        import pypdfium2 as pdfium
    except ImportError:                                     # pragma: no cover
        return []
    doc = None
    try:
        doc = pdfium.PdfDocument(path)
        return [i + 1 for i in range(len(doc))
                if len(doc[i].get_textpage().get_text_bounded().strip())
                < MIN_PAGE_CHARS]
    except Exception as e:                                  # noqa: BLE001
        print(f"ingest: could not scan {path} for unreadable pages: {e}")
        return []
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:                               # noqa: BLE001
                pass
