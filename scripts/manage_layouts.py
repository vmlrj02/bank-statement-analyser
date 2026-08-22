#!/usr/bin/env python3
"""Add, replace, list or remove a bank layout without a redeploy.

Layouts live in two places. The bundled ones under
backend/processor/bsa/layouts/ ship with a release; the ones this script
manages live in S3 and are read by the processor at runtime (within
LAYOUT_CACHE_TTL_S, five minutes by default). An S3 layout whose `id` matches a
bundled one replaces it, so this is also how a broken descriptor gets fixed
without cutting a release.

    python scripts/manage_layouts.py validate path/to/hdfc_savings.yaml
    python scripts/manage_layouts.py put      path/to/hdfc_savings.yaml
    python scripts/manage_layouts.py list
    python scripts/manage_layouts.py rm       hdfc_savings

`put` validates before uploading, always. A descriptor that cannot be parsed is
skipped at runtime with a log line nobody reads, so the check belongs here,
where the person who wrote it is still watching.

The bucket comes from the stack output DataBucketName, or --bucket.
"""
import argparse
import os
import sys

import boto3
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "backend", "processor"))
from bsa.registry import LayoutError, validate_descriptor   # noqa: E402

DEFAULT_PREFIX = "layouts/"


def bucket_name(name, region):
    if name:
        return name
    cf = boto3.client("cloudformation", region_name=region)
    outs = cf.describe_stacks(StackName="BsaStack")["Stacks"][0]["Outputs"]
    return next(o["OutputValue"] for o in outs if o["OutputKey"] == "DataBucketName")


def load_and_check(path):
    with open(path) as fh:
        raw = fh.read()
    d = validate_descriptor(yaml.safe_load(raw), os.path.basename(path))
    return d, raw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["validate", "put", "list", "rm"])
    ap.add_argument("target", nargs="?", help="YAML path (validate/put) or layout id (rm)")
    ap.add_argument("--bucket")
    ap.add_argument("--prefix", default=os.environ.get("LAYOUTS_PREFIX", DEFAULT_PREFIX))
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "ap-south-1"))
    a = ap.parse_args()

    if a.action == "validate":
        if not a.target:
            sys.exit("path to a YAML descriptor required")
        try:
            d, _ = load_and_check(a.target)
        except (LayoutError, yaml.YAMLError) as e:
            sys.exit(f"INVALID: {e}")
        print(f"ok — {d['id']}  ({d['bank']} / {d.get('layout_name', '?')}, "
              f"parser {d.get('parser', 'module')})")
        return

    s3 = boto3.client("s3", region_name=a.region)
    bucket = bucket_name(a.bucket, a.region)

    if a.action == "list":
        # Show both sources, and say which S3 entries are shadowing a bundled
        # one — an override that nobody remembers making is the failure mode.
        from bsa.registry import _load_bundled
        bundled = {lid for lid, _ in _load_bundled()}
        print(f"bundled ({len(bundled)}):")
        for lid in sorted(bundled):
            print(f"  {lid}")
        keys = []
        for page in s3.get_paginator("list_objects_v2").paginate(
                Bucket=bucket, Prefix=a.prefix):
            keys += [o["Key"] for o in page.get("Contents", [])
                     if o["Key"].endswith((".yaml", ".yml"))]
        print(f"\ns3://{bucket}/{a.prefix} ({len(keys)}):")
        if not keys:
            print("  (none)")
        for key in sorted(keys):
            try:
                d = validate_descriptor(
                    yaml.safe_load(s3.get_object(Bucket=bucket, Key=key)["Body"].read()),
                    key)
                note = "  [OVERRIDES BUNDLED]" if d["id"] in bundled else ""
                print(f"  {d['id']:38} {d['bank']}{note}")
            except Exception as e:                          # noqa: BLE001
                print(f"  {key:38} UNUSABLE — {e}")
        return

    if a.action == "put":
        if not a.target:
            sys.exit("path to a YAML descriptor required")
        try:
            d, raw = load_and_check(a.target)
        except (LayoutError, yaml.YAMLError) as e:
            sys.exit(f"refusing to upload an invalid descriptor: {e}")
        key = f"{a.prefix}{d['id']}.yaml"
        s3.put_object(Bucket=bucket, Key=key, Body=raw.encode(),
                      ContentType="application/x-yaml")
        print(f"put s3://{bucket}/{key}  ({d['bank']} / "
              f"{d.get('layout_name', '?')}, parser {d.get('parser', 'module')})")
        print(f"live within {os.environ.get('LAYOUT_CACHE_TTL_S', '300')}s "
              f"on already-warm Lambdas; immediately on a cold start.")
        return

    if a.action == "rm":
        if not a.target:
            sys.exit("layout id required")
        key = f"{a.prefix}{a.target}.yaml"
        s3.delete_object(Bucket=bucket, Key=key)
        print(f"removed s3://{bucket}/{key}")
        print("if a bundled layout shares this id it becomes active again.")


if __name__ == "__main__":
    main()
