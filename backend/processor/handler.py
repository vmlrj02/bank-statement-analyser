"""Processor Lambda — extracts one statement per invocation, then merges.

Trigger: S3 ObjectCreated on uploads/{job_id}/{i}.pdf (legacy: uploads/{id}.pdf)

Each uploaded file fires its own event, and each invocation handles only its
own file: extract, normalise, write the rows to work/{job_id}/{i}.json. The
invocation that completes the final file merges every part, validates and
publishes. That gives each statement a full Lambda timeout of its own and lets
files process in parallel, so a twenty-file job costs about as long as its
slowest single statement rather than the sum of all of them.

Persisting per-file results also means a failed or timed-out job can be retried
without re-running — and re-paying for — the extractions that already finished.

Writes : work/{job_id}/{i}.json (intermediate, expires after 7 days)
         outputs/{job_id}/statement_transactions.csv, statement_analysis.xlsx,
         statement.json, preview.json ; job status + summary in DynamoDB.
"""
import json
import os
import re
import shutil
import time
import traceback
from datetime import date
from dataclasses import asdict
from types import SimpleNamespace

import boto3

from bsa.categorize import categorize, category_detail
from bsa.completeness import check_completeness
from bsa.credit_summary import credit_summary
from bsa.ingest import PasswordRequired
from bsa.integrity import account_integrity
from bsa.models import JobResult, StatementMeta, Txn, ValidationReport
from bsa.normalize import normalize, dedup_merge
from bsa.pipeline import extract_one
from bsa.publish import publish
from bsa.validate import validate

s3 = boto3.client("s3")
ddb = boto3.resource("dynamodb")
TABLE = ddb.Table(os.environ["JOBS_TABLE"])
BUCKET = os.environ["DATA_BUCKET"]

# A presigned PUT has no content-length ceiling, so the size gate lives here:
# an arbitrarily large object is refused BEFORE it is downloaded into /tmp,
# and refused as a normal per-file failure, so the rest of the upload still
# merges and the person is told which file was too big.
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))


def _ddb_safe(v):
    """DynamoDB's resource interface rejects Python floats — every number must
    be a Decimal. Convert recursively on the way in (the API converts back to
    plain numbers on the way out). NaN/Inf can't be a Decimal, so they become
    None. Note: the in-memory test fake accepts floats, so this gap only shows
    in production — hence converting at the single write path, not per caller."""
    import math
    from decimal import Decimal
    if isinstance(v, bool):
        return v
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
        return Decimal(str(round(v, 4)))
    if isinstance(v, dict):
        return {k: _ddb_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_ddb_safe(x) for x in v]
    if isinstance(v, (set, frozenset)):
        # A number set must hold Decimals too; a string set is unchanged. Without
        # this a float inside a set would slip past and DynamoDB would reject it.
        return {_ddb_safe(x) for x in v}
    return v


def _update(job_id, **attrs):
    """Write job attributes, always stamping updated_at.

    The sweeper decides a job is stuck from how long it has sat without
    progress, and created_at cannot answer that: a twenty-file job legitimately
    runs for a long time after it was created. Stamping here — the one place
    every status change goes through — means no writer can forget to.
    """
    attrs = _ddb_safe(dict(attrs, updated_at=int(time.time())))
    expr = ", ".join(f"#k{i} = :v{i}" for i in range(len(attrs)))
    TABLE.update_item(
        Key={"job_id": job_id},
        UpdateExpression=f"SET {expr}",
        ExpressionAttributeNames={f"#k{i}": k for i, k in enumerate(attrs)},
        ExpressionAttributeValues={f":v{i}": v for i, v in enumerate(attrs.values())},
    )


def _slug(bank: str, account_no: str) -> str:
    """Stable, path-safe id for one account's output folder."""
    return re.sub(r"[^a-z0-9]+", "-", f"{bank}-{account_no}".lower()).strip("-") or "account"


_SHORT_BANK = {"icici bank": "ICICI", "axis bank": "AXIS", "hdfc bank": "HDFC",
               "state bank of india": "SBI", "kotak mahindra bank": "KOTAK"}


def _short_bank(bank: str) -> str:
    return _SHORT_BANK.get((bank or "").strip().lower(), (bank or "").strip())


