"""The API Lambda: authentication, authorisation, and the review endpoint.

Everything here is reachable from the public internet with no credential but a
password, so the interesting cases are the ones where it must say no.
"""
import json
import sys

import pytest


def _ev(route, token=None, body=None, path_id=None, qs=None, ip=None):
    e = {"routeKey": route, "headers": {}, "queryStringParameters": qs or {}}
    if token:
        e["headers"]["Authorization"] = f"Bearer {token}"
    if body is not None:
        e["body"] = json.dumps(body)
    if path_id:
        e["pathParameters"] = {"id": path_id}
    if ip:
        e["requestContext"] = {"http": {"sourceIp": ip}}
    return e


def _body(resp):
    return json.loads(resp["body"])


@pytest.fixture
def user(api, auth_table):
    def make(email="a@x.com", password="Getitright#2026", role="customer"):
        import secrets
        salt = secrets.token_bytes(16).hex()
        auth_table.put_item(Item={"pk": f"USER#{email}", "email": email,
                                  "role": role, "salt": salt,
                                  "hash": api.hash_password(password, salt)})
        return email, password
    return make


@pytest.fixture
def signed_in(api, user):
    def go(role="customer", email="a@x.com"):
        e, p = user(email=email, role=role)
        r = api.lambda_handler(_ev("POST /auth/login", body={"email": e, "password": p}), None)
        assert r["statusCode"] == 200
        return _body(r)["token"], e
    return go


# ------------------------------------------------------------------ login --

def test_login_succeeds_and_returns_a_role(api, signed_in):
    token, email = signed_in(role="admin")
    r = api.lambda_handler(_ev("GET /auth/me", token), None)
    assert _body(r) == {"email": email, "role": "admin"}


def test_a_wrong_password_and_an_unknown_email_are_indistinguishable(api, user):
    user()
    a = api.lambda_handler(_ev("POST /auth/login",
                               body={"email": "a@x.com", "password": "wrong"}), None)
    b = api.lambda_handler(_ev("POST /auth/login",
                               body={"email": "nobody@x.com", "password": "wrong"}), None)
    assert a["statusCode"] == b["statusCode"] == 401
    assert _body(a) == _body(b)


def test_repeated_failures_lock_the_account_out(api, user):
    """Passwords are the only credential and the endpoint is public, so an
    unthrottled login is an online guessing oracle."""
    email, password = user()
    for _ in range(api.MAX_FAILED_LOGINS):
        r = api.lambda_handler(_ev("POST /auth/login",
                                   body={"email": email, "password": "no"}), None)
        assert r["statusCode"] == 401
    # even the CORRECT password is now refused, which is the point
    r = api.lambda_handler(_ev("POST /auth/login",
                               body={"email": email, "password": password}), None)
    assert r["statusCode"] == 429
    assert "too many failed" in _body(r)["error"]


def test_lockout_applies_to_unknown_emails_too(api):
    """Otherwise a 429 would confirm that an address is registered."""
    for _ in range(api.MAX_FAILED_LOGINS):
        api.lambda_handler(_ev("POST /auth/login",
                               body={"email": "ghost@x.com", "password": "no"}), None)
    r = api.lambda_handler(_ev("POST /auth/login",
                               body={"email": "ghost@x.com", "password": "no"}), None)
    assert r["statusCode"] == 429


def test_a_successful_login_clears_the_failure_count(api, user):
    email, password = user()
    for _ in range(api.MAX_FAILED_LOGINS - 1):
        api.lambda_handler(_ev("POST /auth/login",
                               body={"email": email, "password": "no"}), None)
    assert api.lambda_handler(_ev("POST /auth/login",
                                  body={"email": email, "password": password}),
                              None)["statusCode"] == 200
    for _ in range(api.MAX_FAILED_LOGINS - 1):
        r = api.lambda_handler(_ev("POST /auth/login",
                                   body={"email": email, "password": "no"}), None)
    assert r["statusCode"] == 401       # not locked: the counter restarted


