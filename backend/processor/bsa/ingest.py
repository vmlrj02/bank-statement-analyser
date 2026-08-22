"""Stage 1 — Ingest & prepare.

Opens the PDF (decrypting if a password is supplied), measures text density
per page, and exposes a pdfplumber handle for downstream stages.
"""
from __future__ import annotations

import io
import os
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


def ingest(path: str, password: str | None = None) -> IngestResult:
    if not os.path.exists(path):
        raise IngestError(f"file not found: {path}")
    if os.path.getsize(path) == 0:
        raise IngestError("empty file")

    work_path = path
    # Decrypt if needed
    if HAVE_PIKEPDF:
        try:
            pdf = pikepdf.open(path)                      # unencrypted
            pdf.close()
        except pikepdf.PasswordError:
            if not password:
                raise PasswordRequired("PDF is password-protected")
            try:
                pdf = pikepdf.open(path, password=password)
            except pikepdf.PasswordError:
                raise PasswordRequired("wrong password")
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
    )


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
