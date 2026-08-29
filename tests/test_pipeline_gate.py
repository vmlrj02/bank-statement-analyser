"""The fallback gate sits in the pipeline, not just at the provider call, so an
unrecognised statement is refused before it is read into a request body."""
import pytest

from bsa import pipeline
from bsa.classify import Classification
from bsa.extract.llm_providers import NoLayoutError, ResidencyError
from bsa.ingest import IngestResult


@pytest.fixture
def unknown_bank(monkeypatch, tmp_path):
    pdf = tmp_path / "mystery.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(pipeline, "ingest", lambda p, password=None, filename=None: IngestResult(
        path=str(pdf), n_pages=3, is_digital_text=True, text_density=900.0))
    monkeypatch.setattr(pipeline, "classify", lambda p: Classification(
        layout_id=None, bank=None, page1_text="Some Bank We Do Not Know"))
    return str(pdf)


@pytest.fixture(autouse=True)
def closed_by_default(monkeypatch):
    for k in ("LLM_FALLBACK", "ALLOW_EXTERNAL_LLM", "LLM_PROVIDER"):
        monkeypatch.delenv(k, raising=False)


def test_unknown_bank_fails_fast_with_something_actionable(unknown_bank):
    with pytest.raises(NoLayoutError) as e:
        pipeline.extract_one(unknown_bank)
    msg = str(e.value)
    assert "no layout" in msg and "add a layout descriptor" in msg


def test_an_unreadable_pdf_is_not_blamed_on_a_missing_layout(monkeypatch, tmp_path):
    """A protected or image-only PDF yields no text, so no fingerprint can
    match — but "add a layout descriptor" sends an operator to write a layout
    no parser could ever use. The file, not the registry, must be named."""
    pdf = tmp_path / "locked.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(pipeline, "ingest", lambda p, password=None, filename=None: IngestResult(
        path=str(pdf), n_pages=10, is_digital_text=False, text_density=0.0))
    monkeypatch.setattr(pipeline, "classify", lambda p: Classification(
        layout_id=None, bank=None, page1_text=""))
    with pytest.raises(NoLayoutError) as e:
        pipeline.extract_one(str(pdf))
    msg = str(e.value)
    assert "no readable text" in msg
    assert "add a layout descriptor" not in msg


def test_the_llm_module_is_never_even_imported(unknown_bank, monkeypatch):
    import bsa.extract.llm_fallback as fb
    monkeypatch.setattr(fb, "extract_with_llm", lambda *a, **k:
                        pytest.fail("the LLM path must not be reached"))
    with pytest.raises(NoLayoutError):
        pipeline.extract_one(unknown_bank)


def test_with_the_fallback_on_an_external_provider_is_still_refused(
        unknown_bank, monkeypatch):
    """Two switches, deliberately. Turning the fallback on is about capability;
    it is not consent to send statements to a third party."""
    monkeypatch.setenv("LLM_FALLBACK", "on")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    with pytest.raises(ResidencyError):
        pipeline.extract_one(unknown_bank)


def test_both_switches_open_reaches_the_extractor(unknown_bank, monkeypatch):
    monkeypatch.setenv("LLM_FALLBACK", "on")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ALLOW_EXTERNAL_LLM", "true")
    import bsa.extract.llm_fallback as fb
    from bsa.models import StatementExtract, StatementMeta
    monkeypatch.setattr(fb, "extract_with_llm", lambda *a, **k: StatementExtract(
        meta=StatementMeta(bank="Some Bank", layout="llm_fallback", account_no="1",
                           account_name="", period_from="", period_to="",
                           source_file="mystery.pdf"), rows=[]))
    assert pipeline.extract_one(unknown_bank).meta.layout == "llm_fallback"


def test_a_known_layout_never_consults_the_gate(monkeypatch, tmp_path):
    """A bank with a template must keep working regardless of the LLM flags —
    that is the whole point of writing layouts."""
    pdf = tmp_path / "axis.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(pipeline, "ingest", lambda p, password=None, filename=None: IngestResult(
        path=str(pdf), n_pages=1, is_digital_text=True, text_density=900.0))
    monkeypatch.setattr(pipeline, "classify", lambda p: Classification(
        layout_id="axis_account_statement", bank="Axis Bank", page1_text=""))
    from bsa.models import StatementExtract, StatementMeta
    import bsa.extract.generic_layout as gl
    monkeypatch.setattr(gl, "extract", lambda path, source_file, layout:
                        StatementExtract(meta=StatementMeta(
                            bank="Axis Bank", layout="axis_account_statement",
                            account_no="1", account_name="", period_from="",
                            period_to="", source_file=source_file), rows=[]))
    assert pipeline.extract_one(str(pdf)).meta.bank == "Axis Bank"


def test_a_scanned_pdf_with_a_matching_layout_says_so(monkeypatch, tmp_path):
    """Three situations reach the same fallback. Telling someone "no layout for
    this bank" when a layout matched fine — the file was a scan — sends them
    off to write a descriptor that already exists."""
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(pipeline, "ingest", lambda p, password=None, filename=None: IngestResult(
        path=str(pdf), n_pages=4, is_digital_text=False, text_density=3.0,
        empty_pages=[1, 2, 3, 4]))
    monkeypatch.setattr(pipeline, "classify", lambda p: Classification(
        layout_id="axis_account_statement", bank="Axis Bank", page1_text=""))
    with pytest.raises(NoLayoutError) as e:
        pipeline.extract_one(str(pdf))
    assert "no text layer" in str(e.value)
    assert "axis_account_statement" in str(e.value)


def test_a_layout_naming_a_parser_this_build_lacks_says_so(monkeypatch, tmp_path,
                                                           unknown_bank):
    """Reachable only through an S3 descriptor declaring `parser: module` with
    no module behind it."""
    from bsa import registry
    monkeypatch.setattr(pipeline, "classify", lambda p: Classification(
        layout_id="ghost_layout", bank="Ghost Bank", page1_text=""))
    monkeypatch.setattr(pipeline, "get_layout", lambda lid: {
        "id": "ghost_layout", "bank": "Ghost Bank", "parser": "module",
        "fingerprints": {"any_of": ["Ghost"]}})
    with pytest.raises(NoLayoutError) as e:
        pipeline.extract_one(unknown_bank)
    assert "this build does not have" in str(e.value)
