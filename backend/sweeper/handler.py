"""Sweeper Lambda — nothing stays "processing" forever.

A processor invocation that is hard-killed cannot write its own status. It has
no chance to: the kill is the point. So a job could sit on "processing" with a
spinner in the UI indefinitely, and the only way to find out was to ask.

This closes that two ways, because they fail differently:

  * ON FAILURE (fast, precise). The processor's async invocation sends a
    failure notification to an SQS queue that this function consumes, so a
    timeout or an unhandled crash arrives here with the original S3 event
    attached. We know exactly which file died and can settle it in seconds.

    The queue is not incidental. Naming this function as the processor's
    destination directly is a dependency cycle CloudFormation refuses to
    deploy — a destination grants the PROCESSOR permission to invoke the
    sweeper, while the sweeper needs the processor's name to re-drive a merge
    and permission to invoke it. A queue depends on neither. It also means a
    failure notification outlives a sweeper invocation that itself fails.

  * ON A SCHEDULE (slow, total). Every few minutes, any job that has not
    progressed within its grace period is failed with a plain reason. This is
    the backstop for everything the destination cannot see: an upload the
    browser abandoned, a delivery that never arrived, a merge whose claim went
    stale.

Settling one file is not the end of the story: the other statements in that job
were extracted and are sitting in work/, already paid for. So after settling,
this re-drives the merge by re-invoking the processor with a synthetic event
for a file that DID succeed. The processor sees the work object already exists,
skips extraction, and merges — publishing the accounts that worked, with the
dead file listed as failed. Re-driving through the real path rather than
duplicating the merge here means there is only ever one merge implementation.

Triggers : EventBridge schedule; SQS queue of processor failure notifications
Writes   : job status/error in DynamoDB; invokes the processor to re-drive
"""
import json
import os
import time

import boto3
from boto3.dynamodb.conditions import Attr

ddb = boto3.resource("dynamodb")
lam = boto3.client("lambda")
s3 = boto3.client("s3")
TABLE = ddb.Table(os.environ["JOBS_TABLE"])
BUCKET = os.environ["DATA_BUCKET"]
PROCESSOR = os.environ.get("PROCESSOR_FUNCTION", "")

# An upload the browser started and never finished. Presigned PUT URLs expire
# in 15 minutes, so past an hour it is certain nothing more is coming.
UPLOAD_GRACE_S = int(os.environ.get("UPLOAD_GRACE_S", str(60 * 60)))
# The processor's own ceiling is 15 minutes. Measured from the last progress
# stamp, not from job creation, so a genuinely long multi-file job is safe.
PROCESS_GRACE_S = int(os.environ.get("PROCESS_GRACE_S", str(20 * 60)))
# Every re-drive loop is bounded. A merge that deterministically times out, or
# a file the processor dies on the same way every attempt, would otherwise be
# re-driven every sweep forever — each pass stamping updated_at so the job
# never even looks stuck for long enough to be failed.
MAX_REDRIVES = int(os.environ.get("MAX_REDRIVES", "3"))

LIVE = ("awaiting_upload", "processing", "merging")


def _now() -> int:
    return int(time.time())


def _last_progress(item) -> int:
    return int(item.get("updated_at") or item.get("created_at") or 0)


def _stale_reason(item, now: int):
    """Why this job is stuck, or None if it is simply still working."""
    status = str(item.get("status", ""))
    if status not in LIVE:
        return None
    idle = now - _last_progress(item)
    if status == "awaiting_upload":
        if idle > UPLOAD_GRACE_S:
            return ("the upload never finished — the browser stopped before "
                    "sending every file. Upload again.")
        return None
    if idle > PROCESS_GRACE_S:
        return (f"processing stopped without finishing (no progress for "
                f"{idle // 60} minutes). The statement may be too large for a "
                f"single run — try uploading it in smaller parts.")
    return None


def _settled_indexes(item) -> set:
    """Indexes that already have a result or a recorded failure."""
    done = {str(x) for x in (item.get("extracted") or set())}
    return done | {str(k) for k in (item.get("failed_files") or {})}


