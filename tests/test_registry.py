"""The registry now reads descriptors from S3 at runtime, which means a file
someone uploads decides how a statement is parsed. Its job is to be strict
about what it accepts and impossible to take the pipeline down with."""
import pytest
import yaml

from bsa import registry
from bsa.registry import LayoutError, all_layouts, validate_descriptor


GOOD = {"id": "x_bank", "bank": "X Bank", "parser": "generic",
        "fingerprints": {"any_of": ["X Bank Statement"], "all_of": []}}


@pytest.fixture(autouse=True)
def clean_cache(monkeypatch):
    monkeypatch.delenv("DATA_BUCKET", raising=False)
    registry._cache, registry._cache_at = None, 0.0
    yield
    registry._cache, registry._cache_at = None, 0.0


def test_every_bundled_layout_is_valid():
    """A descriptor that fails validation is skipped at runtime with only a log
    line, so the bank silently stops working. Catch it here instead."""
    layouts = all_layouts(force=True)
    assert len(layouts) >= 7
    for lid, d in layouts.items():
        assert validate_descriptor(d, lid) is d


def test_accepts_a_module_backed_descriptor_with_no_parser_key():
    d = dict(GOOD)
    del d["parser"]
    assert validate_descriptor(d) is d


@pytest.mark.parametrize("mutate,message", [
    (lambda d: d.pop("id"), "no string 'id'"),
    (lambda d: d.update(id=""), "no string 'id'"),
    (lambda d: d.pop("bank"), "no string 'bank'"),
    (lambda d: d.update(parser="os.system"), "expected one of"),
    (lambda d: d.update(fingerprints={"any_of": [], "all_of": []}), "no fingerprints"),
    (lambda d: d.update(fingerprints={"any_of": "X"}), "must be lists"),
    (lambda d: d.update(fingerprints="X"), "non-mapping"),
])
def test_rejects_unusable_descriptors(mutate, message):
    d = {**GOOD, "fingerprints": dict(GOOD["fingerprints"])}
    mutate(d)
    with pytest.raises(LayoutError) as e:
        validate_descriptor(d, "under_test.yaml")
    assert message in str(e.value)


def test_a_parser_name_cannot_be_arbitrary():
    """The descriptor arrives from S3, so `parser` is an allow-list and not a
    hint — it must never be able to name something importable."""
    with pytest.raises(LayoutError):
        validate_descriptor({**GOOD, "parser": "bsa.extract.llm_fallback"})


def test_match_order_is_deterministic_and_priority_wins(monkeypatch):
    """classify() takes the FIRST fingerprint match, so two layouts that could
    both match must resolve the same way on every invocation. Glob order used
    to decide this, which is to say nothing did."""
    monkeypatch.setattr(registry, "_load_bundled", lambda: [
        ("zzz", {**GOOD, "id": "zzz"}),
        ("aaa", {**GOOD, "id": "aaa"}),
        ("mmm", {**GOOD, "id": "mmm", "priority": 10}),
    ])
    assert list(all_layouts(force=True)) == ["mmm", "aaa", "zzz"]


# ------------------------------------------------------------------ S3 overlay

class _S3:
    def __init__(self, objects, explode=False):
        self.objects, self.explode = objects, explode

    def get_paginator(self, _):
        outer = self

        class _P:
            def paginate(self, Bucket, Prefix, **kw):
                if outer.explode:
                    raise RuntimeError("s3 is having a day")
                yield {"Contents": [{"Key": k} for k in outer.objects]}
        return _P()

    def get_object(self, Bucket, Key):
        class _B:
            def read(_s):
                return outer.objects[Key].encode()
        outer = self
        return {"Body": _B()}


def _with_s3(monkeypatch, s3, **env):
    monkeypatch.setenv("DATA_BUCKET", "bucket")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import boto3
    monkeypatch.setattr(boto3, "client", lambda name, **kw: s3)


def test_an_s3_layout_is_added_without_a_redeploy(monkeypatch):
    _with_s3(monkeypatch, _S3({"layouts/hdfc.yaml": yaml.safe_dump(
        {**GOOD, "id": "hdfc_savings", "bank": "HDFC Bank"})}))
    assert "hdfc_savings" in all_layouts(force=True)


def test_an_s3_layout_overrides_a_bundled_one_of_the_same_id(monkeypatch):
    """This is what makes a broken descriptor fixable without a release."""
    patched = yaml.safe_dump({**GOOD, "id": "axis_account_statement",
                              "bank": "Axis Bank (patched)"})
    _with_s3(monkeypatch, _S3({"layouts/axis_account_statement.yaml": patched}))
    assert all_layouts(force=True)["axis_account_statement"]["bank"] == \
        "Axis Bank (patched)"


def test_one_bad_s3_descriptor_does_not_take_the_others_down(monkeypatch):
    _with_s3(monkeypatch, _S3({
        "layouts/broken.yaml": "id: broken\nbank: B\nparser: nonsense\n",
        "layouts/fine.yaml": yaml.safe_dump({**GOOD, "id": "fine_bank"}),
    }))
    got = all_layouts(force=True)
    assert "fine_bank" in got and "broken" not in got


def test_unreachable_s3_degrades_to_the_bundled_set(monkeypatch):
    """The worst case must be the behaviour we had before S3 was involved,
    never a job that cannot run at all."""
    _with_s3(monkeypatch, _S3({}, explode=True))
    assert "axis_account_statement" in all_layouts(force=True)


def test_s3_can_be_switched_off_entirely(monkeypatch):
    _with_s3(monkeypatch, _S3({"layouts/hdfc.yaml": yaml.safe_dump(
        {**GOOD, "id": "hdfc_savings"})}), LAYOUTS_FROM_S3="0")
    assert "hdfc_savings" not in all_layouts(force=True)


def test_non_yaml_keys_under_the_prefix_are_ignored(monkeypatch):
    _with_s3(monkeypatch, _S3({"layouts/README.md": "not yaml at all",
                               "layouts/ok.yaml": yaml.safe_dump(GOOD)}))
    assert "x_bank" in all_layouts(force=True)


def test_the_cache_expires_so_a_warm_container_picks_up_a_new_bank(monkeypatch):
    store = {}
    _with_s3(monkeypatch, _S3(store), LAYOUT_CACHE_TTL_S="0")
    monkeypatch.setattr(registry, "CACHE_TTL_S", 0)
    assert "hdfc_savings" not in all_layouts()
    store["layouts/hdfc.yaml"] = yaml.safe_dump({**GOOD, "id": "hdfc_savings"})
    assert "hdfc_savings" in all_layouts()
