"""Provider-agnostic structured extraction call.

The pipeline wants exactly one thing from an LLM: given a system prompt, a
mixed sequence of text and page images, and a JSON schema, return an object
matching that schema. Each vendor spells that differently — Anthropic forces
a tool call, Gemini takes response_schema, OpenAI takes response_format,
Bedrock takes toolConfig — so each adapter normalises to the one contract in
call_structured() below.

Config is env-driven so switching provider or model needs no code edit:

    LLM_PROVIDER        anthropic | gemini | bedrock   (default anthropic)
    LLM_MODEL           model id override; else DEFAULT_MODELS
    LLM_API_KEY_SECRET  Secrets Manager id holding the key (Lambda)
    LLM_API_KEY         plain key, for local runs
    BEDROCK_REGION      bedrock adapter only

The balance validator downstream is the correctness gate for every provider,
so an adapter only has to be faithful, not clever.
"""
from __future__ import annotations

import base64
import json
import os
from functools import lru_cache
from typing import Any

# Model ids move fast and these WILL go stale — override with LLM_MODEL rather
# than editing here, and confirm against the provider's own model listing.
DEFAULT_MODELS = {
    "gemini": "gemini-3.7-flash",
    "anthropic": "claude-sonnet-5",
    "bedrock": os.environ.get("BEDROCK_MODEL_ID",
                              "apac.anthropic.claude-3-7-sonnet-20250219-v1:0"),
}

# 16384 per chunk: chunks are small, and a lower ceiling keeps each
# streamed call bounded and cheaper to retry.
MAX_OUTPUT_TOKENS = 16384
TOOL_NAME = "record_statement"

# USD per 1M tokens, (input, output). Vendor pricing changes, so a model that
# is not listed reports tokens with cost None rather than a made-up number —
# a wrong cost figure is worse than an absent one on an admin billing screen.
# Override without a deploy via LLM_PRICE_IN / LLM_PRICE_OUT.
PRICES = {
    "claude-opus-5":    (5.00, 25.00),
    "claude-sonnet-5":  (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


def price_for(model: str):
    """(input, output) USD per 1M tokens, or None if unknown."""
    if (env_in := os.environ.get("LLM_PRICE_IN")) and \
            (env_out := os.environ.get("LLM_PRICE_OUT")):
        try:
            return float(env_in), float(env_out)
        except ValueError:
            pass
    for key, pair in PRICES.items():
        if key in model:
            return pair
    return None


def cost_usd(model: str, tokens_in: int, tokens_out: int):
    p = price_for(model)
    if not p:
        return None
    return round(tokens_in / 1e6 * p[0] + tokens_out / 1e6 * p[1], 6)


class LLMError(RuntimeError):
    """Raised when a provider call fails in a way the pipeline should report."""


def provider() -> str:
    return os.environ.get("LLM_PROVIDER", "anthropic").strip().lower()


def model_id() -> str:
    return os.environ.get("LLM_MODEL") or DEFAULT_MODELS[provider()]


@lru_cache(maxsize=1)
def _api_key() -> str:
    """Secrets Manager in Lambda, plain env var locally."""
    if key := os.environ.get("LLM_API_KEY"):
        return key
    secret_id = os.environ.get("LLM_API_KEY_SECRET")
    if not secret_id:
        raise LLMError(
            "No LLM credentials: set LLM_API_KEY locally, or LLM_API_KEY_SECRET "
            "to a Secrets Manager id in Lambda.")
    import boto3
    raw = boto3.client("secretsmanager").get_secret_value(
        SecretId=secret_id)["SecretString"]
    # Accept either a bare key or {"api_key": "..."} / {"<provider>": "..."}
    try:
        blob = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip()
    if isinstance(blob, dict):
        for k in ("api_key", provider(), "key"):
            if blob.get(k):
                return str(blob[k]).strip()
    raise LLMError(f"Secret {secret_id} has no usable key for {provider()}")


# ---------- schema normalisation ----------

def _gemini_schema(node: Any) -> Any:
    """Gemini rejects JSON-Schema union types and a few keywords.

    `{"type": ["number", "null"]}` becomes `{"type": "number", "nullable": true}`,
    and unsupported keywords are dropped rather than passed through.
    """
    if not isinstance(node, dict):
        return [_gemini_schema(v) for v in node] if isinstance(node, list) else node
    out: dict[str, Any] = {}
    for k, v in node.items():
        if k in ("additionalProperties", "$schema", "title"):
            continue
        if k == "type" and isinstance(v, list):
            non_null = [t for t in v if t != "null"]
            out["type"] = non_null[0] if non_null else "string"
            if "null" in v:
                out["nullable"] = True
        elif k in ("properties", "items"):
            out[k] = ({pk: _gemini_schema(pv) for pk, pv in v.items()}
                      if k == "properties" else _gemini_schema(v))
        else:
            out[k] = v
    return out


# ---------- adapters ----------

def _call_gemini(system: str, blocks: list[dict], schema: dict, model: str) -> dict:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=_api_key())
    parts = []
    for b in blocks:
        if "text" in b:
            parts.append(types.Part.from_text(text=b["text"]))
        else:
            parts.append(types.Part.from_bytes(data=b["image_png"],
                                               mime_type="image/png"))
    resp = client.models.generate_content(
        model=model,
        contents=[types.Content(role="user", parts=parts)],
        config=types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=_gemini_schema(schema),
            temperature=0,
            max_output_tokens=MAX_OUTPUT_TOKENS,
        ),
    )
    if not resp.text:
        raise LLMError("gemini returned no text")
    u = getattr(resp, "usage_metadata", None)
    return json.loads(resp.text), {
        "in": getattr(u, "prompt_token_count", 0) or 0,
        "out": getattr(u, "candidates_token_count", 0) or 0}


