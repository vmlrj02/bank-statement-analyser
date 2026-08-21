"""LLM extraction fallback — provider-agnostic.

Used when classify() finds no known layout, or when the PDF is scanned.
Digital PDFs send page text; scanned PDFs send page images (vision).
Output is forced through a JSON schema, so no free-text parsing is needed.
The balance validator downstream is the correctness gate for this path.

This module owns *what* to ask for — chunking, prompt, schema, assembly. It
does not know which vendor answers: llm_providers.call_structured() picks that
from LLM_PROVIDER (gemini | anthropic | openai | bedrock) at call time, so
switching provider or model is an env change, not a code edit.
"""
from __future__ import annotations

import io

import pdfplumber

from ..models import RawRow, StatementMeta, StatementExtract
from .llm_providers import call_structured

PAGES_PER_CHUNK_TEXT = 4
PAGES_PER_CHUNK_VISION = 2
IMAGE_DPI = 180

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "bank": {"type": "string", "description": "Bank or NBFC name as printed"},
        "account_no": {"type": "string"},
        "account_name": {"type": "string"},
        "period_from": {"type": "string", "description": "ISO date or empty"},
        "period_to": {"type": "string", "description": "ISO date or empty"},
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "exactly as printed"},
                    "cheque_no": {"type": "string"},
                    "description": {"type": "string"},
                    "withdrawal": {"type": ["number", "null"]},
                    "deposit": {"type": ["number", "null"]},
                    "balance": {"type": "number"},
                },
                "required": ["date", "description", "balance"],
            },
        },
    },
    "required": ["bank", "account_no", "rows"],
}

SYSTEM_PROMPT = """You extract transactions from Indian bank/NBFC statements.
Rules:
- One output row per transaction row printed in the statement. Never merge, skip, or invent rows.
- Copy the running balance exactly as printed; it is used to arithmetically verify your work.
- Amounts: plain numbers, no separators (Indian formats like 5,03,72,235.09 become 50372235.09).
- Direction comes from Withdrawal/Deposit columns, Dr/Cr suffixes, or parentheses. Put debits in `withdrawal`, credits in `deposit`, never both.
- Dates: copy as printed; do not reformat.
- Multi-line descriptions: join with single spaces in reading order, including any bold title line above the row.
- Skip opening-balance, brought-forward, totals, and summary rows — transactions only.
- If a page contains no transaction rows, return an empty rows list for it."""


def _page_images(pdf, page_indexes: list[int]) -> list[bytes]:
    """Render pages to PNG via pypdfium2 (already a pdfplumber dependency)."""
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(pdf.stream.name if hasattr(pdf.stream, "name") else pdf.path)
    out = []
    for i in page_indexes:
        bitmap = doc[i].render(scale=IMAGE_DPI / 72)
        pil = bitmap.to_pil()
        buf = io.BytesIO()
        pil.save(buf, format="PNG")
        out.append(buf.getvalue())
    doc.close()
    return out


def _reconcile_direction(rows: list[RawRow]) -> int:
    """Derive debit/credit from the running balance instead of trusting the model.

    Models copy the printed balance reliably but misjudge direction on roughly
    one row in ten — on a real 978-row statement, 95 of 107 balance mismatches
    were pure sign flips (a credit filed as a withdrawal). The balance column is
    the statement's own arithmetic, so where |balance delta| matches the row
    amount, the delta's sign decides and the model's classification is overruled.

    Rows whose amount does not match the delta are left untouched: those are
    genuine extraction errors (a missed or misread row), not sign errors, and
    validate() must still fail them rather than have them silently "corrected".
    Returns the number of rows flipped.
    """
    flipped = 0
    for prev, row in zip(rows, rows[1:]):
        amount = (row.withdrawal or 0.0) or (row.deposit or 0.0)
        if not amount:
            continue
        delta = round(row.balance - prev.balance, 2)
        if abs(abs(delta) - abs(amount)) > 0.01:
            continue                      # not a sign problem — leave for validate()
        if delta > 0 and not row.deposit:
            row.withdrawal, row.deposit = None, abs(amount)
            flipped += 1
        elif delta < 0 and not row.withdrawal:
            row.withdrawal, row.deposit = abs(amount), None
            flipped += 1
    return flipped


def extract_with_llm(pdf_path: str, source_file: str) -> StatementExtract:
    with pdfplumber.open(pdf_path) as pdf:
        n = len(pdf.pages)
        texts = [(p.extract_text() or "") for p in pdf.pages]
        digital = sum(len(t) for t in texts[:3]) / max(min(3, n), 1) > 200

        merged_meta, all_rows = None, []
        chunk_size = PAGES_PER_CHUNK_TEXT if digital else PAGES_PER_CHUNK_VISION

        for start in range(0, n, chunk_size):
            idx = list(range(start, min(start + chunk_size, n)))
            if digital:
                body = "\n\n".join(
                    f"=== PAGE {i+1} of {n} ===\n{texts[i]}" for i in idx)
                # one page of trailing context from the previous chunk so a
                # row wrapped across the boundary is completed, with an
                # explicit instruction not to re-emit prior rows
                if start > 0:
                    body = (f"(Context only — rows from page {start} were already "
                            f"extracted; do NOT repeat them.)\n{texts[start-1][-1500:]}"
                            f"\n\n{body}")
                blocks = [{"text": body}]
            else:
                blocks = [{"text": f"Statement pages {idx[0]+1}-{idx[-1]+1} of {n} "
                                   f"as images. Extract every transaction row."}]
                for png in _page_images(pdf, idx):
                    blocks.append({"image_png": png})

            result = call_structured(SYSTEM_PROMPT, blocks, EXTRACTION_SCHEMA)
            if merged_meta is None:
                merged_meta = result
            for r in result.get("rows", []):
                all_rows.append(RawRow(
                    sl_no=None,
                    date=str(r.get("date", "")),
                    cheque_no=str(r.get("cheque_no", "") or ""),
                    description=str(r.get("description", "")).strip(),
                    withdrawal=r.get("withdrawal"),
                    deposit=r.get("deposit"),
                    balance=float(r["balance"]),
                    page=idx[0] + 1,
                ))

    # de-duplicate seam repeats (same date+amounts+balance emitted twice)
    seen, rows = set(), []
    for r in all_rows:
        key = (r.date, r.withdrawal, r.deposit, r.balance, r.description[:40])
        if key in seen:
            continue
        seen.add(key)
        rows.append(r)

    if flipped := _reconcile_direction(rows):
        print(f"llm_fallback: corrected direction on {flipped}/{len(rows)} rows "
              f"from running-balance deltas")

    meta = StatementMeta(
        bank=(merged_meta or {}).get("bank", "Unknown"),
        layout="llm_fallback",
        account_no=(merged_meta or {}).get("account_no", ""),
        account_name=(merged_meta or {}).get("account_name", ""),
        period_from=(merged_meta or {}).get("period_from", ""),
        period_to=(merged_meta or {}).get("period_to", ""),
        source_file=source_file,
        is_digital_text=digital,
    )
    return StatementExtract(meta=meta, rows=rows)