def test_failures_across_many_emails_lock_the_source_ip(api, user, monkeypatch):
    """The per-email counter alone misses credential stuffing: one IP walking
    a list of addresses never trips any single email's threshold."""
    monkeypatch.setattr(api, "MAX_FAILED_LOGINS_IP", 3)
    email, password = user()
    for i in range(3):
        r = api.lambda_handler(_ev("POST /auth/login",
                                   body={"email": f"guess{i}@x.com",
                                         "password": "no"},
                                   ip="203.0.113.9"), None)
        assert r["statusCode"] == 401
    # Even the CORRECT credentials for an untouched email are refused from
    # the locked IP…
    r = api.lambda_handler(_ev("POST /auth/login",
                               body={"email": email, "password": password},
                               ip="203.0.113.9"), None)
    assert r["statusCode"] == 429
    assert "too many failed" in _body(r)["error"]
    # …while the same sign-in from a different IP is untouched.
    r = api.lambda_handler(_ev("POST /auth/login",
                               body={"email": email, "password": password},
                               ip="198.51.100.7"), None)
    assert r["statusCode"] == 200


def test_the_ip_threshold_sits_above_the_email_threshold(api):
    """One client hammering ONE address must hit the email lockout first, so a
    shared office IP is not collateral damage of one forgotten password."""
    assert api.MAX_FAILED_LOGINS_IP > api.MAX_FAILED_LOGINS


def test_a_successful_login_clears_the_ip_counter_too(api, user, monkeypatch):
    monkeypatch.setattr(api, "MAX_FAILED_LOGINS_IP", 3)
    email, password = user()
    for i in range(2):
        api.lambda_handler(_ev("POST /auth/login",
                               body={"email": f"guess{i}@x.com", "password": "no"},
                               ip="203.0.113.9"), None)
    assert api.lambda_handler(_ev("POST /auth/login",
                                  body={"email": email, "password": password},
                                  ip="203.0.113.9"), None)["statusCode"] == 200
    # the counter restarted: two more failures do not add up to a lockout
    for i in range(2):
        r = api.lambda_handler(_ev("POST /auth/login",
                                   body={"email": f"more{i}@x.com", "password": "no"},
                                   ip="203.0.113.9"), None)
        assert r["statusCode"] == 401


def test_an_expired_session_row_is_not_accepted(api, auth_table, signed_in):
    """DynamoDB deletes by TTL lazily, so an expired row can still be read."""
    token, _ = signed_in()
    auth_table.items[f"SESSION#{token}"]["ttl"] = 0
    assert api.lambda_handler(_ev("GET /auth/me", token), None)["statusCode"] == 401


@pytest.mark.parametrize("route", [
    "GET /jobs", "POST /jobs", "GET /jobs/{id}", "GET /jobs/{id}/download",
    "POST /jobs/{id}/review", "POST /jobs/{id}/corrections",
    "GET /jobs/{id}/corrections", "POST /auth/logout", "POST /auth/password",
])
def test_every_route_but_login_requires_a_session(api, route):
    r = api.lambda_handler(_ev(route, body={}, path_id="j1"), None)
    assert r["statusCode"] == 401


def test_logout_kills_the_token_immediately(api, signed_in):
    """Clearing localStorage alone would leave the token valid for 12 hours."""
    token, _ = signed_in()
    assert api.lambda_handler(_ev("POST /auth/logout", token), None)["statusCode"] == 200
    assert api.lambda_handler(_ev("GET /auth/me", token), None)["statusCode"] == 401


# -------------------------------------------------------- password change --

def test_password_change_requires_the_current_password(api, signed_in):
    """A valid session is not permission to set a new password — otherwise a
    stolen token becomes permanent."""
    token, _ = signed_in()
    r = api.lambda_handler(_ev("POST /auth/password", token, body={
        "current_password": "wrong", "new_password": "LongEnough#2026"}), None)
    assert r["statusCode"] == 401


def test_password_change_enforces_a_minimum_length(api, signed_in):
    token, _ = signed_in()
    r = api.lambda_handler(_ev("POST /auth/password", token, body={
        "current_password": "Getitright#2026", "new_password": "short"}), None)
    assert r["statusCode"] == 400


def test_password_change_takes_effect_and_reissues_a_salt(api, auth_table, signed_in):
    token, email = signed_in()
    before = auth_table.items[f"USER#{email}"]["salt"]
    r = api.lambda_handler(_ev("POST /auth/password", token, body={
        "current_password": "Getitright#2026", "new_password": "NewPassword#2026"}), None)
    assert r["statusCode"] == 200
    assert auth_table.items[f"USER#{email}"]["salt"] != before
    assert api.lambda_handler(_ev("POST /auth/login", body={
        "email": email, "password": "Getitright#2026"}), None)["statusCode"] == 401
    assert api.lambda_handler(_ev("POST /auth/login", body={
        "email": email, "password": "NewPassword#2026"}), None)["statusCode"] == 200


