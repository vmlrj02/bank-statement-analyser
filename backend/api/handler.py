"""API Lambda — job lifecycle. boto3 only, no other deps.

Routes (HTTP API v2 routeKey):
  POST /auth/login          : {email, password} -> {token, email, role}
  POST /auth/logout         : end this session
  POST /auth/password       : {current_password, new_password}
  GET  /auth/me             : who the bearer token belongs to
  POST /jobs                : {filename, password?, related_parties?[]}
  POST /jobs/{id}/password  : {idx|filename, password} — unlock a protected
                              file that failed, and re-drive just that file
                              -> {job_id, upload_url}
  GET  /jobs                : recent jobs (newest first; own only unless admin)
  GET  /jobs/{id}           : job record incl. summary
  GET  /jobs/{id}/download  : ?format=csv|xlsx|json|preview|issues&account=<slug>
  POST /jobs/{id}/review    : {note?} -> mark a needs_review job reviewed
  POST /jobs/{id}/corrections : admin: record a reviewer's corrected
                                category/party for one row (training data)
  GET  /jobs/{id}/corrections : admin: list them; ?format=csv exports in the
                                golden-set shape (Description,Amount,Category,Bank)
"""
import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import time
import traceback
import uuid
from decimal import Decimal, InvalidOperation

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
lam = boto3.client("lambda")
TABLE = ddb.Table(os.environ["JOBS_TABLE"])
AUTH = ddb.Table(os.environ["AUTH_TABLE"])
BUCKET = os.environ["DATA_BUCKET"]
PROCESSOR_FUNCTION = os.environ.get("PROCESSOR_FUNCTION", "")
OWNER_INDEX = os.environ.get("OWNER_INDEX", "owner-created_at-index")

SESSION_TTL = 12 * 3600
PBKDF2_ROUNDS = 210_000

# Login throttling. Passwords are the only credential here and the API is
# public, so an unthrottled /auth/login is an offline-speed online guessing
# oracle. Counted per email in the same table, expiring on its own TTL.
# A second, parallel counter is kept per SOURCE IP with a higher threshold:
# the email counter alone lets one client lock a known address out forever
# (a denial of service against the admin), while the IP counter alone would
# miss a distributed guess. Both use the same window and lockout.
MAX_FAILED_LOGINS = 8
MAX_FAILED_LOGINS_IP = int(os.environ.get("MAX_FAILED_LOGINS_IP",
                                          5 * MAX_FAILED_LOGINS))
LOCKOUT_S = 15 * 60
FAIL_WINDOW_S = 15 * 60
MIN_PASSWORD_LEN = 10


def _fail_key(email: str) -> str:
    return f"LOGIN#{email}"


def _ip_fail_key(ip: str) -> str:
    return f"LOGINIP#{ip}"


def _source_ip(event) -> str:
    """The caller's IP as API Gateway HTTP APIs report it; '' when absent
    (tests, direct invokes) — in which case the IP layer simply does not
    count, and the per-email layer still applies."""
    return str((((event or {}).get("requestContext") or {})
                .get("http") or {}).get("sourceIp") or "")


def _lockout_remaining_pk(pk: str) -> int:
    """Seconds this counter must wait, or 0. Counts a wrong email too, so a
    locked account and an unknown one stay indistinguishable."""
    it = AUTH.get_item(Key={"pk": pk}).get("Item")
    if not it:
        return 0
    now = int(time.time())
    if int(it.get("locked_until", 0)) > now:
        return int(it["locked_until"]) - now
    return 0


def _record_failure_pk(pk: str, limit: int) -> None:
    now = int(time.time())
    it = AUTH.get_item(Key={"pk": pk}).get("Item") or {}
    # A slow trickle of wrong guesses should not accumulate into a lockout
    # months later, so the count restarts once the window has passed.
    fails = int(it.get("fails", 0)) + 1 if int(it.get("first_at", 0)) > now - FAIL_WINDOW_S else 1
    item = {"pk": pk, "fails": fails,
            "first_at": int(it.get("first_at", now)) if fails > 1 else now,
            "ttl": now + LOCKOUT_S + FAIL_WINDOW_S}
    if fails >= limit:
        item["locked_until"] = now + LOCKOUT_S
    AUTH.put_item(Item=item)


def _clear_failures_pk(pk: str) -> None:
    try:
        AUTH.delete_item(Key={"pk": pk})
    except Exception:                                       # noqa: BLE001
        pass


def _lockout_remaining(email: str) -> int:
    return _lockout_remaining_pk(_fail_key(email))


