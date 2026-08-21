"""Provider-agnostic structured extraction call.

The pipeline wants exactly one thing from an LLM: given a system prompt, a
mixed sequence of text and page images, and a JSON schema, return an object
matching that schema. Each vendor spells that differently — Anthropic forces
a tool call, Gemini takes response_schema, OpenAI takes response_format,
Bedrock takes toolConfig — so each adapter normalises to the one contract in
call_structured() below.

Config is env-driven so switching provider or model needs no code edit:

    LLM_PROVIDER        gemini | anthropic | openai | bedrock   (default gemini)
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
    "openai": "gpt-5.1",
    "bedrock": os.environ.get("BEDROCK_MODEL_ID",
                              "apac.anthropic.claude-3-7-sonnet-20250219-v1:0"),
}

MAX_OUTPUT_TOKENS = 32000
TOOL_NAME = "record_statement"


class LLMError(RuntimeError):
    """Raised when a provider call fails in a way the pipeline should report."""


def provider() -> str:
    return os.environ.get("LLM_PROVIDER", "gemini").strip().lower()


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
    return json.loads(resp.text)


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
    msg = client.messages.create(
        model=model,
        max_tokens=MAX_OUTPUT_TOKENS,
        system=system,
        messages=[{"role": "user", "content": content}],
        tools=[{"name": TOOL_NAME,
                "description": "Record the extracted statement rows",
                "input_schema": schema}],
        tool_choice={"type": "tool", "name": TOOL_NAME},
    )
    for block in msg.content:
        if block.type == "tool_use":
            return block.input
    raise LLMError("anthropic returned no tool_use block")


def _call_openai(system: str, blocks: list[dict], schema: dict, model: str) -> dict:
    from openai import OpenAI

    client = OpenAI(api_key=_api_key())
    content = []
    for b in blocks:
        if "text" in b:
            content.append({"type": "text", "text": b["text"]})
        else:
            b64 = base64.b64encode(b["image_png"]).decode()
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64}"}})
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": content}],
        response_format={"type": "json_schema", "json_schema": {
            "name": TOOL_NAME, "schema": schema}},
        max_completion_tokens=MAX_OUTPUT_TOKENS,
    )
    text = resp.choices[0].message.content
    if not text:
        raise LLMError("openai returned no content")
    return json.loads(text)


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
    for block in resp["output"]["message"]["content"]:
        if "toolUse" in block:
            return block["toolUse"]["input"]
    raise LLMError("bedrock returned no toolUse block")


_ADAPTERS = {
    "gemini": _call_gemini,
    "anthropic": _call_anthropic,
    "openai": _call_openai,
    "bedrock": _call_bedrock,
}


def call_structured(system: str, blocks: list[dict], schema: dict) -> dict:
    """Return an object matching `schema`.

    `blocks` is a list of {"text": str} and/or {"image_png": bytes} in the
    order the model should read them.
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