def test_password_change_revokes_other_sessions_but_not_the_changing_one(api, signed_in):
    """A password change often means 'someone may have my password' — every
    session issued before it must stop working. The session that proved the
    current password keeps working, deliberately."""
    other, email = signed_in()
    r = api.lambda_handler(_ev("POST /auth/login", body={
        "email": email, "password": "Getitright#2026"}), None)
    current = _body(r)["token"]
    assert current != other
    r = api.lambda_handler(_ev("POST /auth/password", current, body={
        "current_password": "Getitright#2026",
        "new_password": "NewPassword#2026"}), None)
    assert r["statusCode"] == 200
    assert api.lambda_handler(_ev("GET /auth/me", current), None)["statusCode"] == 200
    assert api.lambda_handler(_ev("GET /auth/me", other), None)["statusCode"] == 401


def test_a_session_for_a_removed_user_stops_working(api, auth_table, signed_in):
    """The user row is the source of truth for a session's right to live."""
    token, email = signed_in()
    del auth_table.items[f"USER#{email}"]
    assert api.lambda_handler(_ev("GET /auth/me", token), None)["statusCode"] == 401


def test_a_cli_password_reset_bumps_pwd_version(manage_users, auth_table, monkeypatch):
    """scripts/manage_users.py must stamp pwd_version exactly like the API's
    self-service change, or an operator reset would leave old sessions alive."""
    monkeypatch.setattr(sys, "argv", ["manage_users.py", "add", "x@x.com",
                                      "--password", "FirstPass#123",
                                      "--table", "auth"])
    manage_users.main()
    first = dict(auth_table.items["USER#x@x.com"])
    assert first["pwd_version"] == 1
    monkeypatch.setattr(sys, "argv", ["manage_users.py", "add", "x@x.com",
                                      "--password", "SecondPass#456",
                                      "--table", "auth"])
    manage_users.main()
    second = auth_table.items["USER#x@x.com"]
    assert second["pwd_version"] == 2
    assert second["hash"] != first["hash"]


def test_a_cli_password_reset_revokes_live_api_sessions(api, manage_users,
                                                        signed_in, monkeypatch):
    """The end-to-end claim: an operator resetting a password from the CLI
    kills that user's existing bearer tokens on the next request."""
    token, email = signed_in()
    monkeypatch.setattr(sys, "argv", ["manage_users.py", "add", email,
                                      "--password", "BrandNewPw#2026",
                                      "--table", "auth"])
    manage_users.main()
    assert api.lambda_handler(_ev("GET /auth/me", token), None)["statusCode"] == 401


# ------------------------------------------------------------------- jobs --

def test_a_customer_sees_their_own_uploads_via_the_owner_index(api, jobs_table,
                                                               signed_in):
    """This used to be a scan of the first 200 items filtered by owner. A scan
    returns items in key order, so past 200 jobs a customer could see NONE of
    their own while other tenants' rows filled the page."""
    token, email = signed_in(email="mine@x.com")
    for i in range(300):
        jobs_table.put_item(Item={"job_id": f"other{i}", "owner": "someone@else.com",
                                  "created_at": 1000 + i, "status": "done"})
    jobs_table.put_item(Item={"job_id": "mine1", "owner": email,
                              "created_at": 5, "status": "done"})
    jobs = _body(api.lambda_handler(_ev("GET /jobs", token), None))["jobs"]
    assert [j["job_id"] for j in jobs] == ["mine1"]


def test_a_customer_cannot_read_another_tenants_job(api, jobs_table, signed_in):
    token, _ = signed_in()
    jobs_table.put_item(Item={"job_id": "theirs", "owner": "other@x.com",
                              "status": "done"})
    for route in ("GET /jobs/{id}", "GET /jobs/{id}/download",
                  "POST /jobs/{id}/review"):
        r = api.lambda_handler(_ev(route, token, body={}, path_id="theirs"), None)
        assert r["statusCode"] == 404, route


