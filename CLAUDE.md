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
- backend/processor/bsa/credit_summary.py — the lender-facing conclusion, not
  just categorised rows: per account it derives monthly turnover, average / min
  / closing balance, cash intensity, bounce/return count, penal charges, EMI &
  interest outflow and headroom, related-party share and counterparty
  concentration, plus plain-language "reads" that fire only when a number
  warrants. Deterministic, traceable, no scoring model. Surfaced in the
  workbook's "Credit Assessment" sheet (which sits AFTER the customer's
  nineteen template tabs — see "Workbook = the customer's template"), in the
  API account summary, and on the account card. This is what a credit team
  reads first and what the demo leads with; the categorised transaction sheets
  are the "show the working".
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
5. Categorization = SME lending taxonomy (EMI, ECS, cash, bounce, disbursal,
   "Interest received", "Interest / fee payments", related-party, regular
   transfers — the eighteen tag names are the customer's, spelled exactly as
   the "Category (ABCL)" tab of the labelling master spells them), tiers:
   rules → NACH recurrence → merchant dictionary → LLM. Don't replace with
   consumer spend categories. The fuzzy vocabulary — NBFC/lender names, penal
   vs non-penal charge phrases, cash-deposit spellings — lives in DATA
   (bsa/data/category_rules.yaml), editable by the domain owner without a code
   change, the same reasoning as the layout registry. tests/
   test_categorization_accuracy.py is the ground-truth harness: every reviewer
   rule is a labelled case with a per-category scoreboard, so a categorisation
   change moves a measured number and a regression fails the build — this is
   how we stopped guessing. Point BSA_CATEGORY_TRUTH at a labelled CSV to fold
   real statements in. Precedence subtleties that matter: penal charges resolve
   BEFORE interest (a MAB charge whose ref contains "Int.Pd" is penal, not
   interest); interest is split by SIGN (credit=received, debit=Interest
   payments); penal is a threshold/violation charge only (MAB/POS-threshold),
   NOT an ordinary service fee (card/txn/SMS/folio → Regular debit); a known
   lender name makes a debit an EMI/Interest-payments and a credit a disbursal,
   and names the party. Penal keywords are word-bounded phrases, or bare tokens
   like "AMB"/"POS" match inside names and POS purchases.
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
13. The UI follows the Get It Right brand: deep navy + gold on white, after
    the company logo (boss's instruction, Aug 2026 — replaced the earlier
    strictly-black/white/grey rule). Status must still read without colour
    alone — explicit words plus fill and border weight — and debits use
    accounting parentheses. The UI is also ACCOUNT-first: the sidebar is
    account cards, and the main pane is that account's card, read top to
    bottom in ONE fixed order — chart, key numbers, categorisation (mix,
    confidence, party quality), then the written description (coverage,
    data-quality notes, credit reads), with the month-by-month and top-party
    tables collapsed at the end.

    Only two things may appear as a notice ABOVE the cards, because an
    account card cannot say them: an upload still processing, and files that
    could not be read at all. NEEDS-REVIEW MUST NOT. `needs_review` is an
    upload-wide status — processor/handler.py sets it when the WORST account
    in the upload fails, or any file failed — while the notices render above
    whichever account is selected. The two together put a review banner over
    every account in the upload, including the clean ones, in both the admin
    and the customer view. Balance state belongs to an account and is
    described in that account's own card. The row-level reconciliation
    drill-down survives as an admin-only button on the affected account's
    card; there is no "mark reviewed" workflow, because marking an upload
    said nothing about any single account in it.
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
19. A PROTECTED statement carries its password in its own FILE NAME, and
    ingest.password_candidates() reads it — "Acct Statement pass - 43888983",
    "PSW-176284535-…", "Password -JAME1982", and sometimes the name simply IS
    the password ("133591747.pdf"). All 25 protected files in the sample
    corpus open this way with nothing typed. Two rules keep it safe: a
    candidate is only ever tried AFTER the file has already refused to open
    (so an unprotected statement never pays for it, and a wrong guess costs
    one cheap pikepdf open), and a password the person typed is tried first.
    The original name must be passed as `filename=` because in Lambda the path
    is a scratch "/tmp/…/in.pdf".
    The upload screen therefore does NOT ask up front. isEncrypted() in the
    frontend is a byte scan for "/Encrypt" that a PDF 1.5+ file hides in a
    compressed xref and that an EMPTY-password file carries while opening
    fine — wrong in both directions, and the source of the "our app is asking
    for a password on a file that isn't protected" report. Only a file that
    genuinely cannot be opened comes back asking, as Unlock inside its own
    failure.

