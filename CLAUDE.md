# Bank Statement Analyser — project context

SaaS that extracts + categorizes transactions from Indian bank/NBFC statement
PDFs. Deployed and working: https://dg3uwro4b2d2l.cloudfront.net (NO auth yet —
keep URL private). Stack `BsaStack` in ap-south-1, account 681832767155.

## Layout
- backend/processor/bsa/ — the pipeline package (ingest → classify → extract →
  normalize → categorize → validate → publish). Layouts live in bsa/layouts/*.yaml
  and are matched by page-1 fingerprints; LLM fallback in extract/llm_fallback.py
  handles anything unrecognised.
- backend/processor/bsa/extract/ — two kinds of parser:
  - generic_layout.py — one parser driven entirely by a layout YAML. Prefer this.
    Used by Axis (`parser: generic` in the descriptor).
  - icici_optransactionhistory.py — bank-specific module, for layouts YAML cannot
    express (ICICI marks a row's title line by font face).
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
6. LLM calls should go through Bedrock only — statement data must not leave the
   AWS account. CURRENTLY VIOLATED BY DESIGN: Bedrock is blocked on this account
   (every Anthropic model fails its AWS Marketplace subscription with
   INVALID_PAYMENT_INSTRUMENT), so extraction runs against the Anthropic API
   directly with a key in Secrets Manager (`bsa/llm-api-key`), selected by
   LLM_PROVIDER / LLM_MODEL. This must be resolved before real customers upload
   their statements — same gate as Cognito auth.
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
- ICICI OpTransactionHistory — bank-specific module, 225 rows, passed.
- Axis account statement — generic YAML layout, up to 1782 rows, passed, ~3s.
- Any other bank — LLM fallback (Anthropic direct, see gotcha 6), ~98% accurate,
  minutes and paid per statement. Treat as a stopgap until a layout exists.

## Current next steps
1. Cognito auth (user pool + JWT authorizer + login UI) before sharing the URL,
   together with the gotcha-6 data-residency fix. Both gate real customers.
2. More bank layouts (HDFC, SBI, Kotak) as YAML descriptors — needs one sample
   statement per bank.
3. S3-backed layout registry so a bank can be added without a redeploy
   (registry.py currently globs a read-only directory inside the bundle).
4. Human-review screen for needs_review jobs.
5. A Lambda timeout still leaves a job stuck on "processing" — extraction now
   bails early via get_remaining_time_in_millis, but a hard kill anywhere else
   still cannot write status. A DLQ or status sweeper would close this.
