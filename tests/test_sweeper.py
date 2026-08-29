"""The sweeper exists because a hard-killed Lambda cannot write its own status.
Its whole job is to make "processing forever" impossible, and to prefer
publishing what worked over failing a whole upload."""
import json

import pytest


def job(status="processing", idle=0, now=1_700_000_000, **extra):
    return {"job_id": "j", "status": status, "created_at": now - idle,
            "updated_at": now - idle, **extra}


# ------------------------------------------------------------- staleness --

def test_a_job_making_progress_is_left_alone(sweeper):
    now = 1_700_000_000
    assert sweeper._stale_reason(job(idle=60, now=now), now) is None


def test_a_long_but_live_job_is_left_alone(sweeper):
    """A twenty-file upload legitimately runs a long time. Staleness is idle
    time since the last progress stamp, never age since creation."""
    now = 1_700_000_000
    it = job(idle=0, now=now)
    it["created_at"] = now - 3 * 60 * 60
    assert sweeper._stale_reason(it, now) is None


def test_a_stalled_job_is_reported_with_something_a_person_can_act_on(sweeper):
    now = 1_700_000_000
    reason = sweeper._stale_reason(job(idle=sweeper.PROCESS_GRACE_S + 60, now=now), now)
    assert reason and "smaller parts" in reason


def test_an_abandoned_upload_gets_a_longer_grace_and_its_own_message(sweeper):
    now = 1_700_000_000
    early = job(status="awaiting_upload", idle=sweeper.PROCESS_GRACE_S + 60, now=now)
    assert sweeper._stale_reason(early, now) is None       # not yet
    late = job(status="awaiting_upload", idle=sweeper.UPLOAD_GRACE_S + 60, now=now)
    assert "upload never finished" in sweeper._stale_reason(late, now)


def test_a_stale_merge_claim_is_swept_too(sweeper):
    now = 1_700_000_000
    assert sweeper._stale_reason(
        job(status="merging", idle=sweeper.PROCESS_GRACE_S + 60, now=now), now)


@pytest.mark.parametrize("status", ["done", "failed", "needs_review", "reviewed"])
def test_finished_jobs_are_never_touched(sweeper, status):
    now = 1_700_000_000
    assert sweeper._stale_reason(job(status=status, idle=10**6, now=now), now) is None


def test_a_job_with_no_timestamps_at_all_is_still_swept(sweeper):
    """An item written before updated_at existed must not be immortal."""
    now = 1_700_000_000
    assert sweeper._stale_reason({"job_id": "j", "status": "processing"}, now)


# ---------------------------------------------------------------- sweeping --

def test_a_stuck_job_with_nothing_salvageable_is_failed(sweeper, jobs_table):
    import time
    old = int(time.time()) - sweeper.PROCESS_GRACE_S - 120
    jobs_table.put_item(Item={"job_id": "j", "status": "processing",
                              "created_at": old, "updated_at": old,
                              "files": [{"idx": 0, "key": "uploads/j/0.pdf",
                                         "filename": "a.pdf"}]})
    out = sweeper.lambda_handler({"source": "aws.events"}, None)
    assert out["failed"] == 1
    assert jobs_table.items["j"]["status"] == "failed"
    assert "no progress" in jobs_table.items["j"]["error"]


def test_a_stuck_job_with_finished_work_is_re_driven_instead(sweeper, jobs_table, s3):
    """The other statements are extracted and already paid for. Failing the
    whole upload would throw them away.

    File 1's upload never arrived, so the sweep first settles it as a per-file
    failure (re-driving the merge while an index is unsettled would just loop),
    THEN re-drives the merge through the file that did succeed."""
    import time
    old = int(time.time()) - sweeper.PROCESS_GRACE_S - 120
    jobs_table.put_item(Item={
        "job_id": "j", "status": "processing", "created_at": old, "updated_at": old,
        "expected": 2, "extracted": {"0"},
        "files": [{"idx": 0, "key": "uploads/j/0.pdf", "filename": "a.pdf"},
                  {"idx": 1, "key": "uploads/j/1.pdf", "filename": "b.pdf"}]})
    s3.objects[("bucket", "work/j/0.json")] = b"{}"
    out = sweeper.lambda_handler({"source": "aws.events"}, None)
    assert out["redriven"] == 1 and out["failed"] == 0
    assert "never completed" in jobs_table.items["j"]["failed_files"]["1"]["error"]
    fn, payload = sweeper._fake_lambda.invocations[0]
    assert fn == "proc-fn"
    assert json.loads(payload)["Records"][0]["s3"]["object"]["key"] == \
        "uploads/j/0.pdf"


