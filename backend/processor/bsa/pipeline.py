"""End-to-end pipeline: the same stages Lambda will run, callable locally."""
from __future__ import annotations

import importlib

from .classify import classify
from .categorize import categorize
from .extract.llm_providers import (
    NoLayoutError, ResidencyError, fallback_enabled, residency_block)
from .ingest import ingest
from .models import JobResult, StatementExtract
from .normalize import normalize, dedup_merge
from .publish import publish
from .registry import get_layout
from .validate import validate

TEMPLATE_PARSERS = {
    "icici_optransactionhistory": "bsa.extract.icici_optransactionhistory",
}


def extract_one(path: str, password: str | None = None,
                time_left_ms=None) -> StatementExtract:
    ing = ingest(path, password=password)
    cls = classify(ing.path)

    def _stamp(extract):
        extract.meta.n_pages = ing.n_pages      # page count belongs to the file
        extract.meta.unreadable_pages = list(ing.empty_pages or [])
        extract.meta.producer = ing.producer
        extract.meta.creator = ing.creator
        extract.meta.pdf_created = ing.created
        extract.meta.pdf_modified = ing.modified
        return extract

    if cls.layout_id and ing.is_digital_text:
        layout = get_layout(cls.layout_id)
        source_file = path.split("/")[-1]
        # A layout is either driven entirely by its YAML (parser: generic) or
        # backed by a bank-specific module for the cases YAML cannot express.
        if layout.get("parser") == "generic":
            from .extract.generic_layout import extract as generic_extract
            return _stamp(generic_extract(ing.path, source_file=source_file,
                                          layout=layout))
        if layout.get("parser") == "columnar":
            from .extract.columnar_layout import extract as columnar_extract
            return _stamp(columnar_extract(ing.path, source_file=source_file,
                                           layout=layout))
        if layout.get("parser") == "grouped":
            from .extract.grouped_layout import extract as grouped_extract
            return _stamp(grouped_extract(ing.path, source_file=source_file,
                                          layout=layout))
        if cls.layout_id in TEMPLATE_PARSERS:
            mod = importlib.import_module(TEMPLATE_PARSERS[cls.layout_id])
            return _stamp(mod.extract(ing.path, source_file=source_file,
                                      layout=layout))
    # Unknown layout, or a scanned PDF with no text to parse. The only thing
    # that can read it is an LLM — and that is the one step in this pipeline
    # capable of sending statement data somewhere else, so it is refused here,
    # before the PDF is chunked into a request body, rather than at the client.
    if not fallback_enabled():
        raise NoLayoutError(_why_unreadable(cls, ing) +
                            " and the AI fallback is switched off")
    if block := residency_block():
        raise ResidencyError(block)
    from .extract.llm_fallback import extract_with_llm
    return _stamp(extract_with_llm(ing.path, source_file=path.split("/")[-1],
                                   time_left_ms=time_left_ms))


def _why_unreadable(cls, ing) -> str:
    """Name the actual reason no template could read this file.

    Three different situations end up at the same fallback, and telling a
    person "no layout for this bank" when a layout matched perfectly well — the
    PDF was a scan — sends them off to write a descriptor that already exists.
    """
    if cls.layout_id and not ing.is_digital_text:
        return (f"this file has no text layer (it is a scan), so the "
                f"{cls.layout_id} template cannot read it")
    if not cls.layout_id and not ing.is_digital_text:
        # No readable text AND no fingerprint match: the file, not the
        # registry, is the problem. Saying "add a layout descriptor" here sent
        # an operator off to write a layout for a PDF no layout could read.
        return ("no readable text in this PDF (a scan, an image-only export, "
                "or a protected file), so it cannot be matched to any bank")
    if cls.layout_id:
        # A descriptor matched but nothing claimed it: `parser: module` with no
        # module registered in TEMPLATE_PARSERS. Only reachable via an S3
        # descriptor, since a bundled one would have been caught in review.
        return (f"layout {cls.layout_id} declares a bank-specific parser that "
                f"this build does not have")
    bank = f" ({cls.bank})" if cls.bank else ""
    return (f"no layout for this statement{bank} — add a layout descriptor "
            f"for this bank")


def run(paths: list[str], out_dir: str, password: str | None = None,
        related_parties: list[str] | None = None,
        basename: str = "statement") -> JobResult:
    extracts = [extract_one(p, password=password) for p in paths]
    txn_lists = [normalize(e) for e in extracts]
    txns = dedup_merge(txn_lists) if len(txn_lists) > 1 else txn_lists[0]
    categorize(txns, related_parties=related_parties)
    report = validate(txns)
    result = JobResult(meta=extracts[0].meta, txns=txns, validation=report)
    publish(result, out_dir, basename=basename)
    return result
