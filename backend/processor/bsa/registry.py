"""Layout registry — bundled descriptors, overlaid by ones stored in S3.

Two sources, in this order:

  1. `bsa/layouts/*.yaml` inside the deployment bundle. These are pinned to a
     release and are what a fresh deploy always has.
  2. `s3://$DATA_BUCKET/$LAYOUTS_PREFIX*.yaml`. Same shape, but writable at
     runtime, so a new bank can be added — or a broken descriptor corrected —
     without a redeploy. An S3 descriptor whose `id` matches a bundled one
     replaces it, which is what makes a hotfix possible.

Adding a bank is the item that scales with the customer list, and a CDK deploy
per bank made every addition a release. Manage them with scripts/manage_layouts.py.

Failure policy: this registry must never take the pipeline down. If S3 is
unreachable, or one descriptor is malformed, the affected layouts are logged
and skipped and everything else still loads — the worst case degrades to the
bundled set, which is exactly the behaviour before S3 was involved.
"""
from __future__ import annotations

import glob
import os
import time

import yaml

_LAYOUT_DIR = os.path.join(os.path.dirname(__file__), "layouts")

# Parser modes a descriptor may select. A descriptor arrives from S3 at
# runtime, so this is an allow-list, not a hint: it must not be able to name
# an arbitrary import. Module-backed layouts (`module`, or no parser at all)
# are resolved by pipeline.TEMPLATE_PARSERS, itself keyed by layout id.
PARSER_MODES = frozenset({"generic", "columnar", "grouped", "module"})

# A container is reused across invocations, so without a TTL a new layout would
# only be seen by cold starts — unpredictably, for up to hours.
CACHE_TTL_S = int(os.environ.get("LAYOUT_CACHE_TTL_S", "300"))

_cache: dict[str, dict] | None = None
_cache_at: float = 0.0


class LayoutError(ValueError):
    """A descriptor is not usable as a layout."""


def validate_descriptor(d, origin: str = "") -> dict:
    """Return `d` if it is a usable descriptor, else raise LayoutError.

    Checks only what the loader itself relies on — id, bank, a parser mode we
    recognise, and at least one fingerprint. Per-parser geometry is the
    parser's business and fails loudly at extraction time, where the error can
    name the actual page and column.
    """
    where = f" in {origin}" if origin else ""
    if not isinstance(d, dict):
        raise LayoutError(f"descriptor{where} is {type(d).__name__}, not a mapping")
    lid = d.get("id")
    if not isinstance(lid, str) or not lid.strip():
        raise LayoutError(f"descriptor{where} has no string 'id'")
    if not isinstance(d.get("bank"), str) or not d["bank"].strip():
        raise LayoutError(f"layout {lid!r}{where} has no string 'bank'")
    parser = d.get("parser", "module")
    if parser not in PARSER_MODES:
        raise LayoutError(f"layout {lid!r}{where} has parser {parser!r}; "
                          f"expected one of {sorted(PARSER_MODES)}")
    fp = d.get("fingerprints") or {}
    if not isinstance(fp, dict):
        raise LayoutError(f"layout {lid!r}{where} has non-mapping 'fingerprints'")
    any_of, all_of = fp.get("any_of") or [], fp.get("all_of") or []
    if not isinstance(any_of, list) or not isinstance(all_of, list):
        raise LayoutError(f"layout {lid!r}{where} fingerprints must be lists")
    if not any_of and not all_of:
        # A descriptor with no fingerprint matches nothing, so it would sit in
        # the registry looking installed while never being selected.
        raise LayoutError(f"layout {lid!r}{where} has no fingerprints, so it "
                          f"can never match a statement")
    return d


def _load_bundled() -> list[tuple[str, dict]]:
    out = []
    for path in sorted(glob.glob(os.path.join(_LAYOUT_DIR, "*.yaml"))):
        try:
            with open(path) as fh:
                d = validate_descriptor(yaml.safe_load(fh), os.path.basename(path))
        except (LayoutError, yaml.YAMLError, OSError) as e:
            # A bundled layout is release-pinned, so this is a build error —
            # loud, but still not worth failing every other bank over.
            print(f"registry: skipping bundled {os.path.basename(path)}: {e}")
            continue
        out.append((d["id"], d))
    return out


def _load_s3() -> list[tuple[str, dict]]:
    bucket = os.environ.get("DATA_BUCKET")
    if not bucket or os.environ.get("LAYOUTS_FROM_S3", "1").strip().lower() in \
            ("0", "false", "no", "off"):
        return []
    prefix = os.environ.get("LAYOUTS_PREFIX", "layouts/")
    out = []
    try:
        import boto3
        s3 = boto3.client("s3")
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not key.endswith((".yaml", ".yml")):
                    continue
                try:
                    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
                    d = validate_descriptor(yaml.safe_load(body), key)
                except Exception as e:                        # noqa: BLE001
                    print(f"registry: skipping s3://{bucket}/{key}: {e}")
                    continue
                out.append((d["id"], d))
    except Exception as e:                                    # noqa: BLE001
        # Degrade to the bundled set rather than failing the job. A bank that
        # only exists in S3 will fail its own extraction with a clear "no
        # layout" error, which is the same outcome as never having added it.
        print(f"registry: could not read layouts from s3://{bucket}/{prefix}: {e}")
    return out


def all_layouts(force: bool = False) -> dict[str, dict]:
    """Every usable layout, keyed by id, in deterministic match order.

    Order matters: classify() takes the first fingerprint match, so two layouts
    that could both match a statement must resolve the same way on every
    invocation. Sorted by descending `priority` (default 0) then id, so a more
    specific descriptor can be made to win by raising its priority — glob order
    used to decide this, which is to say nothing decided it.
    """
    global _cache, _cache_at
    if force or _cache is None or (time.time() - _cache_at) > CACHE_TTL_S:
        merged: dict[str, dict] = {}
        for lid, d in _load_bundled() + _load_s3():   # S3 wins on id collision
            merged[lid] = d
        _cache = dict(sorted(merged.items(),
                             key=lambda kv: (-int(kv[1].get("priority", 0) or 0),
                                             kv[0])))
        _cache_at = time.time()
    return _cache


def get_layout(layout_id: str) -> dict:
    return all_layouts()[layout_id]
