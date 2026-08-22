"""API Lambda — job lifecycle. boto3 only, no other deps.

Routes (HTTP API v2 routeKey):
  POST /auth/login          : {email, password} -> {token, email, role}
  POST /auth/logout         : end this session
  POST /auth/password       : {current_password, new_password}
  GET  /auth/me             : who the bearer token belongs to
  POST /jobs                : {filename, password?, related_parties?[]}
                              -> {job_id, upload_url}
  GET  /jobs                : recent jobs (newest first; own only unless admin)
  GET  /jobs/{id}           : job record incl. summary
  GET  /jobs/{id}/download  : ?format=csv|xlsx|json|preview|issues&account=<slug>
  POST /jobs/{id}/review    : {note?} -> mark a needs_review job reviewed
"""
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from decimal import Decimal

import boto3

from boto3.dynamodb.conditions import Key
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
OWNER_INDEX = os.environ.get("OWNER_INDEX", "owner-created_at-index")

SESSION_TTL = 12 * 3600
PBKDF2_ROUNDS = 210_000

# Login throttling. Passwords are the only credential here and the API is
# public, so an unthrottled /auth/login is an offline-speed online guessing
# oracle. Counted per email in the same table, expiring on its own TTL.
MAX_FAILED_LOGINS = 8
LOCKOUT_S = 15 * 60
FAIL_WINDOW_S = 15 * 60
MIN_PASSWORD_LEN = 10


def _fail_key(email: str) -> str:
    return f"LOGIN#{email}"


def _lockout_remaining(email: str) -> int:
    """Seconds this email must wait, or 0. Counts a wrong email too, so a
    locked account and an unknown one stay indistinguishable."""
    it = AUTH.get_item(Key={"pk": _fail_key(email)}).get("Item")
    if not it:
        return 0
    now = int(time.time())
    if int(it.get("locked_until", 0)) > now:
        return int(it["locked_until"]) - now
    return 0


def _record_failure(email: str) -> None:
    now = int(time.time())
    it = AUTH.get_item(Key={"pk": _fail_key(email)}).get("Item") or {}
    # A slow trickle of wrong guesses should not accumulate into a lockout
    # months later, so the count restarts once the window has passed.
    fails = int(it.get("fails", 0)) + 1 if int(it.get("first_at", 0)) > now - FAIL_WINDOW_S else 1
    item = {"pk": _fail_key(email), "fails": fails,
            "first_at": int(it.get("first_at", now)) if fails > 1 else now,
            "ttl": now + LOCKOUT_S + FAIL_WINDOW_S}
    if fails >= MAX_FAILED_LOGINS:
        item["locked_until"] = now + LOCKOUT_S
    AUTH.put_item(Item=item)


def _clear_failures(email: str) -> None:
    try:
        AUTH.delete_item(Key={"pk": _fail_key(email)})
    except Exception:                                       # noqa: BLE001
        pass


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
    if wait := _lockout_remaining(email):
        return _resp(429, {"error": f"too many failed sign-ins — try again in "
                                    f"{max(wait // 60, 1)} minute(s)"})
    user = AUTH.get_item(Key={"pk": f"USER#{email}"}).get("Item")
    # Always compare, even when the user is absent, so a wrong email and a wrong
    # password take the same time and cannot be told apart.
    salt = (user or {}).get("salt", "00" * 16)
    expected = (user or {}).get("hash", "")
    got = hash_password(password, salt)
    if not user or not hmac.compare_digest(got, expected):
        _record_failure(email)
        return _resp(401, {"error": "invalid email or password"})
    _clear_failures(email)

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
    # Row-level reconciliation detail, for the review screen.
    "issues": ("issues.json", "application/json"),
}
INLINE_FORMATS = {"preview", "issues"}


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


def _jsonable(v):
    """DynamoDB hands back Decimal; default=str would emit numbers as strings
    ("1954"), which any consumer then has to re-parse. Emit real JSON numbers."""
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, set):
        return sorted(_jsonable(x) for x in v)
    return v