def test_cost_data_is_admin_only(api, jobs_table, signed_in):
    token, email = signed_in()
    jobs_table.put_item(Item={"job_id": "j", "owner": email, "status": "done",
                              "password": "hunter2",
                              "summary": {"rows": 3, "ai": {"cost_usd": "9.99"}}})
    got = _body(api.lambda_handler(_ev("GET /jobs/{id}", token, path_id="j"), None))
    assert "ai" not in got["summary"] and got["summary"]["rows"] == 3
    assert "password" not in got and "owner" not in got

    admin_token, _ = signed_in(role="admin", email="admin@x.com")
    got = _body(api.lambda_handler(_ev("GET /jobs/{id}", admin_token, path_id="j"), None))
    assert got["summary"]["ai"]["cost_usd"] == "9.99"
    assert "password" not in got          # never, for anyone


def test_pdf_passwords_never_leave_the_api(api, jobs_table, signed_in):
    token, email = signed_in()
    jobs_table.put_item(Item={"job_id": "j", "owner": email, "status": "done",
                              "files": [{"idx": 0, "filename": "a.pdf",
                                         "password": "secret"}]})
    got = _body(api.lambda_handler(_ev("GET /jobs/{id}", token, path_id="j"), None))
    assert "password" not in got["files"][0]


def test_download_rejects_an_unknown_format(api, jobs_table, signed_in):
    token, email = signed_in()
    jobs_table.put_item(Item={"job_id": "j", "owner": email, "status": "done"})
    r = api.lambda_handler(_ev("GET /jobs/{id}/download", token, path_id="j",
                               qs={"format": "../../etc/passwd"}), None)
    assert r["statusCode"] == 400


def test_the_account_slug_cannot_escape_its_prefix(api, jobs_table, signed_in):
    """The slug goes straight into an S3 key."""
    token, email = signed_in()
    jobs_table.put_item(Item={"job_id": "j", "owner": email, "status": "done"})
    url = _body(api.lambda_handler(_ev("GET /jobs/{id}/download", token, path_id="j",
                                       qs={"format": "csv",
                                           "account": "../../uploads"}), None))["url"]
    key = url.split("example.invalid/")[1].split("?")[0]
    assert key.startswith("outputs/j/") and ".." not in key


def test_issues_is_a_downloadable_format(api, jobs_table, signed_in):
    token, email = signed_in()
    jobs_table.put_item(Item={"job_id": "j", "owner": email, "status": "needs_review"})
    r = api.lambda_handler(_ev("GET /jobs/{id}/download", token, path_id="j",
                               qs={"format": "issues", "account": "icici-1"}), None)
    assert r["statusCode"] == 200 and "issues.json" in _body(r)["url"]


def test_jobs_are_capped_per_upload(api, signed_in):
    token, _ = signed_in()
    body = {"files": [{"filename": f"{i}.pdf"} for i in range(50)]}
    got = _body(api.lambda_handler(_ev("POST /jobs", token, body=body), None))
    assert len(got["uploads"]) == api.MAX_FILES_PER_JOB


def test_a_new_job_initialises_failed_files(api, jobs_table, signed_in):
    """Present from birth, so concurrent per-file failure writes in the
    processor and sweeper always take the per-key conditional path instead of
    a last-writer-wins overwrite of the whole map."""
    token, _ = signed_in()
    job_id = _body(api.lambda_handler(_ev("POST /jobs", token,
                                          body={"filename": "a.pdf"}),
                                      None))["job_id"]
    assert jobs_table.items[job_id]["failed_files"] == {}


def test_admin_listing_reaches_past_the_first_scan_pages(api, jobs_table, signed_in):
    """A scan returns hash-key order, not recency. The old 200-item cap meant
    that once the table outgrew 200 jobs the console showed an arbitrary
    slice and could miss every recent upload."""
    token, _ = signed_in(role="admin", email="admin@x.com")
    for i in range(250):
        jobs_table.put_item(Item={"job_id": f"job{i:04d}", "owner": "c@x.com",
                                  "status": "done", "created_at": 1000 + i,
                                  "summary": {"rows": i}})
    jobs_table.page_size = 100          # force real pagination
    jobs = _body(api.lambda_handler(_ev("GET /jobs", token), None))["jobs"]
    assert len(jobs) == 60
    # newest first — including the ones a 200-item scan never reached
    assert [j["job_id"] for j in jobs[:2]] == ["job0249", "job0248"]
    newest = jobs[0]
    # and the projection kept what the console renders
    assert newest["status"] == "done"
    assert newest["owner"] == "c@x.com"
    assert newest["summary"]["rows"] == 249
    assert newest["created_at"] == 1249


