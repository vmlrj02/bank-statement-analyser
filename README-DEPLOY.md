# Bank Statement Analyser — AWS deployment

One CDK stack deploys the whole thing to **ap-south-1 (Mumbai)**:

- **Frontend** — the upload UI (`frontend/index.html`) on S3 + CloudFront.
  Drag-drop PDFs, optional password + related-party names, live job status,
  preview table, CSV/Excel/JSON downloads.
- **API** — API Gateway HTTP API + a Lambda: create job (returns a presigned
  S3 upload URL), list jobs, job status, presigned downloads.
- **Pipeline** — one processor Lambda per uploaded file runs the full `bsa`
  package (extract → normalize → categorize → validate → publish); the
  invocation that finishes the last file merges, groups by account and
  publishes. Outputs go to `outputs/{job_id}/{account-slug}/`; status and
  summary to DynamoDB.
- **Sweeper** — a second Lambda that makes "stuck on processing" impossible.
  It is the processor's on-failure destination AND runs every 15 minutes, and
  it re-drives a partly-finished job rather than discarding the statements
  that already succeeded.
- **Data** — S3 data bucket (SSE, uploads auto-deleted after 30 days,
  outputs after 180) + DynamoDB jobs table (TTL 180 days). Account numbers
  are masked to last-4 before anything is written to outputs.

## Deliberate simplifications

1. **Self-hosted sign-in, not Cognito.** Users and sessions live in DynamoDB;
   the API Lambda verifies a bearer token on every `/jobs*` route. Login is
   throttled and there is a self-service password change, but password RESET
   by email is not built — that needs a verified SES identity.
2. **Single Lambda, not Step Functions.** Each file gets its own invocation
   and its own 15-minute budget, which is what a stage split would mostly have
   bought. The split is still the answer if one statement ever needs more than
   one Lambda's worth of time.
3. **LLM fallback OFF by default.** An unrecognised bank fails with "this bank
   has no layout yet". Two switches guard it — `LLM_FALLBACK` and
   `ALLOW_EXTERNAL_LLM` — so statement data cannot leave the AWS account by
   accident. See CLAUDE.md, "Data residency".

## Prerequisites (on your machine)

- AWS account + credentials configured (`aws sts get-caller-identity` works),
  with rights to create the resources above
- Node.js 18+ and the CDK CLI: `npm install -g aws-cdk`
- Python 3.12+
- **Docker running** (CDK uses it to build the processor Lambda's
  dependencies for the Lambda runtime)

## Deploy

```bash
cd infra
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export AWS_REGION=ap-south-1
cdk bootstrap          # first time only, per account+region
cdk deploy
```

Deploy takes ~5–8 minutes (CloudFront is the slow part). The outputs print:

- `BsaStack.SiteUrl` — open this. That's your app.
- `BsaStack.ApiUrl`, `DataBucketName`, `JobsTableName` — for debugging.

## Tests

Run these before deploying — CI does, and the deploy job depends on them:

```bash
python3 -m venv .venv-test
.venv-test/bin/pip install -r requirements-dev.txt
.venv-test/bin/pytest
```

No AWS account and no network: the three Lambda handlers are loaded by path
with boto3 replaced by in-memory fakes. To check the layouts against real
statements (which cannot be committed):

```bash
BSA_SAMPLE_DIR=/path/to/statements .venv-test/bin/pytest tests/test_layout_samples.py -v
```

## Adding a bank without a deploy

Layouts are read from S3 as well as from the bundle, so a new descriptor is a
file upload:

```bash
python scripts/manage_layouts.py validate hdfc_savings.yaml
python scripts/manage_layouts.py put      hdfc_savings.yaml
python scripts/manage_layouts.py list
```

Live within 5 minutes on a warm Lambda, immediately on a cold start. An S3
descriptor whose `id` matches a bundled one replaces it, which is also how a
broken descriptor gets fixed without cutting a release.

## Smoke test

Upload one of the sample ICICI PDFs. Within ~15 seconds the job should show
**done** with a *balance passed* chip, category chips, a working preview, and
all three downloads. Then upload a statement from a bank with no layout and
confirm it fails gracefully with "this bank has no layout yet" — that message,
and not a provider error, is what proves the residency gate is closed.

## Operating notes

- Logs: CloudWatch log groups for `BsaStack-Processor…`, `BsaStack-ApiFn…`
  and `BsaStack-Sweeper…`. The sweeper logs a line per job it fails or
  re-drives, which is the first place to look if a job ended unexpectedly.
- A failed job's error message is stored on the job record and shown in the UI.
- Costs at MVP volume: single-digit dollars/month (CloudFront + Lambda +
  DynamoDB are effectively free-tier; S3 pennies).
- Tear down: `cdk destroy` — the data bucket and jobs table are `RETAIN`ed
  on purpose; delete them manually if you truly want the data gone.

## What's next

1. More layouts (HDFC, Kotak, whatever customers send) — one sample PDF each,
   and no deploy needed. This is the only item that scales with the customer
   list.
2. Unblock Bedrock's AWS Marketplace subscription. Until then there is no
   in-account inference at all, so a bank with no layout cannot be read.
3. Password reset by email (needs a verified SES identity).
4. See CLAUDE.md, "Current next steps", for the full list with reasoning.