def _collapse_periods(ranges):
    """Merge contiguous statement periods; keep genuine gaps separate.

    Two statements that abut (or overlap) read as one continuous period. A gap
    — the next period starting more than a day after the previous ended — stays
    listed separately, because pretending otherwise would hide a missing month.
    """
    clean = sorted((a, b) for a, b in ranges if a and b)
    if not clean:
        return []
    out = [list(clean[0])]
    for a, b in clean[1:]:
        try:
            prev_end = date.fromisoformat(out[-1][1])
            this_start = date.fromisoformat(a)
            contiguous = (this_start - prev_end).days <= 1
        except ValueError:
            contiguous = False
        if contiguous:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [[a, b] for a, b in out]


def _mask(acct: str) -> str:
    return ("X" * max(len(acct) - 4, 0)) + acct[-4:] if acct else acct


def _clear_password(job_id):
    """Drop PDF passwords once processing is over — top-level and per file."""
    try:
        TABLE.update_item(Key={"job_id": job_id}, UpdateExpression="REMOVE password")
    except Exception:
        pass
    try:
        item = TABLE.get_item(Key={"job_id": job_id}).get("Item") or {}
        files = item.get("files") or []
        if any(isinstance(f, dict) and "password" in f for f in files):
            for f in files:
                if isinstance(f, dict):
                    f.pop("password", None)
            TABLE.update_item(
                Key={"job_id": job_id},
                UpdateExpression="SET files = :f",
                ExpressionAttributeValues={":f": files},
            )
    except Exception:
        pass


def _job_id_from_key(key: str) -> str:
    """uploads/{job_id}/{n}.pdf (multi-file) or uploads/{job_id}.pdf (legacy)."""
    rest = key.split("/", 1)[1] if "/" in key else key
    return rest.split("/", 1)[0] if "/" in rest else rest.rsplit(".", 1)[0]


def _work_key(job_id: str, idx: int) -> str:
    return f"work/{job_id}/{idx}.json"


def _work_exists(job_id: str, idx: int) -> bool:
    try:
        s3.head_object(Bucket=BUCKET, Key=_work_key(job_id, idx))
        return True
    except Exception:
        return False


def _write_work(job_id: str, idx: int, meta, txns) -> None:
    s3.put_object(
        Bucket=BUCKET, Key=_work_key(job_id, idx), ContentType="application/json",
        Body=json.dumps({"meta": asdict(meta),
                         "txns": [asdict(t) for t in txns]}).encode(),
    )


def _read_work(job_id: str, idx: int):
    body = s3.get_object(Bucket=BUCKET, Key=_work_key(job_id, idx))["Body"].read()
    d = json.loads(body)
    return StatementMeta(**d["meta"]), [Txn(**t) for t in d["txns"]]


def _mark_extracted(job_id: str, idx: int) -> int:
    """Record that this file is extracted; return how many are done.

    A set, not a counter, so a re-delivered event cannot inflate the total.
    """
    attrs = TABLE.update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET updated_at = :t ADD extracted :k",
        ExpressionAttributeValues={":k": {str(idx)}, ":t": int(time.time())},
        ReturnValues="UPDATED_NEW",
    ).get("Attributes", {})
    return len(attrs.get("extracted", set()))


# Reconciliation issues are unbounded — a 5000-row statement whose chain breaks
# early can produce thousands — so what travels in the job summary is a digest
# and what travels in issues.json is everything.
ISSUE_SAMPLE = 5


def _issue_kinds(statement_issues) -> dict:
    """{kind: count} across every statement in this account."""
    kinds: dict[str, int] = {}
    for st in statement_issues:
        for i in st.get("issues", []):
            k = i.get("kind", "other")
            kinds[k] = kinds.get(k, 0) + 1
    return kinds


def _issue_sample(statement_issues) -> list:
    """The first few issues, earliest row first, enough to see the shape.

    Deliberately the FIRST ones and not a spread: a broken chain fails at the
    point it breaks, and every later mismatch is usually the same break echoing.
    """
    out = []
    for st in statement_issues:
        for i in st.get("issues", []):
            out.append({"source_file": st.get("source_file", ""),
                        "row_index": i.get("row_index", 0),
                        "kind": i.get("kind", ""),
                        "detail": str(i.get("detail", ""))[:300]})
            if len(out) >= ISSUE_SAMPLE:
                return out
    return out


