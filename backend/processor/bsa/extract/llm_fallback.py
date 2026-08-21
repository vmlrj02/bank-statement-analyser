"""LLM extraction fallback — Claude on Amazon Bedrock.

Used when classify() finds no known layout, or when the PDF is scanned.
Digital PDFs send page text; scanned PDFs send page images (Claude vision).
Output is forced through a tool schema, so no free-text parsing is needed.
The balance validator downstream is the correctness gate for this path.

Model access: enable Anthropic Claude models in the Bedrock console once per
account, then invoke via a global cross-region inference profile (that is how
Claude is reached from ap-south-1). Note: inference may process outside the
region; data at rest stays in-region.
"""
from __future__ import annotations

import io
import json
import os

import pdfplumber

from ..models import RawRow, StatementMeta, StatementExtract

DEFAULT_MODEL_ID = os.environ.get(
    "BEDROCK_MODEL_ID", "global.anthropic.claude-sonnet-4-5-20250929-v1:0")
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "ap-south-1")

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


def _tool_config():
    return {
        "tools": [{
            "toolSpec": {
                "name": "record_statement",
                "description": "Record the extracted statement rows",
                "inputSchema": {"json": EXTRACTION_SCHEMA},
            }
        }],
        "toolChoice": {"tool": {"name": "record_statement"}},
    }


def _converse(client, model_id: str, content_blocks: list[dict]) -> dict:
    resp = client.converse(
        modelId=model_id,
        system=[{"text": SYSTEM_PROMPT}],
        messages=[{"role": "user", "content": content_blocks}],
        toolConfig=_tool_config(),
        inferenceConfig={"maxTokens": 32000, "temperature": 0},
    )
    for block in resp["output"]["message"]["content"]:
        if "toolUse" in block:
            return block["toolUse"]["input"]
    raise RuntimeError("model returned no toolUse block")


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


def extract_with_llm(pdf_path: str, source_file: str,
                     model_id: str = DEFAULT_MODEL_ID,
                     region: str = BEDROCK_REGION) -> StatementExtract:
    import boto3
    client = boto3.client("bedrock-runtime", region_name=region)

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
                    blocks.append({"image": {"format": "png",
                                             "source": {"bytes": png}}})

            result = _converse(client, model_id, blocks)
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
