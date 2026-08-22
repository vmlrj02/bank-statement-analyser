"""Statement data must not leave the AWS account.

This is the gate that stood between the product and real customers, and it is
now a code policy rather than an intention. These tests are the policy: if any
of them stops holding, a bank with no layout can put a customer's transactions
into a third party's request log.
"""
import pytest

from bsa.extract import llm_providers as lp
from bsa.extract.llm_providers import (
    LLMError, NoLayoutError, ResidencyError, external_llm_allowed,
    fallback_enabled, provider, residency_block)


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    for k in ("LLM_FALLBACK", "ALLOW_EXTERNAL_LLM", "LLM_PROVIDER", "LLM_MODEL"):
        monkeypatch.delenv(k, raising=False)


def test_defaults_are_closed():
    """Forgetting to configure anything must be the safe outcome, not the
    permissive one."""
    assert fallback_enabled() is False
    assert external_llm_allowed() is False
    assert provider() == "bedrock"


def test_bedrock_is_allowed_because_it_runs_in_our_account():
    assert residency_block("bedrock") is None


@pytest.mark.parametrize("name", ["anthropic", "gemini", "openai"])
def test_external_providers_are_blocked_by_default(name):
    msg = residency_block(name)
    assert msg and "must not leave" in msg


def test_external_is_allowed_only_with_the_explicit_flag(monkeypatch):
    monkeypatch.setenv("ALLOW_EXTERNAL_LLM", "true")
    assert residency_block("anthropic") is None


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("TRUE", True), ("1", True), ("yes", True), ("on", True),
    ("false", False), ("0", False), ("no", False), ("off", False),
    ("", False), ("   ", False), ("perhaps", False),
])
def test_flag_parsing_is_strict(monkeypatch, raw, expected):
    """Anything that is not an affirmative reads as off. A typo in an env var
    must not silently open the gate."""
    monkeypatch.setenv("ALLOW_EXTERNAL_LLM", raw)
    assert external_llm_allowed() is expected


def test_call_structured_refuses_before_building_a_client(monkeypatch):
    """The adapter is never reached, so no SDK is constructed and no request
    body containing statement text is ever assembled."""
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    called = []
    monkeypatch.setitem(lp._ADAPTERS, "anthropic",
                        lambda *a, **k: called.append(1))
    with pytest.raises(ResidencyError):
        lp.call_structured("sys", [{"text": "ACCOUNT 12345 BALANCE 900"}], {})
    assert called == []


def test_residency_error_is_still_an_llm_error():
    """Existing handling catches LLMError; a policy refusal must not escape it
    as an unhandled exception and fail a whole job with a stack trace."""
    assert issubclass(ResidencyError, LLMError)


def test_no_layout_error_is_not_an_llm_error():
    """Nothing was transmitted and no provider was involved, so it must not be
    reported to the user as an AI failure."""
    assert not issubclass(NoLayoutError, LLMError)


def test_an_in_account_provider_still_works_with_the_flag_off(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "bedrock")
    seen = {}
    monkeypatch.setitem(lp._ADAPTERS, "bedrock",
                        lambda s, b, sc, m: seen.setdefault("model", m) or ({}, {}))
    lp.call_structured("sys", [{"text": "x"}], {})
    assert seen["model"]
