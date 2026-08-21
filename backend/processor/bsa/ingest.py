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


@dataclass
class IngestResult:
    path: str                 # path to a readable (decrypted) PDF
    n_pages: int
    is_digital_text: bool     # True if a usable text layer exists
    text_density: float       # mean chars per page


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
    )
