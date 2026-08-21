"""LLM extraction fallback — Claude on Amazon Bedrock.

Used when classify() finds no known layout, or when the PDF is scanned.
Locally this module is a stub unless AWS credentials + bedrock access exist;
the interface and prompt are the production ones so Lambda uses this file
unchanged.
"""
from __future__ import annotations

import json

from ..models import RawRow, StatementMeta, StatementExtract

EXTRACTION_TOOL = {
    "name": "record_statement",
    "description": "Record the extracted bank statement",
    "input_schema": {
        "type": "object",
        "properties": {
            "bank": {"type": "string"},
            "account_no": {"type": "string"},
            "account_name": {"type": "string"},
            "period_from": {"type": "string", "description": "ISO date"},
            "period_to": {"type": "string", "description": "ISO date"},
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "date": {"type": "string", "description": "as printed"},
                        "cheque_no": {"type": "string"},
                        "description": {"type": "string"},
                        "withdrawal": {"type": ["number", "null"]},
                        "deposit": {"type": ["number", "null"]},
                        "balance": {"type": "number"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["date", "description", "balance"],
                },
            },
        },
        "required": ["bank", "account_no", "rows"],
    },
}

SYSTEM_PROMPT = """You extract transactions from Indian bank/NBFC statements.
Rules:
- One output row per transaction row in the statement. Never merge, skip, or invent rows.
- Copy the running balance exactly as printed; it is used to verify your work.
- Amounts: strip thousands separators (Indian lakh style like 5,03,72,235.09 included).
- A column pair (Withdrawal/Deposit) or a Dr/Cr suffix or parentheses all indicate direction.
- Dates: keep as printed; do not reformat.
- Multi-line descriptions: join with single spaces in reading order.
- If a page is a summary/opening-balance section, do not emit transaction rows for it."""


def extract_with_llm(pdf_path: str, source_file: str,
                     page_texts: list[str] | None = None,
                     model_id: str = "anthropic.claude-sonnet",
                     region: str = "ap-south-1") -> StatementExtract:
    """Send page text (or page images for scans) to Claude via Bedrock,
    chunked with one-row overlap at page joins, and assemble the result."""
    try:
        import boto3  # noqa: F401
    except ImportError:
        raise NotImplementedError(
            "LLM fallback requires boto3 + Bedrock access. "
            "Locally, run against a known layout, or configure AWS credentials."
        )
    # Production implementation (Phase 1): chunk pages, call
    # bedrock-runtime converse() with EXTRACTION_TOOL forced via tool_choice,
    # merge chunks, return StatementExtract. Kept as the single integration
    # point so the pipeline shape doesn't change.
    raise NotImplementedError("Bedrock call wired up in Phase 1 (AWS deploy)")