def _has_work(job_id: str, idx) -> bool:
    try:
        s3.head_object(Bucket=BUCKET, Key=f"work/{job_id}/{idx}.json")
        return True
    except Exception:                                       # noqa: BLE001
        return False


def _upload_exists(key: str) -> bool:
    try:
        s3.head_object(Bucket=BUCKET, Key=key)
        return True
    except Exception:                                       # noqa: BLE001
        return False


def _fail_file(job_id: str, idx, filename: str, error: str) -> None:
    """Record a per-file failure and count the file as settled.

    Mirrors the processor's own _mark_failed: the merge counts settled files,
    so a file that will never produce a result has to be marked both failed and
    extracted, or the job waits on it forever.
    """
    now = _now()
    try:
        TABLE.update_item(
            Key={"job_id": job_id},
            UpdateExpression=("SET failed_files.#i = :v, updated_at = :t "
                              "ADD extracted :k"),
            ExpressionAttributeNames={"#i": str(idx)},
            ExpressionAttributeValues={
                ":v": {"filename": filename, "error": error[:300]},
                ":t": now, ":k": {str(idx)}},
            ConditionExpression="attribute_exists(failed_files)",
        )
    except Exception:                                       # noqa: BLE001
        TABLE.update_item(
            Key={"job_id": job_id},
            UpdateExpression=("SET failed_files = :m, updated_at = :t "
                              "ADD extracted :k"),
            ExpressionAttributeValues={
                ":m": {str(idx): {"filename": filename, "error": error[:300]}},
                ":t": now, ":k": {str(idx)}},
        )


def _fail_job(job_id: str, reason: str) -> None:
    TABLE.update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET #s = :s, #e = :e, updated_at = :t",
        ExpressionAttributeNames={"#s": "status", "#e": "error"},
        ExpressionAttributeValues={":s": "failed", ":e": reason[:900],
                                   ":t": _now()},
    )


def _redrive_allowed(job_id: str, item) -> bool:
    """Count this re-drive against the job's cap; False means the job was
    failed instead of re-driven.

    Every path that re-invokes the processor goes through here, so no re-drive
    loop — a merge that deterministically dies, a file the processor is killed
    on the same way every attempt — can run more than MAX_REDRIVES times.

    Read-then-SET rather than an atomic ADD: the sweeper is the only writer of
    redrive_count, and at worst a concurrent scheduled sweep and destination
    delivery under-count one attempt, against a cap that exists to stop a loop
    of dozens. (The test fake models ADD only for string sets, and modelling a
    real numeric ADD there is not this change's call to make.)
    """
    count = int(item.get("redrive_count", 0) or 0) + 1
    if count > MAX_REDRIVES:
        _fail_job(job_id, (
            f"processing was retried {MAX_REDRIVES} times without completing — "
            "something in this upload fails the same way every attempt. "
            "Try uploading the statements again, in smaller parts."))
        print(f"sweeper: redrive cap reached for {job_id}; failed the job")
        return False
    TABLE.update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET redrive_count = :n, updated_at = :t",
        ExpressionAttributeValues={":n": count, ":t": _now()},
    )
    item["redrive_count"] = count
    return True


def _redrive(job_id: str, item):
    """Re-invoke the processor so it merges whatever did succeed.

    Points at a file whose work object already exists, so the processor skips
    straight past extraction. Returns True when the merge was re-driven,
    "capped" when the re-drive cap failed the job instead (truthy: the job is
    settled, callers must not fail it again), and False when there is nothing
    to merge.
    """
    if not PROCESSOR:
        return False
    files = item.get("files") or []
    for f in files:
        idx = f.get("idx", 0)
        if str(idx) in (item.get("failed_files") or {}):
            continue
        if not _has_work(job_id, idx):
            continue
        if not _redrive_allowed(job_id, item):
            return "capped"
        # A stale "merging" claim would block the processor from taking the
        # merge; the claim expires, but clearing the status here means the
        # re-drive works on the first attempt rather than after the TTL.
        if item.get("status") == "merging":
            TABLE.update_item(
                Key={"job_id": job_id},
                UpdateExpression="SET #s = :p, updated_at = :t REMOVE merging_at",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":p": "processing", ":t": _now()},
            )
        lam.invoke(
            FunctionName=PROCESSOR, InvocationType="Event",
            Payload=json.dumps({"Records": [
                {"s3": {"object": {"key": f.get("key",
                                                f"uploads/{job_id}/{idx}.pdf")}}}
            ]}).encode())
        print(f"sweeper: re-drove merge for {job_id} via idx {idx}")
        return True
    return False