def _friendly(msg: str) -> str:
    """Turn a provider error into something a user can act on.

    The raw text is a JSON blob naming a model and a request id; what the
    person needs to know is that this bank has no template yet and the AI
    account cannot pay for the fallback.
    """
    m = msg.lower()
    # Both of these are policy refusals, not failures: nothing was read and
    # nothing was transmitted. The pipeline has already said WHY in specific
    # terms — no layout, a scanned file, a parser this build lacks — and those
    # lead to different actions, so keep its wording rather than flattening all
    # three into "no layout for this bank".
    if "ai fallback is switched off" in m:
        return msg.split(" and the AI fallback")[0].strip()[:300]
    if "must not leave the account" in m:
        return ("this bank has no layout yet, and AI extraction is restricted "
                "to providers inside our own AWS account")
    if "credit balance is too low" in m:
        return ("no template for this bank yet, so it needs AI extraction — "
                "and the AI account is out of credit")
    if "invalid_payment_instrument" in m or "marketplace subscription" in m:
        return ("no template for this bank yet, and Bedrock cannot complete its "
                "AWS Marketplace subscription on this account")
    if "rate limit" in m or "429" in m:
        return "no template for this bank yet, and the AI provider is rate limiting"
    if "password" in m:
        return msg
    return msg[:300]


def _mark_failed(job_id: str, idx: int, filename: str, msg: str) -> None:
    """Record a per-file failure and still count the file as settled.

    One unreadable statement must not discard an upload: the others have
    already been extracted and paid for. The merge proceeds with what worked
    and the job reports which files did not.
    """
    try:
        TABLE.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET failed_files.#i = :v, updated_at = :t",
            ExpressionAttributeNames={"#i": str(idx)},
            ExpressionAttributeValues={
                ":v": {"filename": filename, "error": _friendly(msg)},
                ":t": int(time.time())},
            ConditionExpression="attribute_exists(failed_files)",
        )
    except Exception:
        TABLE.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET failed_files = :m, updated_at = :t",
            ExpressionAttributeValues={
                ":m": {str(idx): {"filename": filename, "error": _friendly(msg)}},
                ":t": int(time.time())},
        )


# A merge cannot outlive the Lambda that runs it, so a claim older than the
# function timeout belongs to an invocation that is definitely gone.
MERGE_CLAIM_TTL_S = 16 * 60

# A job in one of these states is finished. A late duplicate S3 delivery (S3
# promises at-least-once) must not re-run the merge on it: re-merging a
# reviewed job would recompute "needs_review" and silently discard the
# review a person already did.
TERMINAL_STATUSES = ("done", "needs_review", "reviewed", "failed")


def _claim_merge(job_id: str) -> bool:
    """Exactly one invocation performs the merge; the rest bow out.

    Several files can finish at nearly the same moment, and each would
    otherwise merge and publish the same job concurrently.

    The claim expires. Without that, an invocation hard-killed mid-merge left
    the job on "merging" forever and no later attempt — retry or sweeper —
    could ever take it, which is one of the two ways a job used to get stuck.

    A job already in a TERMINAL status is never re-claimed: the only event
    that can reach here on one is a duplicate or very late S3 delivery, and
    the answer to those is to do nothing. The sweeper's re-drive is unaffected
    — it resets a stale claim to "processing" before re-invoking, so a
    re-driven job arrives here in a live status.
    """
    now = int(time.time())
    cur = str((TABLE.get_item(Key={"job_id": job_id}).get("Item") or {})
              .get("status") or "")
    if cur in TERMINAL_STATUSES:
        return False
    try:
        TABLE.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET #s = :m, merging_at = :now, updated_at = :now",
            ConditionExpression=("attribute_not_exists(#s) OR #s <> :m "
                                 "OR attribute_not_exists(merging_at) "
                                 "OR merging_at < :stale"),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":m": "merging", ":now": now,
                                       ":stale": now - MERGE_CLAIM_TTL_S},
        )
        return True
    except ddb.meta.client.exceptions.ConditionalCheckFailedException:
        return False