def test_a_stale_merge_claim_is_cleared_before_re_driving(sweeper, jobs_table, s3):
    """The processor refuses to take a merge that another invocation holds, so
    the claim has to be released or the re-drive is a no-op."""
    import time
    old = int(time.time()) - sweeper.PROCESS_GRACE_S - 120
    jobs_table.put_item(Item={
        "job_id": "j", "status": "merging", "created_at": old, "updated_at": old,
        "merging_at": old, "expected": 1, "extracted": {"0"},
        "files": [{"idx": 0, "key": "uploads/j/0.pdf", "filename": "a.pdf"}]})
    s3.objects[("bucket", "work/j/0.json")] = b"{}"
    sweeper.lambda_handler({"source": "aws.events"}, None)
    assert jobs_table.items["j"]["status"] == "processing"
    assert "merging_at" not in jobs_table.items["j"]
    assert sweeper._fake_lambda.invocations


def test_an_abandoned_multi_file_upload_settles_instead_of_looping(
        sweeper, jobs_table, s3):
    """The zombie loop: file 0 processed but files 1-2 were never uploaded.
    Re-driving via file 0 made the job look fresh (the processor stamps
    updated_at), saw 1 < 3 settled and exited — every ~20 minutes until the
    work object expired. Now the sweep settles the never-uploaded files first,
    then re-drives the merge exactly once."""
    import time
    old = int(time.time()) - sweeper.PROCESS_GRACE_S - 120
    jobs_table.put_item(Item={
        "job_id": "j", "status": "processing", "created_at": old, "updated_at": old,
        "expected": 3, "extracted": {"0"},
        "files": [{"idx": i, "key": f"uploads/j/{i}.pdf", "filename": f"f{i}.pdf"}
                  for i in range(3)]})
    s3.objects[("bucket", "uploads/j/0.pdf")] = b"%PDF"
    s3.objects[("bucket", "work/j/0.json")] = b"{}"
    # uploads 1 and 2 never arrived — no objects for them

    out = sweeper.lambda_handler({"source": "aws.events"}, None)
    assert out["redriven"] == 1 and out["failed"] == 0
    ff = jobs_table.items["j"]["failed_files"]
    assert set(ff) == {"1", "2"}
    assert "never completed" in ff["1"]["error"]
    # every index now counts as settled, so nothing is left to loop on
    assert jobs_table.items["j"]["extracted"] == {"0", "1", "2"}
    # and the one invocation is the merge re-drive, via the file that worked
    assert len(sweeper._fake_lambda.invocations) == 1
    _fn, payload = sweeper._fake_lambda.invocations[0]
    assert json.loads(payload)["Records"][0]["s3"]["object"]["key"] == \
        "uploads/j/0.pdf"


def test_an_uploaded_but_unextracted_file_is_re_extracted_not_merged(
        sweeper, jobs_table, s3):
    """The bytes are in S3 but the extraction never happened (a lost delivery,
    or a kill between writing work/ and marking extracted). The right move is
    to re-invoke the processor for THAT file — not to merge without it, and
    not to fail a file that can still succeed."""
    import time
    old = int(time.time()) - sweeper.PROCESS_GRACE_S - 120
    jobs_table.put_item(Item={
        "job_id": "j", "status": "processing", "created_at": old, "updated_at": old,
        "expected": 2, "extracted": {"0"},
        "files": [{"idx": 0, "key": "uploads/j/0.pdf", "filename": "a.pdf"},
                  {"idx": 1, "key": "uploads/j/1.pdf", "filename": "b.pdf"}]})
    s3.objects[("bucket", "uploads/j/0.pdf")] = b"%PDF"
    s3.objects[("bucket", "uploads/j/1.pdf")] = b"%PDF"
    s3.objects[("bucket", "work/j/0.json")] = b"{}"
    # no work/j/1.json — file 1 was uploaded but never extracted

    out = sweeper.lambda_handler({"source": "aws.events"}, None)
    assert out["redriven"] == 1 and out["failed"] == 0
    assert len(sweeper._fake_lambda.invocations) == 1
    _fn, payload = sweeper._fake_lambda.invocations[0]
    assert json.loads(payload)["Records"][0]["s3"]["object"]["key"] == \
        "uploads/j/1.pdf"
    it = jobs_table.items["j"]
    assert it["status"] == "processing"          # not failed — it can succeed
    assert "failed_files" not in it
    assert it["redrive_count"] == 1


