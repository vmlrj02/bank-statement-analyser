"""The processor's own logic: the small helpers, the merge claim, and one
end-to-end merge of a multi-account upload."""
import json
from dataclasses import asdict

import pytest

from bsa.models import RawRow, StatementExtract, StatementMeta
from conftest import ConditionalCheckFailed, FakeBoto3, load_module


@pytest.fixture
def proc(jobs_table, s3):
    stub = FakeBoto3({"jobs": jobs_table}, s3=s3)
    mod = load_module("backend/processor/handler.py", "processor_undertest", stub,
                      {"JOBS_TABLE": "jobs", "DATA_BUCKET": "bucket"})
    return mod


# --------------------------------------------------------------- helpers --

def test_periods_collapse_but_a_gap_stays_visible(proc):
    """Two abutting statements read as one period; a missing month must not be
    papered over, because that would hide the gap from whoever reads the report."""
    assert proc._collapse_periods([("2026-01-01", "2026-01-31"),
                                   ("2026-02-01", "2026-02-28")]) == \
        [["2026-01-01", "2026-02-28"]]
    assert proc._collapse_periods([("2026-01-01", "2026-01-31"),
                                   ("2026-03-01", "2026-03-31")]) == \
        [["2026-01-01", "2026-01-31"], ["2026-03-01", "2026-03-31"]]


def test_periods_tolerate_unparseable_dates(proc):
    got = proc._collapse_periods([("nonsense", "2026-01-31"),
                                  ("2026-03-01", "2026-03-31")])
    assert len(got) == 2


@pytest.mark.parametrize("acct,masked", [
    ("058301562192", "XXXXXXXX2192"), ("1234", "1234"), ("", ""), ("7", "7"),
])
def test_account_masking(proc, acct, masked):
    assert proc._mask(acct) == masked


def test_slug_is_path_safe_and_never_empty(proc):
    assert proc._slug("ICICI Bank", "XXXX2192") == "icici-bank-xxxx2192"
    assert proc._slug("", "") == "account"
    assert "/" not in proc._slug("A/B Bank", "../../etc")


@pytest.mark.parametrize("key,job", [
    ("uploads/abc123/0.pdf", "abc123"),
    ("uploads/abc123/11.pdf", "abc123"),
    ("uploads/legacy.pdf", "legacy"),
])
def test_job_id_from_key(proc, key, job):
    assert proc._job_id_from_key(key) == job


def test_policy_refusals_keep_the_specific_reason(proc):
    """No layout, a scanned file and a missing parser module lead to three
    different actions, so they must not all be flattened into one message."""
    assert proc._friendly(
        "no layout for this statement (HDFC Bank) — add a layout descriptor "
        "for this bank and the AI fallback is switched off") == \
        "no layout for this statement (HDFC Bank) — add a layout descriptor for this bank"
    assert "no text layer" in proc._friendly(
        "this file has no text layer (it is a scan), so the axis_account_statement "
        "template cannot read it and the AI fallback is switched off")
    assert "inside our own AWS account" in proc._friendly(
        "LLM provider 'anthropic' ... statement data must not leave the account.")
    assert "out of credit" in proc._friendly(
        "Error: your credit balance is too low to access the API")


def test_issue_digest_counts_kinds_and_samples_the_earliest(proc):
    stmts = [{"source_file": "jan.pdf",
              "issues": [{"row_index": i, "kind": "balance_mismatch",
                          "detail": f"row {i}"} for i in range(10)]},
             {"source_file": "feb.pdf",
              "issues": [{"row_index": 0, "kind": "date_order", "detail": "d"}]}]
    assert proc._issue_kinds(stmts) == {"balance_mismatch": 10, "date_order": 1}
    sample = proc._issue_sample(stmts)
    assert len(sample) == proc.ISSUE_SAMPLE
    assert sample[0] == {"source_file": "jan.pdf", "row_index": 0,
                         "kind": "balance_mismatch", "detail": "row 0"}


# ----------------------------------------------------------- merge claim --

def test_only_one_invocation_claims_the_merge(proc, jobs_table):
    jobs_table.put_item(Item={"job_id": "j", "status": "processing"})
    assert proc._claim_merge("j") is True
    assert proc._claim_merge("j") is False


def test_a_stale_merge_claim_can_be_retaken(proc, jobs_table):
    """A hard-killed invocation used to hold "merging" forever, and no retry or
    sweeper could ever take the job back. The claim expires now."""
    jobs_table.put_item(Item={"job_id": "j", "status": "processing"})
    assert proc._claim_merge("j") is True
    jobs_table.items["j"]["merging_at"] -= proc.MERGE_CLAIM_TTL_S + 60
    assert proc._claim_merge("j") is True


