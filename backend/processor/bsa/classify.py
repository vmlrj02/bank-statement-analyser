"""Stage 2 — Classify: identify (bank, layout) from page-1 fingerprints."""
from __future__ import annotations

from dataclasses import dataclass

import pdfplumber

from .registry import all_layouts


@dataclass
class Classification:
    layout_id: str | None         # None => unknown -> LLM path
    bank: str | None
    page1_text: str


def classify(pdf_path: str) -> Classification:
    with pdfplumber.open(pdf_path) as pdf:
        text = pdf.pages[0].extract_text() or ""
        # Some exports lead with a blank cover page (a scanned wrapper, or a
        # Gmail print preview), so the fingerprint sits on page 2. Fingerprints
        # are page-1 patterns by design, so page 1 is tried first; only when it
        # carries no usable text do we widen the search to the next few pages,
        # rather than let a whole statement fail as "no layout".
        if len(text.strip()) < 40:
            more = [text]
            for page in pdf.pages[1:5]:
                more.append(page.extract_text() or "")
                if sum(len(t) for t in more) > 200:
                    break
            text = "\n".join(more)

    for layout_id, desc in all_layouts().items():
        fp = desc.get("fingerprints", {})
        any_of = fp.get("any_of", [])
        all_of = fp.get("all_of", [])
        if any_of and not any(s in text for s in any_of):
            continue
        if all_of and not all(s in text for s in all_of):
            continue
        if any_of or all_of:
            return Classification(layout_id=layout_id, bank=desc.get("bank"), page1_text=text)

    return Classification(layout_id=None, bank=None, page1_text=text)
