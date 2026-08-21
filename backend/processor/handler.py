"""Processor Lambda — runs the bsa pipeline on each uploaded PDF.

Trigger: S3 ObjectCreated on uploads/{job_id}.pdf
Writes : outputs/{job_id}/statement_transactions.csv, statement_analysis.xlsx,
         statement.json, preview.json ; job status + summary in DynamoDB.
"""
import json
import os
import shutil
import traceback

import boto3

from bsa.categorize import categorize, category_detail
from bsa.ingest import PasswordRequired
from bsa.models import JobResult
from bsa.normalize import normalize, dedup_merge
from bsa.pipeline import extract_one
from bsa.publish import publish
from bsa.validate import validate

s3 = boto3.client("s3")
ddb = boto3.resource("dynamodb")
TABLE = ddb.Table(os.environ["JOBS_TABLE"])
BUCKET = os.environ["DATA_BUCKET"]


def _update(job_id, **attrs):
    expr = ", ".join(f"#k{i} = :v{i}" for i in range(len(attrs)))
    TABLE.update_item(
        Key={"job_id": job_id},
        UpdateExpression=f"SET {expr}",
        ExpressionAttributeNames={f"#k{i}": k for i, k in enumerate(attrs)},
        ExpressionAttributeValues={f":v{i}": v for i, v in enumerate(attrs.values())},
    )


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


def _all_uploaded(job_id: str, key: str, expected: int) -> bool:
    """Record this key and report whether the whole set has now arrived.

    Tracked as a set rather than a counter so re-uploading the same file (or
    re-triggering a job) cannot inflate the count.
    """
    attrs = TABLE.update_item(
        Key={"job_id": job_id},
        UpdateExpression="ADD uploaded_keys :k",
        ExpressionAttributeValues={":k": {key}},
        ReturnValues="UPDATED_NEW",
    ).get("Attributes", {})
    return len(attrs.get("uploaded_keys", set())) >= expected


def _claim(job_id: str) -> bool:
    """Exactly one invocation may run the merge; the rest bow out.

    Every file's upload fires its own event, so without this the last few
    would each start processing the same job.
    """
    try:
        TABLE.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET #s = :p",
            ConditionExpression="attribute_not_exists(#s) OR #s <> :p",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":p": "processing"},
        )
        return True
    except ddb.meta.client.exceptions.ConditionalCheckFailedException:
        return False


def lambda_handler(event, _ctx):
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

        if not _all_uploaded(job_id, key, expected):
            continue                      # still waiting on the rest of the set
        if not _claim(job_id):
            continue                      # another invocation is already on it

        workdir = f"/tmp/{job_id}"
        os.makedirs(workdir, exist_ok=True)
        try:
            # Extract every statement, then merge into one result. dedup_merge
            # drops rows that overlap where statement periods abut.
            extracts, txn_lists = [], []
            for f in sorted(files, key=lambda x: int(x.get("idx", 0))):
                local_pdf = os.path.join(workdir, f"in_{f.get('idx', 0)}.pdf")
                s3.download_file(BUCKET, f.get("key", key), local_pdf)
                ex = extract_one(
                    local_pdf, password=f.get("password"),
                    time_left_ms=getattr(_ctx, "get_remaining_time_in_millis", None))
                extracts.append(ex)
                txn_lists.append(normalize(ex))

            extract = extracts[0]
            txns = dedup_merge(txn_lists) if len(txn_lists) > 1 else txn_lists[0]
            categorize(txns, related_parties=related)
            report = validate(txns)
            # Mask only after uid, dedup and validation have used the real
            # numbers — published outputs must never carry a full account no.
            for t in txns:
                t.account_no = _mask(t.account_no)
            for e in extracts:
                e.meta.account_no = _mask(e.meta.account_no)
            extract.meta.source_file = item.get("filename", "statement.pdf")
            result = JobResult(meta=extract.meta, txns=txns, validation=report)

            outdir = os.path.join(workdir, "out")
            paths = publish(result, outdir, basename="statement")

            preview = [{
                "date": t.date, "description": t.description,
                "amount": t.amount, "balance": t.balance,
                "category": t.category, "detail": category_detail(t),
            } for t in txns[:150]]
            with open(os.path.join(outdir, "preview.json"), "w") as f:
                json.dump({"rows": preview, "total": len(txns)}, f)
            paths["preview"] = os.path.join(outdir, "preview.json")

            for p in paths.values():
                s3.upload_file(p, BUCKET, f"outputs/{job_id}/{os.path.basename(p)}")

            cats = {}
            for t in txns:
                cats[t.category] = cats.get(t.category, 0) + 1
            # a bulk upload may span several banks/accounts and several years
            accounts = sorted({t.account_no for t in txns if t.account_no})
            banks = sorted({t.bank for t in txns if t.bank})
            status = "done" if report.status == "passed" else "needs_review"
            _update(job_id, **{
                "status": status,
                # clear any error left over from a previous failed attempt,
                # otherwise a job reads "done" while still showing an old error
                "error": "",
                "summary": {
                    "rows": len(txns),
                    "validation": report.status,
                    "issues": len(report.issues),
                    "account_no": (accounts[0] if len(accounts) == 1
                                   else f"{len(accounts)} accounts"),
                    "account_name": extract.meta.account_name,
                    "bank": ", ".join(banks) if banks else extract.meta.bank,
                    "accounts": accounts,
                    # merged jobs span every statement's period, not just the first
                    "period_from": min((e.meta.period_from for e in extracts
                                        if e.meta.period_from), default=""),
                    "period_to": max((e.meta.period_to for e in extracts
                                      if e.meta.period_to), default=""),
                    "files": len(extracts),
                    "categories": cats,
                },
            })
        except PasswordRequired:
            _update(job_id, **{"status": "password_required",
                               "error": "PDF is password-protected — recreate the "
                                        "job with the correct password."})
        except NotImplementedError:
            _update(job_id, **{"status": "failed",
                               "error": "This bank layout isn't in the template "
                                        "registry yet, and the LLM fallback "
                                        "(Bedrock) is not wired up in this MVP "
                                        "deployment."})
        except Exception as e:                            # noqa: BLE001
            print(traceback.format_exc())
            _update(job_id, **{"status": "failed", "error": str(e)[:400]})
        finally:
            _clear_password(job_id)
            shutil.rmtree(workdir, ignore_errors=True)
    return {"ok": True}