def test_the_s3_client_pins_the_regional_endpoint_and_sigv4(api):
    """Gotcha 2: presigned URLs minted against the global endpoint break
    browser uploads. The client must be built on the regional endpoint with
    sigv4 and virtual addressing."""
    kw = api._fake_boto3.client_kwargs["s3"]
    assert kw["endpoint_url"] == "https://s3.ap-south-1.amazonaws.com"
    assert kw["region_name"] == "ap-south-1"
    cfg = kw["config"]
    assert cfg.signature_version == "s3v4"
    assert cfg.s3 == {"addressing_style": "virtual"}


def test_decimals_come_back_as_json_numbers(api):
    """A Decimal serialised with default=str emits "1954", which every consumer
    then has to re-parse and some silently mis-compare."""
    from decimal import Decimal
    out = json.loads(api._resp(200, {"n": Decimal("1954"),
                                     "f": Decimal("1.5")})["body"])
    assert out == {"n": 1954, "f": 1.5}


# ----------------------------------------------------------------- review --

def test_review_marks_the_job_and_records_who(api, jobs_table, signed_in):
    token, email = signed_in()
    jobs_table.put_item(Item={"job_id": "j", "owner": email,
                              "status": "needs_review"})
    r = api.lambda_handler(_ev("POST /jobs/{id}/review", token, path_id="j",
                               body={"note": "checked against the PDF"}), None)
    assert r["statusCode"] == 200
    item = jobs_table.items["j"]
    assert item["status"] == "reviewed"
    assert item["review"]["by"] == email
    assert item["review"]["note"] == "checked against the PDF"


def test_review_does_not_rewrite_the_validation_result(api, jobs_table, signed_in):
    """Marking an upload reviewed says a person looked at it. It must not turn
    a failed balance chain into a passed one."""
    token, email = signed_in()
    jobs_table.put_item(Item={
        "job_id": "j", "owner": email, "status": "needs_review",
        "summary": {"validation": "failed",
                    "accounts": [{"slug": "a", "validation": "failed"}]}})
    api.lambda_handler(_ev("POST /jobs/{id}/review", token, path_id="j", body={}), None)
    s = jobs_table.items["j"]["summary"]
    assert s["validation"] == "failed" and s["accounts"][0]["validation"] == "failed"


def test_a_job_that_is_not_awaiting_review_cannot_be_reviewed(api, jobs_table,
                                                              signed_in):
    token, email = signed_in()
    jobs_table.put_item(Item={"job_id": "j", "owner": email, "status": "processing"})
    r = api.lambda_handler(_ev("POST /jobs/{id}/review", token, path_id="j",
                               body={}), None)
    assert r["statusCode"] == 409


# ------------------------------------------------------------ corrections --

def _correction(**kw):
    body = {"description": "ECS/UTIBDE111/Bajaj Finance Ltd_SMS OT",
            "amount": -128182.5, "bank": "Axis Bank", "account": "axis-1234",
            "old_category": "Regular debit", "new_category": "EMI transaction",
            "old_party": "", "new_party": "Bajaj Finance Ltd"}
    body.update(kw)
    return body


def test_corrections_post_and_get_roundtrip(api, jobs_table, signed_in):
    """A reviewer's relabelled row comes back exactly as stored — who, when,
    old and new values — and the amount survives as a JSON number."""
    token, admin = signed_in(role="admin", email="admin@x.com")
    jobs_table.put_item(Item={"job_id": "j", "owner": "cust@x.com",
                              "status": "done"})
    r = api.lambda_handler(_ev("POST /jobs/{id}/corrections", token,
                               body=_correction(), path_id="j"), None)
    assert r["statusCode"] == 200 and _body(r)["count"] == 1
    r = api.lambda_handler(_ev("POST /jobs/{id}/corrections", token,
                               body=_correction(description="CASH DEP-SELF",
                                                amount=500000,
                                                old_category="Regular credit",
                                                new_category="cash deposit",
                                                new_party=""), path_id="j"), None)
    assert r["statusCode"] == 200 and _body(r)["count"] == 2

    got = _body(api.lambda_handler(_ev("GET /jobs/{id}/corrections", token,
                                       path_id="j"), None))["corrections"]
    assert len(got) == 2
    first = got[0]
    assert first["description"] == "ECS/UTIBDE111/Bajaj Finance Ltd_SMS OT"
    assert first["amount"] == -128182.5          # a number, not a string
    assert first["old_category"] == "Regular debit"
    assert first["new_category"] == "EMI transaction"
    assert first["new_party"] == "Bajaj Finance Ltd"
    assert first["corrected_by"] == admin
    assert first["ts"] > 0


