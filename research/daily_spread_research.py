"""Replay the frozen spread candidates once; retain every exclusion and fill."""
import argparse
import json
from pathlib import Path
import daily_spread_policy as S
import daytrade as D
import intraday_vrp as V
from intraday_validation import metrics


def run():
    days = V.sessions('NIFTY')
    boundary = days[len(days)*2//3] if days else None
    rows = {k: [] for k in (*S.CANDIDATES, 'baseline')}
    excluded = []
    for day in days:
        bm, expiry, error = V.load(day, 'NIFTY')
        if error:
            excluded.append({'date': str(day), 'reason': error})
            continue
        lot, source = D.session_lot(day, expiry, 'NIFTY')
        if source == 'unverified fallback':
            excluded.append({'date': str(day), 'reason': source})
            continue
        sims = {k: S.simulate(bm, day, expiry, k, lot) for k in S.CANDIDATES}
        baseline = V.simulate(bm, day, expiry, 0, lot=lot)
        eligible = baseline and baseline['sample_eligible'] and all(s['sample_eligible'] for s in sims.values())
        if not eligible:
            excluded.append({'date': str(day), 'reason': 'incomplete paired coverage',
                             'candidates': {k: s.get('quality_flags') for k,s in sims.items()}})
            continue
        for k, sim in sims.items():
            stress = S.simulate(bm, day, expiry, k, lot, extra_slip=1.0)
            sim['stress_net_rs'] = stress.get('net_rs') if stress.get('sample_eligible') else None
            rows[k].append(sim)
        rows['baseline'].append({'date': str(day), 'net_rs': baseline['net_rs']})
        print(f'{day}: replayed', flush=True)
    report = {'version': S.VERSION, 'status': 'historical_research_only',
              'boundary': str(boundary), 'activation': S.ACTIVATION,
              'excluded': excluded, 'candidates': {},
              'limitations': ['Previously inspected historical data: not independent validation',
                 'One-minute quotes do not model legging, depth, latency or intraminute touches',
                 'Stress adds one premium point per leg on entry and exit; not a worst case',
                 'Same lots do not mean same capital at risk; compare payoff envelopes too']}
    for k, rs in rows.items():
        b = str(boundary)
        report['candidates'][k] = {'earlier': metrics([r for r in rs if r['date'] < b]),
            'later': metrics([r for r in rs if r['date'] >= b]), 'all': metrics(rs), 'rows': rs}
        if k != 'baseline':
            valid = [dict(r, net_rs=r['stress_net_rs']) for r in rs if r['stress_net_rs'] is not None]
            report['candidates'][k]['stress'] = metrics(valid)
            report['candidates'][k]['later_stress'] = metrics([r for r in valid if r['date'] >= b])
    return report


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--output', required=True)
    args = p.parse_args()
    report = run()
    Path(args.output).write_text(json.dumps(report, indent=2))
    print(json.dumps({k: {a:b for a,b in v.items() if a != 'rows'} for k,v in report['candidates'].items()}, indent=2))
