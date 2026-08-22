# Bank Statement Analyser — project context

SaaS that extracts + categorizes transactions from Indian bank/NBFC statement
PDFs. Deployed and working: https://dg3uwro4b2d2l.cloudfront.net — sign-in
required. Auth is self-hosted: users and sessions live in DynamoDB (AuthTable),
passwords are PBKDF2-HMAC-SHA256 with a per-user salt, and the API Lambda checks
a bearer token on every /jobs* route. No external identity provider.
Stack `BsaStack` in ap-south-1, account 681832767155.

## Layout
- backend/processor/bsa/ — the pipeline package (ingest → classify → extract →
  normalize → categorize → validate → publish). Layouts come from TWO places:
  bsa/layouts/*.yaml in the bundle, overlaid by s3://$DATA_BUCKET/layouts/*.yaml
  read at runtime (see "Layout registry"); both are matched by page-1
  fingerprints. The LLM fallback in extract/llm_fallback.py is OFF by default
  (see "Data residency"), so an unrecognised bank fails with a clear message
  rather than being sent anywhere.
- backend/processor/bsa/extract/ — four parser modes, chosen by `parser:` in
  the layout descriptor (see "Working today" for which bank uses which):
  - generic_layout.py  — one line per row, amounts on the dated anchor line.
  - columnar_layout.py — cells wrap across lines; a row is a block reassembled
    by x band.
  - grouped_layout.py  — the amount line is the row; sparse dates and balances.
  - icici_optransactionhistory.py — bank-specific module, for what YAML cannot
    express (ICICI marks a row's title line by font face).
- backend/sweeper/handler.py — makes "stuck on processing" impossible. Runs
  both as the processor's on-failure destination (fast, exact) and on a 15-min
  EventBridge schedule (the backstop). Settles the dead file, then RE-DRIVES
  the merge by re-invoking the processor with a synthetic event for a file
  that succeeded, so there is only ever one merge implementation.
- backend/api/handler.py — jobs API (presigned S3 upload/download, DynamoDB).
  There is NO Cognito: every /jobs* route requires a bearer session token that
  the Lambda itself validates against AuthTable. Roles come from the session
  record: `admin` sees all jobs plus AI usage/cost; `customer` sees only their
  own uploads and never the AI block. POST /auth/login is the only public route,
  and it is throttled: MAX_FAILED_LOGINS failures on an email lock it for
  LOCKOUT_S, counted for unknown emails too so a 429 cannot confirm that an
  address is registered. Also POST /auth/logout (deletes the session row, so a
  sign-out is immediate rather than TTL-eventual), POST /auth/password
  (self-service change, requires the current password), and
  POST /jobs/{id}/review.
  A customer's job list is a QUERY on the owner-created_at-index GSI, never a
  filtered scan — see gotcha 14.
- frontend/index.html — no-build SPA served via CloudFront.
- infra/ — CDK (Python). Deploy: `cd infra && source .venv/bin/activate && cdk deploy`.

## Hard-won gotchas — do not regress these
1. Lambdas are ARM_64 because CDK bundling on this Apple-Silicon Mac downloads
   aarch64 wheels. Keep architecture and bundling in sync.
2. Presigned S3 URLs MUST use the regional endpoint
   (s3.ap-south-1.amazonaws.com) — global endpoint breaks browser uploads.
3. Bundling pip needs --retries 10 --timeout 60 (flaky connection).
4. The release gate is validate.py: running-balance reconciliation on every row.
   Any parser/LLM change must keep sample statements at "passed".
5. Categorization = SME lending taxonomy (EMI/ECS/cash/bounce/disbursal/
   related-party/regular transfers), tiers: rules → NACH recurrence → merchant
   dictionary → LLM. Don't replace with consumer spend categories.
6. Statement data must not leave the AWS account. This was violated by design
   for a while — the fallback called the Anthropic API directly — and is now
   CLOSED IN CODE by two default-off switches, LLM_FALLBACK and
   ALLOW_EXTERNAL_LLM. See "Data residency" below for what each one gates and
   which tests pin it. Bedrock is still blocked on this account
   (INVALID_PAYMENT_INSTRUMENT on its AWS Marketplace subscription), so there
   is no working in-account inference either: an unknown bank simply cannot be
   read, and the answer is to write a layout. Do not "temporarily" flip
   ALLOW_EXTERNAL_LLM on an account that holds real customers.
7. Issue count is NOT a measure of how many rows are wrong. A contiguous run of
   dropped rows produces only ONE balance_mismatch, at the point the chain
   resumes. A real case: the LLM reported "17 issues" on a 1595-row statement
   while actually missing ~187 rows (12%). Judge extraction by whether the whole
   running-balance chain reconciles, not by the issue count.
8. A template parser beats the LLM on every axis where one exists — for Axis it
   was 67x faster, free, and strictly more accurate (0 issues vs 17, no missing
   rows). The LLM's job is to handle a bank ONCE so a layout can be written, not
   to process every statement forever.
9. Amounts are right-aligned, so layout column cutoffs key on the RIGHT edge
   (x1). The left edge moves with digit count; the right edge does not.
10. A statement's table header is often printed on page 1 only. Parsing pages
    that lack a header is required, or continuation pages are silently dropped
    (this cost 143 of 163 rows on the first Axis run).

## Accounts
There is no signup. Create logins with the helper (it hashes locally and writes
straight to the auth table):

    python scripts/manage_users.py add someone@getitright.co.in --role admin
    python scripts/manage_users.py add someone@getitright.co.in --role customer
    python scripts/manage_users.py list
    python scripts/manage_users.py remove someone@getitright.co.in

Passwords are stored only as a salted PBKDF2 hash and cannot be read back — to
change one, set a new one. PBKDF2_ROUNDS must stay identical in
scripts/manage_users.py and backend/api/handler.py or every login fails.

Roles: `admin` sees every job plus the AI usage/cost panel; `customer` sees only
their own uploads and never the AI block.

11. One report per ACCOUNT, not per upload. Files are grouped by
    (bank, account_no); ten files across three accounts produce three cards and
    three CSVs under outputs/{job}/{account-slug}/.
12. Balance validation runs PER SOURCE STATEMENT, never across a merged
    account. Two statements with a gap between them each reconcile; one chain
    over both would compare March's opening balance to January's closing and
    report a failure that is not real. The account shows the worst individual
    statement's status. Pinned by tests/test_period_gap.py.
13. The UI is strictly black/white/grey. Status is carried by words plus fill
    and border weight, never colour; debits use accounting parentheses.
14. Listing a customer's jobs must be a QUERY on the owner index, never a
    scan filtered by owner. A scan returns items in key order, so once the
    table outgrew the scanned page a customer could see none of their own jobs
    while other tenants' rows filled it. Pinned by tests/test_api.py.
15. Anything that reads per-page text for a WHOLE document must go through
    pypdfium2 (bsa.ingest.unreadable_pages), not pdfplumber. pdfminer does full
    layout analysis, which costs about as long again as the entire extraction —
    measured at 4.1s vs 0.13s on a 58-page statement, for the same answer.
16. A merge claim expires (MERGE_CLAIM_TTL_S). An invocation killed mid-merge
    used to hold "merging" forever, and nothing — retry or sweeper — could
    take the job back.
17. Every job status write goes through processor._update, which stamps
    updated_at. The sweeper measures staleness from that, never from
    created_at: a twenty-file job legitimately runs for a long time after it
    was created.

## Data residency — the gate that was blocking real customers

An LLM call is the only thing in this pipeline that can send statement data
anywhere, so it is guarded by two switches, both default-closed, because one
flag is one accident:

    LLM_FALLBACK        off (default) | on
    ALLOW_EXTERNAL_LLM  false (default) | true
    LLM_PROVIDER        bedrock (default) | anthropic | gemini

With the defaults, a bank with no layout fails as "this bank has no layout yet"
and no page of the statement is ever put in a request body — the refusal
happens in pipeline.extract_one, before the PDF is chunked, not at the client.
`ALLOW_EXTERNAL_LLM` is a second, independent gate: even with the fallback on,
only a provider in IN_ACCOUNT_PROVIDERS (bedrock) may be called, and anything
else raises ResidencyError. Turning both on is a deliberate, auditable decision
to send customer statements to a third party. Pinned by tests/test_residency.py
and tests/test_pipeline_gate.py — if those stop passing, the gate is open.

Bedrock is still blocked on this account (INVALID_PAYMENT_INSTRUMENT on its AWS
Marketplace subscription), so in practice an unknown bank cannot be extracted at
all. That is the intended behaviour, and the answer is to write a layout.

## Layout registry — bundled, overlaid from S3

registry.py loads bsa/layouts/*.yaml from the bundle, then overlays
s3://$DATA_BUCKET/layouts/*.yaml. An S3 descriptor whose `id` matches a bundled
one REPLACES it, so a new bank — or a fix to an existing descriptor — is a file
upload, not a release:

    python scripts/manage_layouts.py validate path/to/hdfc_savings.yaml
    python scripts/manage_layouts.py put      path/to/hdfc_savings.yaml
    python scripts/manage_layouts.py list     # shows which S3 entries override
    python scripts/manage_layouts.py rm       hdfc_savings

Live within LAYOUT_CACHE_TTL_S (300s) on a warm container, immediately on a
cold start. Three properties matter and are pinned by tests/test_registry.py:

- `parser` is an ALLOW-LIST, not a hint. A descriptor arrives from S3 at
  runtime and must never be able to name an arbitrary import.
- Match order is deterministic: sorted by descending `priority` then id.
  classify() takes the first fingerprint match, and glob order used to decide
  that, which is to say nothing did.
- Nothing here can take the pipeline down. A malformed descriptor is skipped;
  unreachable S3 degrades to the bundled set.

## Tests

    python -m venv .venv-test
    .venv-test/bin/pip install -r requirements-dev.txt
    .venv-test/bin/pytest                     # ~150 tests, no AWS, no network

tests/conftest.py loads each of the three handler.py files by path under its own
module name, with the environment set and boto3 replaced by in-memory fakes. The
fakes model the parts that carry behaviour — condition expressions, string-set
ADD, ProjectionExpression (real DynamoDB returns only what is projected, so a
field left out of the list reads as absent, silently, in production).

Real statements are the only honest check on a layout, and they cannot be
committed. Point the runner at a folder of them:

    BSA_SAMPLE_DIR=/path/to/statements .venv-test/bin/pytest tests/test_layout_samples.py -v

CI runs pytest and validates every bundled descriptor on every push and PR; the
deploy job depends on it, so CloudFormation is never reached if a test fails.

## Adding a new bank
Preferred path is a YAML descriptor with no Python:
1. Upload one statement; the LLM fallback extracts it and gives you a reference
   result to check against.
2. Dump word geometry with pdfplumber (`extract_words()`), note the right-edge
   x of each numeric column and where narration sits relative to the dated line.
3. Write `bsa/layouts/<bank>_<layout>.yaml` with `parser: generic`, page-1
   `fingerprints`, a `header.account_line` regex with named groups
   (account_no, period_from, period_to), and the `parse.columns` cutoffs.
   Copy axis_account_statement.yaml as the model.
4. Accept it only when validate() reports `passed` with the FULL row count —
   see gotcha 7. Compare row count against the LLM reference; a shortfall means
   dropped pages or rows.
Write a bank-specific module only when the layout genuinely cannot be described
in YAML (e.g. font-face-dependent narration, as in ICICI).

## Working today
Every bank seen so far parses by template — no AI, no per-statement cost.
There are now three parser modes, chosen by `parser:` in the descriptor:
- `generic`  — one line per row, amounts on the dated anchor line.
              Used by Axis, ICICI combined (with section profiles), SBI.
- `columnar` — cells wrap across lines, so a row is a block reassembled by
              x band. Used by ICICI Detailed and Equitas (whose amount column
              is too narrow for 7-figure values, splitting "1,049,823.00").
- `grouped`  — the amount line is the row: the date is printed only when it
              changes and the balance only at the end of a same-day group, with
              narration on the lines below. Used by Vasavi MSCM co-operative.
              Missing balances are DERIVED, so they reconcile by construction —
              what still validates is the group, since the next printed balance
              must match the running total.
Plus the bank-specific ICICI OpTransactionHistory module (font-face narration).

Registry as it stands — 7 layouts across 5 banks (bank != layout; ICICI alone
exports three different shapes, so "we support ICICI" is not a meaningful claim):

    Axis Bank                      Account Statement           generic
    Equitas Small Finance Bank     Statement of Account        columnar
    ICICI Bank                     Combined Account Statement  generic
    ICICI Bank                     Detailed Statement          columnar
    ICICI Bank                     OpTransactionHistory        module
    State Bank of India            Account Statement           generic
    Vasavi MSCM Co-operative Bank  Account Statement           grouped

A bank with NO layout is refused: the LLM fallback is off by default, and even
switched on it may only use an in-account provider, which is currently
unavailable. That is deliberate — the answer was never to top up a key. A
descriptor against an existing parser mode is roughly an hour with one sample
PDF, and it is free, deterministic and balance-verified forever after. A
genuinely new SHAPE costs more, because it needs a new mode first (that is what
`grouped` was). With the S3 registry it no longer needs a deploy either.

A page with NO TEXT LAYER is recorded separately (StatementMeta.unreadable_pages)
and surfaced on the account card and the review screen. Nothing is extracted
from such a page, so its transactions are simply absent and the balance chain
breaks with no other visible cause — seen for real in a statement PDF assembled
by hand, with one month scanned in among digital exports. No layout can fix
that, and saying so is the only useful thing the report can do.

An upload where some files fail still publishes the accounts that worked; the
failed files are listed on the upload with a plain reason.

## Current next steps

1. **More bank layouts** (HDFC, Kotak, and whatever else customers send) as
   YAML descriptors — needs one sample statement per bank. This is now the
   only thing that scales with the customer list, and with the S3 registry it
   no longer needs a deploy. It is also the whole answer to an unknown bank,
   since the LLM fallback is closed by default.
2. **Bedrock.** Until its Marketplace subscription works on this account there
   is no in-account inference at all, so a bank with no layout cannot be read.
   Unblocking it is a billing task, not a code one.
3. **Password reset by email.** Sign-in now has throttling, logout and a
   self-service password change, but a forgotten password still needs an
   operator running scripts/manage_users.py. A real reset needs a verified SES
   identity — a separate decision, not a missing line of code.
4. **Revoke other sessions on a password change.** Finding a user's other
   sessions needs a secondary index on the auth table; today they stay valid
   until their 12-hour TTL, and the UI says so rather than implying otherwise.
5. **Sweeper at scale.** The scheduled sweep is a filtered scan of a table
   holding 180 days of jobs. It is projected and runs every 15 minutes, and
   the on-failure destination handles the common case exactly — but if the
   table grows large, set a `live` attribute while a job runs and query a
   sparse index instead of scanning.
6. **Upload size limits.** A presigned PUT has no content-length ceiling, so a
   client can upload an arbitrarily large object. MAX_FILES_PER_JOB caps count,
   not bytes.