def test_corrections_are_admin_only_even_on_your_own_job(api, jobs_table,
                                                         signed_in):
    """Corrections feed the categorisation truth set — a domain-owner task. A
    customer gets 403 on any job, including one they own."""
    token, email = signed_in(role="customer")
    jobs_table.put_item(Item={"job_id": "j", "owner": email, "status": "done"})
    for route in ("POST /jobs/{id}/corrections", "GET /jobs/{id}/corrections"):
        r = api.lambda_handler(_ev(route, token, body=_correction(),
                                   path_id="j"), None)
        assert r["statusCode"] == 403, route


def test_a_customer_cannot_post_corrections_to_another_owners_job(api, jobs_table,
                                                                  signed_in):
    token, _ = signed_in(role="customer")
    jobs_table.put_item(Item={"job_id": "theirs", "owner": "other@x.com",
                              "status": "done"})
    r = api.lambda_handler(_ev("POST /jobs/{id}/corrections", token,
                               body=_correction(), path_id="theirs"), None)
    assert r["statusCode"] == 403
    assert "corrections" not in jobs_table.items["theirs"]


def test_corrections_on_a_missing_job_are_404(api, signed_in):
    token, _ = signed_in(role="admin", email="admin@x.com")
    r = api.lambda_handler(_ev("POST /jobs/{id}/corrections", token,
                               body=_correction(), path_id="ghost"), None)
    assert r["statusCode"] == 404


@pytest.mark.parametrize("bad", [
    {"description": "   "},                          # nothing to label
    {"amount": "not-a-number"},
    {"new_category": "", "new_party": ""},           # no corrected value at all
])
def test_corrections_reject_incomplete_input(api, jobs_table, signed_in, bad):
    token, _ = signed_in(role="admin", email="admin@x.com")
    jobs_table.put_item(Item={"job_id": "j", "owner": "cust@x.com",
                              "status": "done"})
    r = api.lambda_handler(_ev("POST /jobs/{id}/corrections", token,
                               body=_correction(**bad), path_id="j"), None)
    assert r["statusCode"] == 400
    assert "corrections" not in jobs_table.items["j"]


def test_corrections_csv_export_matches_the_golden_set_shape(api, jobs_table,
                                                             signed_in):
    """The export appends verbatim to tests/data/golden_category_truth.csv, so
    the header and column order must match it exactly. A party-only correction
    teaches no category and stays out of the CSV."""
    import csv as _csv
    import io as _io
    token, _ = signed_in(role="admin", email="admin@x.com")
    jobs_table.put_item(Item={"job_id": "j", "owner": "cust@x.com",
                              "status": "done"})
    for body in (_correction(),
                 _correction(description="CASH DEP-TP-VIKAS", amount=334000,
                             new_category="cash deposit",
                             bank="AU Small Finance Bank", new_party=""),
                 _correction(description="party only", new_category="",
                             new_party="Somebody")):
        r = api.lambda_handler(_ev("POST /jobs/{id}/corrections", token,
                                   body=body, path_id="j"), None)
        assert r["statusCode"] == 200
    r = api.lambda_handler(_ev("GET /jobs/{id}/corrections", token,
                               path_id="j", qs={"format": "csv"}), None)
    assert r["statusCode"] == 200
    assert r["headers"]["content-type"] == "text/csv"
    lines = r["body"].strip().split("\n")
    assert lines[0] == "Description,Amount,Category,Bank"
    rows = list(_csv.DictReader(_io.StringIO(r["body"])))
    assert len(rows) == 2                       # party-only correction excluded
    assert rows[0]["Description"] == "ECS/UTIBDE111/Bajaj Finance Ltd_SMS OT"
    assert float(rows[0]["Amount"]) == -128182.5
    assert rows[0]["Category"] == "EMI transaction"
    assert rows[0]["Bank"] == "Axis Bank"
    assert rows[1]["Amount"] == "334000.0"      # golden-set float style