def _call_anthropic(system: str, blocks: list[dict], schema: dict, model: str) -> dict:
    import anthropic

    client = anthropic.Anthropic(api_key=_api_key())
    content = []
    for b in blocks:
        if "text" in b:
            content.append({"type": "text", "text": b["text"]})
        else:
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/png",
                "data": base64.b64encode(b["image_png"]).decode()}})
    # Forced tool use is the most reliable structured-output path here: it is
    # supported on every current model and needs no schema rewriting.
    # Must stream: the SDK refuses a non-streaming request whose max_tokens
    # could exceed the 10-minute HTTP ceiling, and MAX_OUTPUT_TOKENS trips it.
    # This is a client-side guard, so testing the endpoint over raw HTTP does
    # not exercise it — that gap let a broken adapter reach a real upload.
    with client.messages.stream(
        model=model,
        max_tokens=MAX_OUTPUT_TOKENS,
        system=system,
        messages=[{"role": "user", "content": content}],
        tools=[{"name": TOOL_NAME,
                "description": "Record the extracted statement rows",
                "input_schema": schema}],
        tool_choice={"type": "tool", "name": TOOL_NAME},
    ) as stream:
        msg = stream.get_final_message()
    usage = {"in": getattr(msg.usage, "input_tokens", 0) or 0,
             "out": getattr(msg.usage, "output_tokens", 0) or 0}
    for block in msg.content:
        if block.type == "tool_use":
            return block.input, usage
    raise LLMError("anthropic returned no tool_use block")


def _call_bedrock(system: str, blocks: list[dict], schema: dict, model: str) -> dict:
    import boto3

    client = boto3.client("bedrock-runtime",
                          region_name=os.environ.get("BEDROCK_REGION", "ap-south-1"))
    content = []
    for b in blocks:
        if "text" in b:
            content.append({"text": b["text"]})
        else:
            content.append({"image": {"format": "png",
                                      "source": {"bytes": b["image_png"]}}})
    resp = client.converse(
        modelId=model,
        system=[{"text": system}],
        messages=[{"role": "user", "content": content}],
        toolConfig={
            "tools": [{"toolSpec": {
                "name": TOOL_NAME,
                "description": "Record the extracted statement rows",
                "inputSchema": {"json": schema},
            }}],
            "toolChoice": {"tool": {"name": TOOL_NAME}},
        },
        inferenceConfig={"maxTokens": MAX_OUTPUT_TOKENS, "temperature": 0},
    )
    u = resp.get("usage", {})
    usage = {"in": u.get("inputTokens", 0), "out": u.get("outputTokens", 0)}
    for block in resp["output"]["message"]["content"]:
        if "toolUse" in block:
            return block["toolUse"]["input"], usage
    raise LLMError("bedrock returned no toolUse block")


_ADAPTERS = {
    "gemini": _call_gemini,
    "anthropic": _call_anthropic,
    "bedrock": _call_bedrock,
}


def call_structured(system: str, blocks: list[dict], schema: dict):
    """Return (object matching `schema`, usage dict).

    `blocks` is a list of {"text": str} and/or {"image_png": bytes} in the
    order the model should read them. Usage is {"in": int, "out": int} so the
    caller can report what an extraction actually cost.
    """
    name = provider()
    if name not in _ADAPTERS:
        raise LLMError(f"Unknown LLM_PROVIDER {name!r}; "
                       f"expected one of {sorted(_ADAPTERS)}")
    model = model_id()
    try:
        return _ADAPTERS[name](system, blocks, schema, model)
    except LLMError:
        raise
    except ImportError as e:
        raise LLMError(f"{name} SDK not installed in this bundle: {e}") from e
    except Exception as e:                                   # noqa: BLE001
        raise LLMError(f"{name}/{model} extraction call failed: {e}") from e
