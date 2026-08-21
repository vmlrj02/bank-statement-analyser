"""Layout registry — loads YAML descriptors from bsa/layouts/."""
from __future__ import annotations

import glob
import os

import yaml

_LAYOUT_DIR = os.path.join(os.path.dirname(__file__), "layouts")
_cache: dict[str, dict] | None = None


def all_layouts() -> dict[str, dict]:
    global _cache
    if _cache is None:
        _cache = {}
        for f in glob.glob(os.path.join(_LAYOUT_DIR, "*.yaml")):
            with open(f) as fh:
                d = yaml.safe_load(fh)
            _cache[d["id"]] = d
    return _cache


def get_layout(layout_id: str) -> dict:
    return all_layouts()[layout_id]
