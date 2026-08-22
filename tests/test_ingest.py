"""Ingest measures what can be read, which is what later explains what could
not be extracted."""
import pytest

from bsa.ingest import IngestError, MIN_PAGE_CHARS, ingest


def test_a_missing_file_is_named(tmp_path):
    with pytest.raises(IngestError) as e:
        ingest(str(tmp_path / "nope.pdf"))
    assert "file not found" in str(e.value)


def test_an_empty_file_is_rejected(tmp_path):
    p = tmp_path / "empty.pdf"
    p.write_bytes(b"")
    with pytest.raises(IngestError) as e:
        ingest(str(p))
    assert "empty file" in str(e.value)


def test_the_threshold_distinguishes_a_sparse_page_from_a_scan():
    """Below this a page has no text layer at all rather than little text."""
    assert 0 < MIN_PAGE_CHARS < 200