## Workbook = the customer's template, sheet for sheet

backend/processor/bsa/publish.py renders Output_Template-2.xlsx exactly: the
NINETEEN sheet names, their ORDER and their headers are the customer's, copied
verbatim down to the trailing space in "Sl. No. ". Their analysts read the file
tab by tab against their own template, so a renamed sheet, a reordered one or a
shifted column is a defect to them even when every number is right ("Xns sheet
is missing" was a real bug report). TEMPLATE_SHEETS / XN_HEADERS /
GROUPED_HEADERS in publish.py are that contract, and tests/
test_publish_workbook.py pins it.

Anything of ours beyond the template — Credit Assessment, Category Totals,
Other Xns, the largest-single-transaction sheets — is APPENDED AFTER the
nineteen, never interleaved, so a template-driven reader finds every sheet
where their file says it is. Note the consequence: Credit Assessment is no
longer sheet 1. Summary is.

Two sheets are grids, not lists, because the template is: Summary carries an
identity block then a month-per-COLUMN block (most recent month first, a
Total/Avg column last), and EOD Balances is day-of-month down by month across.

The taxonomy metadata that the Summary needs lives in categorize.py, from the
same master: PAYMENTS_ISSUED and PAYMENTS_DEPOSITED are the DENOMINATORS for
the two bounce-rate rows (a bounce count alone says nothing — three returns
against four payments is a failing account, three against nine hundred is
noise), and NON_TURNOVER is the set of credits that are not business turnover.

18. TURNOVER MEANS BUSINESS CREDITS. Never total credits. NON_TURNOVER
    (categorize.py) is the one definition — loan disbursals, salary, interest
    received, investment returns, refunds and related-party inflows are all
    stripped out; cash deposits DO count, because they are sales receipts and
    their risk is reported separately as cash intensity. The word must mean the
    same thing in the API, the workbook and the card, so anything DERIVED from
    turnover uses the same base: `turnover_trend`, the per-month `turnover`
    series, the last-quarter change, cash intensity (a share OF turnover) and
    debt-service coverage (turnover ÷ EMI). Counting a loan disbursal as
    capacity to repay loans is circular and flatters the most leveraged
    borrower. `avg_monthly_credits` and `total_credits` are the honest
    all-inflow figures and stay that way — they are simply not called turnover.
    Pinned by tests/test_credit_summary.py and tests/test_taxonomy_master.py.

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
- `generic` also supports multi-token dates via `date_parts`, and a wrapped
              year via `infer_year_from_period`: SBI's savings export prints a
              two-digit-day date's YEAR on the next physical line ("17 Aug" on
              the anchor, "2025" below), which was breaking anchor detection and
              merging hundreds of rows into one until the year was completed
              from the statement period.
Plus the bank-specific ICICI OpTransactionHistory module (font-face narration).
That module falls back to NEAREST-anchor assignment when a re-exported PDF has
had its fonts flattened (no Black face), since the title-above/descriptor-below
split can no longer be told apart by font — without it the narration shifts by
one row on hand-reassembled files.

## Statement integrity — fraud/tamper signals (integrity.py)
For lending, whether a statement was doctored matters. The strongest signal was
already there for free: a doctored amount breaks the running-balance chain, so
validate.py is itself a tamper check. Two more come from data we already hold:
- PDF metadata (ingest captures /Producer, /Creator, dates): a genuine bank
  export is made by a server library (iText, OpenPDF); a hand-assembled one
  shows an editing tool (pdf-lib, Photoshop, Quartz) — the ICICI "manual" file
  flags on this.
- A scanned page spliced into a digital export (unreadable_pages).
account_integrity() aggregates these per account into `verified | review` with
plain reasons, surfaced on the account card, the workbook Summary, and the API
summary. Deliberately conservative and false-positive-tolerant — it is a prompt
to a human underwriter, not an accusation. A ModDate after CreationDate is
carried as information only (genuine statements are routinely re-saved), NEVER a
review trigger. Pinned by tests/test_integrity.py.

