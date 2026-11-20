"""The dashboard API serves the current site (site/ = firebase/public) at /,
with /api routes still winning (20 Nov 2026: it used to serve a September page).

Runs in a subprocess because api.py decides what to mount at import time."""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

PROBE = r"""
import json, sys
from starlette.testclient import TestClient
import api
c = TestClient(api.app)
r = c.get("/"); js = c.get("/futures.js"); who = c.get("/api/whoami"); miss = c.get("/nope.js")
print(json.dumps([r.status_code, r.text, js.status_code, js.text, who.status_code, who.json(), miss.status_code]))
"""


def run(root):
    paths = [os.path.join(REPO, d) for d in (".", "pipeline", "quant", "strategy", "signals",
                                             "tracking", "service", "research", "tools", "futures")]
    env = dict(os.environ, OPTIONS_ENGINE_ROOT=str(root), PYTHONPATH=os.pathsep.join(paths),
               DASH_USER="", DASH_PASS="", DASH_TOKEN="")
    out = subprocess.run([sys.executable, "-c", PROBE], env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    import json
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_serves_site_and_keeps_api_routes(tmp_path):
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "index.html").write_text("NEW SITE")
    (tmp_path / "site" / "futures.js").write_text("// futures")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "index.html").write_text("OLD PAGE")
    st, body, js_st, js, who_st, who, miss = run(tmp_path)
    assert (st, body) == (200, "NEW SITE")
    assert (js_st, js) == (200, "// futures")
    assert who_st == 200 and who["auth"] == "none"       # /api still answered by the API
    assert miss == 404


def test_falls_back_to_web_without_site(tmp_path):
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "index.html").write_text("OLD PAGE")
    st, body, *_ = run(tmp_path)
    assert (st, body) == (200, "OLD PAGE")
