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
from bsa.normalize import normalize
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
    try:
        TABLE.update_item(Key={"job_id": job_id}, UpdateExpression="REMOVE password")
    except Exception:
        pass


def lambda_handler(event, _ctx):
    for rec in event.get("Records", []):
        key = rec["s3"]["object"]["key"]                 # uploads/{job_id}.pdf
        job_id = os.path.basename(key).rsplit(".", 1)[0]
        item = TABLE.get_item(Key={"job_id": job_id}).get("Item") or {}
        password = item.get("password")
        related = list(item.get("related_parties", []))

        workdir = f"/tmp/{job_id}"
        os.makedirs(workdir, exist_ok=True)
        local_pdf = os.path.join(workdir, "in.pdf")
        try:
            _update(job_id, **{"status": "processing"})
            s3.download_file(BUCKET, key, local_pdf)

            extract = extract_one(local_pdf, password=password)
            txns = normalize(extract)
            categorize(txns, related_parties=related)
            report = validate(txns)
            extract.meta.account_no = _mask(extract.meta.account_no)
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
            status = "done" if report.status == "passed" else "needs_review"
            _update(job_id, **{
                "status": status,
                "summary": {
                    "rows": len(txns),
                    "validation": report.status,
                    "issues": len(report.issues),
                    "account_no": extract.meta.account_no,
                    "account_name": extract.meta.account_name,
                    "bank": extract.meta.bank,
                    "period_from": extract.meta.period_from,
                    "period_to": extract.meta.period_to,
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