def lambda_handler(event, _ctx):
    # Admin "categorisation playground" — a synchronous invoke from the API to
    # show how a single narration is read, without going near a real job. Reuses
    # the exact pipeline functions so the UI can never drift from production.
    if "try_categorize" in event:
        req = event["try_categorize"]
        from bsa.normalize import detect_mode, extract_counterparty
        desc = re.sub(r"\s+", " ", str(req.get("description", ""))).strip()
        try:
            amount = float(req.get("amount", 0) or 0)
        except (TypeError, ValueError):
            amount = 0.0
        mode = detect_mode(desc)
        t = Txn(date="2025-01-01", cheque_no="", description=desc, amount=amount,
                balance=0.0, mode=mode, counterparty=extract_counterparty(desc, mode))
        t.compute_uid("playground", 0)
        categorize([t])
        return {"description": desc, "amount": amount, "mode": t.mode,
                "party": t.counterparty or "unknown party",
                "category": t.category, "detail": category_detail(t)}

    for rec in event.get("Records", []):
        key = rec["s3"]["object"]["key"]
        job_id = _job_id_from_key(key)
        item = TABLE.get_item(Key={"job_id": job_id}).get("Item") or {}
        related = list(item.get("related_parties", []))
        files = item.get("files") or [{"idx": 0, "key": key,
                                       "filename": item.get("filename",
                                                            "statement.pdf"),
                                       "password": item.get("password")}]
        expected = int(item.get("expected", len(files)))

        # ---- phase 1: extract THIS file only -------------------------------
        # Each upload fires its own event, so each file gets its own Lambda and
        # its own 15 minutes. Extracting all N in one invocation is what pushed
        # a ten-file job past the ceiling, discarding every finished extraction
        # with it. Results are persisted, so a retry only redoes what is missing.
        entry = next((f for f in files if f.get("key") == key), files[0])
        idx = int(entry.get("idx", 0))
        # PID suffix: unique per process, so concurrent test runs (or any two
        # processes sharing this /tmp) cannot delete each other's scratch dirs.
        workdir = f"/tmp/{job_id}_{idx}_{os.getpid()}"
        os.makedirs(workdir, exist_ok=True)
        try:
            if not _work_exists(job_id, idx):
                _update(job_id, **{"status": "processing"})
                src_key = entry.get("key", key)
                # Size gate before the download. head_object is metadata-only;
                # ContentLength may be absent from a fake or a degraded
                # response, in which case the gate simply doesn't fire and the
                # download surfaces any real problem itself.
                try:
                    size = int(s3.head_object(Bucket=BUCKET, Key=src_key)
                               .get("ContentLength", 0) or 0)
                except Exception:                          # noqa: BLE001
                    size = 0
                if size > MAX_UPLOAD_BYTES:
                    raise ValueError(
                        f"this file is {size / (1024 * 1024):.0f} MB — "
                        f"statements over {MAX_UPLOAD_BYTES // (1024 * 1024)} "
                        "MB are not accepted")
                local_pdf = os.path.join(workdir, "in.pdf")
                s3.download_file(BUCKET, src_key, local_pdf)
                ex = extract_one(
                    local_pdf, password=entry.get("password"),
                    time_left_ms=getattr(_ctx, "get_remaining_time_in_millis", None),
                    filename=entry.get("filename"))
                _write_work(job_id, idx, ex.meta, normalize(ex))
        except PasswordRequired:
            _mark_failed(job_id, idx, entry.get("filename", "file"),
                         "password-protected — re-upload with the correct password")
        except Exception as e:                            # noqa: BLE001
            print(traceback.format_exc())
            _mark_failed(job_id, idx, entry.get("filename", "file"), str(e))
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        # ---- phase 2: last one in merges -----------------------------------
        if _mark_extracted(job_id, idx) < expected:
            continue                      # other statements still extracting
        if not _claim_merge(job_id):
            continue                      # another invocation got there first

        try:
            # Group the extracted statements by ACCOUNT. A bulk upload of ten
            # files across three accounts is three reports, not one: balances
            # only chain within an account, so merging across them would be
            # meaningless arithmetic.
            groups: dict[str, dict] = {}
            order: list[str] = []
            fresh = TABLE.get_item(Key={"job_id": job_id}).get("Item") or {}
            failed = fresh.get("failed_files") or {}
            usable = [f for f in sorted(files, key=lambda x: int(x.get("idx", 0)))
                      if str(f.get("idx", 0)) not in failed]
            if not usable:
                _update(job_id, **{
                    "status": "failed",
                    "error": "; ".join(f"{v.get('filename')}: {v.get('error')}"
                                       for v in failed.values())[:900]})
                continue
            # One record per uploaded file, in upload order, carrying the file
            # entry alongside what came out of it. Grouping used to discard that
            # pairing and the AI cost table was rebuilt by zipping the upload
            # order against the group order — which only agree when no two
            # accounts interleave, so a mixed upload reported one file's tokens
            # under another file's name.
            per_file = [(f, *_read_work(job_id, int(f.get("idx", 0))))
                        for f in usable]
            for f, meta, txns in per_file:
                key = f"{meta.bank}|{meta.account_no}"
                if key not in groups:
                    groups[key] = {"metas": [], "lists": [], "files": []}
                    order.append(key)
                groups[key]["metas"].append(meta)
                groups[key]["lists"].append(txns)
                groups[key]["files"].append(f.get("filename", ""))

            outroot = f"/tmp/{job_id}_merge_{os.getpid()}"
            accounts_out, worst = [], "passed"
            for key in order:
                g = groups[key]
                metas, lists = g["metas"], g["lists"]

                # Validation runs PER SOURCE STATEMENT. Reconciling across a
                # merged account would cross the gap between two non-contiguous
                # statements and report a failure that is not real; the account
                # inherits the worst individual statement's status instead.
                per_status, issues_total = [], 0
                # Keep the issue detail. It used to be dropped here with the
                # comment "per-statement detail is in the log", which meant the
                # only record of WHICH rows broke the chain was a CloudWatch
                # line — unreachable from the report and gone in 30 days. A
                # human reviewing a failed statement needs the rows.
                statement_issues = []
                for one, m in zip(lists, metas):
                    r = validate(one)
                    per_status.append(r.status)
                    issues_total += len(r.issues)
                    unreadable = list(getattr(m, "unreadable_pages", []) or [])
                    if r.issues or unreadable:
                        statement_issues.append({
                            "source_file": m.source_file,
                            "status": r.status,
                            "checked_rows": r.checked_rows,
                            # Pages with no text layer explain a balance break
                            # that otherwise has no visible cause: nothing was
                            # read from them, so their rows are simply absent.
                            "unreadable_pages": unreadable,
                            "issues": [asdict(i) for i in r.issues],
                        })
                acct_status = ("failed" if "failed" in per_status else
                               "passed_with_warnings"
                               if "passed_with_warnings" in per_status else "passed")
                if acct_status != "passed" and worst == "passed":
                    worst = acct_status
                if acct_status == "failed":
                    worst = "failed"

                txns = dedup_merge(lists) if len(lists) > 1 else list(lists[0])
                # validate() ran per statement just above and has had its look
                # at the brought-forward rows; from here on they are not
                # transactions and must appear in nothing.
                from bsa.normalize import drop_opening_rows
                txns = drop_opening_rows(txns)
                # Cross-statement identifier resolution: with every statement of
                # the account in one list, a party named in March fills the
                # bare-account-number rows of January.
                from bsa.normalize import (drop_useless_identifiers,
                                            name_instrument_parties,
                                            resolve_identifiers)
                resolve_identifiers(txns)
                # …and clear again afterwards. resolve_identifiers deliberately
                # KEEPS a bare account number as a join key, so the pair must
                # stay together: normalize runs both per statement, and the
                # merged pass here was running only the first half. A number is
                # never a party name — the reviewer's instruction, twice.
                drop_useless_identifiers(txns)
                # Re-assert the instrument names: resolve_identifiers above can
                # put a payee back onto a reversal from a sibling statement.
                name_instrument_parties(txns)
                holder_name = next((m.account_name for m in metas if m.account_name), "")
                categorize(txns, related_parties=related, account_name=holder_name)

                # Mask only after uid, dedup and validation have used the real
                # numbers — published outputs must never carry a full account no.
                masked = _mask(metas[0].account_no)
                for t in txns:
                    t.account_no = masked
                meta0 = metas[0]
                meta0.account_no = masked
                holder = next((m.account_name for m in metas if m.account_name), "")
                meta0.account_name = holder
                meta0.source_file = ", ".join(g["files"])

                report = ValidationReport(
                    status=acct_status, checked_rows=len(txns), issues=[])
                slug = _slug(meta0.bank, masked)
                outdir = os.path.join(outroot, slug)
                paths = publish(JobResult(meta=meta0, txns=txns, validation=report),
                                outdir, basename="statement")
                preview = [{
                    # dd-mm-yyyy in the UI too, per the review spec (ID3).
                    "date": "-".join(reversed(t.date.split("-"))),
                    "description": t.description,
                    "amount": t.amount, "balance": t.balance,
                    "category": t.category, "detail": category_detail(t),
                    "confidence": t.confidence,
                } for t in txns[:150]]
                with open(os.path.join(outdir, "preview.json"), "w") as fh:
                    json.dump({"rows": preview, "total": len(txns)}, fh)
                paths["preview"] = os.path.join(outdir, "preview.json")
                # Full reconciliation detail for the review screen. Written
                # for every account, empty list included, so the UI can fetch
                # it unconditionally rather than probing for a 404.
                with open(os.path.join(outdir, "issues.json"), "w") as fh:
                    json.dump({"account": meta0.account_no, "bank": meta0.bank,
                               "status": acct_status, "total": issues_total,
                               "statements": statement_issues}, fh)
                paths["issues"] = os.path.join(outdir, "issues.json")
                for pth in paths.values():
                    s3.upload_file(pth, BUCKET,
                                   f"outputs/{job_id}/{slug}/{os.path.basename(pth)}")

                from bsa.normalize import party_kind
                from bsa.sme_taxonomy import sme_subcategory
                # `categories` is now the SME classification (the master's
                # "Category (SME)" tab, column B) — that is the category the
                # customer reads. The eighteen ABCL tags are kept as `tags`:
                # they still route rows to the template's sheets and feed the
                # bounce denominators, but "118 Regular debits" is not a
                # category anyone can act on.
                cats: dict[str, int] = {}
                cat_amounts: dict[str, float] = {}
                tags: dict[str, int] = {}
                conf = {"high": 0, "medium": 0, "low": 0}
                # Party quality is a different axis from confidence: how many rows
                # name a real counterparty vs only a machine handle (account
                # number / VPA) vs none. Raw party-fill hides this by counting a
                # handle as "party present"; this splits it out honestly.
                pq = {"named": 0, "handle": 0, "none": 0}
                for t in txns:
                    tags[t.category] = tags.get(t.category, 0) + 1
                    cat = sme_subcategory(t) or t.category
                    cats[cat] = cats.get(cat, 0) + 1
                    cat_amounts[cat] = round(
                        cat_amounts.get(cat, 0.0) + abs(t.amount), 2)
                    conf[t.confidence] = conf.get(t.confidence, 0) + 1
                    kind = party_kind(t.counterparty, t.description)
                    if kind != "na":
                        pq[kind] += 1
                integ = account_integrity(metas, acct_status)
                # Completeness: the statement's own declared Dr/Cr counts vs what
                # we extracted — proof no rows were silently dropped.
                decl: dict = {}
                for m in metas:
                    for k, v in (getattr(m, "declared_totals", None) or {}).items():
                        decl[k] = decl.get(k, 0) + v
                nd = sum(1 for t in txns if t.amount < 0)
                nc = sum(1 for t in txns if t.amount > 0)
                sd = sum(-t.amount for t in txns if t.amount < 0)
                sc = sum(t.amount for t in txns if t.amount > 0)
                completeness = check_completeness(len(txns), nd, nc, decl, sd, sc)
                cs = credit_summary(txns, integ, acct_status)
                if completeness.get("checked") and not completeness.get("complete"):
                    cs["reads"].insert(0, "Extraction incomplete: " +
                                       "; ".join(completeness["notes"]))
                accounts_out.append({
                    "slug": slug,
                    "bank": meta0.bank,
                    "account_no": masked,
                    "holder": holder,
                    "title": " - ".join(x for x in (holder, _short_bank(meta0.bank),
                                                    masked) if x),
                    "rows": len(txns),
                    "pages": sum(int(getattr(m, "n_pages", 0) or 0) for m in metas),
                    "files": g["files"],
                    "statements": len(metas),
                    # Periods come from the transactions actually present, not
                    # from the header the bank declared: one ICICI export names
                    # only its first month while carrying a full year, and the
                    # header would under-report the coverage.
                    "periods": _collapse_periods([
                        (min(t.date for t in one), max(t.date for t in one))
                        for one in lists if one]),
                    "validation": acct_status,
                    "issues": issues_total,
                    # A digest only. The full list lives in issues.json: a job
                    # of twenty statements with a broken chain each would push
                    # the DynamoDB item past its 400KB limit and lose the whole
                    # summary, report included.
                    "issue_kinds": _issue_kinds(statement_issues),
                    "issue_sample": _issue_sample(statement_issues),
                    "unreadable_pages": sum(
                        len(getattr(m, "unreadable_pages", []) or []) for m in metas),
                    # The SME classification, by count and by value.
                    "categories": cats,
                    "category_amounts": cat_amounts,
                    # The ABCL tags, kept for routing and reconciliation.
                    "tags": tags,
                    # Categorisation confidence: how many rows are a certain tag
                    # vs a known-party regular transfer vs genuinely unsure. The
                    # "low" count is what a reviewer eyeballs — the report never
                    # presents those as certain.
                    "confidence": conf,
                    # Party quality: named counterparty vs machine handle
                    # (account/VPA) vs none — the honest read that party-fill
                    # hides by counting a handle as a party.
                    "party_quality": pq,
                    # Statement-integrity signals for lending: balance chain +
                    # scanned-page + PDF-metadata flags. A prompt to look, not
                    # an accusation.
                    "integrity": integ,
                    # The lender-facing conclusion: turnover, balance, cash
                    # intensity, bounces, EMI headroom, concentration + reads.
                    "credit_summary": cs,
                    "completeness": completeness,
                })

            # AI accounting stays per uploaded file across the whole job, and
            # reads straight off per_file so a filename can never be paired
            # with a different statement's usage.
            ordered = usable
            all_metas = [m for _f, m, _t in per_file]
            parts, tin, tout, calls, cost, cost_known = [], 0, 0, 0, 0.0, True
            for f, m, _txns in per_file:
                u = getattr(m, "llm_usage", None) or {}
                parts.append({
                    "filename": f.get("filename", ""),
                    "layout": m.layout,
                    "ai": bool(u),
                    "provider": u.get("provider", ""),
                    "model": u.get("model", ""),
                    "tokens_in": int(u.get("tokens_in", 0) or 0),
                    "tokens_out": int(u.get("tokens_out", 0) or 0),
                    "cost_usd": ("" if u.get("cost_usd") is None
                                 else f"{u['cost_usd']:.6f}"),
                })
                if u:
                    tin += int(u.get("tokens_in", 0) or 0)
                    tout += int(u.get("tokens_out", 0) or 0)
                    calls += int(u.get("calls", 0) or 0)
                    if u.get("cost_usd") is None:
                        cost_known = False
                    else:
                        cost += float(u["cost_usd"])
            ai_block = {
                "used": any(p["ai"] for p in parts),
                "ai_files": sum(1 for p in parts if p["ai"]),
                "tokens_in": tin, "tokens_out": tout, "calls": calls,
                "cost_usd": (f"{cost:.6f}" if cost_known and calls else ""),
                "files": parts,
            }
            # idx travels with the failure so the UI can target THAT file —
            # it is what "unlock and retry" posts back to re-drive one key.
            failed_list = [{"idx": str(k),
                            "filename": v.get("filename", ""),
                            "error": str(v.get("error", ""))[:300]}
                           for k, v in failed.items()]
            _update(job_id, **{
                "status": ("needs_review" if (failed_list or worst != "passed")
                           else "done"),
                # When the report on screen was actually produced. Stamped
                # HERE, at publish, not from updated_at — that moves whenever
                # any status is written (a review mark, for one), so it would
                # claim a report was regenerated when nothing was re-run. With
                # regenerate in the product, a reader has to be able to tell
                # which run they are looking at.
                "generated_at": int(time.time()),
                # clear any error left over from a previous failed attempt,
                # otherwise a job reads "done" while still showing an old error
                "error": "",
                "summary": {
                    "files": len(ordered),
                    "filenames": [f.get("filename", "") for f in ordered],
                    "failed_files": failed_list,
                    "pages": sum(int(getattr(m, "n_pages", 0) or 0) for m in all_metas),
                    "rows": sum(a["rows"] for a in accounts_out),
                    "validation": worst,
                    "issues": sum(a["issues"] for a in accounts_out),
                    "accounts": accounts_out,
                    "ai": ai_block,
                },
            })
        except Exception as e:                            # noqa: BLE001
            print(traceback.format_exc())
            _update(job_id, **{"status": "failed",
                               "error": f"merge: {str(e)[:380]}"})
        finally:
            _clear_password(job_id)
            shutil.rmtree(f"/tmp/{job_id}_merge_{os.getpid()}", ignore_errors=True)
    return {"ok": True}