def _resp(code, body):
    return {"statusCode": code,
            "headers": {"content-type": "application/json",
                        "access-control-allow-origin": "*"},
            "body": json.dumps(_jsonable(body), default=str)}


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

    if route == "POST /auth/logout":
        # Delete the session row rather than waiting for its TTL: a signed-out
        # token must stop working at the moment the person signs out, not up
        # to twelve hours later.
        hdrs = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        tok = hdrs.get("authorization", "")[7:].strip()
        if tok:
            AUTH.delete_item(Key={"pk": f"SESSION#{tok}"})
        return _resp(200, {"ok": True})

    if route == "POST /auth/password":
        # Self-service password change. There is no reset-by-email: sending
        # mail needs a verified SES identity, which is a separate decision.
        # This at least means a password can be rotated without an operator
        # running manage_users.py.
        body = json.loads(event.get("body") or "{}")
        current = str(body.get("current_password") or "")
        new = str(body.get("new_password") or "")
        if len(new) < MIN_PASSWORD_LEN:
            return _resp(400, {"error": f"new password must be at least "
                                        f"{MIN_PASSWORD_LEN} characters"})
        user = AUTH.get_item(Key={"pk": f"USER#{user_sub}"}).get("Item")
        if not user:
            return _resp(404, {"error": "no such user"})
        if not hmac.compare_digest(
                hash_password(current, user.get("salt", "00" * 16)),
                user.get("hash", "")):
            # A valid session is not permission to set a new password without
            # knowing the old one — that would make a stolen token permanent.
            _record_failure(user_sub)
            return _resp(401, {"error": "current password is wrong"})
        salt = secrets.token_bytes(16).hex()
        AUTH.update_item(
            Key={"pk": f"USER#{user_sub}"},
            UpdateExpression="SET salt = :s, #h = :h, #r = :r",
            ExpressionAttributeNames={"#h": "hash", "#r": "rounds"},
            ExpressionAttributeValues={":s": salt,
                                       ":h": hash_password(new, salt),
                                       ":r": PBKDF2_ROUNDS},
        )
        _clear_failures(user_sub)
        # Other sessions for this user stay valid: finding them would need a
        # secondary index on the auth table, and this is the honest note to
        # leave rather than implying they were revoked.
        return _resp(200, {"ok": True})

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
        # A customer's own jobs come from the owner index, newest first.
        # This used to scan the first 200 items and filter by owner, which is
        # not merely slow: DynamoDB scans in key order, so once the table held
        # more than 200 jobs a customer could see NONE of their own while
        # other tenants' rows filled the page. A query cannot do that.
        if is_admin:
            items, kwargs = [], {"Limit": 200}
            while len(items) < 200:
                page = TABLE.scan(**kwargs)
                items += page.get("Items", [])
                if "LastEvaluatedKey" not in page:
                    break
                kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        else:
            items = TABLE.query(
                IndexName=OWNER_INDEX,
                KeyConditionExpression=Key("owner").eq(user_sub),
                ScanIndexForward=False, Limit=60).get("Items", [])
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
                        if fmt not in INLINE_FORMATS else "inline"},
            ExpiresIn=300,
        )
        return _resp(200, {"url": url})

    if route == "POST /jobs/{id}/review" and path_id:
        it = TABLE.get_item(Key={"job_id": path_id}).get("Item")
        if not it or not _owned(it):
            return _resp(404, {"error": "not found"})
        if it.get("status") not in ("needs_review", "reviewed"):
            return _resp(409, {"error": f"job is {it.get('status')}, "
                                        f"not awaiting review"})
        body = json.loads(event.get("body") or "{}")
        # The reconciliation result itself is never overwritten. A review says
        # "a person has looked at these rows and accepted the report as it
        # stands" — it does not turn a failed balance chain into a passed one,
        # and the account cards keep showing what validation actually found.
        TABLE.update_item(
            Key={"job_id": path_id},
            UpdateExpression="SET #s = :s, review = :r, updated_at = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "reviewed",
                ":r": {"by": user_sub, "at": int(time.time()),
                       "note": str(body.get("note") or "")[:500]},
                ":t": int(time.time())},
        )
        return _resp(200, {"ok": True, "status": "reviewed"})

    return _resp(404, {"error": f"no route {route}"})
