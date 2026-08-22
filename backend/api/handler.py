"""API Lambda — job lifecycle. boto3 only, no other deps.

Routes (HTTP API v2 routeKey):
  POST /auth/login          : {email, password} -> {token, email, role}
  GET  /auth/me             : who the bearer token belongs to
  POST /jobs                : {filename, password?, related_parties?[]}
                              -> {job_id, upload_url}
  GET  /jobs                : recent jobs (newest first; own only unless admin)
  GET  /jobs/{id}           : job record incl. summary
  GET  /jobs/{id}/download  : ?format=csv|xlsx|json|preview&account=<slug> -> {url}
"""
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid

import boto3

from botocore.config import Config

REGION = os.environ.get("AWS_REGION", "ap-south-1")
s3 = boto3.client(
    "s3",
    region_name=REGION,
    endpoint_url=f"https://s3.{REGION}.amazonaws.com",
    config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
)

ddb = boto3.resource("dynamodb")
TABLE = ddb.Table(os.environ["JOBS_TABLE"])
AUTH = ddb.Table(os.environ["AUTH_TABLE"])
BUCKET = os.environ["DATA_BUCKET"]

SESSION_TTL = 12 * 3600
PBKDF2_ROUNDS = 210_000


def hash_password(password: str, salt: str) -> str:
    """PBKDF2-HMAC-SHA256. Salt is per user, so identical passwords across
    accounts do not produce identical hashes."""
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt),
                               PBKDF2_ROUNDS).hex()


def _login(body):
    email = str(body.get("email") or body.get("username") or "").strip().lower()
    password = str(body.get("password") or "")
    if not email or not password:
        return _resp(400, {"error": "email and password required"})
    user = AUTH.get_item(Key={"pk": f"USER#{email}"}).get("Item")
    # Always compare, even when the user is absent, so a wrong email and a wrong
    # password take the same time and cannot be told apart.
    salt = (user or {}).get("salt", "00" * 16)
    expected = (user or {}).get("hash", "")
    got = hash_password(password, salt)
    if not user or not hmac.compare_digest(got, expected):
        return _resp(401, {"error": "invalid email or password"})

    token = secrets.token_urlsafe(32)
    now = int(time.time())
    AUTH.put_item(Item={
        "pk": f"SESSION#{token}", "email": email,
        "role": user.get("role", "customer"),
        "created_at": now, "ttl": now + SESSION_TTL,
    })
    return _resp(200, {"token": token, "email": email,
                       "role": user.get("role", "customer"),
                       "expires_in": SESSION_TTL})


def _session(event):
    """(email, role) for a valid bearer token, else (None, None)."""
    hdrs = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    auth = hdrs.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None, None
    token = auth[7:].strip()
    if not token:
        return None, None
    it = AUTH.get_item(Key={"pk": f"SESSION#{token}"}).get("Item")
    # DynamoDB TTL deletes lazily, so an expired row can still be returned —
    # check the timestamp rather than trusting the row's presence.
    if not it or int(it.get("ttl", 0)) <= int(time.time()):
        return None, None
    return it.get("email"), it.get("role", "customer")

MAX_FILES_PER_JOB = 20

