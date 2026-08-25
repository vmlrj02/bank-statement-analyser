"""Shared fixtures.

Three Lambdas in this repo are all called handler.py, and each reads its
configuration from the environment and builds boto3 clients at import time. So
tests load them by path under distinct module names, with the environment set
and boto3 replaced by in-memory fakes — no AWS account, no network, and no
ordering dependency between test files.
"""
import copy
import importlib.util
import os
import sys
import time
from pathlib import Path

import boto3                     # noqa: F401  (real, for its condition classes)
import boto3.dynamodb.conditions  # noqa: F401  imported before boto3 is stubbed,
                                  # so `from boto3.dynamodb.conditions import ...`
                                  # inside a module under test resolves normally
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "processor"))


# ---------------------------------------------------------------- fake AWS --

def _reject_floats(obj, path="item"):
    """Real DynamoDB's resource interface refuses Python floats ("Float types
    are not supported. Use Decimal types instead."). The fake used to accept
    them, so a float written into the job summary passed every test and failed
    only in production. Model the rejection here so that gap can't reopen."""
    if isinstance(obj, bool):
        return
    if isinstance(obj, float):
        raise TypeError(f"Float types are not supported at {path}; use Decimal")
    if isinstance(obj, dict):
        for k, v in obj.items():
            _reject_floats(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _reject_floats(v, f"{path}[{i}]")


class FakeTable:
    """Enough DynamoDB to exercise our own logic.

    Supports the expressions this codebase actually writes — SET, REMOVE, ADD
    on a string set — plus condition expressions expressed as a callable, which
    is where the interesting behaviour is (claiming a merge, first-write-wins).
    """

    def __init__(self, name="T", key="job_id"):
        self.name, self.key = name, key
        self.items = {}
        self.writes = []

    # -- api surface --
    def put_item(self, Item):
        _reject_floats(Item)
        self.items[Item[self.key]] = dict(Item)
        self.writes.append(("put", Item[self.key]))

    def get_item(self, Key):
        it = self.items.get(Key[self.key])
        # Deep, not shallow: real DynamoDB deserialises a fresh object per
        # request, so a handler that mutates what it read (_scrub does) must
        # not be able to affect the stored item or a later read.
        return {"Item": copy.deepcopy(it)} if it is not None else {}

    def delete_item(self, Key):
        self.items.pop(Key[self.key], None)
        self.writes.append(("delete", Key[self.key]))

    def scan(self, **kw):
        items = [copy.deepcopy(v) for v in self.items.values()]
        if (f := kw.get("FilterExpression")) is not None:
            items = [i for i in items if _eval_cond(f, i)]
        # Honour ProjectionExpression. Real DynamoDB returns ONLY the projected
        # attributes, so a handler reading a field it forgot to project sees
        # nothing — silently, and only in production. Modelling it here means
        # a missing field fails a test instead.
        if (proj := kw.get("ProjectionExpression")):
            names = kw.get("ExpressionAttributeNames") or {}
            keep = {names.get(a.strip(), a.strip()) for a in proj.split(",")}
            items = [{k: v for k, v in i.items() if k in keep} for i in items]
        return {"Items": items}

    def query(self, **kw):
        items = [copy.deepcopy(v) for v in self.items.values()]
        if (cond := kw.get("KeyConditionExpression")) is not None:
            items = [i for i in items if _eval_cond(cond, i)]
        items.sort(key=lambda x: x.get("created_at", 0),
                   reverse=not kw.get("ScanIndexForward", True))
        return {"Items": items[:kw.get("Limit", len(items))]}

    def update_item(self, Key, UpdateExpression="", ExpressionAttributeNames=None,
                    ExpressionAttributeValues=None, ConditionExpression=None,
                    ReturnValues=None):
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        _reject_floats(values, "update-values")
        item = self.items.setdefault(Key[self.key], dict(Key))
        if ConditionExpression is not None and not _cond_ok(
                ConditionExpression, item, names, values):
            raise ConditionalCheckFailed()
        _apply_update(item, UpdateExpression, names, values)
        self.writes.append(("update", Key[self.key]))
        return {"Attributes": dict(item)}


class ConditionalCheckFailed(Exception):
    pass


def _eval_cond(cond, item):
    """Evaluate a boto3 Key/Attr condition against a plain dict.

    Only the two operators this codebase uses — equality for the owner-index
    query, IN for the sweeper's status filter.
    """
    e = cond.get_expression()
    op, vals = e["operator"], e["values"]
    name = vals[0].name
    got = item.get(name)
    if op == "=":
        return got == vals[1]
    if op == "IN":
        return got in vals[1]
    raise AssertionError(f"fake does not model operator {op!r}")


def _resolve(tok, names, values):
    tok = tok.strip()
    if tok.startswith("#"):
        return names[tok]
    return tok


def _apply_update(item, expr, names, values):
    """SET a = :v, b.#k = :w  /  REMOVE x  /  ADD s :v  — in any combination."""
    import re
    parts = re.split(r"\s+(SET|REMOVE|ADD)\s+", " " + expr.strip(), flags=re.I)
    # parts == ['', 'SET', 'a = :v', 'ADD', 's :k'] style
    i = 1
    while i < len(parts) - 1:
        verb, body = parts[i].upper(), parts[i + 1]
        if verb == "SET":
            for clause in body.split(","):
                lhs, rhs = clause.split("=", 1)
                lhs, rhs = lhs.strip(), rhs.strip()
                val = values[rhs]
                if "." in lhs:
                    outer, inner = lhs.split(".", 1)
                    item.setdefault(_resolve(outer, names, values), {})[
                        _resolve(inner, names, values)] = val
                else:
                    item[_resolve(lhs, names, values)] = val
        elif verb == "REMOVE":
            for name in body.split(","):
                item.pop(_resolve(name, names, values), None)
        elif verb == "ADD":
            name, ref = body.split()
            key = _resolve(name, names, values)
            item[key] = set(item.get(key, set())) | set(values[ref.strip()])
        i += 2


def _cond_ok(expr, item, names, values):
    """Evaluate the handful of condition expressions this codebase writes."""
    e = expr.strip()
    if e == "attribute_exists(failed_files)":
        return "failed_files" in item
    if e.startswith("attribute_not_exists(#s) OR #s <> :m"):
        # the merge claim, with or without its staleness clause
        status_key = names["#s"]
        cur = item.get(status_key)
        if cur is None or cur != values[":m"]:
            return True
        if "merging_at" not in e:
            return False
        return ("merging_at" not in item
                or item["merging_at"] < values[":stale"])
    raise AssertionError(f"fake does not model condition {e!r}")


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.fail = False

    def _check(self):
        if self.fail:
            raise RuntimeError("s3 unavailable")

    def put_object(self, Bucket, Key, Body, **kw):
        self._check()
        self.objects[(Bucket, Key)] = Body

    def get_object(self, Bucket, Key, **kw):
        self._check()
        if (Bucket, Key) not in self.objects:
            raise KeyError(Key)
        body = self.objects[(Bucket, Key)]
        return {"Body": _Body(body)}

    def head_object(self, Bucket, Key):
        self._check()
        if (Bucket, Key) not in self.objects:
            raise KeyError(Key)
        return {}

    def delete_object(self, Bucket, Key):
        self.objects.pop((Bucket, Key), None)

    def get_paginator(self, _name):
        outer = self

        class _P:
            def paginate(self, Bucket, Prefix="", **kw):
                outer._check()
                yield {"Contents": [{"Key": k}
                                    for (b, k) in sorted(outer.objects)
                                    if b == Bucket and k.startswith(Prefix)]}
        return _P()

    def generate_presigned_url(self, op, Params, ExpiresIn=0):
        return f"https://example.invalid/{Params['Key']}?op={op}"

    def upload_file(self, path, Bucket, Key):
        with open(path, "rb") as fh:
            self.objects[(Bucket, Key)] = fh.read()

    def download_file(self, Bucket, Key, path):
        with open(path, "wb") as fh:
            fh.write(self.objects[(Bucket, Key)])


class _Body:
    def __init__(self, b):
        self._b = b if isinstance(b, bytes) else str(b).encode()

    def read(self):
        return self._b


class FakeLambda:
    def __init__(self):
        self.invocations = []
        # What a RequestResponse invoke reads back from Payload. The categoriser
        # playground invokes the processor synchronously and parses this; tests
        # can override it to model a specific classification or a bad reply.
        self.response = {"description": "x", "amount": 0.0, "mode": "other",
                         "party": "unknown party", "category": "Regular debit",
                         "detail": "regular debit"}

    def invoke(self, FunctionName, InvocationType, Payload):
        self.invocations.append((FunctionName, Payload))
        if InvocationType == "RequestResponse":
            import json as _json
            return {"StatusCode": 200, "Payload": _Body(_json.dumps(self.response))}
        return {"StatusCode": 202}


class FakeBoto3:
    """Stands in for the boto3 module for a module under test."""

    def __init__(self, tables=None, s3=None, lam=None):
        self.tables = tables or {}
        self.s3 = s3 or FakeS3()
        self.lam = lam or FakeLambda()

    def resource(self, name, **kw):
        assert name == "dynamodb"
        return _FakeDDBResource(self.tables)

    def client(self, name, **kw):
        if name == "s3":
            return self.s3
        if name == "lambda":
            return self.lam
        raise AssertionError(f"unexpected client {name}")


class _FakeDDBResource:
    def __init__(self, tables):
        self._tables = tables
        self.meta = _Meta()

    def Table(self, name):
        return self._tables[name]


class _Meta:
    class client:
        exceptions = type("E", (), {"ConditionalCheckFailedException":
                                    ConditionalCheckFailed})


# ------------------------------------------------------------ module loader --

def load_module(rel_path: str, name: str, boto3_stub=None, env=None):
    """Import a handler by path, with its environment and boto3 replaced."""
    old_env = dict(os.environ)
    os.environ.update(env or {})
    real_boto3 = sys.modules.get("boto3")
    if boto3_stub is not None:
        sys.modules["boto3"] = boto3_stub
    try:
        spec = importlib.util.spec_from_file_location(name, ROOT / rel_path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        if boto3_stub is not None:
            if real_boto3 is not None:
                sys.modules["boto3"] = real_boto3
            else:
                sys.modules.pop("boto3", None)
        os.environ.clear()
        os.environ.update(old_env)


# ------------------------------------------------------------------ fixtures --

@pytest.fixture
def jobs_table():
    return FakeTable("jobs", "job_id")


@pytest.fixture
def auth_table():
    return FakeTable("auth", "pk")


@pytest.fixture
def s3():
    return FakeS3()


@pytest.fixture
def api(jobs_table, auth_table, s3):
    """The API Lambda, wired to fakes."""
    stub = FakeBoto3({"jobs": jobs_table, "auth": auth_table}, s3=s3)
    mod = load_module("backend/api/handler.py", "api_handler_undertest", stub, {
        "JOBS_TABLE": "jobs", "AUTH_TABLE": "auth", "DATA_BUCKET": "bucket",
        "AWS_REGION": "ap-south-1", "OWNER_INDEX": "owner-created_at-index",
        "PROCESSOR_FUNCTION": "proc-fn",
    })
    mod._fake_lambda = stub.lam
    return mod


@pytest.fixture
def sweeper(jobs_table, s3):
    stub = FakeBoto3({"jobs": jobs_table}, s3=s3)
    mod = load_module("backend/sweeper/handler.py", "sweeper_undertest", stub, {
        "JOBS_TABLE": "jobs", "DATA_BUCKET": "bucket",
        "PROCESSOR_FUNCTION": "proc-fn",
    })
    mod._fake_lambda = stub.lam
    return mod


@pytest.fixture
def now():
    return int(time.time())
