"""The API Lambda: authentication, authorisation, and the review endpoint.

Everything here is reachable from the public internet with no credential but a
password, so the interesting cases are the ones where it must say no.
"""
import json

import pytest


def _ev(route, token=None, body=None, path_id=None, qs=None):
    e = {"routeKey": route, "headers": {}, "queryStringParameters": qs or {}}
    if token:
        e["headers"]["Authorization"] = f"Bearer {token}"
    if body is not None:
        e["body"] = json.dumps(body)
    if path_id:
        e["pathParameters"] = {"id": path_id}
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


def test_an_expired_session_row_is_not_accepted(api, auth_table, signed_in):
    """DynamoDB deletes by TTL lazily, so an expired row can still be read."""
    token, _ = signed_in()
    auth_table.items[f"SESSION#{token}"]["ttl"] = 0
    assert api.lambda_handler(_ev("GET /auth/me", token), None)["statusCode"] == 401


@pytest.mark.parametrize("route", [
    "GET /jobs", "POST /jobs", "GET /jobs/{id}", "GET /jobs/{id}/download",
    "POST /jobs/{id}/review", "POST /auth/logout", "POST /auth/password",
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
