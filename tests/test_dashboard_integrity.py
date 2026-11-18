"""Exercise the shipped sizing function with a realistic rejected trade."""
import json
from pathlib import Path
import shutil
import subprocess
import pytest

PAGE = Path(__file__).resolve().parents[1] / 'firebase/public/index.html'


def test_integrity_transform_is_idempotent():
    import importlib.util
    path = PAGE.parents[1] / 'v2-src' / 'integrity.py'
    spec = importlib.util.spec_from_file_location('dashboard_integrity', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    page = PAGE.read_text(encoding='utf-8')
    once = module.apply(page)
    assert module.apply(once) == once
    assert 'Net Net' not in once


def test_negative_ev_and_closed_gate_cannot_recommend_lots():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for browser calculation test')
    page = PAGE.read_text(encoding='utf-8')
    start = page.index('function sizing(st, s){')
    end = page.index('\nasync function loadDash()', start)
    function = page[start:end]
    script = """
const values = {capital:2000000,riskpct:1,slpct:30,marginpct:50};
const $ = s => ({value:values[s.slice(1)]});
const pStopAt = () => ({p:0.434,se:0.005});
""" + function + """
console.log(JSON.stringify(sizing({margin:12372,credit_pts:9.7,credit_rs:630.5,
 fees_rs:176,slippage_rs:520,net_ev_rs:-1012,pop:0.888},
 {lot:65,lots:1,gate_passed:false})));
"""
    result = subprocess.run([node, '-e', script], capture_output=True, text=True, check=True)
    sizing = json.loads(result.stdout)
    assert sizing['lots'] == 0 and sizing['marginUsed'] == 0
    assert sizing['full'] == 0 and sizing['fullStop'] == 0


def test_page_uses_net_pnl_and_declares_research_policy():
    page = PAGE.read_text(encoding='utf-8')
    assert 'pnl = +t.pnl_now_rs' not in page
    assert 'id="research-policy"' in page
    assert 'every one on the same side' not in page