def test_redrives_are_capped_so_a_deterministic_failure_cannot_loop(
        sweeper, jobs_table, s3):
    """A merge that dies the same way every attempt used to be re-driven every
    sweep forever. The cap turns it into a failed job with a plain reason."""
    import time
    old = int(time.time()) - sweeper.PROCESS_GRACE_S - 120
    jobs_table.put_item(Item={
        "job_id": "j", "status": "processing", "created_at": old, "updated_at": old,
        "expected": 1, "extracted": {"0"}, "redrive_count": sweeper.MAX_REDRIVES,
        "files": [{"idx": 0, "key": "uploads/j/0.pdf", "filename": "a.pdf"}]})
    s3.objects[("bucket", "work/j/0.json")] = b"{}"
    out = sweeper.lambda_handler({"source": "aws.events"}, None)
    assert out["failed"] == 1 and out["redriven"] == 0
    assert not sweeper._fake_lambda.invocations
    it = jobs_table.items["j"]
    assert it["status"] == "failed"
    assert "retried" in it["error"]


# ------------------------------------------------- on-failure destination --

def _payload(key, message="2026-08-22T00:00:00Z Task timed out after 900.00 seconds"):
    return {"version": "1.0", "requestPayload": {"Records": [
                {"s3": {"object": {"key": key}}}]},
            "responsePayload": {"errorType": "Sandbox.Timedout",
                                "errorMessage": message}}


def _failure_event(key, message="2026-08-22T00:00:00Z Task timed out after 900.00 seconds"):
    """As it actually arrives: an SQS record whose body is the payload.

    The queue sits between the processor and this function because naming the
    function as the destination directly is a dependency cycle CloudFormation
    refuses to deploy.
    """
    return {"Records": [{"messageId": "m1",
                         "body": json.dumps(_payload(key, message))}]}


def test_a_killed_invocation_settles_its_own_file_at_once(sweeper, jobs_table, s3):
    jobs_table.put_item(Item={
        "job_id": "j", "status": "processing", "expected": 2, "extracted": {"0"},
        "files": [{"idx": 0, "key": "uploads/j/0.pdf", "filename": "a.pdf"},
                  {"idx": 1, "key": "uploads/j/1.pdf", "filename": "b.pdf"}]})
    s3.objects[("bucket", "work/j/0.json")] = b"{}"
    out = sweeper.lambda_handler(_failure_event("uploads/j/1.pdf"), None)
    assert out["mode"] == "on_failure"
    failed = jobs_table.items["j"]["failed_files"]["1"]
    assert failed["filename"] == "b.pdf"
    assert "smaller parts" in failed["error"]
    # and the file counts as settled, so the merge is no longer waiting on it
    assert "1" in jobs_table.items["j"]["extracted"]
    assert sweeper._fake_lambda.invocations                # merge re-driven


def test_the_last_file_failing_with_nothing_salvageable_fails_the_job(
        sweeper, jobs_table):
    jobs_table.put_item(Item={
        "job_id": "j", "status": "processing", "expected": 1,
        "files": [{"idx": 0, "key": "uploads/j/0.pdf", "filename": "a.pdf"}]})
    sweeper.lambda_handler(_failure_event("uploads/j/0.pdf", "boom"), None)
    assert jobs_table.items["j"]["status"] == "failed"
    assert "a.pdf" in jobs_table.items["j"]["error"]


def test_a_file_already_settled_is_not_settled_twice(sweeper, jobs_table):
    """The processor may have caught and recorded the error itself before the
    destination fired; overwriting it would replace a precise reason with a
    generic one."""
    jobs_table.put_item(Item={
        "job_id": "j", "status": "processing", "expected": 1, "extracted": {"0"},
        "failed_files": {"0": {"filename": "a.pdf", "error": "wrong password"}},
        "files": [{"idx": 0, "key": "uploads/j/0.pdf", "filename": "a.pdf"}]})
    sweeper.lambda_handler(_failure_event("uploads/j/0.pdf"), None)
    assert jobs_table.items["j"]["failed_files"]["0"]["error"] == "wrong password"


