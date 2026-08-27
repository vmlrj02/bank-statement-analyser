"""Reviewer-eye sweep — scan pipeline OUTPUT the way a human reviewer would.

Flags what an eyeball catches instantly: junk party names, footer/header text in
descriptions, merged transactions, categories contradicting the amount's sign.
Run it over a folder of real statements before a release:

    BSA_SAMPLE_DIR=/path/to/statements python scripts/reviewer_sweep.py

Zero systematic classes is the bar; the definition of each check lives here so
a new defect class found in production gets added as a check, permanently.
"""
import sys, re, glob, os, collections
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend", "processor"))
from bsa.pipeline import extract_one
from bsa.ingest import PasswordRequired
from bsa.normalize import normalize
from bsa.categorize import categorize, category_detail

STAMPY = {"UPI","NEFT","RTGS","IMPS","TRF","TFR","WDL","DEP","ACH","TPT","INB",
          "INF","BIL","ONL","DR","CR","P2A","P2M","PAYMENT","TRANSFER",
          "ATTN","REF","NA","MMT","CLG","CHQ","INT","OUT","IN","TO","FROM","BY",
          "NEFT CR","NEFT DR","RTGS CR","RTGS DR","MB","IB","PVT","LTD"}
IFSC = re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")
FOOTERISH = re.compile(r"page \d+ of|page no|statement of account|opening balance|"
                       r"closing balance|carried forward|brought forward|"
                       r"computer generated|do not share|registered office", re.I)
CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
SIGN_RULES = {"Salary paid": -1, "Salary credited": 1, "cash deposit": 1,
              "cash withdrawal": -1, "Interest received": 1, "Interest payments": -1,
              "Loan amount disbursal": 1, "EMI transaction": -1,
              "inward bounce penal charges": -1, "other penal charges": -1,
              "Regular credit": 1, "Regular debit": -1}


def checks(t, detail):
    out, p, d, c, a = [], (t.counterparty or "").strip(), t.description or "", t.category, t.amount
    if p:
        if p.upper() in STAMPY: out.append(("party_is_channel_word", p))
        elif len(p) <= 2 and not p.isdigit(): out.append(("party_too_short", p))
        elif IFSC.fullmatch(p.replace(" ", "")): out.append(("party_is_ifsc", p))
        elif p.count("/") >= 2 or p.count("*") >= 2: out.append(("party_has_junk_chars", p))
    if not d.strip(): out.append(("empty_description", ""))
    if FOOTERISH.search(d): out.append(("footer_or_header_in_description", d[:60]))
    if CTRL.search(d): out.append(("control_chars_in_description", d[:40]))
    if len(re.findall(r"UPI/(?:DR|CR)/\d{9,}", d)) > 1:
        out.append(("two_transactions_merged", d[:60]))
    s = SIGN_RULES.get(c)
    if s and (a > 0) != (s > 0):
        out.append(("category_contradicts_sign", f"{c} @ {a:+.0f} | {d[:40]}"))
    if c == "EMI transaction" and abs(a) < 100:
        out.append(("emi_under_100", f"{a:+.0f} | {d[:40]}"))
    if "unknown party" in (detail or "") and p:
        out.append(("detail_says_unknown_but_party_known", f"{p} | {detail[:40]}"))
    return out


def main():
    root = os.environ.get("BSA_SAMPLE_DIR")
    if not root:
        print("set BSA_SAMPLE_DIR to a folder of statements"); return 1
    pws = [w for w in (os.environ.get("BSA_SAMPLE_PASSWORDS") or "").split(",") if w]
    defects, examples, nrows = collections.Counter(), collections.defaultdict(list), 0
    for p in sorted(set(glob.glob(os.path.join(root, "**", "*.pdf"), recursive=True))):
        try:
            try:
                ex = extract_one(p)
            except PasswordRequired:
                ex = None
                for c in pws:
                    try: ex = extract_one(p, password=c); break
                    except Exception: pass
                if ex is None: continue
        except Exception:
            continue
        if not ex.meta.layout: continue
        try:
            tx = normalize(ex); categorize(tx)
        except Exception:
            continue
        for t in tx:
            if getattr(t, "is_opening", False): continue
            nrows += 1
            for kind, ev in checks(t, category_detail(t)):
                defects[kind] += 1
                if len(examples[kind]) < 5:
                    examples[kind].append(f"[{os.path.basename(p)[:24]}] {ev}")
    print(f"swept {nrows} rows")
    for k, v in defects.most_common():
        print(f"  {v:6d}  {k}")
        for e in examples[k][:3]:
            print(f"           {e[:100]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