def _record_failure(email: str) -> None:
    _record_failure_pk(_fail_key(email), MAX_FAILED_LOGINS)


def _clear_failures(email: str) -> None:
    _clear_failures_pk(_fail_key(email))


def hash_password(password: str, salt: str) -> str:
    """PBKDF2-HMAC-SHA256. Salt is per user, so identical passwords across
    accounts do not produce identical hashes."""
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt),
                               PBKDF2_ROUNDS).hex()


def _login(body, event=None):
    email = str(body.get("email") or body.get("username") or "").strip().lower()
    password = str(body.get("password") or "")
    if not email or not password:
        return _resp(400, {"error": "email and password required"})
    ip = _source_ip(event)
    wait = _lockout_remaining(email)
    if not wait and ip:
        wait = _lockout_remaining_pk(_ip_fail_key(ip))
    if wait:
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
        if ip:
            _record_failure_pk(_ip_fail_key(ip), MAX_FAILED_LOGINS_IP)
        return _resp(401, {"error": "invalid email or password"})
    _clear_failures(email)
    if ip:
        _clear_failures_pk(_ip_fail_key(ip))

    token = secrets.token_urlsafe(32)
    now = int(time.time())
    AUTH.put_item(Item={
        "pk": f"SESSION#{token}", "email": email,
        "role": user.get("role", "customer"),
        # The user's password generation at sign-in time. A password change
        # bumps the user's pwd_version, which makes every session carrying an
        # older stamp invalid — revocation without a session index.
        "pwd_version": int(user.get("pwd_version", 1)),
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
    # The user row is the source of truth for whether this session may live:
    # a removed user loses their sessions immediately, and a session stamped
    # with an older pwd_version was issued before the password last changed —
    # revoked, without needing an index over sessions. One extra get_item per
    # authenticated request.
    user = AUTH.get_item(Key={"pk": f"USER#{it.get('email')}"}).get("Item")
    if not user:
        return None, None
    if int(it.get("pwd_version", 1)) < int(user.get("pwd_version", 1)):
        return None, None
    return it.get("email"), it.get("role", "customer")

MAX_FILES_PER_JOB = 20

# Reviewer corrections live on the job item itself (like `review`), so they
# must never be able to push it past DynamoDB's 400KB item limit.
MAX_CORRECTIONS = 500

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
        # Corrections are an admin workflow (they carry the reviewer's email
        # and feed the training set) — a customer never sees them.
        item.pop("corrections", None)
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
    """Dispatch, wrapped so every response — including a crash — carries the
    standard JSON + CORS shape. An exception that escaped to API Gateway would
    come back as a bare 500 without CORS headers, which a browser reports as
    an opaque network error rather than anything actionable."""
    try:
        return _handle(event)
    except json.JSONDecodeError:
        return _resp(400, {"error": "request body is not valid JSON"})
    except Exception:                                       # noqa: BLE001
        traceback.print_exc()
        return _resp(500, {"error": "something went wrong — try again"})


def _handle(event):
    route = event.get("routeKey", "")
    path_id = (event.get("pathParameters") or {}).get("id")
    qs = event.get("queryStringParameters") or {}
    if route == "POST /auth/login":
        return _login(json.loads(event.get("body") or "{}"), event)

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
        # Bumping pwd_version revokes every OTHER session for this user:
        # _session rejects any session stamped with an older version, so no
        # index over sessions is needed. Absent means 1 (legacy rows).
        new_version = int(user.get("pwd_version", 1)) + 1
        AUTH.update_item(
            Key={"pk": f"USER#{user_sub}"},
            UpdateExpression="SET salt = :s, #h = :h, #r = :r, pwd_version = :v",
            ExpressionAttributeNames={"#h": "hash", "#r": "rounds"},
            ExpressionAttributeValues={":s": salt,
                                       ":h": hash_password(new, salt),
                                       ":r": PBKDF2_ROUNDS,
                                       ":v": new_version},
        )
        # The session that just proved knowledge of the current password keeps
        # working — re-stamp it with the new version, deliberately.
        hdrs = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        tok = hdrs.get("authorization", "")[7:].strip()
        if tok:
            AUTH.update_item(
                Key={"pk": f"SESSION#{tok}"},
                UpdateExpression="SET pwd_version = :v",
                ExpressionAttributeValues={":v": new_version},
            )
        _clear_failures(user_sub)
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
            # Present from birth so concurrent per-file failure writes always
            # take the per-key conditional path, never a whole-map overwrite.
            "failed_files": {},
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
            # Scan the WHOLE table, projected to what the console renders. The
            # old version stopped at 200 items, and a scan returns hash-key
            # order — so past 200 jobs the admin saw an arbitrary slice and
            # missed the newest. At scale the fix is a constant-partition GSI
            # on created_at; until then an exhaustive projected scan is cheap.
            proj = {"#j": "job_id", "#s": "status", "#fn": "filename",
                    "#f": "files", "#ex": "expected", "#up": "uploaded",
                    "#c": "created_at", "#m": "updated_at", "#o": "owner",
                    "#sm": "summary", "#er": "error", "#rv": "review",
                    "#co": "corrections", "#rp": "related_parties",
                    "#ff": "failed_files"}
            items, kwargs = [], {
                "ProjectionExpression": ", ".join(proj),
                "ExpressionAttributeNames": proj,
            }
            while True:
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

    # A password-protected statement that failed for want of its password is
    # RECOVERABLE without re-uploading: the PDF bytes are already in S3, and
    # the only thing missing is the password. This stores it on the file entry
    # and re-drives the processor for that one key, through the same synthetic
    # S3 event the sweeper uses — so there is still exactly one extraction
    # path. Re-uploading a large statement just to supply a password was the
    # only route before, and on a 60-page file over a phone tether that is a
    # real cost for a typo.
    if route == "POST /jobs/{id}/password" and path_id:
        it = TABLE.get_item(Key={"job_id": path_id}).get("Item")
        if not it or not _owned(it):
            return _resp(404, {"error": "not found"})
        body = json.loads(event.get("body") or "{}")
        password = str(body.get("password") or "")
        if not password:
            return _resp(400, {"error": "password required"})
        want = body.get("idx")
        name = str(body.get("filename") or "")
        entry = None
        for i, f in enumerate(it.get("files") or []):
            if not isinstance(f, dict):
                continue
            if (want is not None and str(f.get("idx", i)) == str(want)) or \
               (want is None and name and f.get("filename") == name):
                entry, pos = f, i
                break
        if entry is None:
            return _resp(404, {"error": "no such file on this upload"})
        idx = str(entry.get("idx", pos))
        # Only offer this where a password is actually the problem. Re-driving
        # a file that failed for an unreadable scan or a missing layout would
        # burn an extraction and fail again in exactly the same way.
        failed = (it.get("failed_files") or {}).get(idx) or {}
        reason = str(failed.get("error") or "")
        if "password" not in reason.lower() and it.get("status") != "password_required":
            return _resp(409, {"error": "this file did not fail for a password"})
        key = entry.get("key", f"uploads/{path_id}/{idx}.pdf")
        now = int(time.time())
        # The password is stored the same way it is at upload — on the file
        # entry, and cleared by the processor once extraction is over.
        # The whole files list is rewritten rather than files[n].password, the
        # same read-modify-write the processor uses to CLEAR passwords. Only
        # this route and that one ever touch `files` after creation, so there
        # is nothing here to clobber; per-file failures live in failed_files,
        # which is updated by key and is untouched by this write.
        entries = [dict(f) if isinstance(f, dict) else f
                   for f in (it.get("files") or [])]
        entries[pos]["password"] = password
        TABLE.update_item(
            Key={"job_id": path_id},
            UpdateExpression=("SET files = :f, #s = :s, updated_at = :t "
                              "REMOVE failed_files.#i"),
            ExpressionAttributeNames={"#s": "status", "#i": idx},
            ExpressionAttributeValues={":f": entries, ":s": "processing",
                                       ":t": now},
        )
        if PROCESSOR_FUNCTION:
            lam.invoke(
                FunctionName=PROCESSOR_FUNCTION, InvocationType="Event",
                Payload=json.dumps({"Records": [
                    {"s3": {"object": {"key": key}}}]}).encode())
        return _resp(200, {"ok": True, "status": "processing",
                           "filename": entry.get("filename", "")})

    # Reviewer corrections — captured training data. A correction is one row
    # the reviewer relabelled (right category and/or party), appended to the
    # job item the same way `review` is stored: one read, one write, no new
    # table. Admin-only both ways: corrections feed the categorisation truth
    # set (tests/data/golden_category_truth.csv), which is a domain-owner
    # task, not a customer control — so a customer gets 403 on ANY job,
    # their own included.
    if route in ("POST /jobs/{id}/corrections",
                 "GET /jobs/{id}/corrections") and path_id:
        if not is_admin:
            return _resp(403, {"error": "admin only"})
        it = TABLE.get_item(Key={"job_id": path_id}).get("Item")
        if not it:
            return _resp(404, {"error": "not found"})
        existing = list(it.get("corrections") or [])

        if route.startswith("GET"):
            if qs.get("format") == "csv":
                # The EXACT shape of the golden set, so an export can be
                # appended to it verbatim. A party-only correction teaches no
                # category and is left out of the CSV (it stays in the JSON).
                buf = io.StringIO()
                w = csv.writer(buf, lineterminator="\n")
                w.writerow(["Description", "Amount", "Category", "Bank"])
                for c in existing:
                    if not c.get("new_category"):
                        continue
                    w.writerow([c.get("description", ""),
                                repr(float(c.get("amount", 0))),
                                c.get("new_category", ""),
                                c.get("bank", "")])
                return {"statusCode": 200,
                        "headers": {"content-type": "text/csv",
                                    "access-control-allow-origin": "*",
                                    "content-disposition":
                                        f'attachment; filename="corrections_{path_id}.csv"'},
                        "body": buf.getvalue()}
            return _resp(200, {"corrections": existing})

        body = json.loads(event.get("body") or "{}")
        desc = str(body.get("description") or "").strip()
        if not desc:
            return _resp(400, {"error": "description required"})
        try:
            # Decimal, never float: the resource interface refuses floats.
            amount = Decimal(str(body.get("amount") or 0).replace(",", ""))
        except InvalidOperation:
            return _resp(400, {"error": "amount must be a number"})
        new_cat = str(body.get("new_category") or "").strip()[:80]
        new_party = str(body.get("new_party") or "").strip()[:120]
        # Free-text feedback: "how this SHOULD have been handled", recorded
        # against a reconciliation issue or a row where a label correction is
        # not the right shape. Stays in the JSON record for the domain owner;
        # the golden-set CSV export already skips entries with no category.
        note = str(body.get("note") or "").strip()[:500]
        if not new_cat and not new_party and not note:
            return _resp(400, {"error": "a corrected category, party or note "
                                        "is required"})
        if len(existing) >= MAX_CORRECTIONS:
            return _resp(409, {"error": f"correction limit reached "
                                        f"({MAX_CORRECTIONS} per job)"})
        correction = {
            "uid": str(body.get("uid") or "")[:80],
            "description": desc[:300],
            "amount": amount,
            "bank": str(body.get("bank") or "")[:80],
            "account": str(body.get("account") or "")[:80],
            "old_category": str(body.get("old_category") or "")[:80],
            "new_category": new_cat,
            "old_party": str(body.get("old_party") or "")[:120],
            "new_party": new_party,
            "note": note,
            # Where the feedback came from: "preview" row, "reconciliation"
            # issue — so the training review can weigh them differently.
            "source": str(body.get("source") or "")[:40],
            "corrected_by": user_sub,
            "ts": int(time.time()),
        }
        TABLE.update_item(
            Key={"job_id": path_id},
            UpdateExpression="SET corrections = :c, updated_at = :t",
            ExpressionAttributeValues={":c": existing + [correction],
                                       ":t": int(time.time())},
        )
        return _resp(200, {"ok": True, "count": len(existing) + 1})

    # Categorisation playground (beta) — admin-only. Lets the domain owner type a
    # narration and see exactly how the engine reads it (mode, party, category),
    # so the categorisation logic is inspectable from the UI without a developer.
    # The actual work runs in the processor, which owns the pipeline code, so the
    # UI can never diverge from what a real statement gets. No statement data,
    # no S3, no DynamoDB — just one string in, one classification out.
    if route == "POST /admin/try-categorize":
        if not is_admin:
            return _resp(403, {"error": "admin only"})
        if not PROCESSOR_FUNCTION:
            return _resp(503, {"error": "categoriser not configured"})
        body = json.loads(event.get("body") or "{}")
        desc = str(body.get("description") or "").strip()
        if not desc:
            return _resp(400, {"error": "description required"})
        try:
            amount = float(str(body.get("amount") or 0).replace(",", ""))
        except ValueError:
            return _resp(400, {"error": "amount must be a number"})
        try:
            out = lam.invoke(
                FunctionName=PROCESSOR_FUNCTION, InvocationType="RequestResponse",
                Payload=json.dumps({"try_categorize":
                                    {"description": desc[:500], "amount": amount}}
                                   ).encode(),
            )
            result = json.loads(out["Payload"].read() or "{}")
        except Exception as e:                                   # noqa: BLE001
            return _resp(502, {"error": f"categoriser unavailable: {e}"})
        if not isinstance(result, dict) or "category" not in result:
            return _resp(502, {"error": "categoriser returned no result"})
        return _resp(200, result)

    return _resp(404, {"error": f"no route {route}"})