FORMAT_KEYS = {
    "csv": ("statement_transactions.csv", "text/csv"),
    "xlsx": ("statement_analysis.xlsx",
             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "json": ("statement.json", "application/json"),
    "preview": ("preview.json", "application/json"),
}


def _scrub(item, is_admin=False):
    """Strip anything the caller must not see.

    Passwords always go. AI usage and cost are admin-only: a customer sees the
    report, not what it cost us to produce.
    """
    item.pop("password", None)
    for f in item.get("files") or []:
        if isinstance(f, dict):
            f.pop("password", None)
    if not is_admin:
        item.pop("owner", None)
        summary = item.get("summary")
        if isinstance(summary, dict):
            summary.pop("ai", None)
    return item


def _resp(code, body):
    return {"statusCode": code,
            "headers": {"content-type": "application/json",
                        "access-control-allow-origin": "*"},
            "body": json.dumps(body, default=str)}


def lambda_handler(event, _ctx):
    route = event.get("routeKey", "")
    path_id = (event.get("pathParameters") or {}).get("id")
    qs = event.get("queryStringParameters") or {}
    if route == "POST /auth/login":
        return _login(json.loads(event.get("body") or "{}"))

    # Every other route — all of /jobs* — requires a valid session token.
    user_sub, role = _session(event)
    if not user_sub:
        return _resp(401, {"error": "not authenticated"})
    is_admin = (role == "admin")

    if route == "GET /auth/me":
        return _resp(200, {"email": user_sub, "role": role})

    def _owned(it):
        """Admins see everything; everyone else only their own uploads."""
        return is_admin or it.get("owner") == user_sub

    if route == "POST /jobs":
        body = json.loads(event.get("body") or "{}")
        # One job spans N statements and produces ONE merged output. Accepts
        # {files:[{filename, password?}]}; the single-file {filename, password}
        # shape is still honoured so older clients keep working.
        files = body.get("files")
        if not isinstance(files, list) or not files:
            files = [{"filename": body.get("filename") or "statement.pdf",
                      "password": body.get("password")}]
        files = files[:MAX_FILES_PER_JOB]

        job_id = uuid.uuid4().hex[:12]
        now = int(time.time())
        entries, uploads = [], []
        for i, f in enumerate(files):
            name = str(f.get("filename") or f"statement_{i+1}.pdf")[:200]
            key = f"uploads/{job_id}/{i}.pdf"
            entry = {"idx": i, "key": key, "filename": name}
            if f.get("password"):
                entry["password"] = str(f["password"])   # cleared after processing
            entries.append(entry)
            uploads.append({
                "filename": name,
                "upload_url": s3.generate_presigned_url(
                    "put_object",
                    Params={"Bucket": BUCKET, "Key": key,
                            "ContentType": "application/pdf"},
                    ExpiresIn=900),
            })

        label = entries[0]["filename"] if len(entries) == 1 else \
            f"{len(entries)} statements — {entries[0]['filename']}"
        item = {
            "job_id": job_id,
            "status": "awaiting_upload",
            "filename": label,
            "files": entries,
            "expected": len(entries),
            "uploaded": 0,
            "created_at": now,
            "ttl": now + 180 * 24 * 3600,
            "owner": user_sub,
        }
        if body.get("related_parties"):
            item["related_parties"] = [str(p)[:80] for p in body["related_parties"]][:20]
        TABLE.put_item(Item=item)
        # upload_url kept at top level so a single-file client needs no change
        return _resp(200, {"job_id": job_id, "uploads": uploads,
                           "upload_url": uploads[0]["upload_url"]})

    if route == "GET /jobs":
        # MVP single-tenant: scan and sort client-side (bounded)
        items = TABLE.scan(Limit=200).get("Items", [])
        items = [it for it in items if _owned(it)]
        for it in items:
            _scrub(it, is_admin)
        items.sort(key=lambda x: x.get("created_at", 0), reverse=True)
        return _resp(200, {"jobs": items[:60]})

    if route == "GET /jobs/{id}" and path_id:
        it = TABLE.get_item(Key={"job_id": path_id}).get("Item")
        if not it or not _owned(it):
            return _resp(404, {"error": "not found"})
        return _resp(200, _scrub(it, is_admin))

    if route == "GET /jobs/{id}/download" and path_id:
        owner_item = TABLE.get_item(Key={"job_id": path_id}).get("Item")
        if not owner_item or not _owned(owner_item):
            return _resp(404, {"error": "not found"})
        fmt = qs.get("format", "csv")
        if fmt not in FORMAT_KEYS:
            return _resp(400, {"error": f"format must be one of {list(FORMAT_KEYS)}"})
        fname, ctype = FORMAT_KEYS[fmt]
        # Outputs are per account: outputs/{job}/{account-slug}/{file}. Jobs
        # published before that split have no slug, so an absent ?account
        # falls back to the flat path rather than 404-ing old reports.
        acct = (qs.get("account") or "").strip()
        acct = "".join(c for c in acct if c.isalnum() or c == "-")[:80]
        key = f"outputs/{path_id}/{acct}/{fname}" if acct else f"outputs/{path_id}/{fname}"
        prefix = f"{acct}_" if acct else ""
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": BUCKET, "Key": key,
                    "ResponseContentType": ctype,
                    "ResponseContentDisposition":
                        f'attachment; filename="{prefix}{fname}"'
                        if fmt != "preview" else "inline"},
            ExpiresIn=300,
        )
        return _resp(200, {"url": url})

    return _resp(404, {"error": f"no route {route}"})
