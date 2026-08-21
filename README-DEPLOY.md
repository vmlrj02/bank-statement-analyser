# Bank Statement Analyser — AWS deployment (Phase 1 MVP)

One CDK stack deploys the whole thing to **ap-south-1 (Mumbai)**:

- **Frontend** — the upload UI (`frontend/index.html`) on S3 + CloudFront.
  Drag-drop PDFs, optional password + related-party names, live job status,
  preview table, CSV/Excel/JSON downloads.
- **API** — API Gateway HTTP API + a Lambda: create job (returns a presigned
  S3 upload URL), list jobs, job status, presigned downloads.
- **Pipeline** — one processor Lambda runs the full `bsa` package
  (extract → normalize → categorize → validate → publish) on every PDF that
  lands in `uploads/`. Outputs go to `outputs/{job_id}/`; status and summary
  to DynamoDB.
- **Data** — S3 data bucket (SSE, uploads auto-deleted after 30 days,
  outputs after 180) + DynamoDB jobs table (TTL 180 days). Account numbers
  are masked to last-4 before anything is written to outputs.

## MVP simplifications (deliberate)

1. **No sign-in yet.** Anyone with the CloudFront URL can upload and view
   jobs. Fine for private testing; add Cognito before sharing beyond your
   team — bank statements are sensitive. (The architecture doc describes the
   Cognito setup; it bolts onto this stack without restructuring.)
2. **Single Lambda, not Step Functions.** The pipeline takes seconds per
   statement, so one function is simpler to operate. The stage split comes
   back when the Bedrock LLM / OCR paths are wired in.
3. **LLM fallback not wired.** Unknown bank layouts fail with a clear
   message. Known layouts: ICICI OpTransactionHistory (more added by
   dropping a YAML + parser into `backend/processor/bsa/`).

## Prerequisites (on your machine)

- AWS account + credentials configured (`aws sts get-caller-identity` works),
  with rights to create the resources above
- Node.js 18+ and the CDK CLI: `npm install -g aws-cdk`
- Python 3.11+
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

## Smoke test

Upload one of the sample ICICI PDFs. Within ~15 seconds the job should show
**done** with a green *balance passed* chip, category chips, a working
preview, and all three downloads. Then upload any non-ICICI PDF and confirm
it fails gracefully with the "layout not in registry" message.

## Operating notes

- Logs: CloudWatch log groups for `BsaStack-Processor…` and `BsaStack-ApiFn…`.
- A failed job's error message is stored on the job record and shown in the UI.
- Costs at MVP volume: single-digit dollars/month (CloudFront + Lambda +
  DynamoDB are effectively free-tier; S3 pennies).
- Tear down: `cdk destroy` — the data bucket and jobs table are `RETAIN`ed
  on purpose; delete them manually if you truly want the data gone.

## What's next (Phase 1.5 → 2)

1. Wire Bedrock into `backend/processor/bsa/extract/llm_fallback.py`
   (interface + prompt are already there) → "any bank" support.
2. Cognito user pool + JWT authorizer on the API + login on the UI.
3. More template layouts (HDFC, SBI, Axis, Kotak…) as YAML + parser pairs.
4. Human-review screen for `needs_review` jobs.
5. Step Functions split once the LLM path makes stage retries valuable.