def _reconcile(job_id: str, item):
    """Settle or restart every file a stale job is still waiting on.

    Re-driving the merge while an index is unsettled is a zombie loop: the
    re-driven processor skips extraction (its work object exists), stamps
    updated_at via _mark_extracted — so the job looks fresh again — sees
    settled < expected, and exits. Repeat every sweep until the work objects
    expire. So before any merge re-drive, each unsettled index is resolved by
    looking at its upload object:

      * missing  — the upload never completed (presigned PUTs expire in 15
        minutes, and this job is already past its grace period). Fail the FILE,
        which counts it settled, so the merge stops waiting on it.
      * present  — the bytes arrived but the extraction never finished (a lost
        delivery, or a kill between writing work/ and marking extracted).
        Re-invoke the processor for THAT file, through the same capped path.

    Returns "invoked" when the processor was re-invoked, "capped" when the
    re-drive cap failed the job, None when only in-place settling happened
    (the caller should then re-read the item and re-drive the merge).
    """
    settled = _settled_indexes(item)
    missing, restart = [], []
    for f in item.get("files") or []:
        idx = str(f.get("idx", 0))
        if idx in settled:
            continue
        key = f.get("key", f"uploads/{job_id}/{idx}.pdf")
        if _upload_exists(key):
            restart.append((idx, key))
        else:
            missing.append((idx, f.get("filename", "file")))
    for idx, filename in missing:
        _fail_file(job_id, idx, filename,
                   "the upload never completed — this file was never received. "
                   "Upload it again.")
        print(f"sweeper: settled {job_id} idx {idx} — upload never completed")
    if not restart or not PROCESSOR:
        return None
    if not _redrive_allowed(job_id, item):
        return "capped"
    for idx, key in restart:
        lam.invoke(
            FunctionName=PROCESSOR, InvocationType="Event",
            Payload=json.dumps({"Records": [
                {"s3": {"object": {"key": key}}}]}).encode())
        print(f"sweeper: re-invoked processor for {job_id} idx {idx} "
              "(uploaded but never extracted)")
    return "invoked"


# ---------- on-failure destination ----------

def _handle_invocation_failure(event) -> None:
    """Settle the one file whose processor invocation died."""
    req = (event.get("requestPayload") or {})
    err = (event.get("responsePayload") or {})
    reason = str(err.get("errorMessage") or err.get("errorType")
                 or "the processor stopped without finishing")
    for rec in req.get("Records", []):
        key = ((rec.get("s3") or {}).get("object") or {}).get("key", "")
        if not key.startswith("uploads/"):
            continue
        rest = key.split("/", 1)[1]
        job_id = rest.split("/", 1)[0] if "/" in rest else rest.rsplit(".", 1)[0]
        item = TABLE.get_item(Key={"job_id": job_id}).get("Item") or {}
        entry = next((f for f in (item.get("files") or [])
                      if f.get("key") == key), None)
        idx = entry.get("idx", 0) if entry else 0
        if str(idx) in _settled_indexes(item):
            # A retry or the sweeper got there first — with one exception: the
            # dead invocation may have been the MERGE itself, whose triggering
            # file is settled by definition. Skipping it silently left the job
            # on "merging" until the slow scheduled sweep noticed. Re-drive it
            # now, through the same capped path.
            if item.get("status") == "merging":
                _redrive(job_id, item)
                print(f"sweeper: re-drove dead merge for {job_id}")
            continue
        filename = (entry or {}).get("filename", "file")
        timed_out = "timed out" in reason.lower()
        friendly = ("this statement could not be processed in one run — try "
                    "uploading it in smaller parts" if timed_out
                    else f"this statement could not be processed — {reason[:180]}")
        _fail_file(job_id, idx, filename, friendly)
        print(f"sweeper: settled {job_id} idx {idx} after invocation failure")

        fresh = TABLE.get_item(Key={"job_id": job_id}).get("Item") or {}
        expected = int(fresh.get("expected", len(fresh.get("files") or [])) or 0)
        if len(_settled_indexes(fresh)) >= expected and not _redrive(job_id, fresh):
            _fail_job(job_id, "; ".join(
                f"{v.get('filename')}: {v.get('error')}"
                for v in (fresh.get("failed_files") or {}).values()))


