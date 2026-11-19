"""Read-only replay of old versus active new paper policy on every stored day.

Writes only the requested research output. Never changes either paper journal.
"""
import argparse
import json
import daily_spread_policy as S
import daytrade as D
import intraday_vrp as V
from intraday_validation import metrics


def replay():
    rows = []
    for day in V.sessions('NIFTY'):
        bm, expiry, error = V.load(day, 'NIFTY')
        if error:
            rows.append({'date': str(day), 'error': error, 'paired_clean': False})
            continue
        lot, source = D.session_lot(day, expiry, 'NIFTY')
        old = V.simulate(bm, day, expiry, width=0, lot=lot,
                         costs=V.P.current_costs_for('NIFTY'))
        new = S.simulate(bm, day, expiry, candidate=S.ACTIVE, lot=lot)
        verified = source != 'unverified fallback'
        def compact(r):
            if not r:
                return {'net_rs': None, 'sample_eligible': False, 'quality_flags': ['unpriceable']}
            return {k:r.get(k) for k in ('entry','exit','exit_reason','net_rs',
                     'fees_rs','sample_eligible','quality_flags','status','shape','legs')}
        row = {'date': str(day), 'lot': lot, 'lot_source': source,
               'old': compact(old), 'new': compact(new),
               'paired_clean': bool(verified and old and old['sample_eligible'] and new['sample_eligible'])}
        row['difference_rs'] = new['net_rs']-old['net_rs'] if old and new.get('net_rs') is not None else None
        rows.append(row)
        print(str(day), 'clean' if row['paired_clean'] else 'excluded', flush=True)
    paired = [r for r in rows if r['paired_clean']]
    summary = {}
    for key in ('old', 'new'):
        rs = [r[key] for r in paired]
        summary[key] = {**metrics(rs), 'wins': sum(r['net_rs'] > 0 for r in rs),
                        'losses': sum(r['net_rs'] < 0 for r in rs),
                        'fees': round(sum(r['fees_rs'] for r in rs),2)}
    summary['difference_rs'] = round(sum(r['difference_rs'] for r in paired),2)
    summary['new_better_days'] = sum(r['difference_rs'] > 0 for r in paired)
    return {'recorded_sessions':len(rows), 'paired_sessions':len(paired),
            'policy':S.ACTIVE, 'version':S.VERSION, 'summary':summary,'rows':rows,
            'basis':'One same-day instrument-master lot; recorded bid/ask and estimated order costs. '
                    'Paired clean sessions only in totals. Historical replay, not independent validation. '
                    'Equal lots do not mean equal capital or risk.'}


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--output', required=True)
    args = p.parse_args()
    report = replay()
    with open(args.output,'w') as f:
        json.dump(report,f,indent=2)
    print(json.dumps({k:v for k,v in report.items() if k != 'rows'},indent=2))