def test_every_status_write_stamps_progress(proc, jobs_table):
    """The sweeper measures staleness from updated_at; a writer that forgets to
    stamp it would make a live job look stuck."""
    proc._update("j", status="processing")
    assert jobs_table.items["j"]["updated_at"] > 0
    proc._mark_failed("j", 0, "a.pdf", "boom")
    assert jobs_table.items["j"]["updated_at"] > 0


def test_redelivered_events_cannot_inflate_the_extracted_count(proc, jobs_table):
    jobs_table.put_item(Item={"job_id": "j"})
    assert proc._mark_extracted("j", 0) == 1
    assert proc._mark_extracted("j", 0) == 1
    assert proc._mark_extracted("j", 1) == 2


# ------------------------------------------------------------ full merge --

def _extract(bank, acct, src, rows):
    """rows: (date, amount, balance)."""
    return StatementExtract(
        meta=StatementMeta(bank=bank, layout="test", account_no=acct,
                           account_name="HARI SINGH", period_from="",
                           period_to="", source_file=src, n_pages=2),
        rows=[RawRow(sl_no=None, date=d, cheque_no="",
                     description=f"UPI/PARTY/{i}",
                     withdrawal=-a if a < 0 else None,
                     deposit=a if a > 0 else None, balance=b, page=1)
              for i, (d, a, b) in enumerate(rows)])


@pytest.fixture
def three_file_job(proc, jobs_table, s3, monkeypatch):
    """Three files, two accounts, INTERLEAVED — file 1 belongs to the second
    account. That interleaving is what the AI-accounting bug needed to show."""
    files = [{"idx": i, "key": f"uploads/j1/{i}.pdf", "filename": f"f{i}.pdf"}
             for i in range(3)]
    jobs_table.put_item(Item={"job_id": "j1", "status": "processing",
                              "files": files, "expected": 3, "owner": "u@x"})
    for f in files:
        s3.objects[("bucket", f["key"])] = b"%PDF-1.4\n"

    extracts = {
        0: _extract("ICICI Bank", "058301562192", "f0.pdf",
                    [("01-01-2026", -100.0, 900.0), ("02-01-2026", 50.0, 950.0)]),
        1: _extract("Axis Bank", "999888777", "f1.pdf",
                    [("01-01-2026", -20.0, 4980.0)]),
        2: _extract("ICICI Bank", "058301562192", "f2.pdf",
                    [("03-01-2026", -10.0, 940.0)]),
    }
    # Only file 1 went through the LLM, and it is in the middle of the upload.
    extracts[1].meta.llm_usage = {"provider": "bedrock", "model": "claude-x",
                                  "tokens_in": 1000, "tokens_out": 200,
                                  "calls": 1, "cost_usd": 0.005}
    order = iter([extracts[0], extracts[1], extracts[2]])
    monkeypatch.setattr(proc, "extract_one",
                        lambda path, password=None, time_left_ms=None: next(order))
    return proc, jobs_table, s3, files


def _run_all(proc, files):
    for f in files:
        proc.lambda_handler({"Records": [{"s3": {"object": {"key": f["key"]}}}]},
                            None)


def test_a_multi_account_upload_publishes_one_report_per_account(three_file_job):
    proc, jobs_table, s3, files = three_file_job
    _run_all(proc, files)
    item = jobs_table.items["j1"]
    assert item["status"] == "done"
    accounts = item["summary"]["accounts"]
    assert len(accounts) == 2
    assert {a["bank"] for a in accounts} == {"ICICI Bank", "Axis Bank"}
    icici = next(a for a in accounts if a["bank"] == "ICICI Bank")
    assert icici["rows"] == 3 and icici["statements"] == 2
    assert icici["account_no"] == "XXXXXXXX2192"       # never the full number


def test_ai_cost_is_attributed_to_the_file_that_actually_used_ai(three_file_job):
    """The regression. Grouping by account reorders the statements, and the AI
    table used to be rebuilt by zipping upload order against group order — so
    with two accounts interleaved, f1's tokens were reported against f2."""
    proc, jobs_table, s3, files = three_file_job
    _run_all(proc, files)
    ai = jobs_table.items["j1"]["summary"]["ai"]
    by_name = {p["filename"]: p for p in ai["files"]}
    assert by_name["f1.pdf"]["ai"] is True
    assert by_name["f1.pdf"]["tokens_in"] == 1000
    assert by_name["f0.pdf"]["ai"] is False and by_name["f2.pdf"]["ai"] is False
    assert ai["ai_files"] == 1 and ai["tokens_in"] == 1000


