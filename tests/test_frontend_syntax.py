"""Every inline <script> block in the SPA must at least parse.

The frontend is a single no-build file served straight from S3/CloudFront —
nothing compiles, bundles or lints it, so a stray brace ships a blank login
screen with no build failure anywhere. `node --check` is the cheapest possible
gate: it parses without executing (no DOM, no network), which is exactly the
right level for a file full of fetch() calls.

CI runs the same check as a standalone step in deploy.yml; this test keeps it
runnable locally and inside the pytest suite.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parents[1] / "frontend" / "index.html"

# Inline blocks only: a <script src="..."> tag has no body to parse (config.js
# is generated at deploy time and does not exist in the repo).
SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                       re.S | re.I)


def test_every_inline_script_block_parses(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node is not on PATH; CI runs this gate in deploy.yml")
    html = INDEX.read_text()
    blocks = [b for b in SCRIPT_RE.findall(html) if b.strip()]
    # The app IS one big inline script; finding none means the extraction
    # regex broke, not that the page went script-free.
    assert blocks, "no inline <script> blocks found in frontend/index.html"
    for i, js in enumerate(blocks, start=1):
        f = tmp_path / f"block_{i}.js"
        f.write_text(js)
        r = subprocess.run(["node", "--check", str(f)],
                           capture_output=True, text=True)
        assert r.returncode == 0, \
            f"inline <script> block {i} fails node --check:\n{r.stderr}"