def test_a_dead_merge_is_re_driven_not_ignored(sweeper, jobs_table, s3):
    """When the MERGE invocation dies, its triggering file is settled by
    definition, and the destination used to skip it silently — leaving the job
    on "merging" until the slow scheduled sweep. Now it re-drives at once,
    through the capped path."""
    jobs_table.put_item(Item={
        "job_id": "j", "status": "merging", "merging_at": 1_700_000_000,
        "expected": 1, "extracted": {"0"},
        "files": [{"idx": 0, "key": "uploads/j/0.pdf", "filename": "a.pdf"}]})
    s3.objects[("bucket", "work/j/0.json")] = b"{}"
    out = sweeper.lambda_handler(_failure_event("uploads/j/0.pdf"), None)
    assert out["mode"] == "on_failure"
    it = jobs_table.items["j"]
    # the FILE was fine — no per-file failure invented for it
    assert not it.get("failed_files")
    assert it["status"] == "processing"          # stale claim released
    assert "merging_at" not in it
    assert it["redrive_count"] == 1
    assert sweeper._fake_lambda.invocations


def test_a_dead_merge_still_respects_the_redrive_cap(sweeper, jobs_table, s3):
    jobs_table.put_item(Item={
        "job_id": "j", "status": "merging", "merging_at": 1_700_000_000,
        "expected": 1, "extracted": {"0"},
        "redrive_count": sweeper.MAX_REDRIVES,
        "files": [{"idx": 0, "key": "uploads/j/0.pdf", "filename": "a.pdf"}]})
    s3.objects[("bucket", "work/j/0.json")] = b"{}"
    sweeper.lambda_handler(_failure_event("uploads/j/0.pdf"), None)
    assert jobs_table.items["j"]["status"] == "failed"
    assert "retried" in jobs_table.items["j"]["error"]
    assert not sweeper._fake_lambda.invocations


def test_an_unrelated_key_is_ignored(sweeper, jobs_table):
    sweeper.lambda_handler(_failure_event("outputs/j/report.csv"), None)
    assert jobs_table.items == {}


def test_the_scan_projects_every_attribute_the_sweep_reads(sweeper, jobs_table, s3):
    """The scan projects a subset to keep it cheap. DynamoDB returns ONLY what
    is projected, so a field left out of the list simply reads as absent — in
    production, silently. This exercises the sweep against a table whose items
    are trimmed to exactly the projection."""
    import time
    old = int(time.time()) - sweeper.PROCESS_GRACE_S - 120
    jobs_table.put_item(Item={
        "job_id": "j", "status": "merging", "created_at": old, "updated_at": old,
        "merging_at": old, "expected": 2, "extracted": {"0"},
        "failed_files": {"1": {"filename": "b.pdf", "error": "no layout"}},
        "files": [{"idx": 0, "key": "uploads/j/0.pdf", "filename": "a.pdf"},
                  {"idx": 1, "key": "uploads/j/1.pdf", "filename": "b.pdf"}],
        # bulky attributes deliberately NOT projected
        "summary": {"accounts": [{"slug": "a"} for _ in range(50)]},
        "owner": "u@x", "related_parties": ["x"],
    })
    s3.objects[("bucket", "work/j/0.json")] = b"{}"
    out = sweeper.lambda_handler({"source": "aws.events"}, None)
    assert out["redriven"] == 1
    assert sweeper._fake_lambda.invocations


def test_a_bare_payload_is_still_accepted(sweeper, jobs_table):
    """So one failure can be replayed by invoking the function with it."""
    jobs_table.put_item(Item={
        "job_id": "j", "status": "processing", "expected": 1,
        "files": [{"idx": 0, "key": "uploads/j/0.pdf", "filename": "a.pdf"}]})
    out = sweeper.lambda_handler(_payload("uploads/j/0.pdf", "boom"), None)
    assert out["mode"] == "on_failure"
    assert jobs_table.items["j"]["status"] == "failed"


def test_several_failures_in_one_batch_are_all_settled(sweeper, jobs_table, s3):
    jobs_table.put_item(Item={
        "job_id": "j", "status": "processing", "expected": 3, "extracted": {"0"},
        "files": [{"idx": i, "key": f"uploads/j/{i}.pdf", "filename": f"f{i}.pdf"}
                  for i in range(3)]})
    s3.objects[("bucket", "work/j/0.json")] = b"{}"
    out = sweeper.lambda_handler({"Records": [
        {"messageId": "m1", "body": json.dumps(_payload("uploads/j/1.pdf"))},
        {"messageId": "m2", "body": json.dumps(_payload("uploads/j/2.pdf"))},
    ]}, None)
    assert out["handled"] == 2
    assert set(jobs_table.items["j"]["failed_files"]) == {"1", "2"}


def test_an_unparseable_queue_message_falls_through_to_a_sweep(sweeper, jobs_table):
    """It must not crash the invocation — SQS would redeliver it forever."""
    out = sweeper.lambda_handler(
        {"Records": [{"messageId": "m1", "body": "not json at all"}]}, None)
    assert out["mode"] == "scheduled"
