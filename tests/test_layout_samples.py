"""Regression over real statements.

The layouts are the product, and the only honest check on one is a real PDF
reconciling at its full row count. Those PDFs contain live account data and
cannot be committed, so this suite runs against a directory you point it at:

    BSA_SAMPLE_DIR=/path/to/statements pytest tests/test_layout_samples.py -v

Each PDF must classify to a layout, extract, and validate to "passed". A file
needing a password can be given one as BSA_SAMPLE_PW_<stem in caps>.
Without the variable the whole module skips, so CI stays green on a checkout
that has no statements — CI cannot have them either.
"""
import os
from pathlib import Path

import pytest

SAMPLE_DIR = os.environ.get("BSA_SAMPLE_DIR")
pytestmark = pytest.mark.skipif(
    not SAMPLE_DIR, reason="set BSA_SAMPLE_DIR to a folder of statement PDFs")

PDFS = sorted(Path(SAMPLE_DIR).glob("*.pdf")) if SAMPLE_DIR else []


def _password_for(path: Path):
    key = "BSA_SAMPLE_PW_" + "".join(
        c if c.isalnum() else "_" for c in path.stem).upper()
    return os.environ.get(key)


@pytest.mark.parametrize("pdf", PDFS, ids=lambda p: p.name)
def test_sample_statement_reconciles(pdf):
    from bsa.classify import classify
    from bsa.ingest import ingest
    from bsa.normalize import normalize
    from bsa.pipeline import extract_one
    from bsa.validate import validate

    ing = ingest(str(pdf), password=_password_for(pdf))
    cls = classify(ing.path)
    if not cls.layout_id:
        pytest.skip(f"no layout for {pdf.name} yet — this is the work item, "
                    f"not a regression")

    extract = extract_one(str(pdf), password=_password_for(pdf))
    txns = normalize(extract)
    assert txns, f"{pdf.name} classified as {cls.layout_id} but produced no rows"
    report = validate(txns)

    # A page with no text layer contributes no rows, so the chain breaks and no
    # layout could have prevented it. That is a property of the file, not a
    # regression in the parser, so it is called out rather than failed — but
    # only when there really are unreadable pages to blame.
    unreadable = list(extract.meta.unreadable_pages or [])
    if report.status != "passed" and unreadable:
        pytest.skip(f"{pdf.name} [{cls.layout_id}]: {len(unreadable)} page(s) "
                    f"have no text layer ({unreadable[:8]}), so their rows are "
                    f"unreadable by any template — {report.status} over "
                    f"{len(txns)} rows")

    # Judge extraction by whether the WHOLE chain reconciles, never by the
    # issue count: a contiguous run of dropped rows shows as one mismatch.
    assert report.status == "passed", (
        f"{pdf.name} [{cls.layout_id}] {report.status} over {len(txns)} rows; "
        f"first issue: {report.issues[0].detail if report.issues else 'n/a'}")

    # A statement can reconcile yet still be mis-extracted: the SBI "Nithya"
    # description bug mangled every narration to "TRANSFER- - / -" while the
    # balance chain still passed, so party detection collapsed to ~2%. Guard the
    # quality too — on statements with enough name-bearing transfer rows, party
    # fill must not fall through the floor. It is a catastrophe detector (catches
    # a 2% collapse), not an accuracy bar, so the threshold is deliberately low
    # and only applies when there are enough candidates to be meaningful.
    import re as _re
    from bsa.categorize import categorize
    categorize(txns)
    unnameable = _re.compile(r"UPISETTLEMENT|\bPOS\b|ATW-|CHRGS|/GST/|CASH", _re.I)
    transferish = _re.compile(r"UPI|NEFT|RTGS|IMPS|MMT|ACH|TRANSFER|TRF|INF[/-]", _re.I)
    nameable = [t for t in txns
                if transferish.search(t.description) and not unnameable.search(t.description)]
    filled = sum(1 for t in nameable
                 if t.counterparty and t.counterparty != "unknown party")
    pct = round(100 * filled / len(nameable)) if nameable else None
    # Co-operative banks transfer internally by a member/loan serial number, not
    # a name, so a low fill there is real, not a regression — exempt them.
    bank = (extract.meta.bank or "").lower()
    is_coop = "co-op" in bank or "cooperative" in bank or "co operative" in bank
    if len(nameable) >= 25 and not is_coop:
        assert filled / len(nameable) >= 0.10, (
            f"{pdf.name} [{cls.layout_id}]: party fill collapsed to {pct}% "
            f"({filled}/{len(nameable)} name-bearing rows) — a description or "
            f"party-rule regression, even though the balance still reconciles")
    print(f"{pdf.name}: {cls.layout_id} — {len(txns)} rows, passed, party {pct}%")
