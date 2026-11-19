"""Run frozen experiments without changing any active policy or journal."""
import argparse
import json
import confirmation_research as R
import diagnose_daily_spread as A
import intraday_vrp as V
import daytrade as D


def summarize(rows):
    xs = [r for r in rows if r.get('sample_eligible')]
    equity = peak = dd = 0.
    for r in xs:
        equity += r['net_rs']; peak = max(peak, equity); dd = max(dd, peak-equity)
    return {'n': len(xs), 'net': sum(r['net_rs'] for r in xs), 'max_drawdown': dd,
            'wins': sum(r['net_rs'] > 0 for r in xs),
            'mean_r': sum(r['net_r'] for r in xs)/len(xs) if xs else None,
            'fallback_n': sum(r['fallback'] for r in xs),
            'fallback_net': sum(r['net_rs'] for r in xs if r['fallback']),
            'earlier_net': sum(r['net_rs'] for r in xs if r['date'] < '2026-10-06'),
            'later_net': sum(r['net_rs'] for r in xs if r['date'] >= '2026-10-06')}


def run():
    rows = []
    for day in V.sessions('NIFTY'):
        # Freeze development cohort to the previous recorded comparison.
        if str(day) > '2026-10-23':
            continue
        bm, expiry, error = V.load(day, 'NIFTY')
        lot, source = D.session_lot(day, expiry, 'NIFTY') if not error else (0, 'unverified fallback')
        meta = A.metadata(day, expiry) if not error else {}
        for rule in R.RULES:
            for structure in ('spread', 'long'):
                for scenario, slip, delay in [('quoted',0.,0),('slippage',1.,0),('delay',0.,1)]:
                    result = ({'sample_eligible':False,'quality_flags':[error or source]}
                              if error or source == 'unverified fallback' else R.simulate(bm,meta,rule,lot,structure,slip,delay))
                    rows.append({**result,'date':str(day),'experiment':f'{rule}/{structure}/{scenario}'})
        print(day, 'complete', flush=True)
    keys = sorted({r['experiment'] for r in rows})
    groups = {k:[r for r in rows if r['experiment']==k] for k in keys}
    common = set.intersection(*[{r['date'] for r in rs if r.get('sample_eligible')} for rs in groups.values()])
    return {'notice':'Development replay, not independent validation. One lot; net_r is analytical normalization, not executable equal-risk sizing.',
            'common_dates':sorted(common), 'summary':{k:summarize(rs) for k,rs in groups.items()},
            'paired_summary':{k:summarize([r for r in rs if r['date'] in common]) for k,rs in groups.items()},'rows':rows}


if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);args=p.parse_args()
    result=run()
    with open(args.output,'w') as f:json.dump(result,f,indent=2,allow_nan=False)
    print(json.dumps(result['summary'],indent=2))
