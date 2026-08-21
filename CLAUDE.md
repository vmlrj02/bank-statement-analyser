# Bank Statement Analyser — project context

SaaS that extracts + categorizes transactions from Indian bank/NBFC statement
PDFs. Deployed and working: https://dg3uwro4b2d2l.cloudfront.net (NO auth yet —
keep URL private). Stack `BsaStack` in ap-south-1, account 681832767155.

## Layout
- backend/processor/bsa/ — the pipeline package (ingest → classify → extract →
  normalize → categorize → validate → publish). Template parser: ICICI
  OpTransactionHistory. LLM fallback: Claude on Bedrock (extract/llm_fallback.py),
  model from env BEDROCK_MODEL_ID.
- backend/api/handler.py — jobs API (presigned S3 upload/download, DynamoDB).
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

## Current next steps
1. Test LLM fallback with a non-ICICI bank PDF (Bedrock model access must be
   enabled once in the ap-south-1 console).
2. Cognito auth (user pool + JWT authorizer + login UI) before sharing the URL.
3. More bank template layouts (HDFC, SBI, Axis, Kotak) as YAML + parser pairs.
4. Human-review screen for needs_review jobs.
