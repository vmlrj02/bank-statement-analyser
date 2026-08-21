"""CLI: python -m bsa <statement.pdf> [more.pdf ...] -o outdir"""
from __future__ import annotations

import argparse
import sys

from .pipeline import run


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bsa", description="Bank Statement Analyser — Phase 0")
    ap.add_argument("pdfs", nargs="+", help="statement PDF(s); multiple files of the same account are merged + deduped")
    ap.add_argument("-o", "--out", default="out", help="output directory")
    ap.add_argument("-p", "--password", default=None, help="PDF password if protected")
    ap.add_argument("--related-party", action="append", default=[],
                    help="related-party name (repeatable) for loan-application context")
    ap.add_argument("--basename", default="statement")
    args = ap.parse_args(argv)

    result = run(args.pdfs, args.out, password=args.password,
                 related_parties=args.related_party, basename=args.basename)
    v = result.validation
    print(f"account   : {result.meta.account_no} ({result.meta.account_name})")
    print(f"rows      : {len(result.txns)}")
    print(f"validation: {v.status} ({v.checked_rows} rows checked, {len(v.issues)} issues)")
    for issue in v.issues[:10]:
        print("  -", issue.kind, "|", issue.detail)
    print(f"outputs in: {args.out}/")
    return 0 if v.status != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
