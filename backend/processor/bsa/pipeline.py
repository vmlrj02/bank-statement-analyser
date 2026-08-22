"""End-to-end pipeline: the same stages Lambda will run, callable locally."""
from __future__ import annotations

import importlib

from .classify import classify
from .categorize import categorize
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
    # unknown layout or scanned -> LLM path
    from .extract.llm_fallback import extract_with_llm
    return _stamp(extract_with_llm(ing.path, source_file=path.split("/")[-1],
                                   time_left_ms=time_left_ms))


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
