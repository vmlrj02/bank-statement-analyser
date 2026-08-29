#!/usr/bin/env python3
"""Layout queue: the operator loop for banks with no layout yet.

An upload from an unknown bank fails, by design, with "this bank has no
layout yet" — the LLM fallback is closed and the answer is a YAML layout,
written once per bank. This script is the loop around that:

    python scripts/layout_queue.py list                # what is waiting
    python scripts/layout_queue.py fetch <job> [idx]   # pull the sample PDF
    python scripts/layout_queue.py retry <job> <idx>   # after the layout is live

`fetch` downloads the failed file to ./layout-work/ so a layout can be
written against it (copy axis_account_statement.yaml as the model, then
manage_layouts.py validate + put — the S3 registry serves it without a
deploy). `retry` clears that file's failure and hands it back to the
processor; the normal merge machinery (with the sweeper as backstop)
republishes the job.

DATA RESIDENCY: `fetch` moves a customer's statement OUT of the AWS account
onto this machine. Use a consented or redacted sample for real customers.

Resource names come from the BsaStack CloudFormation outputs.
"""
import argparse
import json
import os
import sys
import time

import boto3

REGION = os.environ.get("AWS_REGION", "ap-south-1")
NEEDLE = "no layout"


def outputs():
    cf = boto3.client("cloudformation", region_name=REGION)
    outs = cf.describe_stacks(StackName="BsaStack")["Stacks"][0]["Outputs"]
    return {o["OutputKey"]: o["OutputValue"] for o in outs}


def jobs_table(o):
    return boto3.resource("dynamodb", region_name=REGION).Table(o["JobsTableName"])


def iter_jobs(table):
    lek = None
    while True:
        kw = {}
        if lek:
            kw["ExclusiveStartKey"] = lek
        r = table.scan(**kw)
        yield from r.get("Items", [])
        lek = r.get("LastEvaluatedKey")
        if not lek:
            return


def cmd_list(args):
    o = outputs()
    rows = []
    for it in iter_jobs(jobs_table(o)):
        for idx, f in (it.get("failed_files") or {}).items():
            if NEEDLE in (f.get("error") or "").lower():
                rows.append((int(it.get("created_at", 0)), it["job_id"],
                             str(idx), f.get("filename", ""), it.get("owner", "")))
    rows.sort(reverse=True)
    if not rows:
        print("layout queue is empty — no failure mentions a missing layout")
        return
    for ts, job, idx, fn, owner in rows:
        when = time.strftime("%d %b %H:%M", time.localtime(ts))
        print(f"{when}  {job}  idx={idx}  {fn}  ({owner})")
    print(f"\n{len(rows)} file(s) waiting on a layout. Next:")
    print("  python scripts/layout_queue.py fetch <job_id> [idx]")


def cmd_fetch(args):
    o = outputs()
    it = jobs_table(o).get_item(Key={"job_id": args.job}).get("Item")
    if not it:
        sys.exit(f"no job {args.job}")
    failed = it.get("failed_files") or {}
    idx = args.idx
    if idx is None:
        if len(failed) == 1:
            idx = next(iter(failed))
        else:
            sys.exit(f"job has {len(failed)} failed files — pass an idx from: "
                     f"{sorted(failed)}")
    f = failed.get(str(idx)) or {}
    key = f"uploads/{args.job}/{idx}.pdf"
    os.makedirs("layout-work", exist_ok=True)
    safe = (f.get("filename") or f"{idx}.pdf").replace("/", "_")
    path = os.path.join("layout-work", f"{args.job[:8]}-{idx}-{safe}")
    boto3.client("s3", region_name=REGION).download_file(o["DataBucketName"], key, path)
    print(f"sample -> {path}")
    print("\nnext:")
    print("  1. write the descriptor — copy backend/processor/bsa/layouts/"
          "axis_account_statement.yaml as the model")
    print("  2. python scripts/manage_layouts.py validate <yaml>")
    print("     python scripts/manage_layouts.py put      <yaml>")
    print(f"  3. python scripts/layout_queue.py retry {args.job} {idx}")


def cmd_retry(args):
    o = outputs()
    t = jobs_table(o)
    it = t.get_item(Key={"job_id": args.job}).get("Item")
    if not it:
        sys.exit(f"no job {args.job}")
    if str(args.idx) not in (it.get("failed_files") or {}):
        sys.exit(f"idx {args.idx} is not a failed file on this job")
    # Clear the failure and put the job back in processing; the processor
    # re-extracts this one file and the normal merge machinery republishes.
    t.update_item(
        Key={"job_id": args.job},
        UpdateExpression="SET #s = :p, updated_at = :t REMOVE failed_files.#i, #e",
        ExpressionAttributeNames={"#s": "status", "#i": str(args.idx), "#e": "error"},
        ExpressionAttributeValues={":p": "processing", ":t": int(time.time())},
    )
    lam = boto3.client("lambda", region_name=REGION)
    env = lam.get_function_configuration(
        FunctionName=o["SweeperFunctionName"])["Environment"]["Variables"]
    processor = next(v for k, v in env.items() if "PROCESSOR" in k.upper())
    lam.invoke(FunctionName=processor, InvocationType="Event",
               Payload=json.dumps({"Records": [{"s3": {"object": {
                   "key": f"uploads/{args.job}/{args.idx}.pdf"}}}]}).encode())
    print("re-driven — the job is back in processing and republishes when done.")
    print("NOTE a warm container may serve the OLD layout set for up to "
          "5 minutes (LAYOUT_CACHE_TTL_S); if it fails with the same error, "
          "wait and retry once more.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="failed files waiting on a layout")
    p = sub.add_parser("fetch", help="download the sample PDF to ./layout-work/")
    p.add_argument("job")
    p.add_argument("idx", nargs="?", default=None)
    p = sub.add_parser("retry", help="clear the failure and reprocess the file")
    p.add_argument("job")
    p.add_argument("idx")
    args = ap.parse_args()
    {"list": cmd_list, "fetch": cmd_fetch, "retry": cmd_retry}[args.cmd](args)


if __name__ == "__main__":
    main()
