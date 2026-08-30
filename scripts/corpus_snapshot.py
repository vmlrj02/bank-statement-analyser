#!/usr/bin/env python3
"""Corpus snapshot — catch quality drift that every other check is blind to.

WHY THIS EXISTS. On 30 August a change put thirty characters of branch
boilerplate onto 145 of 192 SBI rows and turned one of them into a
counterparty. It shipped. Balances reconciled (the amounts were untouched),
506 unit tests passed (they only assert what someone already thought of), and
the categorisation harness stayed at 322/322 (a closed set of hand-written
cases). The reviewer found it in a screenshot, hours later.

Nothing we had could see it, because every check we had was about
CORRECTNESS OF A KNOWN THING. This one is about the SHAPE OF EVERYTHING: run
the corpus, record what the output looks like in aggregate, and shout when it
moves. That regression would have read as SBI's mean description length
jumping 30 characters — impossible to miss.

    python scripts/corpus_snapshot.py --update   # record a new baseline
    python scripts/corpus_snapshot.py            # compare, exit 1 on drift

The statements carry live account data and cannot be committed, so the corpus
is a directory you point at (same convention as tests/test_layout_samples.py):

    BSA_CORPUS_DIR=~/Downloads/Samples-3 python scripts/corpus_snapshot.py

The BASELINE is committed — it is only numbers — so a drift shows up in a diff
and a deliberate improvement is a reviewable change to it.

FILE SELECTION IS FIXED, not "everything in the folder", because a snapshot
that changes when someone drops a new PDF in is worthless. Sorted, capped per
bank, capped by size: the same files today and next month.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import warnings
from collections import Counter, defaultdict

warnings.filterwarnings("ignore")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend", "processor"))

BASELINE = os.path.join(HERE, "..", "tests", "data", "corpus_snapshot.json")

# Selection is BY LAYOUT, and the chosen files are recorded in the baseline.
#
# Two earlier attempts failed the only test that matters — reinstating the SBI
# regression this exists to catch, and seeing whether it shouts. Taking the two
# smallest files per bank missed it (small statements are short, clean and
# single-page: the opposite of where parsing breaks). Spreading four files per
# bank across the size range missed it too, and that failure is the instructive
# one: SBI alone has SEVEN layouts, so no per-BANK quota can cover them.
#
# What regresses is a LAYOUT. So the sample takes files per layout id, and
# --update writes the resulting file list into the baseline. That makes the
# selection explicit and reviewable — you can read which statements the gate
# actually watches — and makes every later run measure exactly those files
# rather than re-deriving a set that drifts as the folder changes.
PER_LAYOUT = 2
MAX_BYTES = 8_000_000

# How much a metric may move before it is called drift. Tuned to sit BELOW the
# regression that prompted this (SBI's description length moved ~30 chars, its
# party rate several points) and ABOVE the noise of an ordinary rule change.
THRESHOLDS = {
    "rows": 0,              # any change in row count is structural — never noise
    "not_reconciling": 0,   # any new reconciliation failure
    "party_named_pct": 1.0,
    "mean_desc_len": 3.0,   # characters
    "category_mix_pct": 3.0,
}


def _all_candidates(root: str):
    out = []
    for d in sorted(os.listdir(root)):
        fp = os.path.join(root, d)
        if not os.path.isdir(fp):
            continue
        for dp, _, g in os.walk(fp):
            for f in sorted(g):
                if not f.lower().endswith(".pdf"):
                    continue
                full = os.path.join(dp, f)
                if os.path.getsize(full) <= MAX_BYTES:
                    out.append((d, full))
    return sorted(out, key=lambda x: (x[0], os.path.getsize(x[1]), x[1]))


def _parse(path: str):
    """(bank_layout_id, txns) or (None, None) if the file cannot be read."""
    from bsa.categorize import categorize
    from bsa.normalize import normalize
    from bsa.pipeline import extract_one
    try:
        ex = extract_one(path, filename=os.path.basename(path))
        txns = normalize(ex)
        categorize(txns)
        return ex.meta.layout, txns
    except Exception:
        return None, None


def choose(root: str):
    """Pick PER_LAYOUT files for every layout present in the corpus.

    Deliberately parses everything once — the only reliable way to know which
    layout a statement uses is to classify it, and a cheaper guess is what let
    two earlier versions of this sample miss the file that regressed.
    """
    by_layout = defaultdict(list)
    for bank, path in _all_candidates(root):
        layout, txns = _parse(path)
        if layout and txns:
            # Keep the parsed rows: --update would otherwise parse the whole
            # corpus, then parse the chosen files a second time.
            by_layout[layout].append((bank, path, txns))
    chosen = []
    for layout in sorted(by_layout):
        chosen += [(b, p, layout, t) for b, p, t in by_layout[layout][:PER_LAYOUT]]
    return chosen


def measure(items) -> dict:
    """Aggregate the output of a fixed file list, keyed by LAYOUT.

    Keyed by layout rather than by bank because that is the unit that breaks:
    "SBI moved" is ambiguous when SBI has seven layouts, while
    "sbi_soa_internet moved" points straight at the descriptor to look at.
    """
    from bsa.normalize import party_kind
    from bsa.publish import category_label
    from bsa.validate import validate

    per = defaultdict(lambda: {
        "files": 0, "rows": 0, "not_reconciling": 0, "unreadable_files": 0,
        "kinds": Counter(), "cats": Counter(), "desc_lens": [],
    })
    for bank, path, layout, *cached in items:
        b = per[layout]
        txns = cached[0] if cached else _parse(path)[1]
        if txns is None:
            # A file that stops parsing IS the signal — recorded, not fatal, so
            # one broken statement cannot mask the rest of the run.
            b["unreadable_files"] += 1
            continue
        b["files"] += 1
        if validate(txns).status != "passed":
            b["not_reconciling"] += 1
        b["rows"] += len(txns)
        for t in txns:
            b["kinds"][party_kind(t.counterparty, t.description)] += 1
            b["cats"][category_label(t)] += 1
            b["desc_lens"].append(len(t.description or ""))

    out = {}
    for name, b in sorted(per.items()):
        rows = b["rows"] or 1
        nameable = rows - b["kinds"]["na"]
        out[name] = {
            "files": b["files"],
            "unreadable_files": b["unreadable_files"],
            "rows": b["rows"],
            "not_reconciling": b["not_reconciling"],
            "party_named_pct": round(100 * b["kinds"]["named"] / rows, 1),
            "party_nameable_pct": round(
                100 * (b["kinds"]["named"] + b["kinds"]["handle"])
                / max(nameable, 1), 1),
            "mean_desc_len": round(statistics.fmean(b["desc_lens"]), 1)
                             if b["desc_lens"] else 0.0,
            # Only the categories carrying weight: a long tail of one-row
            # labels would make every run look different for no reason.
            "top_categories": {k: round(100 * v / rows, 1)
                               for k, v in b["cats"].most_common(6)},
        }
    return out


def _drift(base: dict, now: dict) -> list[str]:
    out = []
    for key in sorted(set(base) | set(now)):
        b, n = base.get(key), now.get(key)
        if b is None:
            out.append(f"{key}: NEW layout in this run")
            continue
        if n is None:
            out.append(f"{key}: MISSING from this run — corpus changed?")
            continue
        for metric, tol in THRESHOLDS.items():
            if metric == "category_mix_pct":
                continue
            bv, nv = b.get(metric, 0), n.get(metric, 0)
            if abs(nv - bv) > tol:
                out.append(f"{key}: {metric} {bv} -> {nv}")
        tol = THRESHOLDS["category_mix_pct"]
        for c in sorted(set(b["top_categories"]) | set(n["top_categories"])):
            bv = b["top_categories"].get(c, 0.0)
            nv = n["top_categories"].get(c, 0.0)
            if abs(nv - bv) > tol:
                out.append(f"{key}: category {c!r} {bv}% -> {nv}%")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="corpus snapshot / drift gate")
    ap.add_argument("--update", action="store_true",
                    help="re-select files and record a new baseline")
    ap.add_argument("--corpus", default=os.environ.get("BSA_CORPUS_DIR"))
    args = ap.parse_args()

    root = os.path.expanduser(args.corpus) if args.corpus else None
    if not root or not os.path.isdir(root):
        print("no corpus: set BSA_CORPUS_DIR to a folder of statement PDFs "
              "(skipping, not failing — CI has no statements either)")
        return 0

    if args.update:
        chosen = choose(root)
        snap = {
            "files": [{"layout": lay, "bank": bank,
                       "path": os.path.relpath(p, root)}
                      for bank, p, lay, _t in chosen],
            "per_layout": measure(chosen),
        }
        os.makedirs(os.path.dirname(BASELINE), exist_ok=True)
        with open(BASELINE, "w") as fh:
            json.dump(snap, fh, indent=1, sort_keys=True)
            fh.write("\n")
        print(f"baseline written: {len(snap['files'])} files, "
              f"{len(snap['per_layout'])} layouts")
        for lay, m in snap["per_layout"].items():
            print(f"  {lay:<32} {m['rows']:>6} rows  "
                  f"party {m['party_named_pct']:>5}%  desc {m['mean_desc_len']}")
        return 0

    if not os.path.exists(BASELINE):
        print("no baseline yet — run with --update")
        return 1
    with open(BASELINE) as fh:
        base = json.load(fh)

    items, missing = [], []
    for f in base["files"]:
        full = os.path.join(root, f["path"])
        (items if os.path.exists(full) else missing).append(
            (f["bank"], full, f["layout"]) if os.path.exists(full) else f["path"])
    if missing:
        print(f"{len(missing)} baseline file(s) not in this corpus — "
              f"comparison is partial:")
        for m in missing[:5]:
            print(f"  {m}")

    drift = _drift(base["per_layout"], measure(items))
    if not drift:
        print(f"no drift across {len(items)} files / {len(base['per_layout'])} layouts")
        return 0
    print("DRIFT — the output moved. Check every line is a change you meant:\n")
    for line in drift:
        print(f"  {line}")
    print("\nIf all of it is intended, re-record with --update and commit the "
          "baseline, so the change is visible in review.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
