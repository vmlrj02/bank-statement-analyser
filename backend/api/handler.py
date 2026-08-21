"""API Lambda — job lifecycle. boto3 only, no other deps.

Routes (HTTP API v2 routeKey):
  POST /jobs                : {filename, password?, related_parties?[]}
                              -> {job_id, upload_url}
  GET  /jobs                : recent jobs (newest first)
  GET  /jobs/{id}           : job record incl. summary
  GET  /jobs/{id}/download  : ?format=csv|xlsx|json|preview -> {url}
"""
import json
import os
import time
import uuid

import boto3

s3 = boto3.client("s3")
ddb = boto3.resource("dynamodb")
TABLE = ddb.Table(os.environ["JOBS_TABLE"])
BUCKET = os.environ["DATA_BUCKET"]

FORMAT_KEYS = {
    "csv": ("statement_transactions.csv", "text/csv"),
    "xlsx": ("statement_analysis.xlsx",
             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "json": ("statement.json", "application/json"),
    "preview": ("preview.json", "application/json"),
}


def _resp(code, body):
    return {"statusCode": code,
            "headers": {"content-type": "application/json",
                        "access-control-allow-origin": "*"},
            "body": json.dumps(body, default=str)}


def lambda_handler(event, _ctx):
    route = event.get("routeKey", "")
    path_id = (event.get("pathParameters") or {}).get("id")
    qs = event.get("queryStringParameters") or {}

    if route == "POST /jobs":
        body = json.loads(event.get("body") or "{}")
        filename = (body.get("filename") or "statement.pdf")[:200]
        job_id = uuid.uuid4().hex[:12]
        key = f"uploads/{job_id}.pdf"
        item = {
            "job_id": job_id,
            "status": "awaiting_upload",
            "filename": filename,
            "created_at": int(time.time()),
            "ttl": int(time.time()) + 180 * 24 * 3600,
        }
        if body.get("password"):
            item["password"] = body["password"]        # deleted after processing
        if body.get("related_parties"):
            item["related_parties"] = [str(p)[:80] for p in body["related_parties"]][:20]
        TABLE.put_item(Item=item)
        url = s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": BUCKET, "Key": key, "ContentType": "application/pdf"},
            ExpiresIn=900,
        )
        return _resp(200, {"job_id": job_id, "upload_url": url})

    if route == "GET /jobs":
        # MVP single-tenant: scan and sort client-side (bounded)
        items = TABLE.scan(Limit=200).get("Items", [])
        for it in items:
            it.pop("password", None)
        items.sort(key=lambda x: x.get("created_at", 0), reverse=True)
        return _resp(200, {"jobs": items[:60]})

    if route == "GET /jobs/{id}" and path_id:
        it = TABLE.get_item(Key={"job_id": path_id}).get("Item")
        if not it:
            return _resp(404, {"error": "not found"})
        it.pop("password", None)
        return _resp(200, it)

    if route == "GET /jobs/{id}/download" and path_id:
        fmt = qs.get("format", "csv")
        if fmt not in FORMAT_KEYS:
            return _resp(400, {"error": f"format must be one of {list(FORMAT_KEYS)}"})
        fname, ctype = FORMAT_KEYS[fmt]
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": BUCKET, "Key": f"outputs/{path_id}/{fname}",
                    "ResponseContentType": ctype,
                    "ResponseContentDisposition":
                        f'attachment; filename="{path_id}_{fname}"'
                        if fmt != "preview" else "inline"},
            ExpiresIn=300,
        )
        return _resp(200, {"url": url})

    return _resp(404, {"error": f"no route {route}"})
