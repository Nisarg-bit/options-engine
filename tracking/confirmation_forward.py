"""Scheduled frozen-rule paper replay. Never sends broker orders.

Post-session evaluation is prospective by rule freeze, not live fill evidence.
Historical checks are written to a different file by --historical-date.
"""
import argparse
from datetime import datetime, date
import hashlib
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import confirmation_research as R
import diagnose_daily_spread as A
import intraday_vrp as V
import daytrade as D

VERSION = 'confirmation_long_forward_v1'
START = date(2026, 10, 27)
SCENARIOS = (('quoted', 0., 0), ('slippage', 1., 0), ('delay', 0., 1), ('combined', 1., 1))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify(manifest):
    expected = json.loads(Path(manifest).read_text())
    for path, sha in expected.items():
        if digest(path) != sha:
            raise RuntimeError(f'frozen dependency changed: {path}')
    return digest(manifest)


def save_new(path, row):
    """First observation is permanent; re-running cannot rewrite its outcome."""
    path = Path(path)
    rows = json.loads(path.read_text()) if path.exists() else []
    if any(r['date'] == row['date'] for r in rows):
        return False
    rows.append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(sorted(rows, key=lambda r:r['date']), indent=2, allow_nan=False))
    os.replace(tmp, path)
    return True


def eligible_day(day, now):
    local = now.astimezone(ZoneInfo('Asia/Kolkata'))
    return day >= START and (day < local.date() or (day == local.date() and local.strftime('%H:%M') >= '15:45'))


def evaluate(day, manifest_hash, historical=False):
    bm, expiry, error = V.load(day, 'NIFTY')
    base = {'date':str(day), 'version':VERSION, 'paper_only':True,
            'evaluation_mode':'retrospective_check' if historical else 'frozen_rules_post_session_paper',
            'observed_at':datetime.now(ZoneInfo('UTC')).isoformat(), 'manifest_sha256':manifest_hash,
            'live_orders':False, 'live_fill_validation':False}
    if error:
        return {**base, 'status':'unavailable','error':error,'scenarios':{}}
    size, source = D.session_lot(day, expiry, 'NIFTY')
    base.update(expiry=str(expiry), lot=size, lot_source=source)
    if source == 'unverified fallback':
        return {**base,'status':'unavailable','error':'unverified lot','scenarios':{}}
    meta = A.metadata(day, expiry)
    results = {name:R.simulate(bm,meta,'continuation',size,'long',slip,delay)
               for name,slip,delay in SCENARIOS}
    return {**base, 'status':'evaluated', 'scenarios':results,
            'bars':{'first':min(bm) if bm else None,'last':max(bm) if bm else None},
            'data_sha256':{str(p):digest(p) for p in sorted((Path(V.BARS)/f'date={day}'/'underlying=NIFTY').glob('*.parquet'))}}


def summary(rows):
    out = {}
    for name,_,_ in SCENARIOS:
        clean = [r['scenarios'][name] for r in rows if r.get('scenarios',{}).get(name,{}).get('sample_eligible')]
        equity = peak = dd = 0.
        for r in clean:
            equity += r['net_rs']; peak=max(peak,equity); dd=max(dd,peak-equity)
        out[name]={'eligible_sessions':len(clean),'net_rs':equity,'max_drawdown_rs':dd,
                   'wins':sum(r['net_rs']>0 for r in clean), 'fallback_sessions':sum(r['fallback'] for r in clean)}
    return {'version':VERSION,'paper_only':True,'evaluation_mode':'frozen_rules_post_session_paper',
            'start':str(START),'recorded_sessions':len(rows),'scenarios':out,
            'live_ready':False,'review_after_sessions':20,
            'note':'20 sessions is a review checkpoint, not a profitability or live-readiness threshold.'}


def main():
    p=argparse.ArgumentParser();p.add_argument('--output-dir',required=True);p.add_argument('--manifest',required=True)
    p.add_argument('--historical-date',type=date.fromisoformat);a=p.parse_args()
    fingerprint=verify(a.manifest)
    dest=Path(a.output_dir);dest.mkdir(parents=True,exist_ok=True)
    if a.historical_date:
        if a.historical_date >= START:
            raise ValueError('historical check must precede forward start')
        row=evaluate(a.historical_date,fingerprint,True)
        save_new(dest/'retrospective_checks.json',row)
        print(json.dumps(row,indent=2));return
    journal=dest/'forward_journal.json'
    now=datetime.now(ZoneInfo('UTC'))
    existing=json.loads(journal.read_text()) if journal.exists() else []
    done={r['date'] for r in existing}
    for day in V.sessions('NIFTY'):
        if eligible_day(day,now) and str(day) not in done:
            row=evaluate(day,fingerprint)
            save_new(journal,row)
    rows=json.loads(journal.read_text()) if journal.exists() else []
    report=summary(rows)
    report.update(last_run_utc=now.isoformat(),manifest_sha256=fingerprint,
                  latest_available_session=str(max(V.sessions('NIFTY'))) if V.sessions('NIFTY') else None)
    temporary=dest/'status.tmp';temporary.write_text(json.dumps(report,indent=2));os.replace(temporary,dest/'status.json')
    print(json.dumps(report,indent=2))


if __name__=='__main__':
    main()