def test_corrections_are_scrubbed_from_a_customers_job_view(api, jobs_table,
                                                            signed_in):
    """The job record carries the corrections list, but it is an admin
    workflow (it names the reviewer) — a customer's own job view omits it."""
    token, email = signed_in(role="customer")
    jobs_table.put_item(Item={"job_id": "j", "owner": email, "status": "done",
                              "corrections": [{"description": "x",
                                               "new_category": "cash deposit",
                                               "corrected_by": "admin@x.com"}]})
    got = _body(api.lambda_handler(_ev("GET /jobs/{id}", token, path_id="j"), None))
    assert "corrections" not in got

    admin_token, _ = signed_in(role="admin", email="admin@x.com")
    got = _body(api.lambda_handler(_ev("GET /jobs/{id}", admin_token,
                                       path_id="j"), None))
    assert got["corrections"][0]["corrected_by"] == "admin@x.com"


# ------------------------------------------------ categoriser playground (beta) --

def test_categoriser_playground_is_admin_only(api, signed_in):
    """A customer must not reach the categoriser playground — it is an admin
    diagnostic. The gate is the role on the session, same as the AI block."""
    token, _ = signed_in(role="customer")
    r = api.lambda_handler(_ev("POST /admin/try-categorize", token,
                               body={"description": "x", "amount": -1}), None)
    assert r["statusCode"] == 403


def test_categoriser_playground_needs_authentication(api):
    r = api.lambda_handler(_ev("POST /admin/try-categorize",
                               body={"description": "x"}), None)
    assert r["statusCode"] == 401


def test_categoriser_playground_invokes_the_processor_and_returns_it(api, signed_in):
    """An admin's narration is classified by the processor (the one place the
    pipeline lives) and the result is passed straight back to the UI."""
    api._fake_lambda.response = {"category": "EMI transaction",
                                 "party": "Bajaj Finance Ltd", "mode": "emi",
                                 "detail": "EMI paid to Bajaj Finance Ltd"}
    token, _ = signed_in(role="admin")
    r = api.lambda_handler(_ev("POST /admin/try-categorize", token,
                               body={"description": "ECS/.../Bajaj Finance Ltd",
                                     "amount": -128182}), None)
    assert r["statusCode"] == 200
    assert _body(r)["category"] == "EMI transaction"
    assert _body(r)["party"] == "Bajaj Finance Ltd"
    # It really went to the processor with the narration in the payload.
    fn, payload = api._fake_lambda.invocations[-1]
    assert fn == "proc-fn"
    sent = json.loads(payload)["try_categorize"]
    assert sent["description"].startswith("ECS/") and sent["amount"] == -128182


def test_categoriser_playground_rejects_an_empty_narration(api, signed_in):
    token, _ = signed_in(role="admin")
    r = api.lambda_handler(_ev("POST /admin/try-categorize", token,
                               body={"description": "   ", "amount": 0}), None)
    assert r["statusCode"] == 400


# ----------------------------------------------------------- error envelope --

def test_a_malformed_json_body_is_a_400_not_a_crash(api):
    e = _ev("POST /auth/login")
    e["body"] = "{definitely not json"
    r = api.lambda_handler(e, None)
    assert r["statusCode"] == 400
    assert "not valid JSON" in _body(r)["error"]
    assert r["headers"]["access-control-allow-origin"] == "*"


def test_a_malformed_body_on_an_authenticated_route_is_also_400(api, signed_in):
    token, _ = signed_in()
    e = _ev("POST /jobs", token)
    e["body"] = "{"
    assert api.lambda_handler(e, None)["statusCode"] == 400


def test_an_unexpected_error_returns_a_json_500_with_cors(api, jobs_table,
                                                          signed_in, monkeypatch,
                                                          capsys):
    """API Gateway's own 500 carries no CORS headers, so a browser reports it
    as an opaque network error. Ours must stay a readable JSON response —
    with the traceback logged, not swallowed."""
    token, _ = signed_in()

    def boom(**_kw):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(jobs_table, "get_item", boom)
    r = api.lambda_handler(_ev("GET /jobs/{id}", token, path_id="j"), None)
    assert r["statusCode"] == 500
    assert _body(r) == {"error": "something went wrong — try again"}
    assert r["headers"]["access-control-allow-origin"] == "*"
    assert "kaboom" in capsys.readouterr().err
