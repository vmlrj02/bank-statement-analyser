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
import shutil
import traceback
from dataclasses import asdict
from types import SimpleNamespace

import boto3

from bsa.categorize import categorize, category_detail
from bsa.ingest import PasswordRequired
from bsa.models import JobResult, StatementMeta, Txn
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
        UpdateExpression="ADD extracted :k",
        ExpressionAttributeValues={":k": {str(idx)}},
        ReturnValues="UPDATED_NEW",
    ).get("Attributes", {})
    return len(attrs.get("extracted", set()))


def _claim_merge(job_id: str) -> bool:
    """Exactly one invocation performs the merge; the rest bow out.

    Several files can finish at nearly the same moment, and each would
    otherwise merge and publish the same job concurrently.
    """
    try:
        TABLE.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET #s = :m",
            ConditionExpression="attribute_not_exists(#s) OR #s <> :m",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":m": "merging"},
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

        # ---- phase 1: extract THIS file only -------------------------------
        # Each upload fires its own event, so each file gets its own Lambda and
        # its own 15 minutes. Extracting all N in one invocation is what pushed
        # a ten-file job past the ceiling, discarding every finished extraction
        # with it. Results are persisted, so a retry only redoes what is missing.
        entry = next((f for f in files if f.get("key") == key), files[0])
        idx = int(entry.get("idx", 0))
        workdir = f"/tmp/{job_id}_{idx}"
        os.makedirs(workdir, exist_ok=True)
        try:
            if not _work_exists(job_id, idx):
                _update(job_id, **{"status": "processing"})
                local_pdf = os.path.join(workdir, "in.pdf")
                s3.download_file(BUCKET, entry.get("key", key), local_pdf)
                ex = extract_one(
                    local_pdf, password=entry.get("password"),
                    time_left_ms=getattr(_ctx, "get_remaining_time_in_millis", None))
                _write_work(job_id, idx, ex.meta, normalize(ex))
        except PasswordRequired:
            _update(job_id, **{"status": "password_required",
                               "error": f"{entry.get('filename', 'A file')} is "
                                        "password-protected — recreate the job "
                                        "with the correct password."})
            continue
        except Exception as e:                            # noqa: BLE001
            print(traceback.format_exc())
            _update(job_id, **{"status": "failed",
                               "error": f"{entry.get('filename', 'file')}: "
                                        f"{str(e)[:350]}"})
            continue
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        # ---- phase 2: last one in merges -----------------------------------
        if _mark_extracted(job_id, idx) < expected:
            continue                      # other statements still extracting
        if not _claim_merge(job_id):
            continue                      # another invocation got there first

        try:
            extracts, txn_lists = [], []
            for f in sorted(files, key=lambda x: int(x.get("idx", 0))):
                meta, txns = _read_work(job_id, int(f.get("idx", 0)))
                extracts.append(SimpleNamespace(meta=meta))
                txn_lists.append(txns)

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

            # phase 1's workdir is already gone; the merge needs its own
            outdir = f"/tmp/{job_id}_merge"
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

            # Per-statement AI accounting. A template-parsed file has no usage
            # and costs nothing; that contrast is the point of the admin view.
            # DynamoDB rejects floats, so money is stored as a string.
            ordered = sorted(files, key=lambda x: int(x.get("idx", 0)))
            parts, tin, tout, calls, cost, cost_known = [], 0, 0, 0, 0.0, True
            for f, e in zip(ordered, extracts):
                u = getattr(e.meta, "llm_usage", None) or {}
                parts.append({
                    "filename": f.get("filename", ""),
                    "layout": e.meta.layout,
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
                    "ai": ai_block,
                },
            })
        except Exception as e:                            # noqa: BLE001
            print(traceback.format_exc())
            _update(job_id, **{"status": "failed",
                               "error": f"merge: {str(e)[:380]}"})
        finally:
            _clear_password(job_id)
            shutil.rmtree(f"/tmp/{job_id}_merge", ignore_errors=True)
    return {"ok": True}