Registry as it stands — 31 layouts across 14 banks. Bank != layout:
ICICI alone exports six different shapes and SBI seven, which is why "we
support ICICI" is not a meaningful claim — a layout is matched by its page-1
fingerprint, not by the bank's name:

    AU Small Finance Bank          Statement (Perfios)                 columnar
    Axis Bank                      Account Statement Report            module
    Axis Bank                      Axis Account Statement              generic
    Axis Bank                      Cash Credit Statement               generic
    Axis Bank                      Statement of Account                generic
    City Union Bank                Statement of Account                generic
    Equitas Small Finance Bank     Statement of Account                columnar
    HDFC Bank                      Statement of account                generic
    ICICI Bank                     Account Statement                   columnar
    ICICI Bank                     Combined Account Statement          generic
    ICICI Bank                     Detailed Statement                  columnar
    ICICI Bank                     Detailed Statement (net-banking)    generic
    ICICI Bank                     Monthly Statement                   generic
    ICICI Bank                     OpTransactionHistory                module
    IDFC FIRST Bank                Statement of Account                generic
    Indian Overseas Bank           Account Statement                   generic
    IndusInd Bank                  Account Statement                   generic
    IndusInd Bank                  Account Statement (bank-reference)  generic
    Punjab National Bank           Account Statement (current)         generic
    Punjab National Bank           Statement of Account                generic
    Punjab National Bank           Statement of Account                generic
    State Bank of India            Account Statement                   generic
    State Bank of India            Account Statement                   generic
    State Bank of India            Savings statement                   generic
    State Bank of India            Savings-cheque statement            generic
    State Bank of India            Statement of Account                generic
    State Bank of India            Statement of Account (SB)           generic
    State Bank of India            Statement of Account (internet)     generic
    Union Bank of India            Statement of Account                generic
    Vasavi MSCM Co-operative Bank  Account Statement                   grouped
    YES Bank                       Statement of Account                generic

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

1. **The last two of the founder's top seven.** The target list is HDFC,
   Axis, ICICI, SBI, Kotak, BoB, PNB. Five are done; the gap is:
   - **Bank of Baroda** — three samples are in hand (Samples-3/bob) and all
     three currently fail with "no layout". They are clean digital text in a
     wrapped-cell shape, so this is an ordinary `columnar` descriptor.
   - **Kotak** — no layout AND no sample anywhere, so this one is blocked on
     getting a statement, not on us.
   Also unsupported, with samples in hand: Karnataka Bank (6 files), Deutsche
   Bank (2), Canara (1). A descriptor is roughly an hour and, with the S3
   registry, needs no deploy — it is the whole answer to an unknown bank,
   since the LLM fallback is closed by default.
2. **PNB mangles wrapped narration** (diagnosed, not yet fixed). PNB wraps the
   Description cell at its EDGE, mid-token, so one row prints as
   "UPI/CR/25845" / "9693857/ROS" / "HAN". generic_layout flattens a row's
   narration lines into one word list and joins with spaces, so the party
   reads "ROS HAN" and never consolidates with the same party spelled whole
   elsewhere; "ROSHAN INFRASTRUC TURE" breaks the same way.

   The fix is NOT a blanket no-separator join — PNB wraps mid-token on some
   lines and at a space on others, and gluing the latter gives "SAHUON
   &BORWELLS". The geometry tells them apart: a HARD wrap fills the cell to
   its right edge (~x1 227 on that export), a space wrap stops short ("HAN"
   ends at 208). So the join must be per-line and edge-aware, which needs the
   parser to keep each narration line's right edge instead of flattening to
   words.
   A second, separate defect on the same layout: pnb_ca_statement's
   `remarks_x_max: 370` swallows the Branch Name column, which is why every
   description on that export carries a stray " -".

3. **Bedrock.** Until its Marketplace subscription works on this account there
   is no in-account inference at all, so a bank with no layout cannot be read.
   Unblocking it is a billing task, not a code one.
4. **Password reset by email.** Sign-in now has throttling, logout and a
   self-service password change, but a forgotten password still needs an
   operator running scripts/manage_users.py. A real reset needs a verified SES
   identity — a separate decision, not a missing line of code.
5. ~~Revoke other sessions on a password change.~~ DONE — no GSI was needed:
   the USER# row carries a `pwd_version`, each session is stamped with it at
   login, and _session rejects a stale stamp. A password change (API or
   scripts/manage_users.py) bumps the version, killing every other session
   while deliberately re-stamping the one that made the change.
6. **Sweeper at scale.** The scheduled sweep is a filtered scan of a table
   holding 180 days of jobs. It is projected and runs every 15 minutes, and
   the on-failure destination handles the common case exactly — but if the
   table grows large, set a `live` attribute while a job runs and query a
   sparse index instead of scanning.
7. **Upload size limits.** Mostly closed: the processor refuses any file whose
   ContentLength exceeds MAX_UPLOAD_BYTES (default 50 MB) as a per-file failure
   before download, and the UI rejects oversize files client-side. What remains
   open is the presigned PUT itself, which still has no byte ceiling at the S3
   edge — stopping the bytes from landing at all needs a switch to
   generate_presigned_post with a content-length-range condition (and a POST
   CORS rule), a deliberate upload-contract change.