def test_validation_detail_survives_into_a_reviewable_file(proc, jobs_table,
                                                           s3, monkeypatch):
    """Issue detail used to be dropped at the merge with the note that it was
    "in the log" — which meant a failed statement's actual rows were
    unreachable from the report."""
    files = [{"idx": 0, "key": "uploads/j2/0.pdf", "filename": "broken.pdf"}]
    jobs_table.put_item(Item={"job_id": "j2", "status": "processing",
                              "files": files, "expected": 1, "owner": "u@x"})
    s3.objects[("bucket", files[0]["key"])] = b"%PDF-1.4\n"
    bad = _extract("ICICI Bank", "1234", "broken.pdf",
                   [("01-01-2026", -100.0, 900.0),
                    ("02-01-2026", 50.0, 9999.0)])      # chain breaks here
    monkeypatch.setattr(proc, "extract_one",
                        lambda *a, **k: bad)
    _run_all(proc, files)

    item = jobs_table.items["j2"]
    assert item["status"] == "needs_review"
    acct = item["summary"]["accounts"][0]
    assert acct["validation"] == "failed"
    assert acct["issue_kinds"] == {"balance_mismatch": 1}
    assert acct["issue_sample"] and "9999.00" in acct["issue_sample"][0]["detail"]

    key = next(k for (b, k) in s3.objects if k.endswith("issues.json"))
    detail = json.loads(s3.objects[("bucket", key)])
    assert detail["status"] == "failed"
    assert detail["statements"][0]["source_file"] == "broken.pdf"
    assert detail["statements"][0]["issues"][0]["kind"] == "balance_mismatch"


def test_a_failed_file_does_not_discard_the_accounts_that_worked(
        proc, jobs_table, s3, monkeypatch):
    files = [{"idx": i, "key": f"uploads/j3/{i}.pdf", "filename": f"f{i}.pdf"}
             for i in range(2)]
    jobs_table.put_item(Item={"job_id": "j3", "status": "processing",
                              "files": files, "expected": 2, "owner": "u@x"})
    for f in files:
        s3.objects[("bucket", f["key"])] = b"%PDF-1.4\n"

    good = _extract("Axis Bank", "42", "f0.pdf", [("01-01-2026", -20.0, 4980.0)])
    calls = iter([good, RuntimeError("no layout for this statement and the AI "
                                     "fallback is switched off")])

    def fake(*a, **k):
        v = next(calls)
        if isinstance(v, Exception):
            raise v
        return v
    monkeypatch.setattr(proc, "extract_one", fake)
    _run_all(proc, files)

    item = jobs_table.items["j3"]
    assert item["status"] == "needs_review"
    assert len(item["summary"]["accounts"]) == 1
    failed = item["summary"]["failed_files"]
    assert failed[0]["filename"] == "f1.pdf"
    assert failed[0]["error"] == "no layout for this statement"


def test_unreadable_pages_are_reported_as_the_cause_of_a_break(
        proc, jobs_table, s3, monkeypatch):
    """A page with no text layer contributes no rows, so the chain breaks with
    no other visible cause. Seen for real: a statement PDF assembled by hand
    with one month scanned in among digital exports."""
    files = [{"idx": 0, "key": "uploads/j4/0.pdf", "filename": "mixed.pdf"}]
    jobs_table.put_item(Item={"job_id": "j4", "status": "processing",
                              "files": files, "expected": 1, "owner": "u@x"})
    s3.objects[("bucket", files[0]["key"])] = b"%PDF-1.4\n"
    ex = _extract("ICICI Bank", "0527", "mixed.pdf",
                  [("31-10-2025", 100.0, 6717607.59),
                   ("01-12-2025", -30970.0, 7105741.59)])   # November is missing
    ex.meta.unreadable_pages = [14, 15, 16]
    monkeypatch.setattr(proc, "extract_one", lambda *a, **k: ex)
    _run_all(proc, files)

    acct = jobs_table.items["j4"]["summary"]["accounts"][0]
    assert acct["validation"] == "failed"
    assert acct["unreadable_pages"] == 3

    key = next(k for (b, k) in s3.objects if k.endswith("issues.json"))
    detail = json.loads(s3.objects[("bucket", key)])
    assert detail["statements"][0]["unreadable_pages"] == [14, 15, 16]


def test_a_fully_readable_statement_reports_no_unreadable_pages(three_file_job):
    proc, jobs_table, s3, files = three_file_job
    _run_all(proc, files)
    for a in jobs_table.items["j1"]["summary"]["accounts"]:
        assert a["unreadable_pages"] == 0