# ---------- scheduled sweep ----------

def _sweep() -> dict:
    now = _now()
    swept, redriven, scanned = 0, 0, 0
    # Project only what the sweep reads. A finished job's `summary` carries
    # every account, its category counts and the AI table, and dominates the
    # item; leaving it out keeps a scan of a large table cheap to deserialise.
    # NOTE ON SCALE: this is a filtered scan, so it reads every job in the
    # table, and the table keeps 180 days of them. The on-failure destination
    # already handles the common case exactly and instantly — this is only the
    # backstop — so it runs infrequently. If the table ever grows to the point
    # where this is a cost line, the fix is a sparse index: set a `live`
    # attribute while a job is running and remove it when it settles, then
    # query that instead of scanning.
    kwargs = {
        "FilterExpression": Attr("status").is_in(list(LIVE)),
        "ProjectionExpression": ("job_id, #s, created_at, updated_at, expected, "
                                 "extracted, failed_files, files, merging_at, "
                                 "redrive_count"),
        "ExpressionAttributeNames": {"#s": "status"},
    }
    while True:
        page = TABLE.scan(**kwargs)
        for item in page.get("Items", []):
            scanned += 1
            reason = _stale_reason(item, now)
            if not reason:
                continue
            job_id = item["job_id"]
            if item.get("status") == "awaiting_upload":
                _fail_job(job_id, reason)
                swept += 1
                print(f"sweeper: failed stuck job {job_id} — {reason}")
                continue
            # Prefer publishing what worked over failing the whole upload: the
            # other statements are extracted and sitting in work/. But FIRST
            # settle or restart anything the job is still waiting on — a merge
            # re-driven while an index is unsettled just refreshes updated_at
            # and exits, which looped every sweep on a job whose later files
            # were never uploaded.
            files = item.get("files") or []
            expected = int(item.get("expected", len(files)) or 0)
            if len(_settled_indexes(item)) < expected:
                got = _reconcile(job_id, item)
                if got == "invoked":
                    redriven += 1
                    continue
                if got == "capped":
                    swept += 1
                    continue
                # Missing uploads were failed in place; re-read to see whether
                # everything is now settled and the merge can be re-driven.
                item = TABLE.get_item(Key={"job_id": job_id}).get("Item") or item
                if len(_settled_indexes(item)) < expected:
                    _fail_job(job_id, reason)
                    swept += 1
                    print(f"sweeper: failed stuck job {job_id} — {reason}")
                    continue
            got = _redrive(job_id, item)
            if got == "capped":
                swept += 1
            elif got:
                redriven += 1
            else:
                _fail_job(job_id, reason)
                swept += 1
                print(f"sweeper: failed stuck job {job_id} — {reason}")
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return {"scanned": scanned, "failed": swept, "redriven": redriven}


def _failure_notifications(event):
    """Yield each async-failure payload carried by this event.

    Arrives as SQS records whose body is the destination payload. The bare
    payload shape is still accepted so the function can be invoked by hand
    with one, which is how you replay a specific failure.
    """
    if not isinstance(event, dict):
        return
    if "responsePayload" in event:
        yield event
        return
    for rec in event.get("Records", []):
        body = rec.get("body")
        if body is None:
            continue
        try:
            payload = json.loads(body)
        except (TypeError, ValueError):
            print(f"sweeper: ignoring unparseable queue message: {body!r:.200}")
            continue
        if isinstance(payload, dict) and "responsePayload" in payload:
            yield payload


def lambda_handler(event, _ctx):
    notifications = list(_failure_notifications(event))
    if notifications:
        for payload in notifications:
            _handle_invocation_failure(payload)
        return {"ok": True, "mode": "on_failure", "handled": len(notifications)}
    out = _sweep()
    print(f"sweeper: {out}")
    return {"ok": True, "mode": "scheduled", **out}
